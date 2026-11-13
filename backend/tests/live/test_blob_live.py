"""Opt-in live test of the production object store (Backblaze B2, ADR-0004) through S3BlobStore.

Proves what production relies on: an upload works with a bucket-scoped key, the bucket is private, a
presigned URL serves the object with its content type, and delete works.

Run (reads BACKBLAZE_ID / BACKBLAZE_KEY from the environment or ../.env):
  BACKBLAZE_ENDPOINT=https://s3.us-east-005.backblazeb2.com BACKBLAZE_BUCKET=storyweaver-audio \
    uv run pytest -m live tests/live/test_blob_live.py -s
"""

import os
import re
import uuid
from pathlib import Path

import httpx
import pytest

from storyweaver.storage.blob import S3BlobStore, S3Config

pytestmark = pytest.mark.live

ENV_FILE = Path(__file__).resolve().parents[3] / ".env"


def _setting(name: str) -> str | None:
    value = os.getenv(name)
    if value or not ENV_FILE.exists():
        return value
    match = re.search(rf"^{name}=(.*)$", ENV_FILE.read_text(encoding="utf-8"), re.MULTILINE)
    return match.group(1).strip() if match else None


ENDPOINT = _setting("BACKBLAZE_ENDPOINT") or "https://s3.us-east-005.backblazeb2.com"
BUCKET = _setting("BACKBLAZE_BUCKET") or "storyweaver-audio"
KEY_ID, KEY = _setting("BACKBLAZE_ID"), _setting("BACKBLAZE_KEY")


@pytest.mark.skipif(not (KEY_ID and KEY), reason="BACKBLAZE_ID / BACKBLAZE_KEY not set")
async def test_b2_upload_presign_fetch_delete() -> None:
    pytest.importorskip("boto3")
    assert KEY_ID and KEY
    region = ENDPOINT.split("//", 1)[-1].split(".")[1]  # s3.<region>.backblazeb2.com
    store = S3BlobStore(
        S3Config(
            endpoint_url=ENDPOINT, bucket=BUCKET, access_key_id=KEY_ID, secret_access_key=KEY, region=region
        )
    )
    key = f"live-test/{uuid.uuid4()}.mp3"
    body = b"ID3" + os.urandom(1000)

    await store.put(key, body, "audio/mpeg")
    try:
        url = store.presigned_get_url(key, 120)
        async with httpx.AsyncClient(timeout=30) as http:
            signed = await http.get(url)
            unsigned = await http.get(f"{ENDPOINT}/{BUCKET}/{key}")
        print(f"\nB2: presigned GET {signed.status_code}, unsigned GET {unsigned.status_code}")
        assert signed.status_code == 200 and signed.content == body
        assert signed.headers["content-type"] == "audio/mpeg"
        assert unsigned.status_code in (401, 403)  # the bucket is private
    finally:
        await store.delete(key)
    async with httpx.AsyncClient(timeout=30) as http:
        assert (await http.get(store.presigned_get_url(key, 120))).status_code == 404


@pytest.mark.skipif(not (KEY_ID and KEY), reason="BACKBLAZE_ID / BACKBLAZE_KEY not set")
async def test_b2_delete_prefix_removes_a_whole_story() -> None:
    pytest.importorskip("boto3")
    assert KEY_ID and KEY
    region = ENDPOINT.split("//", 1)[-1].split(".")[1]
    store = S3BlobStore(
        S3Config(
            endpoint_url=ENDPOINT, bucket=BUCKET, access_key_id=KEY_ID, secret_access_key=KEY, region=region
        )
    )
    prefix = f"live-test/{uuid.uuid4()}/"
    keys = [f"{prefix}audio/scene-{i}.mp3" for i in (1, 2)]
    for key in keys:
        await store.put(key, b"ID3" + os.urandom(100), "audio/mpeg")

    assert await store.delete_prefix(prefix) == 2
    assert await store.delete_prefix(prefix) == 0
    async with httpx.AsyncClient(timeout=30) as http:
        for key in keys:
            assert (await http.get(store.presigned_get_url(key, 60))).status_code == 404
