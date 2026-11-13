"""DELETE /v1/stories/{id} and the expiry sweep, on a real Postgres with in-memory object storage."""

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from story_samples import make_planned_story
from storyweaver.api.main import create_app
from storyweaver.config import get_settings
from storyweaver.jobs.worker import Worker
from storyweaver.llm.client import LLMResult
from storyweaver.llm.planner import StoryPlanner
from storyweaver.orchestrator.narration import Narrator
from storyweaver.orchestrator.narration_jobs import NarrateSceneJob, StoryNarration
from storyweaver.orchestrator.story_deletion import StoryDeletion
from storyweaver.orchestrator.story_service import PlanStoryJob, StoryService, make_story_failure_hook
from storyweaver.safety.guards import SafetyVerdict
from storyweaver.speech.tts import Speech
from storyweaver.storage.blob import BlobError, InMemoryBlobStore
from storyweaver.storage.db import create_engine, create_session_factory
from storyweaver.storage.models import CharacterRow, JobKind, JobRow, SceneRow, StoryRow, StoryStatus
from tts_fakes import FakeTTS, make_router

pytestmark = pytest.mark.integration

API_KEY = {"X-API-Key": "test-api-key"}  # pragma: allowlist secret
Sessions = async_sessionmaker[AsyncSession]


class StubLLM:
    async def complete_json(self, **_: Any) -> LLMResult:
        return LLMResult(json.dumps(make_planned_story(5)), "p", 100, 900, 5.0, "stop")


class AllowAll:
    async def check(self, _: Any) -> SafetyVerdict:
        return SafetyVerdict(allowed=True)


class FlakyBlobs(InMemoryBlobStore):
    """In-memory storage whose prefix deletes fail while `broken` is set."""

    broken = False

    async def delete_prefix(self, prefix: str) -> int:
        if self.broken:
            raise BlobError("storage down")
        return await super().delete_prefix(prefix)


@pytest.fixture
def blobs() -> FlakyBlobs:
    return FlakyBlobs()


@pytest.fixture
def client(
    database_url: str, session: AsyncSession, blobs: FlakyBlobs, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    with TestClient(create_app()) as test_client:
        services = test_client.app.state.services  # type: ignore[attr-defined]
        services.stories = StoryService(services.sessions, input_guard=AllowAll())  # type: ignore[arg-type]
        services.blobs = blobs
        services.deletion = StoryDeletion(services.sessions, blobs)
        yield test_client


@pytest.fixture
async def db(database_url: str) -> AsyncIterator[Sessions]:
    engine = create_engine(database_url)
    yield create_session_factory(engine)
    await engine.dispose()


def _worker(sessions: Sessions, blobs: InMemoryBlobStore, kokoro: FakeTTS | None = None) -> Worker:
    tts, _ = make_router(None, kokoro or FakeTTS("kokoro"))
    narrator = Narrator(tts)
    planner = StoryPlanner(StubLLM(), planner_model="p", repair_model="r", max_tokens=100, temperature=0.0)  # type: ignore[arg-type]
    return Worker(
        sessions,
        {
            JobKind.PLAN: PlanStoryJob(sessions, planner, AllowAll(), StoryNarration(narrator)),  # type: ignore[arg-type]
            JobKind.TTS: NarrateSceneJob(sessions, narrator, blobs),
        },
        on_final_failure=make_story_failure_hook(sessions),
    )


def _create(client: TestClient) -> str:
    response = client.post("/v1/stories", json={"prompt": "a little dragon"}, headers=API_KEY)
    return str(response.json()["story_id"])


async def _counts(sessions: Sessions) -> dict[str, int]:
    async with sessions() as s:
        return {
            model.__tablename__: int((await s.execute(select(func.count()).select_from(model))).scalar_one())
            for model in (StoryRow, CharacterRow, SceneRow, JobRow)
        }


async def test_delete_removes_rows_audio_and_jobs(
    client: TestClient, db: Sessions, blobs: FlakyBlobs
) -> None:
    worker = _worker(db, blobs)
    story_id = _create(client)
    while await worker.run_once():
        pass
    other_id = _create(client)  # an unrelated story must survive
    assert len([k for k in blobs.objects if story_id in k]) == 5

    response = client.delete(f"/v1/stories/{story_id}", headers=API_KEY)

    assert response.status_code == 204
    assert not [k for k in blobs.objects if story_id in k]
    assert client.get(f"/v1/stories/{story_id}", headers=API_KEY).status_code == 404
    assert client.delete(f"/v1/stories/{story_id}", headers=API_KEY).status_code == 404
    assert await _counts(db) == {"stories": 1, "characters": 0, "scenes": 0, "jobs": 1}  # other's plan job
    assert client.get(f"/v1/stories/{other_id}", headers=API_KEY).status_code == 200


def test_delete_requires_the_api_key_and_a_uuid(client: TestClient) -> None:
    assert client.delete(f"/v1/stories/{uuid.uuid4()}").status_code == 401
    assert client.delete("/v1/stories/not-a-uuid", headers=API_KEY).status_code == 422
    assert client.delete(f"/v1/stories/{uuid.uuid4()}", headers=API_KEY).status_code == 404


def test_a_story_still_being_made_cannot_be_deleted(client: TestClient) -> None:
    story_id = _create(client)  # queued: its plan job hasn't run

    response = client.delete(f"/v1/stories/{story_id}", headers=API_KEY)

    assert response.status_code == 409
    assert response.json()["code"] == "story_in_progress"
    assert response.headers["Retry-After"] == "30"
    assert client.get(f"/v1/stories/{story_id}", headers=API_KEY).json()["status"] == "queued"


async def test_storage_failure_hides_the_story_and_a_retry_finishes(
    client: TestClient, db: Sessions, blobs: FlakyBlobs
) -> None:
    worker = _worker(db, blobs)
    story_id = _create(client)
    while await worker.run_once():
        pass
    blobs.broken = True

    failed = client.delete(f"/v1/stories/{story_id}", headers=API_KEY)

    assert failed.status_code == 503 and failed.json()["code"] == "delete_incomplete"
    assert client.get(f"/v1/stories/{story_id}", headers=API_KEY).status_code == 404  # already hidden
    async with db() as s:
        assert (await s.get(StoryRow, uuid.UUID(story_id))).status == StoryStatus.DELETING  # type: ignore[union-attr]
    blobs.broken = False
    assert client.delete(f"/v1/stories/{story_id}", headers=API_KEY).status_code == 204
    assert not [k for k in blobs.objects if story_id in k]
    assert (await _counts(db))["stories"] == 0


async def test_deleting_while_a_scene_is_narrated_leaves_no_audio_behind(
    client: TestClient, db: Sessions, blobs: FlakyBlobs
) -> None:
    deletion = StoryDeletion(db, blobs)
    story_id = _create(client)

    class DeletesMidway(FakeTTS):
        """Deletes the story while scene 1 is being spoken: the upload happens after the deletion."""

        async def synthesize(self, text: str, voice: str) -> Speech:
            if not self.calls:
                assert await deletion.delete(uuid.UUID(story_id)) is True
            return await super().synthesize(text, voice)

    worker = _worker(db, blobs, DeletesMidway("kokoro"))
    assert await worker.run_once() is True  # plan job: story ready, 5 tts jobs queued
    assert await worker.run_once() is True  # scene 1: deleted midway, audio discarded

    assert blobs.objects == {}
    assert await _counts(db) == {"stories": 0, "characters": 0, "scenes": 0, "jobs": 0}
    assert await worker.run_once() is False  # the other scenes' jobs went with the story


async def test_expiry_deletes_only_old_finished_stories_and_stuck_deletions(
    client: TestClient, db: Sessions, blobs: FlakyBlobs
) -> None:
    worker = _worker(db, blobs)
    old_ready, recent_ready, old_queued, stuck = (_create(client) for _ in range(4))
    for _ in range(4):
        while await worker.run_once():
            pass
    long_ago = datetime.now(UTC) - timedelta(days=31)
    async with db() as s, s.begin():
        await s.execute(
            update(StoryRow)
            .where(StoryRow.id.in_([uuid.UUID(old_ready), uuid.UUID(old_queued)]))
            .values(created_at=long_ago, updated_at=long_ago)
        )
        await s.execute(
            update(StoryRow)
            .where(StoryRow.id == uuid.UUID(old_queued))
            .values(status=StoryStatus.QUEUED, updated_at=long_ago)
        )
        await s.execute(  # a deletion interrupted 10 minutes ago, on a recent story
            update(StoryRow)
            .where(StoryRow.id == uuid.UUID(stuck))
            .values(status=StoryStatus.DELETING, updated_at=text("now() - interval '10 minutes'"))
        )

    deleted = await StoryDeletion(db, blobs).expire(timedelta(days=30))

    assert deleted == 2
    async with db() as s:
        remaining = {str(i) for i in (await s.execute(select(StoryRow.id))).scalars()}
    assert remaining == {recent_ready, old_queued}
    assert not [k for k in blobs.objects if old_ready in k or stuck in k]
    assert len([k for k in blobs.objects if recent_ready in k]) == 5
