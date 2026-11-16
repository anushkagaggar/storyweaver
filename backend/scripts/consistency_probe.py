"""Compare IP-Adapter settings for character consistency on one stored story (needs the GPU to itself).

Re-draws later pages from the story's own page-1 picture with several (adapter, strength, seed) choices
and writes one PNG per page and choice, to judge by eye: the character should stay recognisable while each
page still shows its own scene.

    docker compose -f infra/docker-compose.yml --env-file .env stop api
    docker compose -f infra/docker-compose.yml --env-file .env run --rm --no-deps \
        -v "D:/lenovo/work/storyweaver/backend/scripts:/app/scripts:ro" -v "<out dir>:/out" \
        api python scripts/consistency_probe.py <story id> /out
"""

import asyncio
import io
import sys
import time
import uuid

import torch
from diffusers import DPMSolverMultistepScheduler, StableDiffusionPipeline
from PIL import Image

from storyweaver.config import get_settings
from storyweaver.imaging.params import NEGATIVE_PROMPT
from storyweaver.imaging.sd15 import IP_ADAPTER_REPO, IP_ADAPTER_REVISION, download_weights
from storyweaver.orchestrator.illustration_jobs import main_character, scene_prompt
from storyweaver.services import build_blob_store
from storyweaver.storage.db import create_engine, create_session_factory
from storyweaver.storage.repositories import StoryRepository

PAGES = (2, 3, 4, 5)
# (label, adapter weights, strength, vary the seed per page)
CHOICES = [
    ("plus-0.35-page-seed", "ip-adapter-plus_sd15.safetensors", 0.35, True),
    ("plus-0.4-page-seed", "ip-adapter-plus_sd15.safetensors", 0.4, True),
]


async def load_story(story_id: uuid.UUID) -> tuple[bytes, list[tuple[int, str, int]]]:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    async with create_session_factory(engine)() as session:
        row = await StoryRepository(session).get(story_id)
        if row is None:
            raise SystemExit(f"No story {story_id}.")
        characters = list(row.characters)
        scenes = sorted(row.scenes, key=lambda s: s.idx)
        first_key = scenes[0].image_key
        pages = []
        for scene in scenes:
            lead = main_character(scene, characters)
            pages.append((scene.idx, scene_prompt(scene, lead), lead.seed if lead else scene.idx))
    await engine.dispose()
    blobs = build_blob_store(settings)
    if blobs is None or first_key is None:
        raise SystemExit("No object storage, or page 1 has no picture.")
    return await blobs.get(first_key), pages


def main() -> None:
    story_id, out = uuid.UUID(sys.argv[1]), sys.argv[2]
    reference_png, pages = asyncio.run(load_story(story_id))
    reference = Image.open(io.BytesIO(reference_png)).convert("RGB")
    settings = get_settings()
    path = download_weights(settings.image_model, settings.image_model_revision)
    pipe = StableDiffusionPipeline.from_pretrained(
        path, dtype=torch.float16, variant="fp16", use_safetensors=True
    )
    pipe.scheduler = DPMSolverMultistepScheduler.from_config(pipe.scheduler.config, use_karras_sigmas=True)
    pipe.to("cuda")
    pipe.set_progress_bar_config(disable=True)
    loaded: str | None = None
    for label, weights, scale, page_seed in CHOICES:
        if weights != loaded:
            if loaded is not None:
                pipe.unload_ip_adapter()
            if weights is not None:
                files = ["models/image_encoder/config.json", "models/image_encoder/model.safetensors"]
                adapter = download_weights(
                    IP_ADAPTER_REPO, IP_ADAPTER_REVISION, files=[*files, f"models/{weights}"]
                )
                pipe.load_ip_adapter(adapter, subfolder="models", weight_name=weights)
                pipe.to("cuda")
            loaded = weights
        for idx, prompt, seed in pages:
            if idx not in PAGES:
                continue
            extra = {}
            if weights is not None:
                pipe.set_ip_adapter_scale(scale)
                extra["ip_adapter_image"] = reference
            started = time.monotonic()
            image = pipe(
                prompt=prompt,
                negative_prompt=NEGATIVE_PROMPT,
                width=settings.image_width,
                height=settings.image_height,
                num_inference_steps=settings.image_steps,
                guidance_scale=settings.image_guidance,
                generator=torch.Generator("cpu").manual_seed(seed + idx if page_seed else seed),
                **extra,
            ).images[0]
            image.save(f"{out}/page{idx}-{label}.png")
            print(f"page {idx} {label}: {time.monotonic() - started:.1f} s", flush=True)


main()
