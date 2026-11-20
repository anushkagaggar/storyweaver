"""Story use cases: accept a story request (API) and plan it (worker job). Plan §6.4 steps 1-2.

V1 order: input safety check -> insert story -> enqueue `plan` -> 202. Rejected ideas are never stored.
The worker plans the story, saves it as CHECKING (hidden from readers), runs the output safety check and then
marks it READY. A retry after a deferred check re-checks the saved plan instead of planning again.
V2: marking it READY also picks the TTS engine and voices and enqueues one `tts` job per scene, scene 1 first
(orchestrator/narration_jobs.py). V3: and one `image` job per scene (orchestrator/illustration_jobs.py).
"""

import hashlib
import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from storyweaver.domain.story import AgeBand, SceneCount, Story, character_seed
from storyweaver.imaging.generator import PNG
from storyweaver.jobs.queue import JobQueue
from storyweaver.jobs.worker import (
    DeferJobError,
    FailureHook,
    JobContext,
    PermanentJobError,
    RetryableJobError,
)
from storyweaver.llm.client import LLMError, LLMRateLimitedError
from storyweaver.llm.planner import PlanningError, StoryPlanner, StoryRequest
from storyweaver.observability.tracing import trace_scope
from storyweaver.orchestrator.illustration_jobs import main_character, mark_scene_image_failed
from storyweaver.orchestrator.narration_jobs import StoryNarration, mark_scene_audio_failed
from storyweaver.orchestrator.picture_request import picture_caption, picture_subject, picture_title
from storyweaver.routing.limits import VisitorLimiter
from storyweaver.routing.router import QuotaExhaustedError
from storyweaver.safety.guards import (
    BRANDS,
    OUTPUT_REJECTED_MESSAGE,
    InputGuard,
    OutputGuard,
    SafetyCheckError,
    SafetyVerdict,
)
from storyweaver.storage.blob import BlobError, BlobStore, reference_image_key
from storyweaver.storage.models import (
    CharacterRow,
    ImageStatus,
    JobKind,
    SceneRow,
    StoryKind,
    StoryRow,
    StoryStatus,
)
from storyweaver.storage.repositories import StoryRepository, row_to_story

logger = logging.getLogger(__name__)


class IdempotencyConflictError(Exception):
    """The Idempotency-Key was already used with a different request."""


@dataclass
class UnsafeInputError(Exception):
    verdict: SafetyVerdict


@dataclass
class SafetyUnavailableError(Exception):
    """The safety check could not run (quota, outage, or not configured). Fail closed: nothing is created."""

    retry_after_seconds: float
    reason: str

    def __str__(self) -> str:
        return self.reason


class PicturesUnavailableError(Exception):
    """This server draws no pictures (no image engine, storage or worker)."""


class CharacterNotFoundError(Exception):
    """No such character in a ready story of the caller's (or no story_id given with a character_id)."""


@dataclass(frozen=True)
class SourceCharacter:
    name: str
    species: str
    visual: str
    seed: int
    reference_key: str | None


class UnclearPictureError(Exception):
    """Nothing to draw was left once the request words were taken out ("draw", "please")."""


# The picture's one character: its look is the request, so the self-check looks for what was asked for.
PICTURE_CHARACTER = "main"
PICTURE_BRANDS = "picture_brands"
# A borrowed character's seed is moved past its story's pages (seed + 1..6): with page 1's own seed, the
# picture copied page 1's layout and ignored the new scene (20 Nov 2026). The fixed look and page 1's
# picture (the IP-Adapter reference) keep it recognisable instead.
BORROWED_SEED_OFFSET = 100


@dataclass(frozen=True)
class AcceptedStory:
    story_id: uuid.UUID
    job_id: uuid.UUID
    status: str
    replayed: bool


def picture_request_hash(
    subject: str, owner_id: str, story_id: uuid.UUID | None = None, character_id: str | None = None
) -> str:
    """Never equal to a story's request_hash: an Idempotency-Key reused across the two is a conflict."""
    canonical = json.dumps(
        {
            "kind": StoryKind.PICTURE.value,
            "subject": subject,
            "owner": owner_id,
            "story": str(story_id) if story_id else None,
            "character": character_id,
        },
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def request_hash(prompt: str, age_band: AgeBand, num_scenes: SceneCount, owner_id: str) -> str:
    """The owner is part of it, so another user reusing an Idempotency-Key gets a conflict, not the story."""
    canonical = json.dumps(
        {"prompt": prompt, "age_band": age_band, "num_scenes": num_scenes, "owner": owner_id}, sort_keys=True
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


class StoryService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        input_guard: InputGuard | None,
        visitor_limiter: VisitorLimiter | None = None,
        *,
        picture_limiter: VisitorLimiter | None = None,
        pictures_enabled: bool = False,
    ) -> None:
        self._sessions = sessions
        self._input_guard = input_guard
        self._visitor_limiter = visitor_limiter
        self._picture_limiter = picture_limiter
        # Pictures need the worker and an image engine with somewhere to store them (services.py).
        self.pictures_enabled = pictures_enabled
        # Where a character's reference picture is copied for a picture of it (set in services.py).
        self.blobs: BlobStore | None = None
        # Called after a job is committed, to wake the in-process worker (Worker.wake); it does not poll.
        self.on_enqueued: Callable[[], None] | None = None

    async def create(
        self,
        *,
        prompt: str,
        age_band: AgeBand,
        num_scenes: SceneCount,
        idempotency_key: str | None,
        owner_id: str,
    ) -> AcceptedStory:
        """Raises LimitExceededError when this user has made too many stories (counted before the safety
        check, so rejected ideas count too: they cost Groq quota as well)."""
        digest = request_hash(prompt, age_band, num_scenes, owner_id)
        if idempotency_key is not None:
            existing = await self._replay(idempotency_key, digest)
            if existing is not None:
                return existing  # this exact request already passed the safety check (and was counted)
        if self._visitor_limiter is not None:
            await self._visitor_limiter.acquire(owner_id)
        verdict = await self._check_input(prompt)
        try:
            async with self._sessions() as session, session.begin():
                story = await StoryRepository(session).create(
                    prompt_text=prompt,
                    age_band=age_band,
                    num_scenes=num_scenes,
                    idempotency_key=idempotency_key,
                    request_hash=digest,
                    adaptation=verdict.adaptation,
                    owner_id=owner_id,
                )
                job = await JobQueue(session).enqueue(
                    JobKind.PLAN, {"story_id": str(story.id)}, story_id=story.id
                )
                accepted = AcceptedStory(story.id, job.id, story.status, replayed=False)
        except IntegrityError:
            # A concurrent request with the same Idempotency-Key won the race.
            if idempotency_key is None:
                raise
            replay = await self._replay(idempotency_key, digest)
            if replay is None:
                raise
            return replay
        if self.on_enqueued is not None:
            self.on_enqueued()
        return accepted

    async def create_picture(
        self,
        *,
        prompt: str,
        idempotency_key: str | None,
        owner_id: str,
        story_id: uuid.UUID | None = None,
        character_id: str | None = None,
    ) -> AcceptedStory:
        """One picture from words like "draw a purple dragon" (plan V3, speech -> picture).

        A one-page story of kind "picture": the same input checks as a story, then a READY row with one
        scene and one `image` job, so ownership, sharing, deletion, expiry and the picture pipeline (GPU
        budget, self-check, placeholder) all apply. No planner and no narration.

        With `story_id` (and optionally `character_id`, default: the story's first character) it draws that
        character from one of the caller's own stories (MCP `illustrate`'s character_ref): its fixed look and
        seed, and the story's page-1 picture as the IP-Adapter reference when that page shows it. Raises
        PicturesUnavailableError, UnclearPictureError, CharacterNotFoundError, UnsafeInputError,
        LimitExceededError, SafetyUnavailableError or IdempotencyConflictError.
        """
        if not self.pictures_enabled:
            raise PicturesUnavailableError
        subject = picture_subject(prompt)
        if subject is None:
            raise UnclearPictureError
        if character_id is not None and story_id is None:
            raise CharacterNotFoundError
        digest = picture_request_hash(subject, owner_id, story_id, character_id)
        if idempotency_key is not None:
            existing = await self._replay(idempotency_key, digest)
            if existing is not None:
                return existing
        source = await self._source_character(owner_id, story_id, character_id) if story_id else None
        if self._picture_limiter is not None:
            await self._picture_limiter.acquire(owner_id)
        verdict = await self._check_input(subject)
        if verdict.adaptation == BRANDS:
            # A story's planner swaps in original characters; a picture would have to draw the brand's.
            raise UnsafeInputError(SafetyVerdict(False, stage="policy", category=PICTURE_BRANDS))
        picture_id = uuid.uuid4()
        reference = await self._copy_reference(source, picture_id) if source else None
        try:
            async with self._sessions() as session, session.begin():
                story = await StoryRepository(session).create(
                    prompt_text=prompt,
                    age_band="4-6",
                    num_scenes=1,
                    idempotency_key=idempotency_key,
                    request_hash=digest,
                    owner_id=owner_id,
                    kind=StoryKind.PICTURE,
                    story_id=picture_id,
                )
                story.title = picture_title(subject)
                story.status = StoryStatus.READY
                session.add_all(
                    [
                        CharacterRow(
                            story_id=story.id,
                            char_key=PICTURE_CHARACTER,
                            position=0,
                            name=source.name if source else "",
                            species=source.species if source else "",
                            # The character's own look is what the picture check looks for.
                            visual_desc=source.visual if source else subject,
                            seed=source.seed + BORROWED_SEED_OFFSET
                            if source
                            else character_seed(str(story.id), PICTURE_CHARACTER),
                            ref_image_key=reference,
                        ),
                        SceneRow(
                            story_id=story.id,
                            idx=1,
                            text=picture_caption(subject),
                            image_prompt=subject,
                            character_keys=[PICTURE_CHARACTER],
                            lines=[],
                            image_status=ImageStatus.PENDING,
                        ),
                    ]
                )
                job = await JobQueue(session).enqueue(
                    JobKind.IMAGE, {"story_id": str(story.id), "idx": 1}, story_id=story.id
                )
                accepted = AcceptedStory(story.id, job.id, story.status, replayed=False)
        except IntegrityError:
            if idempotency_key is None:
                raise
            replay = await self._replay(idempotency_key, digest)
            if replay is None:
                raise
            return replay
        if self.on_enqueued is not None:
            self.on_enqueued()
        return accepted

    async def _source_character(
        self, owner_id: str, story_id: uuid.UUID, character_id: str | None
    ) -> "SourceCharacter":
        """A character of one of the caller's own ready stories, or CharacterNotFoundError (also for someone
        else's story, like every story route)."""
        async with self._sessions() as session:
            story = await StoryRepository(session).get(story_id)
            if story is None or story.owner_id != owner_id or story.status != StoryStatus.READY:
                raise CharacterNotFoundError
            characters = list(story.characters)
            if character_id is None:
                character = characters[0] if characters else None
            else:
                character = next((c for c in characters if c.char_key == character_id), None)
            if character is None:
                raise CharacterNotFoundError
            # Page 1's picture is the reference when it is a real picture of this character.
            scenes = sorted(story.scenes, key=lambda s: s.idx)
            first = scenes[0] if scenes else None
            reference = None
            if (
                first is not None
                and first.image_status == ImageStatus.READY
                and first.image_key
                and (lead := main_character(first, characters)) is not None
                and lead.char_key == character.char_key
            ):
                reference = first.image_key
            return SourceCharacter(
                name=character.name,
                species=character.species,
                visual=character.visual_desc,
                seed=character.seed,
                reference_key=reference,
            )

    async def _copy_reference(self, source: "SourceCharacter", picture_id: uuid.UUID) -> str | None:
        """Copy the reference under the picture's own prefix; without it the picture still keeps the look."""
        if source.reference_key is None or self.blobs is None:
            return None
        key = reference_image_key(picture_id)
        try:
            await self.blobs.put(key, await self.blobs.get(source.reference_key), PNG)
        except BlobError as exc:
            logger.warning("Could not copy a character's reference picture (%s); drawing without it.", exc)
            return None
        return key

    async def _check_input(self, prompt: str) -> SafetyVerdict:
        if self._input_guard is None:
            raise SafetyUnavailableError(60, "Safety checks are not configured.")
        try:
            with trace_scope("story.input_check"):  # no story id yet: rejected ideas are never stored
                verdict = await self._input_guard.check(prompt)
        except QuotaExhaustedError as exc:
            raise SafetyUnavailableError(exc.retry_after_seconds, "Safety check quota reached.") from exc
        except LLMRateLimitedError as exc:
            raise SafetyUnavailableError(exc.retry_after_seconds or 60, "Safety check rate limited.") from exc
        except (LLMError, SafetyCheckError) as exc:
            logger.warning("Input safety check unavailable: %s", exc)
            raise SafetyUnavailableError(30, "Safety check unavailable.") from exc
        if not verdict.allowed:
            raise UnsafeInputError(verdict)
        return verdict

    async def _replay(self, key: str, digest: str) -> AcceptedStory | None:
        async with self._sessions() as session:
            story = await StoryRepository(session).get_by_idempotency_key(key)
            if story is None:
                return None
            if story.request_hash != digest:
                raise IdempotencyConflictError(key)
            job = await JobQueue(session).latest_for_story(story.id)
            if job is None:  # cannot happen: story and job are created in one transaction
                raise RuntimeError(f"Story {story.id} has no job.")
            return AcceptedStory(story.id, job.id, story.status, replayed=True)


class PlanStoryJob:
    """Worker handler for `plan` jobs: plan, save as CHECKING, run the output safety check, mark READY.

    Holds no DB transaction while an LLM runs.
    """

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        planner: StoryPlanner,
        output_guard: OutputGuard,
        narration: StoryNarration | None = None,
        *,
        illustrate: bool = False,
    ) -> None:
        self._sessions = sessions
        self._planner = planner
        self._output_guard = output_guard
        self._narration = narration  # None: stories have no audio (no object storage configured)
        self._illustrate = illustrate  # queue an image job per scene (needs object storage)

    async def __call__(self, context: JobContext) -> None:
        if context.story_id is None:
            raise PermanentJobError("invalid_job", "Plan job has no story.")
        # One trace per attempt, grouped by story id (Langfuse session): planner, repair, output check.
        with trace_scope("story.plan", session_id=str(context.story_id)):
            await self._run(context, context.story_id)

    async def _run(self, context: JobContext, story_id: uuid.UUID) -> None:
        async with self._sessions() as session, session.begin():
            repo = StoryRepository(session)
            row = await repo.get(story_id)
            if row is None:
                raise PermanentJobError("not_found", "Story no longer exists.")
            if row.status == StoryStatus.READY:
                return  # already done (duplicate delivery)
            planned = row_to_story(row) if row.status == StoryStatus.CHECKING else None
            if planned is None:
                await repo.set_status(row, StoryStatus.PLANNING)
            request = StoryRequest(
                prompt=row.prompt_text,
                age_band=row.age_band,  # type: ignore[arg-type]  # validated on insert
                num_scenes=row.num_scenes,  # type: ignore[arg-type]
                adaptation=row.adaptation,
            )

        if planned is None:
            planned = await self._plan(story_id, context, request)
            async with self._sessions() as session, session.begin():
                repo = StoryRepository(session)
                row = await repo.get(story_id)
                if row is None:
                    raise PermanentJobError("not_found", "Story was deleted while planning.")
                await repo.save_plan(row, planned, status=StoryStatus.CHECKING)

        try:
            verdict = await self._output_guard.check(planned)
        except QuotaExhaustedError as exc:
            raise DeferJobError(exc.retry_after_seconds, f"Output safety check: {exc}") from exc
        except LLMRateLimitedError as exc:
            raise DeferJobError(exc.retry_after_seconds or 60, "Output safety check rate limited.") from exc
        except (LLMError, SafetyCheckError) as exc:
            raise RetryableJobError(f"Output safety check unavailable: {exc}") from exc
        if not verdict.allowed:
            logger.warning("Story %s rejected by the output check (category=%s).", story_id, verdict.category)
            raise PermanentJobError("unsafe_output", OUTPUT_REJECTED_MESSAGE)

        await self._publish(story_id, planned)
        logger.info("Story %s is ready.", story_id)

    async def _publish(self, story_id: uuid.UUID, planned: Story) -> None:
        """Mark the story READY and, in the same transaction, queue its audio and pictures (scene 1 first)."""
        narration = await self._narration.prepare(story_id, planned) if self._narration else None
        async with self._sessions() as session, session.begin():
            repo = StoryRepository(session)
            row = await repo.get(story_id)
            if row is None:
                raise PermanentJobError("not_found", "Story was deleted while checking.")
            await repo.set_status(row, StoryStatus.READY)
            if narration is not None:
                await repo.set_narration(row, narration.engine, narration.voices)
                await _enqueue_scenes(JobQueue(session), row, JobKind.TTS)
            if self._illustrate:
                await repo.mark_images_pending(row)
                await _enqueue_scenes(JobQueue(session), row, JobKind.IMAGE)

    async def _plan(self, story_id: uuid.UUID, context: JobContext, request: StoryRequest) -> Story:
        try:
            result = await self._planner.plan(request, story_key=str(story_id))
        except QuotaExhaustedError as exc:
            await self._set_status(story_id, StoryStatus.QUEUED)
            raise DeferJobError(exc.retry_after_seconds, str(exc)) from exc
        except LLMRateLimitedError as exc:
            await self._set_status(story_id, StoryStatus.QUEUED)
            raise DeferJobError(exc.retry_after_seconds or 60, "All models rate limited.") from exc
        except PlanningError as exc:
            raise PermanentJobError("planning_failed", "; ".join(exc.problems)) from exc
        except LLMError as exc:
            if not context.is_last_attempt:
                await self._set_status(story_id, StoryStatus.QUEUED)
            raise RetryableJobError(str(exc)) from exc
        logger.info("Story %s planned (repaired=%s).", story_id, result.repaired)
        return result.story

    async def _set_status(self, story_id: uuid.UUID, status: StoryStatus) -> None:
        async with self._sessions() as session, session.begin():
            repo = StoryRepository(session)
            story = await repo.get(story_id)
            if story is not None:
                await repo.set_status(story, status)


def make_story_failure_hook(sessions: async_sessionmaker[AsyncSession]) -> FailureHook:
    """Once a job has failed for good: a plan job fails its story; a tts or image job only loses that page's
    audio or picture."""

    async def on_final_failure(context: JobContext, code: str, detail: str) -> None:
        if context.story_id is None:
            return
        if context.kind in (JobKind.TTS, JobKind.IMAGE):
            idx = context.payload.get("idx")
            if isinstance(idx, int):
                mark = mark_scene_audio_failed if context.kind == JobKind.TTS else mark_scene_image_failed
                await mark(sessions, context.story_id, idx)
            return
        async with sessions() as session, session.begin():
            repo = StoryRepository(session)
            story = await repo.get(context.story_id)
            if story is not None:
                await repo.mark_failed(story, error_code=code, error_detail=detail)

    return on_final_failure


async def _enqueue_scenes(queue: JobQueue, row: StoryRow, kind: JobKind) -> None:
    for scene in row.scenes:
        await queue.enqueue(
            kind, {"story_id": str(row.id), "idx": scene.idx}, story_id=row.id, order=scene.idx
        )
