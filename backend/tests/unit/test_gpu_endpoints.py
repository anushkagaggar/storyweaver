from types import SimpleNamespace
from typing import Any, cast

import gradio as gr
import pytest
from fastapi.testclient import TestClient

from storyweaver.api import gpu_endpoints
from storyweaver.api.deps import api_key_is_valid
from storyweaver.api.main import create_app
from storyweaver.config import get_settings

pytestmark = pytest.mark.unit

TEST_API_KEY = "test-api-key"  # pragma: allowlist secret  (set by the autouse fixture in conftest.py)


def _request(headers: dict[str, str]) -> gr.Request:
    return cast(gr.Request, SimpleNamespace(headers=headers))


@pytest.mark.parametrize(
    ("provided", "expected"), [(TEST_API_KEY, True), ("wrong", False), ("", False), (None, False)]
)
def test_api_key_is_valid(provided: str | None, expected: bool) -> None:
    assert api_key_is_valid(provided, get_settings()) is expected


@pytest.mark.parametrize("headers", [{}, {"x-api-key": "wrong"}])
def test_gpu_check_rejects_missing_or_wrong_key(headers: dict[str, str]) -> None:
    with pytest.raises(gr.Error):
        gpu_endpoints.gpu_check(_request(headers))


def test_gpu_check_runs_gpu_function_with_valid_key(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[bool] = []

    def fake_matmul() -> dict[str, Any]:
        calls.append(True)
        return {"ok": True, "device": "fake"}

    monkeypatch.setattr(gpu_endpoints, "run_gpu_matmul", fake_matmul)

    result = gpu_endpoints.gpu_check(_request({"x-api-key": TEST_API_KEY}))

    assert result == {"ok": True, "device": "fake"}
    assert calls == [True]


def test_app_is_gradio_server_without_rest_gpu_route(client: TestClient) -> None:
    assert isinstance(create_app(), gr.Server)
    assert client.get("/v1/gpu-check", headers={"X-API-Key": TEST_API_KEY}).status_code == 404
