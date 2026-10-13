import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit


def test_health_endpoints_need_no_api_key(client: TestClient) -> None:
    assert client.get("/health/live").json() == {"status": "ok"}
    assert client.get("/health/ready").json() == {"status": "ready"}


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"}])
def test_v1_routes_reject_missing_or_wrong_api_key(client: TestClient, headers: dict[str, str]) -> None:
    response = client.post("/v1/echo", json={"message": "hi"}, headers=headers)

    assert response.status_code == 401
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["detail"] == "Missing or invalid API key."


def test_echo_returns_message_and_request_id(client: TestClient, auth_headers: dict[str, str]) -> None:
    response = client.post("/v1/echo", json={"message": "hello"}, headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["message"] == "hello"
    assert body["request_id"] == response.headers["X-Request-ID"]


def test_echo_validation_error_is_problem_json(client: TestClient, auth_headers: dict[str, str]) -> None:
    response = client.post("/v1/echo", json={"message": ""}, headers=auth_headers)

    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["errors"][0]["loc"] == ["body", "message"]


def test_valid_incoming_request_id_is_propagated(client: TestClient) -> None:
    response = client.get("/health/live", headers={"X-Request-ID": "abc-123"})

    assert response.headers["X-Request-ID"] == "abc-123"


def test_docs_and_schema_hidden_in_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    from storyweaver.api.main import create_app
    from storyweaver.config import get_settings

    monkeypatch.setenv("APP_ENV", "prod")
    get_settings.cache_clear()

    with TestClient(create_app()) as prod_client:
        assert prod_client.get("/docs").status_code == 404
        assert prod_client.get("/openapi.json").status_code == 404
        assert prod_client.get("/health/live").status_code == 200


def test_malformed_incoming_request_id_is_replaced(client: TestClient) -> None:
    response = client.get("/health/live", headers={"X-Request-ID": "bad id\twith spaces"})

    assert response.headers["X-Request-ID"] != "bad id\twith spaces"
    assert len(response.headers["X-Request-ID"]) == 32
