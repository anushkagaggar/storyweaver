import dataclasses
import io

import pytest
from PIL import Image

from storyweaver.imaging.params import GenerationParams, cache_key
from storyweaver.imaging.placeholder import PlaceholderImage, draw_placeholder

pytestmark = pytest.mark.unit

BASE = GenerationParams(
    prompt="a small purple dragon in a cosy cave",
    seed=42,
    model="stabilityai/stable-diffusion-xl-base-1.0",
    model_revision="main",
    width=1152,
    height=896,
    sampler="dpmpp_2m_karras",
    steps=25,
    guidance=6.0,
)

CHANGES = {
    "prompt": "a small green dragon",
    "seed": 43,
    "model": "other/model",
    "model_revision": "abc123",
    "width": 1024,
    "height": 1024,
    "sampler": "euler_a",
    "steps": 30,
    "guidance": 7.5,
    "negative_prompt": "nothing",
    "lora": "storyweaver/style-lora",
    "lora_revision": "v2",
    "lora_scale": 0.8,
    "reference_sha256": "f" * 64,
    "reference_scale": 0.6,
    "pipeline_version": 99,
}


def test_every_field_is_part_of_the_cache_key() -> None:
    # A new field without an entry here fails, so nothing that changes the image can be left out of the key.
    assert set(CHANGES) == {f.name for f in dataclasses.fields(GenerationParams)}
    base = cache_key(BASE)
    for name, value in CHANGES.items():
        assert cache_key(dataclasses.replace(BASE, **{name: value})) != base, name


def test_the_cache_key_is_stable_and_round_trips() -> None:
    assert cache_key(BASE) == cache_key(dataclasses.replace(BASE))
    assert len(cache_key(BASE)) == 64
    assert GenerationParams.from_json(BASE.to_json()) == BASE


async def test_placeholder_is_a_deterministic_png_with_the_right_shape() -> None:
    png = await PlaceholderImage().generate(BASE)
    image = Image.open(io.BytesIO(png))
    assert image.format == "PNG"
    assert image.width == 512 and image.height == 398  # 1152x896 scaled to 512 on the long side
    assert draw_placeholder(42, 1152, 896) == png
    assert draw_placeholder(43, 1152, 896) != png  # each page gets its own colours
