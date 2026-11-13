"""Story endpoints (plan §6.8): POST /v1/stories -> 202, GET /v1/stories/{id}, GET /v1/jobs/{id}."""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from storyweaver.api.deps import BlobStoreDep, SessionsDep, SettingsDep, StoryServiceDep, require_api_key
from storyweaver.api.errors import problem_response
from storyweaver.domain.story import AgeBand, Line, SceneCount
from storyweaver.jobs.queue import JobQueue
from storyweaver.orchestrator.story_service import (
    IdempotencyConflictError,
    SafetyUnavailableError,
    UnsafeInputError,
)
from storyweaver.safety.guards import BRANDS
from storyweaver.storage.blob import BlobStore
from storyweaver.storage.models import SceneRow, SceneStatus, StoryRow, StoryStatus
from storyweaver.storage.repositories import StoryRepository

router = APIRouter(prefix="/v1", tags=["stories"], dependencies=[Depends(require_api_key)])

IdempotencyKey = Annotated[
    str | None, Header(alias="Idempotency-Key", min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
]


class CreateStoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=3, max_length=500, description="The parent's story idea.")
    age_band: AgeBand = "4-6"
    num_scenes: SceneCount = 5

    @field_validator("prompt")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = " ".join(value.split())
        if len(value) < 3:
            raise ValueError("prompt must contain at least 3 characters")
        return value


class CreateStoryResponse(BaseModel):
    story_id: uuid.UUID
    job_id: uuid.UUID
    status: str


class CharacterOut(BaseModel):
    id: str
    name: str
    species: str
    visual: str


AudioStatus = Literal["none", "pending", "ready", "failed"]


class SceneOut(BaseModel):
    idx: int
    text: str
    lines: list[Line]
    image_url: str | None = None  # V3
    # A presigned MP3 URL, valid for AUDIO_URL_TTL_SECONDS; poll the story again for a fresh one.
    audio_url: str | None = None
    # "pending" while the page's narration is queued; "none" when the story has no narration.
    audio_status: AudioStatus = "none"


# Parent-facing notes for adapted ideas (stories.adaptation).
ADAPTATION_NOTICES = {
    BRANDS: "Your idea named characters we can't use, so we made up our own characters inspired by it.",
}


class StoryError(BaseModel):
    code: str
    detail: str | None


class StoryOut(BaseModel):
    id: uuid.UUID
    status: str
    age_band: str
    num_scenes: int
    title: str | None
    moral: str | None
    setting: str | None
    characters: list[CharacterOut]
    scenes: list[SceneOut]
    notice: str | None = None
    # "orpheus" or "kokoro" once narration is queued; null for a story without narration.
    tts_engine: str | None = None
    error: StoryError | None
    created_at: datetime
    updated_at: datetime


class JobOut(BaseModel):
    id: uuid.UUID
    kind: str
    status: str
    attempts: int
    story_id: uuid.UUID | None
    run_after: datetime
    created_at: datetime
    updated_at: datetime


@router.post(
    "/stories",
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        422: {"description": "Invalid body, unsafe story idea, or Idempotency-Key reused"},
        429: {"description": "Safety-check quota reached; see Retry-After"},
        503: {"description": "Safety check unavailable; see Retry-After"},
    },
    response_model=CreateStoryResponse,
)
async def create_story(
    body: CreateStoryRequest,
    request: Request,
    response: Response,
    stories: StoryServiceDep,
    idempotency_key: IdempotencyKey = None,
) -> CreateStoryResponse | JSONResponse:
    try:
        accepted = await stories.create(
            prompt=body.prompt,
            age_band=body.age_band,
            num_scenes=body.num_scenes,
            idempotency_key=idempotency_key,
        )
    except UnsafeInputError as exc:
        verdict = exc.verdict
        return problem_response(
            request,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            verdict.message,
            extra={"code": "unsafe_input", "category": verdict.category, "reason": verdict.reason},
        )
    except SafetyUnavailableError as exc:
        quota = "quota" in exc.reason or "rate limited" in exc.reason
        return problem_response(
            request,
            status.HTTP_429_TOO_MANY_REQUESTS if quota else status.HTTP_503_SERVICE_UNAVAILABLE,
            "Too many stories right now. Please try again shortly."
            if quota
            else "Story creation is temporarily unavailable. Please try again shortly.",
            headers={"Retry-After": str(max(1, round(exc.retry_after_seconds)))},
            extra={"code": "safety_check_unavailable"},
        )
    except IdempotencyConflictError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "This Idempotency-Key was already used with a different request.",
        ) from exc
    response.headers["Location"] = f"/v1/stories/{accepted.story_id}"
    if accepted.replayed:
        response.headers["Idempotent-Replayed"] = "true"
    return CreateStoryResponse(story_id=accepted.story_id, job_id=accepted.job_id, status=accepted.status)


@router.get("/stories/{story_id}")
async def get_story(
    story_id: uuid.UUID, sessions: SessionsDep, blobs: BlobStoreDep, settings: SettingsDep
) -> StoryOut:
    async with sessions() as session:
        story = await StoryRepository(session).get(story_id)
    if story is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Story not found.")
    # Content is shown only after the output safety check has passed.
    visible = story.status == StoryStatus.READY
    return StoryOut(
        id=story.id,
        status=story.status,
        age_band=story.age_band,
        num_scenes=story.num_scenes,
        title=story.title if visible else None,
        moral=story.moral if visible else None,
        setting=story.setting if visible else None,
        characters=[
            CharacterOut(id=c.char_key, name=c.name, species=c.species, visual=c.visual_desc)
            for c in (story.characters if visible else [])
        ],
        scenes=[
            _scene_out(story, s, blobs, settings.audio_url_ttl_seconds)
            for s in (story.scenes if visible else [])
        ],
        notice=ADAPTATION_NOTICES.get(story.adaptation or "") if visible else None,
        tts_engine=story.tts_engine if visible else None,
        error=StoryError(code=story.error_code, detail=story.error_detail) if story.error_code else None,
        created_at=story.created_at,
        updated_at=story.updated_at,
    )


def _scene_out(story: StoryRow, scene: SceneRow, blobs: BlobStore | None, ttl: int) -> SceneOut:
    audio_url = None
    audio_status: AudioStatus = "none"
    if story.tts_engine is not None:
        if scene.status == SceneStatus.AUDIO_FAILED:
            audio_status = "failed"
        elif scene.status == SceneStatus.AUDIO_READY and scene.audio_key and blobs is not None:
            audio_url = blobs.presigned_get_url(scene.audio_key, ttl)
            audio_status = "ready"
        elif scene.status == SceneStatus.TEXT_READY:
            audio_status = "pending"
    return SceneOut(
        idx=scene.idx,
        text=scene.text,
        lines=[Line.model_validate(line) for line in scene.lines],
        audio_url=audio_url,
        audio_status=audio_status,
    )


@router.get("/jobs/{job_id}")
async def get_job(job_id: uuid.UUID, sessions: SessionsDep) -> JobOut:
    async with sessions() as session:
        job = await JobQueue(session).get(job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found.")
    return JobOut(
        id=job.id,
        kind=job.kind,
        status=job.status,
        attempts=job.attempts,
        story_id=job.story_id,
        run_after=job.run_after,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )
