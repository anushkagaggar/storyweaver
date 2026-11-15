"""Plan -> per-scene image jobs -> image URLs on a real Postgres; generators and storage are fakes."""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from story_samples import make_planned_story
from storyweaver.api.main import create_app
from storyweaver.config import get_settings
from storyweaver.imaging.generator import ImageQuotaExhaustedError, ImageUnavailableError
from storyweaver.imaging.params import GenerationParams, cache_key
from storyweaver.jobs.queue import MAX_ATTEMPTS
from storyweaver.jobs.worker import JobContext, Worker
from storyweaver.llm.client import LLMResult
from storyweaver.llm.planner import StoryPlanner
from storyweaver.orchestrator.illustration_jobs import (
    IllustrateSceneJob,
    ImageSettings,
    LocalIllustrator,
)
from storyweaver.orchestrator.story_service import PlanStoryJob, StoryService, make_story_failure_hook
from storyweaver.routing.limits import GpuBudget
from storyweaver.routing.store import InMemoryQuotaStore
from storyweaver.safety.guards import SafetyVerdict
from storyweaver.storage.blob import InMemoryBlobStore, image_key
from storyweaver.storage.db import create_engine, create_session_factory
from storyweaver.storage.models import JobKind, JobRow, JobStatus

pytestmark = pytest.mark.integration

API_KEY = {"X-API-Key": "test-api-key"}  # pragma: allowlist secret
Sessions = async_sessionmaker[AsyncSession]
SETTINGS = ImageSettings(
    model="m", model_revision="r", width=512, height=512, sampler="s", steps=1, guidance=1.0
)
PNG_BYTES = b"\x89PNG\r\n\x1a\nfake"


class StubLLM:
    async def complete_json(self, **_: Any) -> LLMResult:
        return LLMResult(json.dumps(make_planned_story(5)), "p", 100, 900, 5.0, "stop")


class AllowAll:
    async def check(self, _: Any) -> SafetyVerdict:
        return SafetyVerdict(allowed=True)


class FakeGenerator:
    name = "fake"

    def __init__(self, errors: list[Exception] | None = None) -> None:
        self.errors = list(errors or [])
        self.calls: list[GenerationParams] = []

    async def generate(self, params: GenerationParams) -> bytes:
        self.calls.append(params)
        if self.errors:
            raise self.errors.pop(0)
        return PNG_BYTES


@pytest.fixture
def blobs() -> InMemoryBlobStore:
    return InMemoryBlobStore()


@pytest.fixture
def client(
    database_url: str, session: AsyncSession, blobs: InMemoryBlobStore, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    with TestClient(create_app()) as test_client:
        services = test_client.app.state.services  # type: ignore[attr-defined]
        services.stories = StoryService(services.sessions, input_guard=AllowAll())  # type: ignore[arg-type]
        services.blobs = blobs
        yield test_client


@pytest.fixture
async def db(database_url: str) -> AsyncIterator[Sessions]:
    engine = create_engine(database_url)
    yield create_session_factory(engine)
    await engine.dispose()


def _worker(sessions: Sessions, blobs: InMemoryBlobStore, generator: FakeGenerator | None) -> Worker:
    planner = StoryPlanner(
        StubLLM(),  # type: ignore[arg-type]
        planner_model="p",
        repair_model="r",
        max_tokens=100,
        temperature=0.0,
    )
    illustrator = LocalIllustrator(generator, blobs) if generator is not None else None
    return Worker(
        sessions,
        {
            JobKind.PLAN: PlanStoryJob(sessions, planner, AllowAll(), illustrate=True),  # type: ignore[arg-type]
            JobKind.IMAGE: IllustrateSceneJob(sessions, blobs, SETTINGS, illustrator),
        },
        on_final_failure=make_story_failure_hook(sessions),
    )


def _story(client: TestClient, story_id: str) -> Any:
    return client.get(f"/v1/stories/{story_id}", headers=API_KEY).json()


async def _plan(client: TestClient, worker: Worker) -> str:
    created = client.post("/v1/stories", json={"prompt": "a little dragon"}, headers=API_KEY).json()
    assert await worker.run_once(frozenset({JobKind.PLAN})) is True
    return str(created["story_id"])


async def _image_jobs(sessions: Sessions) -> list[JobRow]:
    async with sessions() as s:
        rows = await s.execute(select(JobRow).where(JobRow.kind == JobKind.IMAGE).order_by(JobRow.run_after))
        return list(rows.scalars())


async def _make_due(sessions: Sessions) -> None:
    due = text("now() - interval '1 hour' + (payload->>'idx')::int * interval '1 millisecond'")
    async with sessions() as s, s.begin():
        await s.execute(update(JobRow).where(JobRow.kind == JobKind.IMAGE).values(run_after=due))


async def test_each_scene_gets_an_image_scene_one_first(
    client: TestClient, db: Sessions, blobs: InMemoryBlobStore
) -> None:
    generator = FakeGenerator()
    worker = _worker(db, blobs, generator)
    story_id = await _plan(client, worker)

    story = _story(client, story_id)
    assert [s["image_status"] for s in story["scenes"]] == ["pending"] * 5
    assert [job.payload["idx"] for job in await _image_jobs(db)] == [1, 2, 3, 4, 5]

    while await worker.run_once():
        pass

    story = _story(client, story_id)
    assert [s["image_status"] for s in story["scenes"]] == ["ready"] * 5
    key = image_key(story_id, cache_key(generator.calls[0]))
    assert story["scenes"][0]["image_url"].startswith(f"memory://blobs/{key}?expires=")
    assert blobs.objects[key] == (PNG_BYTES, "image/png")
    # Scene 1 is centred on its first declared character: that character's seed keeps it recognisable.
    assert generator.calls[0].seed != 0 and generator.calls[0].prompt


async def test_a_retry_reuses_an_image_a_crashed_attempt_already_stored(
    client: TestClient, db: Sessions, blobs: InMemoryBlobStore
) -> None:
    generator = FakeGenerator()
    worker = _worker(db, blobs, generator)
    story_id = await _plan(client, worker)
    job = (await _image_jobs(db))[0]

    # First attempt: the image is made and stored, then the worker "crashes" before recording it.
    handler = IllustrateSceneJob(db, blobs, SETTINGS, LocalIllustrator(generator, blobs))
    failing = _CrashBeforeSave(handler)
    context = JobContext(job.id, uuid.UUID(story_id), 1, False, JobKind.IMAGE, dict(job.payload))
    with pytest.raises(RuntimeError):
        await failing(context)
    assert len(generator.calls) == 1 and _story(client, story_id)["scenes"][0]["image_status"] == "pending"

    # The retry finds the stored image and records it without generating again.
    await handler(context)
    assert len(generator.calls) == 1
    assert _story(client, story_id)["scenes"][0]["image_status"] == "ready"


class _CrashBeforeSave:
    def __init__(self, handler: IllustrateSceneJob) -> None:
        self._handler = handler

    async def __call__(self, context: JobContext) -> None:
        async def crash(*_: Any) -> bool:
            raise RuntimeError("worker died")

        self._handler._save = crash  # type: ignore[method-assign]
        try:
            await self._handler(context)
        finally:
            del self._handler._save


async def test_no_gpu_budget_uses_the_placeholder_at_once(
    client: TestClient, db: Sessions, blobs: InMemoryBlobStore
) -> None:
    generator = FakeGenerator(errors=[ImageQuotaExhaustedError(3600, "ZeroGPU quota used up")])
    worker = _worker(db, blobs, generator)
    story_id = await _plan(client, worker)

    assert await worker.run_once(frozenset({JobKind.IMAGE})) is True

    scene = _story(client, story_id)["scenes"][0]
    assert scene["image_status"] == "placeholder"
    assert f"stories/{story_id}/images/placeholder-1.png" in scene["image_url"]
    assert (await _image_jobs(db))[0].status == JobStatus.SUCCEEDED  # no retries spent on a dead quota
    # The placeholder is not stored under the real image's key, so a later request can still make it.
    assert image_key(story_id, cache_key(generator.calls[0])) not in blobs.objects


async def test_when_the_daily_gpu_budget_is_spent_pages_get_the_placeholder(
    client: TestClient, db: Sessions, blobs: InMemoryBlobStore
) -> None:
    generator = FakeGenerator()
    worker = _worker(db, blobs, generator)
    budget = GpuBudget(InMemoryQuotaStore(), seconds_per_day=20)
    worker._handlers[JobKind.IMAGE] = IllustrateSceneJob(  # type: ignore[attr-defined]
        db, blobs, SETTINGS, LocalIllustrator(generator, blobs), gpu_budget=budget, gpu_seconds_per_image=12
    )
    await budget.reserve(15)  # earlier stories used 15 of today's 20 GPU-seconds: one more image won't fit
    story_id = await _plan(client, worker)
    while await worker.run_once():
        pass

    assert generator.calls == []  # no GPU call was made
    assert {s["image_status"] for s in _story(client, story_id)["scenes"]} == {"placeholder"}


async def test_a_flaky_gpu_is_retried_then_the_placeholder_stands_in(
    client: TestClient, db: Sessions, blobs: InMemoryBlobStore
) -> None:
    generator = FakeGenerator(errors=[ImageUnavailableError("GPU busy")] * MAX_ATTEMPTS)
    worker = _worker(db, blobs, generator)
    story_id = await _plan(client, worker)

    for _ in range(MAX_ATTEMPTS):
        await _make_due(db)
        assert await worker.run_once(frozenset({JobKind.IMAGE})) is True

    assert len(generator.calls) == MAX_ATTEMPTS
    assert _story(client, story_id)["scenes"][0]["image_status"] == "placeholder"


async def test_placeholder_engine_never_calls_a_generator(
    client: TestClient, db: Sessions, blobs: InMemoryBlobStore
) -> None:
    worker = _worker(db, blobs, generator=None)  # IMAGE_ENGINE=placeholder
    story_id = await _plan(client, worker)
    while await worker.run_once():
        pass

    scenes = _story(client, story_id)["scenes"]
    assert [s["image_status"] for s in scenes] == ["placeholder"] * 5
    data, content_type = blobs.objects[f"stories/{story_id}/images/placeholder-1.png"]
    assert content_type == "image/png" and data.startswith(b"\x89PNG")


async def test_a_sleeping_job_in_one_lane_does_not_block_another(db: Sessions, client: TestClient) -> None:
    """The point of lanes: a narration job waiting out Orpheus's pacing must not hold up planning."""
    started, release = asyncio.Event(), asyncio.Event()
    planned: list[uuid.UUID] = []

    async def slow_tts(_: JobContext) -> None:
        started.set()
        await release.wait()

    async def plan(context: JobContext) -> None:
        assert context.story_id is not None
        planned.append(context.story_id)

    worker = Worker(db, {JobKind.TTS: slow_tts, JobKind.PLAN: plan}, poll_interval=0.05)
    created = client.post("/v1/stories", json={"prompt": "a little dragon"}, headers=API_KEY).json()
    async with db() as s, s.begin():
        from storyweaver.jobs.queue import JobQueue

        await JobQueue(s).enqueue(JobKind.TTS, {"story_id": created["story_id"], "idx": 1})

    stop = asyncio.Event()
    run = asyncio.create_task(worker.run_forever(stop))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        for _ in range(100):  # the plan lane finishes while the tts lane is still asleep
            if planned:
                break
            await asyncio.sleep(0.05)
        assert planned == [uuid.UUID(created["story_id"])]
        assert not release.is_set()
    finally:
        release.set()
        stop.set()
        await asyncio.wait_for(run, timeout=5)
