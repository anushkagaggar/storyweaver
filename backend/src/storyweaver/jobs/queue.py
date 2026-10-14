"""Postgres-backed job queue (plan §6.4). Workers claim with FOR UPDATE SKIP LOCKED, so they never collide.

All times use the database clock (now()), so several workers agree on what is due.
"""

import hashlib
import json
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from storyweaver.storage.models import JobKind, JobRow, JobStatus

MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 5
BACKOFF_FACTOR = 4


def payload_hash(kind: JobKind, payload: dict[str, Any]) -> str:
    canonical = json.dumps({"kind": kind.value, "payload": payload}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def backoff_seconds(attempts: int) -> int:
    """5 s after the first failure, 20 s after the second (plan: exponential backoff, 3 attempts)."""
    return int(BACKOFF_BASE_SECONDS * BACKOFF_FACTOR ** max(0, attempts - 1))


class JobQueue:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def enqueue(
        self, kind: JobKind, payload: dict[str, Any], story_id: uuid.UUID | None = None
    ) -> JobRow:
        """Insert a job, or return the existing job with the same kind and payload (idempotent)."""
        digest = payload_hash(kind, payload)
        await self._session.execute(
            insert(JobRow)
            .values(
                id=uuid.uuid4(),
                kind=kind,
                story_id=story_id,
                status=JobStatus.QUEUED,
                attempts=0,
                payload=payload,
                payload_hash=digest,
            )
            .on_conflict_do_nothing(index_elements=["payload_hash"])
        )
        job = (await self._session.execute(select(JobRow).where(JobRow.payload_hash == digest))).scalar_one()
        return job

    async def get(self, job_id: uuid.UUID) -> JobRow | None:
        return await self._session.get(JobRow, job_id)

    async def latest_for_story(self, story_id: uuid.UUID) -> JobRow | None:
        result = await self._session.execute(
            select(JobRow).where(JobRow.story_id == story_id).order_by(JobRow.created_at.desc()).limit(1)
        )
        return result.scalar_one_or_none()

    async def claim_next(self) -> JobRow | None:
        """Atomically take the oldest due job, mark it running and count the attempt."""
        due = (
            select(JobRow.id)
            .where(JobRow.status == JobStatus.QUEUED, JobRow.run_after <= func.now())
            .order_by(JobRow.run_after)
            .limit(1)
            .with_for_update(skip_locked=True)
            .scalar_subquery()
        )
        result = await self._session.execute(
            update(JobRow)
            .where(JobRow.id == due)
            .values(status=JobStatus.RUNNING, locked_at=func.now(), attempts=JobRow.attempts + 1)
            .returning(JobRow)
        )
        return result.scalar_one_or_none()

    async def succeed(self, job: JobRow) -> None:
        job.status = JobStatus.SUCCEEDED
        job.locked_at = None
        job.last_error = None
        await self._session.flush()

    async def fail(self, job: JobRow, error: str, *, retryable: bool) -> bool:
        """Record a failure. Returns True if the job was requeued for another attempt."""
        job.last_error = error[:2000]
        job.locked_at = None
        if retryable and job.attempts < MAX_ATTEMPTS:
            job.status = JobStatus.QUEUED
            job.run_after = func.now() + timedelta(seconds=backoff_seconds(job.attempts))
            await self._session.flush()
            return True
        job.status = JobStatus.FAILED
        await self._session.flush()
        return False

    async def defer(self, job: JobRow, seconds: float, reason: str) -> None:
        """Requeue after `seconds` without using up an attempt (e.g. Groq quota exhausted)."""
        job.status = JobStatus.QUEUED
        job.attempts = max(0, job.attempts - 1)
        job.locked_at = None
        job.last_error = reason[:2000]
        job.run_after = func.now() + timedelta(seconds=max(1.0, seconds))
        await self._session.flush()

    async def requeue_stale(self, older_than_seconds: int) -> int:
        """Requeue jobs left RUNNING by a crashed worker. Returns how many were requeued."""
        result = await self._session.execute(
            update(JobRow)
            .where(
                JobRow.status == JobStatus.RUNNING,
                JobRow.locked_at < func.now() - timedelta(seconds=older_than_seconds),
            )
            .values(status=JobStatus.QUEUED, locked_at=None, run_after=func.now())
        )
        return int(getattr(result, "rowcount", 0) or 0)
