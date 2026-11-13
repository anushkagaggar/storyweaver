"""Plan -> per-scene tts jobs -> audio URLs on a real Postgres; TTS engines and object storage are fakes."""

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
from storyweaver.jobs.queue import MAX_ATTEMPTS
from storyweaver.jobs.worker import Worker
from storyweaver.llm.planner import StoryPlanner
from storyweaver.orchestrator.narration import Narrator
from storyweaver.orchestrator.narration_jobs import NarrateSceneJob, StoryNarration
from storyweaver.orchestrator.story_service import PlanStoryJob, StoryService, make_story_failure_hook
from storyweaver.speech.tts import TTSRateLimitedError, TTSUnavailableError
from storyweaver.speech.voices import KOKORO_VOICES, ORPHEUS_VOICES
from storyweaver.storage.blob import InMemoryBlobStore
from storyweaver.storage.db import create_engine, create_session_factory
from storyweaver.storage.models import CharacterRow, JobKind, JobRow, JobStatus
from tts_fakes import FakeTTS, make_router

pytestmark = pytest.mark.integration

API_KEY = {"X-API-Key": "test-api-key"}  # pragma: allowlist secret
Sessions = async_sessionmaker[AsyncSession]


class StubLLM:
    async def complete_json(self, **_: Any) -> Any:
        import json

        from storyweaver.llm.client import LLMResult

        return LLMResult(json.dumps(make_planned_story(5)), "p", 100, 900, 5.0, "stop")


class AllowAll:
    async def check(self, _: Any) -> Any:
        from storyweaver.safety.guards import SafetyVerdict

        return SafetyVerdict(allowed=True)


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


def _worker(sessions: Sessions, narrator: Narrator, blobs: InMemoryBlobStore) -> Worker:
    planner = StoryPlanner(StubLLM(), planner_model="p", repair_model="r", max_tokens=100, temperature=0.0)  # type: ignore[arg-type]
    narration = StoryNarration(narrator)
    return Worker(
        sessions,
        {
            JobKind.PLAN: PlanStoryJob(sessions, planner, AllowAll(), narration),  # type: ignore[arg-type]
            JobKind.TTS: NarrateSceneJob(sessions, narrator, blobs),
        },
        on_final_failure=make_story_failure_hook(sessions),
    )


def _story(client: TestClient, story_id: str) -> Any:
    return client.get(f"/v1/stories/{story_id}", headers=API_KEY).json()


async def _plan(client: TestClient, worker: Worker) -> str:
    created = client.post("/v1/stories", json={"prompt": "a little dragon"}, headers=API_KEY).json()
    assert await worker.run_once() is True  # the plan job
    return str(created["story_id"])


async def _tts_jobs(sessions: Sessions) -> list[JobRow]:
    async with sessions() as s:
        rows = await s.execute(select(JobRow).where(JobRow.kind == JobKind.TTS).order_by(JobRow.run_after))
        return list(rows.scalars())


async def _make_due(sessions: Sessions) -> None:
    """Skip the retry backoff, keeping the scenes in order."""
    due = text("now() - interval '1 hour' + (payload->>'idx')::int * interval '1 millisecond'")
    async with sessions() as s, s.begin():
        await s.execute(update(JobRow).where(JobRow.kind == JobKind.TTS).values(run_after=due))


async def test_story_is_ready_at_once_and_each_scene_gets_audio(
    client: TestClient, db: Sessions, blobs: InMemoryBlobStore
) -> None:
    orpheus, kokoro = FakeTTS("orpheus"), FakeTTS("kokoro")
    tts, _ = make_router(orpheus, kokoro)
    worker = _worker(db, Narrator(tts), blobs)

    story_id = await _plan(client, worker)

    story = _story(client, story_id)
    assert story["status"] == "ready" and story["tts_engine"] == "orpheus"
    assert [s["audio_status"] for s in story["scenes"]] == ["pending"] * 5
    assert all(s["audio_url"] is None for s in story["scenes"])
    jobs = await _tts_jobs(db)
    assert [job.payload["idx"] for job in jobs] == [1, 2, 3, 4, 5]  # scene 1 first
    async with db() as s:
        voices = {c.char_key: c.voice_id for c in (await s.execute(select(CharacterRow))).scalars()}
    orpheus_ids = {v.id for v in ORPHEUS_VOICES}
    assert set(voices) == {"pip", "glow"} and set(voices.values()) <= orpheus_ids
    assert len(set(voices.values())) == 2 and "hannah" not in voices.values()  # hannah is the narrator

    while await worker.run_once():
        pass

    story = _story(client, story_id)
    assert [s["audio_status"] for s in story["scenes"]] == ["ready"] * 5
    first = story["scenes"][0]["audio_url"]
    assert first.startswith(f"memory://blobs/stories/{story_id}/audio/scene-1.mp3?expires=")
    data, content_type = blobs.objects[f"stories/{story_id}/audio/scene-1.mp3"]
    assert content_type == "audio/mpeg" and len(data) > 1000
    assert kokoro.calls == []
    assert {voice for _, voice in orpheus.calls} == {"hannah", voices["glow"]}  # pip never speaks


async def test_without_orpheus_quota_the_whole_story_uses_kokoro(
    client: TestClient, db: Sessions, blobs: InMemoryBlobStore
) -> None:
    orpheus, kokoro = FakeTTS("orpheus"), FakeTTS("kokoro")
    tts, _ = make_router(orpheus, kokoro)
    assert await tts.choose(["x" * 3200]) == "orpheus"  # uses up the day's budget (3,240 characters)
    worker = _worker(db, Narrator(tts), blobs)

    story_id = await _plan(client, worker)
    while await worker.run_once():
        pass

    story = _story(client, story_id)
    assert story["tts_engine"] == "kokoro"
    assert [s["audio_status"] for s in story["scenes"]] == ["ready"] * 5
    assert orpheus.calls == [] and kokoro.calls


async def test_orpheus_outage_retries_then_falls_back_to_kokoro(
    client: TestClient, db: Sessions, blobs: InMemoryBlobStore
) -> None:
    orpheus = FakeTTS("orpheus", errors=[TTSUnavailableError("down")] * MAX_ATTEMPTS)
    kokoro = FakeTTS("kokoro")
    tts, _ = make_router(orpheus, kokoro)
    worker = _worker(db, Narrator(tts), blobs)
    story_id = await _plan(client, worker)

    for _ in range(MAX_ATTEMPTS):
        await _make_due(db)
        assert await worker.run_once() is True  # scene 1, attempts 1..3

    jobs = await _tts_jobs(db)
    assert jobs[0].status == JobStatus.SUCCEEDED and jobs[0].attempts == MAX_ATTEMPTS
    assert kokoro.calls and {voice for _, voice in kokoro.calls} <= {v.id for v in KOKORO_VOICES}
    assert _story(client, story_id)["scenes"][0]["audio_status"] == "ready"


async def test_short_rate_limit_defers_and_long_one_switches_to_kokoro(
    client: TestClient, db: Sessions, blobs: InMemoryBlobStore
) -> None:
    orpheus = FakeTTS(
        "orpheus",
        errors=[
            TTSRateLimitedError(retry_after_seconds=30),
            TTSRateLimitedError(retry_after_seconds=3600),
        ],
    )
    kokoro = FakeTTS("kokoro")
    tts, _ = make_router(orpheus, kokoro)
    worker = _worker(db, Narrator(tts), blobs)
    story_id = await _plan(client, worker)

    assert await worker.run_once() is True
    first = (await _tts_jobs(db))[0]
    assert first.status == JobStatus.QUEUED and first.attempts == 0  # deferred, no attempt used

    await _make_due(db)
    assert await worker.run_once() is True
    assert (await _tts_jobs(db))[0].status == JobStatus.SUCCEEDED
    assert kokoro.calls
    assert _story(client, story_id)["scenes"][0]["audio_status"] == "ready"


async def test_scene_audio_failure_keeps_the_story_readable(
    client: TestClient, db: Sessions, blobs: InMemoryBlobStore
) -> None:
    kokoro = FakeTTS("kokoro", errors=[TTSUnavailableError("broken")] * MAX_ATTEMPTS)
    tts, _ = make_router(None, kokoro)
    worker = _worker(db, Narrator(tts), blobs)
    story_id = await _plan(client, worker)

    for _ in range(MAX_ATTEMPTS):
        await _make_due(db)
        await worker.run_once()

    story = _story(client, story_id)
    assert story["status"] == "ready" and story["error"] is None
    assert story["scenes"][0]["audio_status"] == "failed"
    assert story["scenes"][1]["audio_status"] == "pending"


async def test_story_without_narration_reports_none(client: TestClient, db: Sessions) -> None:
    planner = StoryPlanner(StubLLM(), planner_model="p", repair_model="r", max_tokens=100, temperature=0.0)  # type: ignore[arg-type]
    worker = Worker(db, {JobKind.PLAN: PlanStoryJob(db, planner, AllowAll())})  # type: ignore[arg-type]
    story_id = await _plan(client, worker)

    story = _story(client, story_id)
    assert story["status"] == "ready" and story["tts_engine"] is None
    assert {s["audio_status"] for s in story["scenes"]} == {"none"}
    assert await _tts_jobs(db) == []
    assert uuid.UUID(story_id)
