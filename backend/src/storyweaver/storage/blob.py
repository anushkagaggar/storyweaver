"""Object storage for generated media (plan V2: narration MP3s; V3: images).

One `S3BlobStore` serves every environment: SeaweedFS in the local Docker stack and Backblaze B2 in
production (ADR-0004), both through the S3 API. Buckets are private; readers get short-lived presigned GET
URLs, so no media is public and nothing needs a CDN.

Only generated media is stored here. A child's recorded voice is never stored (plan §6.6).
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class BlobError(Exception):
    """The object store could not complete a request (outage, credentials, missing bucket)."""


def s3_error(exc: Exception) -> str:
    """`ClientError` alone cannot be diagnosed; S3 puts the useful part in the response.

    Returns something like `ClientError(NoSuchBucket, HTTP 404)`. The code and status say whether the
    bucket name, the key, the key's permissions or the region is wrong. Never includes credentials: only
    the error code and HTTP status are taken, never the request headers or the signature.
    """
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = response.get("Error", {}).get("Code")
        status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code:
            return f"{type(exc).__name__}({code}{f', HTTP {status}' if status else ''})"
    return type(exc).__name__


class BlobStore(Protocol):
    async def put(self, key: str, data: bytes, content_type: str) -> None: ...

    def presigned_get_url(self, key: str, expires_seconds: int) -> str:
        """A URL anyone can GET until it expires. Computed locally: no network call."""
        ...

    async def delete(self, key: str) -> None: ...

    async def delete_prefix(self, prefix: str) -> int:
        """Delete every object whose key starts with `prefix`. Returns how many were deleted."""
        ...


@dataclass(frozen=True)
class S3Config:
    endpoint_url: str
    bucket: str
    access_key_id: str
    secret_access_key: str = field(repr=False)
    region: str = "us-east-1"
    # The host browsers use. Locally the API reaches SeaweedFS as http://s3:8333 but browsers need
    # http://localhost:8333; a presigned URL is signed for one host, so it needs its own client.
    public_endpoint_url: str | None = None
    # Create the bucket on first use (local SeaweedFS only; production keys are scoped to one bucket).
    create_bucket: bool = False


class S3BlobStore:
    def __init__(self, config: S3Config) -> None:
        import boto3  # imported here so code paths without object storage don't need boto3
        from botocore.config import Config

        self._config = config
        botocore_config = Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},  # SeaweedFS needs path-style; B2 supports both
            retries={"max_attempts": 3, "mode": "standard"},
            # botocore >= 1.36 adds CRC checksums to every upload by default; not every S3-compatible
            # store accepts them, so send them only when an operation requires one.
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            connect_timeout=5,
            read_timeout=30,
        )
        credentials: dict[str, Any] = {
            "aws_access_key_id": config.access_key_id,
            "aws_secret_access_key": config.secret_access_key,
            "region_name": config.region,
            "config": botocore_config,
        }
        self._client = boto3.client("s3", endpoint_url=config.endpoint_url, **credentials)
        self._signer = (
            boto3.client("s3", endpoint_url=config.public_endpoint_url, **credentials)
            if config.public_endpoint_url and config.public_endpoint_url != config.endpoint_url
            else self._client
        )
        self._bucket_ready = not config.create_bucket
        self._bucket_lock = asyncio.Lock()

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        await self._ensure_bucket()
        try:
            await asyncio.to_thread(
                self._client.put_object,
                Bucket=self._config.bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
            )
        except Exception as exc:
            raise BlobError(f"Upload of {key} failed: {s3_error(exc)}") from exc

    def presigned_get_url(self, key: str, expires_seconds: int) -> str:
        url: str = self._signer.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._config.bucket, "Key": key},
            ExpiresIn=expires_seconds,
        )
        return url

    async def delete(self, key: str) -> None:
        try:
            await asyncio.to_thread(self._client.delete_object, Bucket=self._config.bucket, Key=key)
        except Exception as exc:
            raise BlobError(f"Delete of {key} failed: {s3_error(exc)}") from exc

    async def delete_prefix(self, prefix: str) -> int:
        # One DeleteObject per key, not DeleteObjects: a story has a handful of files, and the batch call
        # needs a checksum header that not every S3-compatible store accepts.
        if not prefix.endswith("/"):
            raise ValueError("delete_prefix needs a prefix ending in '/'.")
        try:
            keys = await asyncio.to_thread(self._list_keys, prefix)
            for key in keys:
                await asyncio.to_thread(self._client.delete_object, Bucket=self._config.bucket, Key=key)
        except BlobError:
            raise
        except Exception as exc:
            raise BlobError(f"Delete of {prefix}* failed: {s3_error(exc)}") from exc
        return len(keys)

    def _list_keys(self, prefix: str) -> list[str]:
        if not self._bucket_ready:
            from botocore.exceptions import ClientError

            try:
                self._client.head_bucket(Bucket=self._config.bucket)
            except ClientError:
                return []  # local bucket not created yet: nothing to delete
        keys: list[str] = []
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._config.bucket, Prefix=prefix):
            keys.extend(item["Key"] for item in page.get("Contents", []))
        return keys

    async def _ensure_bucket(self) -> None:
        if self._bucket_ready:
            return
        async with self._bucket_lock:
            if self._bucket_ready:
                return
            from botocore.exceptions import ClientError

            try:
                await asyncio.to_thread(self._client.head_bucket, Bucket=self._config.bucket)
            except ClientError:
                logger.info("Creating bucket %s.", self._config.bucket)
                try:
                    await asyncio.to_thread(self._client.create_bucket, Bucket=self._config.bucket)
                except Exception as exc:
                    raise BlobError(f"Could not create bucket: {s3_error(exc)}") from exc
            except Exception as exc:
                raise BlobError(f"Object storage unreachable: {s3_error(exc)}") from exc
            self._bucket_ready = True


class InMemoryBlobStore:
    """Single-process stand-in for tests and for running without object storage."""

    def __init__(self, base_url: str = "memory://blobs") -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
        self._base_url = base_url.rstrip("/")

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        self.objects[key] = (data, content_type)

    def presigned_get_url(self, key: str, expires_seconds: int) -> str:
        return f"{self._base_url}/{key}?expires={expires_seconds}"

    async def delete(self, key: str) -> None:
        self.objects.pop(key, None)

    async def delete_prefix(self, prefix: str) -> int:
        if not prefix.endswith("/"):
            raise ValueError("delete_prefix needs a prefix ending in '/'.")
        keys = [key for key in self.objects if key.startswith(prefix)]
        for key in keys:
            del self.objects[key]
        return len(keys)


def story_prefix(story_id: object) -> str:
    """Every object of a story lives under this prefix, so deleting a story deletes them all."""
    return f"stories/{story_id}/"


def scene_audio_key(story_id: object, idx: int) -> str:
    return f"{story_prefix(story_id)}audio/scene-{idx}.mp3"
