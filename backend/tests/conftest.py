from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from storyweaver.api.main import create_app
from storyweaver.config import get_settings

TEST_API_KEY = "test-api-key"


@pytest.fixture(autouse=True)
def _test_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("STORYWEAVER_API_KEY", TEST_API_KEY)
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    monkeypatch.setenv("LLM_PLANNER", "groq:openai/gpt-oss-20b")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"X-API-Key": TEST_API_KEY}
