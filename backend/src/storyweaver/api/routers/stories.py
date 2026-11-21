"""Story endpoints (plan §6.8): POST/GET /v1/stories, GET/DELETE /v1/stories/{id}, POST
/v1/stories/{id}/share, GET /v1/shared/{token}, GET /v1/jobs/{id}, and POST /v1/pictures.

A picture ("draw a purple dragon", V3) is a one-page story of kind "picture": it is read, listed, shared and
deleted through the same story endpoints.

Every story has an owner (auth/user_token.py): the signed-in user, from X-User-Token, or the operator for
API-key-only callers. Only the owner can read, list, share or delete a story; a share link lets anyone read
it until the link expires.
"""

import time
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from storyweaver.api.deps import (
    BlobStoreDep,
    OwnerDep,
    SessionsDep,
    SettingsDep,
    ShareLinksDep,
    StoryDeletionDep,
    StoryServiceDep,
    require_api_key,
)
from storyweaver.api.errors import problem_response
from storyweaver.domain.story import AgeBand, Line, SceneCount
from storyweaver.jobs.queue import JobQueue
from storyweaver.orchestrator.story_deletion import StoryInProgressError
from storyweaver.orchestrator.story_service import (
    AcceptedStory,
    CharacterNotFoundError,
    IdempotencyConflictError,
    PicturesUnavailableError,
    SafetyUnavailableError,
    UnclearPictureError,
    UnsafeInputError,
)
from storyweaver.routing.limits import LimitExceededError
from storyweaver.routing.router import is_daily_wait
from storyweaver.safety.guards import BRANDS
from storyweaver.storage.blob import BlobError, BlobStore
from storyweaver.storage.models import ImageStatus, SceneRow, SceneStatus, StoryRow, StoryStatus
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


class CreatePictureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(
        min_length=3, max_length=300, description='What to draw, as said or typed: "draw a purple dragon".'
    )
    # character_ref: draw a character from one of your own stories ("Tilly at the beach").
    story_id: uuid.UUID | None = Field(default=None, description="A story of yours whose character to draw.")
    character_id: str | None = Field(
        default=None, max_length=32, description="That story's character id; default: its first character."
    )

    @field_validator("prompt")
    @classmethod
    def _strip(cls, value: str) -> str:
        return " ".join(value.split())


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
ImageStatusOut = Literal["none", "pending", "ready", "placeholder", "failed"]


class SceneOut(BaseModel):
    idx: int
    text: str
    lines: list[Line]
    # A presigned PNG URL (same lifetime as audio_url). Set for "ready" and "placeholder".
    image_url: str | None = None
    # "placeholder": no GPU image could be made, so a soft wordless picture stands in. "none": no pictures.
    image_status: ImageStatusOut = "none"
    # A presigned MP3 URL, valid for AUDIO_URL_TTL_SECONDS; poll the story again for a fresh one.
    audio_url: str | None = None
    # "pending" while the page's narration is queued; "none" when the story has no narration.
    audio_status: AudioStatus = "none"


# Parent-facing notes for adapted ideas (stories.adaptation).
ADAPTATION_NOTICES = {
    BRANDS: "Your idea named characters we can't use, so we made up our own characters inspired by it.",
}


PICTURE_LIMIT_MESSAGES = {
    "hour": "You've drawn lots of pictures this hour! Please come back in a little while for more.",
    "day": "That's all the pictures for today. Please come back tomorrow for more.",
}

VISITOR_LIMIT_MESSAGES = {
    "hour": "You've made lots of stories this hour! Please come back in a little while for more.",
    "day": "That's all the stories for today. Please come back tomorrow for more.",
}


class StoryError(BaseModel):
    code: str
    detail: str | None


class StoryOut(BaseModel):
    id: uuid.UUID
    kind: str  # "story", or "picture" for one page drawn from a request
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


class StorySummaryOut(BaseModel):
    id: uuid.UUID
    kind: str
    status: str
    title: str | None  # set once the story is ready
    created_at: datetime


class StoryListOut(BaseModel):
    stories: list[StorySummaryOut]


class ShareOut(BaseModel):
    # Read it at GET /v1/shared/{token} (the website: /s/{token}) until expires_at; it is never renewed.
    token: str
    expires_at: datetime


class SharedStoryOut(StoryOut):
    share_expires_at: datetime


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
    owner: OwnerDep,
    idempotency_key: IdempotencyKey = None,
) -> CreateStoryResponse | JSONResponse:
    try:
        accepted = await stories.create(
            prompt=body.prompt,
            age_band=body.age_band,
            num_scenes=body.num_scenes,
            idempotency_key=idempotency_key,
            owner_id=owner,
        )
    except (LimitExceededError, UnsafeInputError, SafetyUnavailableError, IdempotencyConflictError) as exc:
        return _creation_problem(request, exc, VISITOR_LIMIT_MESSAGES)
    return _accepted(response, accepted)


@router.post(
    "/pictures",
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        404: {"description": "story_id/character_id names no character in a ready story of yours"},
        422: {"description": "Invalid body, nothing to draw, unsafe idea, or Idempotency-Key reused"},
        429: {"description": "Too many pictures, or safety-check quota reached; see Retry-After"},
        503: {"description": "Pictures or the safety check unavailable; see Retry-After"},
    },
    response_model=CreateStoryResponse,
)
async def create_picture(
    body: CreatePictureRequest,
    request: Request,
    response: Response,
    stories: StoryServiceDep,
    owner: OwnerDep,
    idempotency_key: IdempotencyKey = None,
) -> CreateStoryResponse | JSONResponse:
    """One picture from words like "draw a purple dragon". Read it with GET /v1/stories/{story_id}."""
    try:
        accepted = await stories.create_picture(
            prompt=body.prompt,
            idempotency_key=idempotency_key,
            owner_id=owner,
            story_id=body.story_id,
            character_id=body.character_id,
        )
    except PicturesUnavailableError:
        return problem_response(
            request,
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Pictures aren't available right now. Please try again later.",
            headers={"Retry-After": "300"},
            extra={"code": "pictures_unavailable"},
        )
    except CharacterNotFoundError:
        return problem_response(
            request,
            status.HTTP_404_NOT_FOUND,
            "We couldn't find that character in your stories.",
            extra={"code": "character_not_found"},
        )
    except UnclearPictureError:
        return problem_response(
            request,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            'Please say what to draw, for example "a purple dragon".',
            extra={"code": "nothing_to_draw"},
        )
    except (LimitExceededError, UnsafeInputError, SafetyUnavailableError, IdempotencyConflictError) as exc:
        return _creation_problem(request, exc, PICTURE_LIMIT_MESSAGES)
    return _accepted(response, accepted)


def _accepted(response: Response, accepted: AcceptedStory) -> CreateStoryResponse:
    response.headers["Location"] = f"/v1/stories/{accepted.story_id}"
    if accepted.replayed:
        response.headers["Idempotent-Replayed"] = "true"
    return CreateStoryResponse(story_id=accepted.story_id, job_id=accepted.job_id, status=accepted.status)


def _creation_problem(request: Request, exc: Exception, limit_messages: dict[str, str]) -> JSONResponse:
    """The answer for a story or picture request that was refused before anything was stored."""
    if isinstance(exc, LimitExceededError):
        return problem_response(
            request,
            status.HTTP_429_TOO_MANY_REQUESTS,
            limit_messages[exc.window],
            headers={"Retry-After": str(max(1, round(exc.retry_after_seconds)))},
            extra={"code": "visitor_limit", "window": exc.window},
        )
    if isinstance(exc, UnsafeInputError):
        verdict = exc.verdict
        return problem_response(
            request,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            verdict.message,
            extra={"code": "unsafe_input", "category": verdict.category, "reason": verdict.reason},
        )
    if isinstance(exc, SafetyUnavailableError):
        quota = "quota" in exc.reason or "rate limited" in exc.reason
        if quota and is_daily_wait(exc.retry_after_seconds):
            message = "We've made all the stories we can for today. Please come back tomorrow."
        elif quota:
            message = "Too many stories right now. Please try again shortly."
        else:
            message = "Story creation is temporarily unavailable. Please try again shortly."
        return problem_response(
            request,
            status.HTTP_429_TOO_MANY_REQUESTS if quota else status.HTTP_503_SERVICE_UNAVAILABLE,
            message,
            headers={"Retry-After": str(max(1, round(exc.retry_after_seconds)))},
            extra={"code": "safety_check_unavailable"},
        )
    return problem_response(
        request,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "This Idempotency-Key was already used with a different request.",
    )


@router.get("/stories")
async def list_stories(
    sessions: SessionsDep, owner: OwnerDep, limit: Annotated[int, Query(ge=1, le=50)] = 20
) -> StoryListOut:
    """The caller's own stories, newest first ("My stories")."""
    async with sessions() as session:
        rows = await StoryRepository(session).list_for_owner(owner, limit=limit)
    return StoryListOut(
        stories=[
            StorySummaryOut(
                id=row.id,
                kind=row.kind,
                status=row.status,
                title=row.title if row.status == StoryStatus.READY else None,
                created_at=row.created_at,
            )
            for row in rows
        ]
    )


@router.get("/stories/{story_id}")
async def get_story(
    story_id: uuid.UUID, sessions: SessionsDep, blobs: BlobStoreDep, settings: SettingsDep, owner: OwnerDep
) -> StoryOut:
    story = await _owned_story(sessions, story_id, owner)
    return _story_out(story, blobs, settings.audio_url_ttl_seconds)


@router.post(
    "/stories/{story_id}/share",
    status_code=status.HTTP_201_CREATED,
    responses={
        404: {"description": "No such story, or not the caller's"},
        409: {"description": "The story isn't ready to read yet"},
    },
    response_model=ShareOut,
)
async def share_story(
    story_id: uuid.UUID, request: Request, sessions: SessionsDep, owner: OwnerDep, links: ShareLinksDep
) -> ShareOut | JSONResponse:
    """A link anyone can read the story with, until `expires_at` (SHARE_LINK_TTL_SECONDS after now). Only the
    owner can make one. Each call makes a new link with its own expiry; none is ever extended."""
    story = await _owned_story(sessions, story_id, owner)
    if story.status != StoryStatus.READY:
        return problem_response(
            request,
            status.HTTP_409_CONFLICT,
            "This story can be shared once it's ready.",
            extra={"code": "story_not_ready"},
        )
    token, shared = links.make(story.id)
    return ShareOut(token=token, expires_at=datetime.fromtimestamp(shared.expires_at, UTC))


@router.get("/shared/{token}", responses={404: {"description": "Unknown, expired or deleted"}})
async def get_shared_story(
    token: str, sessions: SessionsDep, blobs: BlobStoreDep, settings: SettingsDep, links: ShareLinksDep
) -> SharedStoryOut:
    """Read a story through its owner's share link. Needs no sign-in (the API key still applies).

    Picture and audio links expire with the share link, so a reader can't keep listening after it ends.
    """
    now = time.time()
    shared = links.read(token, now)
    if shared is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, SHARE_LINK_GONE)
    async with sessions() as session:
        story = await StoryRepository(session).get(shared.story_id)
    if story is None or story.status != StoryStatus.READY:
        raise HTTPException(status.HTTP_404_NOT_FOUND, SHARE_LINK_GONE)
    ttl = max(1, min(settings.audio_url_ttl_seconds, shared.seconds_left(now)))
    out = _story_out(story, blobs, ttl)
    return SharedStoryOut(**out.model_dump(), share_expires_at=datetime.fromtimestamp(shared.expires_at, UTC))


SHARE_LINK_GONE = "This link has expired or the story was deleted. Ask for a new link."


async def _owned_story(
    sessions: async_sessionmaker[AsyncSession], story_id: uuid.UUID, owner: str
) -> StoryRow:
    """The story, if it exists and belongs to `owner`. Anyone else's story is "not found", the same as a
    missing one, so ids can't be probed. Stories from before sign-in (no owner) belong to nobody."""
    async with sessions() as session:
        story = await StoryRepository(session).get(story_id)
    if story is None or story.status == StoryStatus.DELETING or story.owner_id != owner:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Story not found.")
    return story


def _story_out(story: StoryRow, blobs: BlobStore | None, ttl: int) -> StoryOut:
    # Content is shown only after the output safety check has passed.
    visible = story.status == StoryStatus.READY
    return StoryOut(
        id=story.id,
        kind=story.kind,
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
        scenes=[_scene_out(story, s, blobs, ttl) for s in (story.scenes if visible else [])],
        notice=ADAPTATION_NOTICES.get(story.adaptation or "") if visible else None,
        tts_engine=story.tts_engine if visible else None,
        error=StoryError(code=story.error_code, detail=story.error_detail) if story.error_code else None,
        created_at=story.created_at,
        updated_at=story.updated_at,
    )


@router.delete(
    "/stories/{story_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        404: {"description": "No such story, already deleted, or not the caller's"},
        409: {"description": "The story is still being made"},
        503: {"description": "Its files couldn't be deleted yet; retry (the story already reads as deleted)"},
    },
)
async def delete_story(
    story_id: uuid.UUID, request: Request, deletion: StoryDeletionDep, owner: OwnerDep
) -> Response:
    """Delete a story, its pages, pictures and narration audio (privacy). Only its owner can; a share link
    can't (docs/dpia.md)."""
    try:
        deleted = await deletion.delete(story_id, owner_id=owner)
    except StoryInProgressError:
        return problem_response(
            request,
            status.HTTP_409_CONFLICT,
            "This story is still being made. Please try again in a minute.",
            headers={"Retry-After": "30"},
            extra={"code": "story_in_progress"},
        )
    except BlobError:
        return problem_response(
            request,
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The story is hidden, but its audio and pictures couldn't be removed yet. "
            "Please try again shortly.",
            headers={"Retry-After": "30"},
            extra={"code": "delete_incomplete"},
        )
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Story not found.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


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
    image_url = None
    image_status: ImageStatusOut = "none"
    if scene.image_status in (ImageStatus.READY, ImageStatus.PLACEHOLDER):
        if scene.image_key and blobs is not None:
            image_url = blobs.presigned_get_url(scene.image_key, ttl)
            image_status = "ready" if scene.image_status == ImageStatus.READY else "placeholder"
    elif scene.image_status in (ImageStatus.PENDING, ImageStatus.FAILED):
        image_status = "pending" if scene.image_status == ImageStatus.PENDING else "failed"
    return SceneOut(
        idx=scene.idx,
        text=scene.text,
        lines=[Line.model_validate(line) for line in scene.lines],
        image_url=image_url,
        image_status=image_status,
        audio_url=audio_url,
        audio_status=audio_status,
    )


@router.get("/jobs/{job_id}")
async def get_job(job_id: uuid.UUID, sessions: SessionsDep, owner: OwnerDep) -> JobOut:
    async with sessions() as session:
        job = await JobQueue(session).get(job_id)
        story = await StoryRepository(session).get(job.story_id) if job and job.story_id else None
    if job is None or story is None or story.owner_id != owner:
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
