"""In-process async job worker (plan §6.4). Celery is not used: it does not support Windows (plan risk 17)."""

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from storyweaver.jobs.queue import MAX_ATTEMPTS, JobQueue
from storyweaver.storage.models import JobKind, JobRow

logger = logging.getLogger(__name__)


class RetryableJobError(Exception):
    """Transient failure: retry with backoff (up to MAX_ATTEMPTS)."""


@dataclass
class PermanentJobError(Exception):
    """The job cannot succeed by retrying; `code` is stored on the story."""

    code: str
    detail: str

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


@dataclass
class DeferJobError(Exception):
    """Not a failure: try again after `seconds` without using an attempt (e.g. quota exhausted)."""

    seconds: float
    reason: str


@dataclass(frozen=True)
class JobContext:
    job_id: uuid.UUID
    story_id: uuid.UUID | None
    attempts: int
    is_last_attempt: bool
    kind: JobKind = JobKind.PLAN
    payload: dict[str, Any] = field(default_factory=dict)


GENERIC_FAILURE_DETAIL = "The story could not be generated. Please try again."

Handler = Callable[[JobContext], Awaitable[None]]
QueueChange = Callable[[JobQueue, JobRow], Awaitable[object]]
FailureHook = Callable[[JobContext, str, str], Awaitable[None]]


class Worker:
    """Runs jobs in lanes: one claim loop per lane, each with its own capacity.

    A lane only claims its own job kinds, so a narration job sleeping through Orpheus's per-minute pacing
    never holds up planning or illustration. The loops share one event loop, which keeps them independent
    only while handlers never block it: CPU work (Kokoro, Whisper, MP3 encoding) runs in threads, and GPU
    work is an HTTP call to the Gradio endpoint. By default every kind gets its own lane.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        handlers: dict[JobKind, Handler],
        *,
        lanes: dict[str, frozenset[JobKind]] | None = None,
        on_final_failure: FailureHook | None = None,
        poll_interval: float = 1.0,
        stale_after_seconds: int = 600,
    ) -> None:
        self._sessions = session_factory
        self._handlers = handlers
        self.lanes = lanes or {kind.value: frozenset({kind}) for kind in handlers}
        unhandled = set().union(*self.lanes.values()) - set(handlers)
        if unhandled:
            raise ValueError(f"Lanes name job kinds without a handler: {sorted(unhandled)}")
        self._on_final_failure = on_final_failure
        self._poll_interval = poll_interval
        self._stale_after = stale_after_seconds

    async def run_forever(self, stop: asyncio.Event) -> None:
        logger.info("Job worker started with lanes %s.", sorted(self.lanes))
        async with self._sessions() as session, session.begin():
            requeued = await JobQueue(session).requeue_stale(self._stale_after)
            if requeued:
                logger.warning("Requeued %d stale running job(s).", requeued)
        await asyncio.gather(*(self._run_lane(name, kinds, stop) for name, kinds in self.lanes.items()))
        logger.info("Job worker stopped.")

    async def _run_lane(self, name: str, kinds: frozenset[JobKind], stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                processed = await self.run_once(kinds)
            except Exception:
                logger.exception("Job worker lane %s loop error; continuing.", name)
                processed = False
            if not processed:
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self._poll_interval)

    async def run_once(self, kinds: frozenset[JobKind] | None = None) -> bool:
        """Claim and process one due job of `kinds` (default: any handled kind). False when there was none."""
        async with self._sessions() as session, session.begin():
            job = await JobQueue(session).claim_next(kinds if kinds is not None else self._handlers.keys())
            if job is None:
                return False
            context = JobContext(
                job_id=job.id,
                story_id=job.story_id,
                attempts=job.attempts,
                is_last_attempt=job.attempts >= MAX_ATTEMPTS,
                kind=JobKind(job.kind),
                payload=dict(job.payload),
            )
            kind = context.kind

        logger.info("Running job %s (%s), attempt %d.", context.job_id, kind, context.attempts)
        try:
            await self._handlers[kind](context)
        except DeferJobError as exc:
            seconds, reason = exc.seconds, exc.reason
            await self._update(context, lambda q, j: q.defer(j, seconds, reason))
            logger.info("Job %s deferred %.0fs: %s", context.job_id, seconds, reason)
        except PermanentJobError as exc:
            code, detail, message = exc.code, exc.detail, str(exc)
            await self._update(context, lambda q, j: q.fail(j, message, retryable=False))
            await self._final_failure(context, code, detail)
        except Exception as exc:
            retryable = isinstance(exc, RetryableJobError)
            if not retryable:
                logger.exception("Job %s raised unexpectedly.", context.job_id)
            message = f"{type(exc).__name__}: {exc}"
            requeued = await self._update(context, lambda q, j: q.fail(j, message, retryable=True))
            if not requeued:
                code = "upstream_unavailable" if retryable else "internal_error"
                await self._final_failure(
                    context, code, "The story could not be generated. Please try again."
                )
        else:
            await self._update(context, lambda q, j: q.succeed(j))
        return True

    async def _update(
        self, context: JobContext, change: Callable[[JobQueue, JobRow], Awaitable[object]]
    ) -> bool:
        async with self._sessions() as session, session.begin():
            queue = JobQueue(session)
            job = await queue.get(context.job_id)
            if job is None:
                return False
            return bool(await change(queue, job))

    async def _final_failure(self, context: JobContext, code: str, detail: str) -> None:
        logger.warning("Job %s failed permanently (%s).", context.job_id, code)
        if self._on_final_failure is not None:
            await self._on_final_failure(context, code, detail)
