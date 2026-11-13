"""BlobStore adapters. S3 presigning is computed locally, so these tests need no object store."""

from urllib.parse import parse_qs, urlparse

import pytest

from storyweaver.config import Settings
from storyweaver.services import build_blob_store
from storyweaver.storage.blob import InMemoryBlobStore, S3BlobStore, S3Config, scene_audio_key

pytestmark = pytest.mark.unit

CONFIG = S3Config(
    endpoint_url="http://s3:8333",
    public_endpoint_url="http://localhost:8333",
    bucket="storyweaver-audio",
    access_key_id="test-access",
    secret_access_key="test-secret",  # pragma: allowlist secret
    region="us-east-005",
)


def test_scene_audio_key_is_stable() -> None:
    assert scene_audio_key("abc", 3) == "stories/abc/audio/scene-3.mp3"


async def test_in_memory_store_round_trip() -> None:
    store = InMemoryBlobStore()
    await store.put("a/b.mp3", b"ID3", "audio/mpeg")
    assert store.objects["a/b.mp3"] == (b"ID3", "audio/mpeg")
    assert store.presigned_get_url("a/b.mp3", 60) == "memory://blobs/a/b.mp3?expires=60"
    await store.delete("a/b.mp3")
    await store.delete("a/b.mp3")  # deleting a missing key is not an error
    assert store.objects == {}


def test_presigned_url_uses_the_public_host_path_style_and_sigv4() -> None:
    pytest.importorskip("boto3")
    url = S3BlobStore(CONFIG).presigned_get_url("stories/x/audio/scene-1.mp3", 900)

    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    assert f"{parsed.scheme}://{parsed.netloc}" == "http://localhost:8333"
    assert parsed.path == "/storyweaver-audio/stories/x/audio/scene-1.mp3"
    assert query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
    assert query["X-Amz-Expires"] == ["900"]
    assert query["X-Amz-Credential"][0].startswith("test-access/")
    assert "/us-east-005/s3/aws4_request" in query["X-Amz-Credential"][0]
    assert "test-secret" not in url


def test_presigned_url_defaults_to_the_api_endpoint() -> None:
    pytest.importorskip("boto3")
    config = S3Config(
        endpoint_url="https://s3.us-east-005.backblazeb2.com",
        bucket="storyweaver-audio",
        access_key_id="k",
        secret_access_key="s",  # pragma: allowlist secret
        region="us-east-005",
    )
    url = S3BlobStore(config).presigned_get_url("k.mp3", 60)
    assert url.startswith("https://s3.us-east-005.backblazeb2.com/storyweaver-audio/k.mp3?")


def test_secret_is_not_in_the_config_repr() -> None:
    assert "test-secret" not in repr(CONFIG)


@pytest.mark.parametrize(
    ("env", "enabled"),
    [
        ({}, False),
        ({"S3_ENDPOINT_URL": "http://s3:8333"}, False),
        ({"S3_ENDPOINT_URL": "http://s3:8333", "S3_ACCESS_KEY_ID": "a", "S3_SECRET_ACCESS_KEY": ""}, False),
        ({"S3_ENDPOINT_URL": "http://s3:8333", "S3_ACCESS_KEY_ID": "a", "S3_SECRET_ACCESS_KEY": "b"}, True),
    ],
)
def test_blob_storage_needs_endpoint_and_both_keys(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], enabled: bool
) -> None:
    for name in ("S3_ENDPOINT_URL", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(name, env.get(name, ""))
    settings = Settings()  # type: ignore[call-arg]
    assert settings.blob_storage_enabled is enabled
    if not enabled:
        assert build_blob_store(settings) is None
