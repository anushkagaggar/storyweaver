"""Story endpoints (plan §6.8): POST /v1/stories -> 202, GET /v1/stories/{id}, GET /v1/jobs/{id}."""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from storyweaver.api.deps import SessionsDep, StoryServiceDep, require_api_key
from storyweaver.api.errors import problem_response
from storyweaver.domain.story import AgeBand, Line, SceneCount
from storyweaver.jobs.queue import JobQueue
from storyweaver.orchestrator.story_service import (
    IdempotencyConflictError,
    SafetyUnavailableError,
    UnsafeInputError,
)
from storyweaver.safety.guards import INPUT_REJECTED_MESSAGE
from storyweaver.storage.models import StoryStatus
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


class SceneOut(BaseModel):
    idx: int
    text: str
    lines: list[Line]
    image_url: str | None = None  # V3
    audio_url: str | None = None  # V2


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
            INPUT_REJECTED_MESSAGE,
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
async def get_story(story_id: uuid.UUID, sessions: SessionsDep) -> StoryOut:
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
            SceneOut(idx=s.idx, text=s.text, lines=[Line.model_validate(line) for line in s.lines])
            for s in (story.scenes if visible else [])
        ],
        error=StoryError(code=story.error_code, detail=story.error_detail) if story.error_code else None,
        created_at=story.created_at,
        updated_at=story.updated_at,
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
