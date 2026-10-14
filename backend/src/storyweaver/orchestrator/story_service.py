"""Story use cases: accept a story request (API) and plan it (worker job). Plan §6.4 steps 1-2.

V1 order: input safety check -> insert story -> enqueue `plan` -> 202. Rejected ideas are never stored.
The worker plans the story, saves it as CHECKING (hidden from readers), runs the output safety check and then
marks it READY. A retry after a deferred check re-checks the saved plan instead of planning again.
TTS and image jobs are added in V2/V3.
"""

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from storyweaver.domain.story import AgeBand, SceneCount, Story
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
from storyweaver.routing.router import QuotaExhaustedError
from storyweaver.safety.guards import (
    OUTPUT_REJECTED_MESSAGE,
    InputGuard,
    OutputGuard,
    SafetyCheckError,
    SafetyVerdict,
)
from storyweaver.storage.models import JobKind, StoryStatus
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


@dataclass(frozen=True)
class AcceptedStory:
    story_id: uuid.UUID
    job_id: uuid.UUID
    status: str
    replayed: bool


def request_hash(prompt: str, age_band: AgeBand, num_scenes: SceneCount) -> str:
    canonical = json.dumps({"prompt": prompt, "age_band": age_band, "num_scenes": num_scenes}, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


class StoryService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], input_guard: InputGuard | None) -> None:
        self._sessions = sessions
        self._input_guard = input_guard

    async def create(
        self,
        *,
        prompt: str,
        age_band: AgeBand,
        num_scenes: SceneCount,
        idempotency_key: str | None,
    ) -> AcceptedStory:
        digest = request_hash(prompt, age_band, num_scenes)
        if idempotency_key is not None:
            existing = await self._replay(idempotency_key, digest)
            if existing is not None:
                return existing  # this exact request already passed the safety check
        await self._check_input(prompt)
        try:
            async with self._sessions() as session, session.begin():
                story = await StoryRepository(session).create(
                    prompt_text=prompt,
                    age_band=age_band,
                    num_scenes=num_scenes,
                    idempotency_key=idempotency_key,
                    request_hash=digest,
                )
                job = await JobQueue(session).enqueue(
                    JobKind.PLAN, {"story_id": str(story.id)}, story_id=story.id
                )
                return AcceptedStory(story.id, job.id, story.status, replayed=False)
        except IntegrityError:
            # A concurrent request with the same Idempotency-Key won the race.
            if idempotency_key is None:
                raise
            replay = await self._replay(idempotency_key, digest)
            if replay is None:
                raise
            return replay

    async def _check_input(self, prompt: str) -> None:
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
        self, sessions: async_sessionmaker[AsyncSession], planner: StoryPlanner, output_guard: OutputGuard
    ) -> None:
        self._sessions = sessions
        self._planner = planner
        self._output_guard = output_guard

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

        await self._set_status(story_id, StoryStatus.READY)
        logger.info("Story %s is ready.", story_id)

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
    """Mark the story failed once its job has failed for good."""

    async def on_final_failure(context: JobContext, code: str, detail: str) -> None:
        if context.story_id is None:
            return
        async with sessions() as session, session.begin():
            repo = StoryRepository(session)
            story = await repo.get(context.story_id)
            if story is not None:
                await repo.mark_failed(story, error_code=code, error_detail=detail)

    return on_final_failure
