import pytest
from fastapi.testclient import TestClient

from storyweaver.api.routers.speech import KNOWN_VOICES

pytestmark = pytest.mark.unit

URL = "/v1/speech/voices"


def test_lists_every_voice_for_both_engines(client: TestClient, auth_headers: dict[str, str]) -> None:
    response = client.get(URL, headers=auth_headers)

    assert response.status_code == 200
    engines = response.json()["engines"]
    assert set(engines) == {"orpheus", "kokoro"}
    assert engines["orpheus"]["narrator"] == "hannah"
    assert engines["kokoro"]["narrator"] == "af_heart"
    listed = sorted(v["id"] for engine in engines.values() for v in engine["voices"])
    assert listed == KNOWN_VOICES  # exactly the voices /narrate accepts
    for engine in engines.values():
        assert engine["narrator"] in {v["id"] for v in engine["voices"]}
        assert all(v["label"] and v["accent"] in {"en-us", "en-gb"} for v in engine["voices"])


def test_needs_the_api_key(client: TestClient) -> None:
    assert client.get(URL).status_code == 401
