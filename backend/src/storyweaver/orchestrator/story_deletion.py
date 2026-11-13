"""Deleting stories: on request (DELETE /v1/stories/{id}) and automatically after STORY_RETENTION_DAYS.

Privacy (docs/dpia.md risk 4): a story's idea text, pages and narration shouldn't outlive their use.

Order matters, so a failure never leaves files nobody can find:
1. Lock the story row and mark it DELETING. Reads treat it as gone from here on.
2. Delete every object under `stories/{id}/` in object storage.
3. Delete the rows (characters, scenes and jobs cascade).

A failure in step 2 leaves the story DELETING; deleting it again, or the next sweep, finishes the job.

Narration jobs save their audio key under the same row lock and only while the story is READY, removing their
own upload otherwise (narration_jobs.py). So whichever of the two runs first, no audio file is left behind.

Stories still being made (queued, planning, checking) can't be deleted: the plan job is still writing them.
"""

import asyncio
import logging
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from storyweaver.storage.blob import BlobError, BlobStore, story_prefix
from storyweaver.storage.models import DELETABLE_STATUSES, StoryStatus
from storyweaver.storage.repositories import StoryRepository

logger = logging.getLogger(__name__)

# A DELETING story older than this was interrupted (e.g. object storage was down); the sweep finishes it.
STUCK_DELETION_AFTER = timedelta(minutes=5)
SWEEP_BATCH = 50


@dataclass
class StoryInProgressError(Exception):
    """The story is still being made, so it can't be deleted yet."""

    status: str


class StoryDeletion:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], blobs: BlobStore | None) -> None:
        self._sessions = sessions
        self._blobs = blobs

    async def delete(self, story_id: uuid.UUID) -> bool:
        """Delete a story and its files. False if there was no such story.

        Raises StoryInProgressError for a story still being made, and BlobError when object storage fails (the
        story then stays DELETING and a retry finishes it).
        """
        async with self._sessions() as session, session.begin():
            repo = StoryRepository(session)
            row = await repo.lock(story_id)
            if row is None:
                return False
            if row.status not in DELETABLE_STATUSES:
                raise StoryInProgressError(row.status)
            row.status = StoryStatus.DELETING

        files = await self._blobs.delete_prefix(story_prefix(story_id)) if self._blobs is not None else 0

        async with self._sessions() as session, session.begin():
            await StoryRepository(session).delete_row(story_id)
        logger.info("Story %s deleted (%d file(s)).", story_id, files)
        return True

    async def expire(self, retention: timedelta, *, now: datetime | None = None) -> int:
        """Delete up to SWEEP_BATCH expired stories and interrupted deletions. Returns how many."""
        current = now or datetime.now(UTC)
        async with self._sessions() as session:
            ids = await StoryRepository(session).expired_ids(
                created_before=current - retention,
                stuck_before=current - STUCK_DELETION_AFTER,
                limit=SWEEP_BATCH,
            )
        deleted = 0
        for story_id in ids:
            try:
                deleted += await self.delete(story_id)
            except (BlobError, StoryInProgressError) as exc:
                logger.warning("Expiry could not delete story %s yet: %s", story_id, exc)
        return deleted


class RetentionSweeper:
    """Runs StoryDeletion.expire every `interval_seconds` in the API process (beside the job worker)."""

    def __init__(self, deletion: StoryDeletion, *, retention: timedelta, interval_seconds: float) -> None:
        self._deletion = deletion
        self._retention = retention
        self._interval = interval_seconds

    async def run_forever(self, stop: asyncio.Event) -> None:
        logger.info("Story expiry on: stories are deleted %d day(s) after creation.", self._retention.days)
        while not stop.is_set():
            try:
                while (count := await self._deletion.expire(self._retention)) >= SWEEP_BATCH:
                    logger.info("Expired %d stories; more to do.", count)
                    if stop.is_set():
                        return
                if count:
                    logger.info("Expired %d stories.", count)
            except Exception:
                logger.exception("Story expiry sweep failed; will try again.")
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._interval)
