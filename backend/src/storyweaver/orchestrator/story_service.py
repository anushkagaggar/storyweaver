"""Story use cases: accept a story request (API) and plan it (worker job). Plan §6.4 steps 1-2.

V1 order: input safety (slice 5) → insert story → enqueue `plan` → 202. The worker then plans the story and
saves it. TTS and image jobs are added in V2/V3.
"""

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from storyweaver.domain.story import AgeBand, SceneCount
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
from storyweaver.routing.router import QuotaExhaustedError
from storyweaver.storage.models import JobKind, StoryStatus
from storyweaver.storage.repositories import StoryRepository

logger = logging.getLogger(__name__)


class IdempotencyConflictError(Exception):
    """The Idempotency-Key was already used with a different request."""


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
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

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
                return existing
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
    """Worker handler for `plan` jobs. Holds no DB transaction while the LLM runs."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession], planner: StoryPlanner) -> None:
        self._sessions = sessions
        self._planner = planner

    async def __call__(self, context: JobContext) -> None:
        if context.story_id is None:
            raise PermanentJobError("invalid_job", "Plan job has no story.")

        async with self._sessions() as session, session.begin():
            repo = StoryRepository(session)
            story = await repo.get(context.story_id)
            if story is None:
                raise PermanentJobError("not_found", "Story no longer exists.")
            if story.status == StoryStatus.READY:
                return  # already planned (duplicate delivery)
            await repo.set_status(story, StoryStatus.PLANNING)
            request = StoryRequest(
                prompt=story.prompt_text,
                age_band=story.age_band,  # type: ignore[arg-type]  # validated on insert
                num_scenes=story.num_scenes,  # type: ignore[arg-type]
            )

        try:
            result = await self._planner.plan(request, story_key=str(context.story_id))
        except QuotaExhaustedError as exc:
            await self._set_status(context.story_id, StoryStatus.QUEUED)
            raise DeferJobError(exc.retry_after_seconds, str(exc)) from exc
        except LLMRateLimitedError as exc:
            await self._set_status(context.story_id, StoryStatus.QUEUED)
            raise DeferJobError(exc.retry_after_seconds or 60, "All models rate limited.") from exc
        except PlanningError as exc:
            raise PermanentJobError("planning_failed", "; ".join(exc.problems)) from exc
        except LLMError as exc:
            if not context.is_last_attempt:
                await self._set_status(context.story_id, StoryStatus.QUEUED)
            raise RetryableJobError(str(exc)) from exc

        async with self._sessions() as session, session.begin():
            repo = StoryRepository(session)
            story = await repo.get(context.story_id)
            if story is None:
                raise PermanentJobError("not_found", "Story was deleted while planning.")
            await repo.save_plan(story, result.story)
        logger.info("Story %s planned (repaired=%s).", context.story_id, result.repaired)

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
