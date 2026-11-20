"""An idle worker must leave the database alone (so Neon can suspend), yet still pick up work promptly."""

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from sqlalchemy import func, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from storyweaver.jobs.queue import JobQueue
from storyweaver.jobs.worker import JobContext, Worker
from storyweaver.storage.db import create_engine, create_session_factory
from storyweaver.storage.models import JobKind, JobRow

pytestmark = pytest.mark.integration

Sessions = async_sessionmaker[AsyncSession]


@pytest.fixture
async def db(database_url: str) -> AsyncIterator[Sessions]:
    engine = create_engine(database_url)
    yield create_session_factory(engine)
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE stories, jobs CASCADE"))
    await engine.dispose()


async def _enqueue(db: Sessions, kind: JobKind, payload: dict[str, object], delay: float = 0.0) -> None:
    async with db() as s, s.begin():
        job = await JobQueue(s).enqueue(kind, payload)
        if delay:
            due = func.now() + timedelta(seconds=delay)
            await s.execute(update(JobRow).where(JobRow.id == job.id).values(run_after=due))


async def _until(condition: asyncio.Event, seconds: float = 5.0) -> None:
    await asyncio.wait_for(condition.wait(), timeout=seconds)


class Recorder:
    def __init__(self) -> None:
        self.done = asyncio.Event()
        self.kinds: list[JobKind] = []

    async def __call__(self, context: JobContext) -> None:
        self.kinds.append(context.kind)
        self.done.set()


def _count_looks(worker: Worker) -> list[int]:
    looks = [0]
    original = worker.run_once

    async def counted(kinds: frozenset[JobKind] | None = None) -> bool:
        looks[0] += 1
        return await original(kinds)

    worker.run_once = counted  # type: ignore[method-assign]
    return looks


async def test_an_idle_worker_stops_querying(db: Sessions) -> None:
    worker = Worker(db, {JobKind.PLAN: Recorder(), JobKind.TTS: Recorder()}, poll_interval=0.01)
    looks = _count_looks(worker)
    stop = asyncio.Event()
    run = asyncio.create_task(worker.run_forever(stop))
    try:
        await asyncio.sleep(0.5)  # with the old 1 s polling at 0.01 s this would be ~100 looks
        assert looks[0] == 2  # one look per lane, then asleep
    finally:
        stop.set()
        await asyncio.wait_for(run, timeout=5)


async def test_wake_picks_up_a_new_job(db: Sessions) -> None:
    plan = Recorder()
    worker = Worker(db, {JobKind.PLAN: plan}, poll_interval=0.01)
    stop = asyncio.Event()
    run = asyncio.create_task(worker.run_forever(stop))
    try:
        await asyncio.sleep(0.2)  # asleep with nothing queued
        await _enqueue(db, JobKind.PLAN, {"n": 1})
        worker.wake()
        await _until(plan.done, 2)
    finally:
        stop.set()
        await asyncio.wait_for(run, timeout=5)


async def test_a_deferred_job_runs_when_due_without_a_wake(db: Sessions) -> None:
    plan = Recorder()
    await _enqueue(db, JobKind.PLAN, {"n": 2}, delay=0.5)
    worker = Worker(db, {JobKind.PLAN: plan}, poll_interval=0.01)
    stop = asyncio.Event()
    run = asyncio.create_task(worker.run_forever(stop))
    try:
        await asyncio.sleep(0.2)
        assert not plan.done.is_set()  # not due yet
        await _until(plan.done, 3)
    finally:
        stop.set()
        await asyncio.wait_for(run, timeout=5)


async def test_a_finished_job_wakes_the_other_lanes(db: Sessions) -> None:
    """A plan job queues narration; the sleeping tts lane must notice without an outside wake()."""
    tts = Recorder()

    async def plan(_: JobContext) -> None:
        await _enqueue(db, JobKind.TTS, {"idx": 1})

    worker = Worker(db, {JobKind.PLAN: plan, JobKind.TTS: tts}, poll_interval=0.01)
    stop = asyncio.Event()
    run = asyncio.create_task(worker.run_forever(stop))
    try:
        await asyncio.sleep(0.2)
        await _enqueue(db, JobKind.PLAN, {"n": 3})
        worker.wake()
        await _until(tts.done, 3)
    finally:
        stop.set()
        await asyncio.wait_for(run, timeout=5)
