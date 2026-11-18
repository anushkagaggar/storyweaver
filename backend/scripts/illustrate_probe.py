"""Try the picture route on a Space: its self-call, then real pictures through `illustrate` (plan V3 step 4).

Meant for a temporary *test* Space without a database (so nothing touches real stories):
1. GET /v1/gpu-route-check: the Space calls its own `gpu_check` through its public URL with its HF_TOKEN,
   exactly as its worker would call `illustrate`. Proves ZeroGPU accepts the self-call.
2. Page 1, then page 2 with page 1 as the IP-Adapter reference, drawn by the Space's IMAGE_ENGINE through
   `illustrate` from here (charged to HF_TOKEN from .env, else ZeroGPU's anonymous quota).
3. Both pictures are downloaded from the bucket into <out dir> to judge by eye, then deleted from it.

    cd backend
    PYTHONPATH=src uv run --frozen --no-sync python scripts/illustrate_probe.py https://<test>.hf.space <out>

Reads from ../.env: PROBE_API_KEY (the test Space's STORYWEAVER_API_KEY; falls back to STORYWEAVER_API_KEY),
HF_TOKEN (optional) and BACKBLAZE_ID / BACKBLAZE_KEY / BACKBLAZE_ENDPOINT / BACKBLAZE_BUCKET (to fetch the
pictures and clean up). Prompts and seeds are fixed, so runs are comparable.

--local (after the out dir) dry-runs it against the Docker stack instead: SD 1.5 settings, SeaweedFS on :8333,
and the api started with IMAGE_GRADIO_URL=http://localhost:7860.
"""

import asyncio
import dataclasses
import hashlib
import sys
import time
import uuid
from pathlib import Path

import boto3
import httpx
from dotenv import dotenv_values

from storyweaver.config import SD15_DEFAULTS, SDXL_DEFAULTS
from storyweaver.imaging.params import GenerationParams, cache_key, styled_prompt
from storyweaver.imaging.remote import GradioIllustrator
from storyweaver.imaging.sd15 import IP_ADAPTER_MODEL
from storyweaver.imaging.sdxl import SDXL_IP_ADAPTER_MODEL
from storyweaver.storage.blob import image_key, story_prefix

FOX = "a small orange fox wearing a green hat"
PAGES = [
    f"{FOX}, standing in a sunny meadow full of daisies",
    f"{FOX}, helping a little brown owl beside a sparkling stream",
]


def page_params(idx: int, reference: bytes | None, local: bool) -> GenerationParams:
    defaults = SD15_DEFAULTS if local else {**SDXL_DEFAULTS, "image_width": 1152, "image_height": 896}
    params = GenerationParams(
        prompt=styled_prompt(PAGES[idx - 1]),
        seed=4242 + idx,
        model=str(defaults["image_model"]),
        model_revision=str(defaults["image_model_revision"]),
        width=int(defaults["image_width"]),
        height=int(defaults["image_height"]),
        sampler="dpmpp_2m_karras",
        steps=25,
        guidance=6.0,
    )
    if reference is None:
        return params
    return dataclasses.replace(
        params,
        reference_sha256=hashlib.sha256(reference).hexdigest(),
        reference_scale=0.4,
        reference_model=IP_ADAPTER_MODEL if local else SDXL_IP_ADAPTER_MODEL,
    )


async def probe(space: str, out: Path, env: dict[str, str | None], local: bool) -> int:
    api_key = env.get("PROBE_API_KEY") or env.get("STORYWEAVER_API_KEY") or ""
    hf_token = env.get("HF_TOKEN") or None
    space = space.rstrip("/")
    print(f"HF token: {'yes' if hf_token else 'no (anonymous ZeroGPU quota)'}")

    started = time.monotonic()
    headers = {"X-API-Key": api_key}
    if hf_token:  # a private Space's proxy answers 404 to anyone without a token that can read it
        headers["Authorization"] = f"Bearer {hf_token}"
    async with httpx.AsyncClient(timeout=200) as client:
        check = await client.get(f"{space}/v1/gpu-route-check", headers=headers)
    print(f"1. self-call: HTTP {check.status_code} in {time.monotonic() - started:.1f} s: {check.text[:400]}")

    prefix = "S3_" if local else "BACKBLAZE_"
    s3 = boto3.client(
        "s3",
        endpoint_url="http://localhost:8333" if local else env.get("BACKBLAZE_ENDPOINT"),
        region_name="us-east-1" if local else None,
        aws_access_key_id=env.get(f"{prefix}ACCESS_KEY_ID" if local else "BACKBLAZE_ID"),
        aws_secret_access_key=env.get(f"{prefix}SECRET_ACCESS_KEY" if local else "BACKBLAZE_KEY"),
    )
    bucket = "storyweaver-audio" if local else env.get("BACKBLAZE_BUCKET")
    story = uuid.uuid4()
    engine = "sd15" if local else "sdxl"
    illustrator = GradioIllustrator(space, api_key, engine=engine, hf_token=hf_token, timeout_seconds=300)
    reference: bytes | None = None
    failures = 0
    try:
        for idx in (1, 2):
            params = page_params(idx, reference, local)
            key = image_key(story, cache_key(params))
            started = time.monotonic()
            try:
                await illustrator.illustrate(story, params, key, reference)
            except Exception as exc:
                failures += 1
                took = time.monotonic() - started
                print(f"2. page {idx}: FAILED after {took:.1f} s: {type(exc).__name__}: {exc}")
                break
            took, path = time.monotonic() - started, out / f"page{idx}.png"
            png = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
            await asyncio.to_thread(path.write_bytes, png)
            print(f"2. page {idx}: drawn and stored in {took:.1f} s -> {path}")
            reference = png
    finally:
        listed = s3.list_objects_v2(Bucket=bucket, Prefix=story_prefix(story)).get("Contents", [])
        for item in listed:
            s3.delete_object(Bucket=bucket, Key=item["Key"])
        print(f"3. removed {len(listed)} probe object(s) from the bucket")
    self_call_ok = check.status_code == 200 and check.json().get("ok") is True
    return 0 if self_call_ok and not failures else 1


def main() -> int:
    env = dotenv_values(Path(__file__).resolve().parents[2] / ".env")
    out = Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    return asyncio.run(probe(sys.argv[1], out, env, "--local" in sys.argv[3:]))


if __name__ == "__main__":
    sys.exit(main())
