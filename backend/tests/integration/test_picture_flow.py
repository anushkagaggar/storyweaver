"""POST /v1/pictures -> one image job -> a one-page "picture" on a real Postgres (V3 speech -> picture).

The input guard, the generator and the storage are fakes; the worker, queue and API are real.
"""

import dataclasses
import hashlib
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from story_samples import make_planned_story
from storyweaver.api.main import create_app
from storyweaver.config import get_settings
from storyweaver.imaging.params import GenerationParams
from storyweaver.jobs.worker import Worker
from storyweaver.llm.planner import PlannedStory
from storyweaver.orchestrator.illustration_jobs import IllustrateSceneJob, ImageSettings, LocalIllustrator
from storyweaver.orchestrator.story_service import BORROWED_SEED_OFFSET, PICTURE_CHARACTER, StoryService
from storyweaver.routing.limits import VisitorLimiter
from storyweaver.routing.store import InMemoryQuotaStore
from storyweaver.safety.guards import BRANDS, SafetyVerdict
from storyweaver.storage.blob import InMemoryBlobStore, reference_image_key
from storyweaver.storage.db import create_engine, create_session_factory
from storyweaver.storage.models import ImageStatus, JobKind, StoryRow
from storyweaver.storage.repositories import StoryRepository
from user_tokens import ALICE, BOB, USER_TOKEN_SECRET, as_user

pytestmark = pytest.mark.integration

API_KEY = {"X-API-Key": "test-api-key"}  # pragma: allowlist secret
Sessions = async_sessionmaker[AsyncSession]
SETTINGS = ImageSettings(
    model="m", model_revision="r", width=512, height=512, sampler="s", steps=1, guidance=1.0
)
PNG_BYTES = b"\x89PNG\r\n\x1a\nfake"
DRAGON = {"prompt": "Can you draw me a purple dragon, please?"}


class StubGuard:
    def __init__(self) -> None:
        self.verdict = SafetyVerdict(allowed=True)
        self.checked: list[str] = []

    async def check(self, text: str) -> SafetyVerdict:
        self.checked.append(text)
        return self.verdict


class FakeGenerator:
    name = "fake"

    def __init__(self) -> None:
        self.calls: list[GenerationParams] = []
        self.references: list[bytes | None] = []

    async def generate(self, params: GenerationParams, reference: bytes | None = None) -> bytes:
        self.calls.append(params)
        self.references.append(reference)
        return PNG_BYTES


@pytest.fixture
def guard() -> StubGuard:
    return StubGuard()


@pytest.fixture
def blobs() -> InMemoryBlobStore:
    return InMemoryBlobStore()


@pytest.fixture
def client(
    database_url: str,
    session: AsyncSession,
    guard: StubGuard,
    blobs: InMemoryBlobStore,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    # `session` is requested only for its cleanup (it truncates the tables after each test).
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("USER_TOKEN_SECRET", USER_TOKEN_SECRET)
    get_settings.cache_clear()
    with TestClient(create_app()) as test_client:
        services = test_client.app.state.services  # type: ignore[attr-defined]
        services.stories = StoryService(services.sessions, input_guard=guard, pictures_enabled=True)  # type: ignore[arg-type]
        services.blobs = blobs
        services.stories.blobs = blobs
        yield test_client


@pytest.fixture
async def db(database_url: str) -> AsyncIterator[Sessions]:
    engine = create_engine(database_url)
    yield create_session_factory(engine)
    await engine.dispose()


def _worker(db: Sessions, blobs: InMemoryBlobStore, generator: FakeGenerator) -> Worker:
    job = IllustrateSceneJob(db, blobs, SETTINGS, LocalIllustrator(generator, blobs))
    return Worker(db, {JobKind.IMAGE: job})


def _draw(client: TestClient, body: dict[str, str] = DRAGON, sub: str = ALICE, **headers: str) -> Any:
    return client.post("/v1/pictures", json=body, headers={**API_KEY, **as_user(sub, **headers)})


async def _story_count(db: Sessions) -> int:
    async with db() as s:
        return int((await s.execute(select(func.count()).select_from(StoryRow))).scalar_one())


async def test_a_spoken_request_becomes_one_illustrated_page(
    client: TestClient, db: Sessions, blobs: InMemoryBlobStore, guard: StubGuard
) -> None:
    created = _draw(client)
    assert created.status_code == 202
    story_id = created.json()["story_id"]
    assert created.headers["Location"] == f"/v1/stories/{story_id}"
    assert guard.checked == ["a purple dragon"]  # the request words are gone before the safety check

    before = client.get(f"/v1/stories/{story_id}", headers={**API_KEY, **as_user()}).json()
    assert before["kind"] == "picture" and before["status"] == "ready"
    assert before["title"] == "A purple dragon" and before["num_scenes"] == 1
    assert [s["image_status"] for s in before["scenes"]] == ["pending"]

    generator = FakeGenerator()
    assert await _worker(db, blobs, generator).run_once() is True

    after = client.get(f"/v1/stories/{story_id}", headers={**API_KEY, **as_user()}).json()
    (page,) = after["scenes"]
    assert page["text"] == "A purple dragon." and page["image_status"] == "ready" and page["image_url"]
    assert page["audio_status"] == "none"  # pictures are not narrated
    (params,) = generator.calls
    assert params.prompt.endswith("a purple dragon")
    assert after["characters"][0]["id"] == PICTURE_CHARACTER

    listed = client.get("/v1/stories", headers={**API_KEY, **as_user()}).json()["stories"]
    assert [(s["id"], s["kind"], s["title"]) for s in listed] == [(story_id, "picture", "A purple dragon")]


async def test_replaying_the_request_returns_the_same_picture(client: TestClient, db: Sessions) -> None:
    first = _draw(client, **{"Idempotency-Key": "draw-0001"})
    again = _draw(client, **{"Idempotency-Key": "draw-0001"})
    assert again.status_code == 202 and again.headers["Idempotent-Replayed"] == "true"
    assert again.json()["story_id"] == first.json()["story_id"]
    other = _draw(client, {"prompt": "draw a red bus"}, **{"Idempotency-Key": "draw-0001"})
    assert other.status_code == 422  # same key, different request
    story = client.post(
        "/v1/stories",
        json={"prompt": "a purple dragon"},
        headers={**API_KEY, **as_user(), "Idempotency-Key": "draw-0001"},
    )
    assert story.status_code == 422  # a picture's key can't be reused for a story
    assert await _story_count(db) == 1


async def test_unsafe_and_unclear_requests_store_nothing(
    client: TestClient, db: Sessions, guard: StubGuard
) -> None:
    unclear = _draw(client, {"prompt": "draw me please"})
    assert unclear.status_code == 422 and unclear.json()["code"] == "nothing_to_draw"

    guard.verdict = SafetyVerdict(False, "policy", "violence", "A weapon.")
    unsafe = _draw(client, {"prompt": "draw a monster with a knife"})
    assert unsafe.status_code == 422 and unsafe.json()["code"] == "unsafe_input"

    guard.verdict = SafetyVerdict(True, adaptation=BRANDS)
    brand = _draw(client, {"prompt": "draw a famous cartoon mouse"})
    assert brand.status_code == 422 and brand.json()["category"] == "picture_brands"
    assert "draw characters from films" in brand.json()["detail"]

    assert await _story_count(db) == 0


def test_pictures_have_their_own_limit(client: TestClient, guard: StubGuard) -> None:
    services = client.app.state.services  # type: ignore[attr-defined]
    pictures = VisitorLimiter(InMemoryQuotaStore(), per_hour=2, per_day=30, secret="s", namespace="pictures")
    services.stories = StoryService(
        services.sessions,
        input_guard=guard,  # type: ignore[arg-type]
        picture_limiter=pictures,
        pictures_enabled=True,
    )
    assert _draw(client).status_code == 202
    assert _draw(client, {"prompt": "draw a red bus"}).status_code == 202
    limited = _draw(client, {"prompt": "draw a green frog"})
    assert limited.status_code == 429 and limited.json()["code"] == "visitor_limit"
    assert "pictures" in limited.json()["detail"]
    assert _draw(client, sub=BOB).status_code == 202  # another parent still can


def test_only_the_owner_can_see_a_picture(client: TestClient) -> None:
    story_id = _draw(client).json()["story_id"]
    assert client.get(f"/v1/stories/{story_id}", headers={**API_KEY, **as_user(BOB)}).status_code == 404
    assert client.get(f"/v1/stories/{story_id}", headers={**API_KEY, **as_user(ALICE)}).status_code == 200


def test_without_pictures_on_this_server_the_request_is_refused(client: TestClient) -> None:
    client.app.state.services.stories.pictures_enabled = False  # type: ignore[attr-defined]
    refused = _draw(client)
    assert refused.status_code == 503 and refused.json()["code"] == "pictures_unavailable"
    assert int(refused.headers["Retry-After"]) > 0


def test_the_request_needs_the_api_key_and_a_prompt(client: TestClient) -> None:
    assert client.post("/v1/pictures", json=DRAGON).status_code == 401
    assert client.post("/v1/pictures", json={}, headers=API_KEY).status_code == 422
    assert client.post("/v1/pictures", json={"prompt": "a" * 301}, headers=API_KEY).status_code == 422
    assert uuid.UUID(client.post("/v1/pictures", json=DRAGON, headers=API_KEY).json()["story_id"])


# --- character_ref: a character from one of the parent's own stories (MCP illustrate) ---

PAGE_ONE = b"\x89PNG\r\n\x1a\npage-one-of-pip"


async def _ready_story(db: Sessions, blobs: InMemoryBlobStore, owner: str = ALICE) -> uuid.UUID:
    """A planned, READY story whose page 1 has a real picture (main character: pip)."""
    plan = PlannedStory.model_validate(make_planned_story(5)).to_story(age_band="4-6", story_key="k")
    async with db() as s, s.begin():
        repo = StoryRepository(s)
        row = await repo.create(prompt_text="a dragon", age_band="4-6", num_scenes=5, owner_id=owner)
        await repo.save_plan(row, plan)
        story_id = row.id
    async with db() as s, s.begin():
        scene = await StoryRepository(s).get_scene(story_id, 1)
        assert scene is not None
        scene.image_key = f"stories/{story_id}/images/page-one.png"
        scene.image_status = ImageStatus.READY
        await blobs.put(scene.image_key, PAGE_ONE, "image/png")
    return story_id


async def test_a_character_from_a_story_keeps_its_look_seed_and_page_one_reference(
    client: TestClient, db: Sessions, blobs: InMemoryBlobStore, guard: StubGuard
) -> None:
    source = await _ready_story(db, blobs)
    created = _draw(client, {"prompt": "draw Pip at the beach", "story_id": str(source)})
    assert created.status_code == 202
    picture_id = uuid.UUID(created.json()["story_id"])
    assert guard.checked == ["Pip at the beach"]

    async with db() as s:
        source_row = await StoryRepository(s).get(source)
        picture_row = await StoryRepository(s).get(picture_id)
    assert source_row is not None and picture_row is not None
    pip = next(c for c in source_row.characters if c.char_key == "pip")
    (drawn,) = picture_row.characters
    assert (drawn.visual_desc, drawn.name) == (pip.visual_desc, "Pip")
    assert drawn.seed == pip.seed + BORROWED_SEED_OFFSET  # not page 1's seed: a new layout
    assert drawn.ref_image_key == reference_image_key(picture_id)  # copied under the picture's own prefix
    assert (await blobs.get(drawn.ref_image_key)) == PAGE_ONE

    generator = FakeGenerator()
    settings = dataclasses.replace(SETTINGS, reference_scale=0.4, reference_model="ip-adapter")
    job = IllustrateSceneJob(db, blobs, settings, LocalIllustrator(generator, blobs))
    assert await Worker(db, {JobKind.IMAGE: job}).run_once() is True
    (params,) = generator.calls
    assert generator.references == [PAGE_ONE]
    assert params.reference_sha256 == hashlib.sha256(PAGE_ONE).hexdigest()
    assert params.seed == pip.seed + BORROWED_SEED_OFFSET + 1
    assert pip.visual_desc in params.prompt and params.prompt.endswith("Pip at the beach")


async def test_another_character_gets_no_reference_from_a_page_it_isnt_on(
    client: TestClient, db: Sessions, blobs: InMemoryBlobStore
) -> None:
    source = await _ready_story(db, blobs)
    created = _draw(client, {"prompt": "draw Glow", "story_id": str(source), "character_id": "glow"})
    assert created.status_code == 202
    async with db() as s:
        picture_row = await StoryRepository(s).get(uuid.UUID(created.json()["story_id"]))
    assert picture_row is not None
    (drawn,) = picture_row.characters
    assert drawn.visual_desc == "tiny firefly with a soft golden glow" and drawn.ref_image_key is None


async def test_characters_are_only_drawn_from_your_own_ready_stories(
    client: TestClient, db: Sessions, blobs: InMemoryBlobStore
) -> None:
    source = await _ready_story(db, blobs, owner=BOB)

    def code(body: dict[str, str]) -> tuple[int, str | None]:
        response = _draw(client, {"prompt": "draw Pip", **body})
        return response.status_code, response.json().get("code")

    assert code({"story_id": str(source)}) == (404, "character_not_found")  # Bob's story, asked by Alice
    assert code({"story_id": str(uuid.uuid4())}) == (404, "character_not_found")
    assert code({"character_id": "pip"}) == (404, "character_not_found")  # no story_id
    mine = await _ready_story(db, blobs)
    assert code({"story_id": str(mine), "character_id": "nobody"}) == (404, "character_not_found")
    assert await _story_count(db) == 2  # nothing was made
