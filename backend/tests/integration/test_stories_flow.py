"""POST /v1/stories -> worker -> GET, against a real Postgres. The planner is stubbed (no Groq quota used)."""

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from story_samples import make_planned_story
from storyweaver.api.main import create_app
from storyweaver.config import get_settings
from storyweaver.jobs.queue import MAX_ATTEMPTS, JobQueue
from storyweaver.jobs.worker import Worker
from storyweaver.llm.client import ChatMessage, LLMResult, LLMUnavailableError
from storyweaver.llm.planner import StoryPlanner
from storyweaver.orchestrator.story_service import PlanStoryJob, make_story_failure_hook
from storyweaver.routing.router import QuotaExhaustedError
from storyweaver.storage.db import create_engine, create_session_factory
from storyweaver.storage.models import JobKind, JobRow, JobStatus

pytestmark = pytest.mark.integration

API_KEY = {"X-API-Key": "test-api-key"}  # pragma: allowlist secret
BODY = {"prompt": "a little dragon who is scared of the dark"}


class StubLLM:
    """Returns valid stories, or raises the queued exceptions first."""

    def __init__(self, *failures: Exception) -> None:
        self.failures = list(failures)
        self.calls = 0

    async def complete_json(self, *, model: str, messages: list[ChatMessage], **_: Any) -> LLMResult:
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        scenes = 6 if "Exactly 6 scenes" in messages[0].content else 5
        return LLMResult(json.dumps(make_planned_story(scenes)), model, 100, 900, 5.0, "stop")


@pytest.fixture
def client(database_url: str, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    # `session` is requested only for its cleanup (it truncates the tables after each test).
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    with TestClient(create_app()) as test_client:
        yield test_client


Sessions = async_sessionmaker[AsyncSession]


@pytest.fixture
async def db(database_url: str) -> AsyncIterator[Sessions]:
    """Sessions on the test's own event loop (the app's engine lives on TestClient's loop)."""
    engine = create_engine(database_url)
    yield create_session_factory(engine)
    await engine.dispose()


def _worker(sessions: Sessions, llm: StubLLM) -> Worker:
    planner = StoryPlanner(llm, planner_model="p", repair_model="r", max_tokens=100, temperature=0.0)
    return Worker(
        sessions,
        {JobKind.PLAN: PlanStoryJob(sessions, planner)},
        on_final_failure=make_story_failure_hook(sessions),
    )


async def _make_due(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions() as s, s.begin():
        await s.execute(update(JobRow).values(run_after=text("now()")))


def _create(client: TestClient, body: dict[str, Any] | None = None, key: str | None = None) -> Any:
    headers = {**API_KEY, **({"Idempotency-Key": key} if key else {})}
    return client.post("/v1/stories", json=body or BODY, headers=headers)


def test_create_returns_202_and_story_is_queued(client: TestClient) -> None:
    response = _create(client)

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert response.headers["Location"] == f"/v1/stories/{body['story_id']}"
    story = client.get(f"/v1/stories/{body['story_id']}", headers=API_KEY).json()
    assert story["status"] == "queued" and story["scenes"] == [] and story["num_scenes"] == 5
    job = client.get(f"/v1/jobs/{body['job_id']}", headers=API_KEY).json()
    assert job["kind"] == "plan" and job["status"] == "queued" and job["attempts"] == 0


def test_idempotency_replay_and_conflict(client: TestClient) -> None:
    first = _create(client, key="key-12345678")
    replay = _create(client, key="key-12345678")
    conflict = _create(client, {"prompt": "something else entirely"}, key="key-12345678")

    assert replay.status_code == 202 and replay.json() == first.json()
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert conflict.status_code == 422
    assert "Idempotency-Key" in conflict.json()["detail"]


@pytest.mark.parametrize(
    "body",
    [
        {"prompt": "  "},
        {"prompt": "x" * 501},
        {"prompt": "a dragon", "age_band": "7-9"},
        {"prompt": "a dragon", "num_scenes": 4},
        {"prompt": "a dragon", "extra": 1},
    ],
)
def test_invalid_requests_are_422(client: TestClient, body: dict[str, Any]) -> None:
    response = _create(client, body)

    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"


def test_unknown_ids_are_404_and_bad_ids_422(client: TestClient) -> None:
    assert client.get(f"/v1/stories/{uuid.uuid4()}", headers=API_KEY).status_code == 404
    assert client.get(f"/v1/jobs/{uuid.uuid4()}", headers=API_KEY).status_code == 404
    assert client.get("/v1/stories/not-a-uuid", headers=API_KEY).status_code == 422


async def test_worker_plans_story_end_to_end(client: TestClient, db: Sessions) -> None:
    created = _create(client, {"prompt": "a hedgehog learns to swim", "num_scenes": 6}).json()

    assert await _worker(db, StubLLM()).run_once() is True

    story = client.get(f"/v1/stories/{created['story_id']}", headers=API_KEY).json()
    assert story["status"] == "ready"
    assert story["title"] == "Pip and the Night Lights"
    assert [s["idx"] for s in story["scenes"]] == [1, 2, 3, 4, 5, 6]
    assert [c["id"] for c in story["characters"]] == ["pip", "glow"]
    assert story["scenes"][0]["image_url"] is None
    job = client.get(f"/v1/jobs/{created['job_id']}", headers=API_KEY).json()
    assert job["status"] == "succeeded" and job["attempts"] == 1


async def test_planning_failure_fails_story_without_retry(client: TestClient, db: Sessions) -> None:
    created = _create(client).json()
    bad = make_planned_story()
    bad["scenes"].pop()
    llm = StubLLM()
    llm.complete_json = _always(json.dumps(bad))  # type: ignore[method-assign]

    await _worker(db, llm).run_once()

    story = client.get(f"/v1/stories/{created['story_id']}", headers=API_KEY).json()
    assert story["status"] == "failed"
    assert story["error"]["code"] == "planning_failed"
    assert "exactly 5 scenes" in story["error"]["detail"]
    assert client.get(f"/v1/jobs/{created['job_id']}", headers=API_KEY).json()["status"] == "failed"


async def test_quota_exhausted_defers_without_using_an_attempt(client: TestClient, db: Sessions) -> None:
    created = _create(client).json()

    await _worker(db, StubLLM(QuotaExhaustedError(retry_after_seconds=42))).run_once()

    job = client.get(f"/v1/jobs/{created['job_id']}", headers=API_KEY).json()
    assert job["status"] == "queued" and job["attempts"] == 0
    assert client.get(f"/v1/stories/{created['story_id']}", headers=API_KEY).json()["status"] == "queued"
    sessions = db
    async with sessions() as s:
        assert await JobQueue(s).claim_next() is None  # not due for ~42 s


async def test_upstream_errors_retry_then_fail_story(client: TestClient, db: Sessions) -> None:
    created = _create(client).json()
    worker = _worker(db, StubLLM(*[LLMUnavailableError("down") for _ in range(MAX_ATTEMPTS)]))
    sessions = db

    for attempt in range(1, MAX_ATTEMPTS + 1):
        assert await worker.run_once() is True
        job = client.get(f"/v1/jobs/{created['job_id']}", headers=API_KEY).json()
        assert job["attempts"] == attempt
        await _make_due(sessions)

    assert job["status"] == "failed"
    story = client.get(f"/v1/stories/{created['story_id']}", headers=API_KEY).json()
    assert story["status"] == "failed" and story["error"]["code"] == "upstream_unavailable"


async def test_concurrent_claims_never_take_the_same_job(client: TestClient, db: Sessions) -> None:
    _create(client, {"prompt": "first story idea"})
    _create(client, {"prompt": "second story idea"})
    sessions = db

    async with sessions() as a, a.begin(), sessions() as b, b.begin():
        job_a = await JobQueue(a).claim_next()
        job_b = await JobQueue(b).claim_next()
        assert job_a is not None and job_b is not None
        assert job_a.id != job_b.id
        async with sessions() as c, c.begin():
            assert await JobQueue(c).claim_next() is None  # both rows locked


async def test_enqueue_is_idempotent_and_stale_jobs_are_requeued(client: TestClient, db: Sessions) -> None:
    created = _create(client).json()
    sessions = db

    async with sessions() as s, s.begin():
        again = await JobQueue(s).enqueue(
            JobKind.PLAN, {"story_id": created["story_id"]}, story_id=uuid.UUID(created["story_id"])
        )
        assert str(again.id) == created["job_id"]
        claimed = await JobQueue(s).claim_next()
        assert claimed is not None and claimed.status == JobStatus.RUNNING
    async with sessions() as s, s.begin():
        await s.execute(update(JobRow).values(locked_at=text("now() - interval '1 hour'")))
        assert await JobQueue(s).requeue_stale(older_than_seconds=600) == 1


def _always(content: str) -> Any:
    async def complete_json(*, model: str, **_: Any) -> LLMResult:
        return LLMResult(content, model, 1, 1, 1.0, "stop")

    return complete_json
