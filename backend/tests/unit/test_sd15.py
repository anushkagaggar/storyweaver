import dataclasses
import io
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image

from storyweaver.config import SD15_DEFAULTS, Settings
from storyweaver.imaging.generator import ImageRejectedError, ImageUnavailableError
from storyweaver.imaging.params import GenerationParams
from storyweaver.imaging.sd15 import SD15Local
from storyweaver.orchestrator.illustration_jobs import LocalIllustrator
from storyweaver.services import build_illustrator

pytestmark = pytest.mark.unit

PARAMS = GenerationParams(
    prompt="a small purple dragon in a cosy cave, soft children's-book style",
    seed=7,
    model=str(SD15_DEFAULTS["image_model"]),
    model_revision=str(SD15_DEFAULTS["image_model_revision"]),
    width=768,
    height=576,
    sampler="dpmpp_2m_karras",
    steps=20,
    guidance=6.0,
)


class OutOfMemoryError(RuntimeError):
    """Named like torch.cuda.OutOfMemoryError, which is what the adapter recognises."""


class FakePipeline:
    def __init__(self, *, nsfw: bool = False, fail: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.nsfw = nsfw
        self.fail = fail

    def __call__(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        if self.fail is not None:
            raise self.fail
        image = Image.new("RGB", (kwargs["width"], kwargs["height"]), (200, 180, 240))
        return SimpleNamespace(images=[image], nsfw_content_detected=[self.nsfw])


class FakeLoader:
    def __init__(self, pipe: FakePipeline) -> None:
        self.pipe = pipe
        self.loaded: list[tuple[str, str, str]] = []

    def __call__(self, model: str, revision: str, sampler: str) -> FakePipeline:
        self.loaded.append((model, revision, sampler))
        return self.pipe


def sd15(pipe: FakePipeline) -> tuple[SD15Local, FakeLoader]:
    loader = FakeLoader(pipe)
    return SD15Local(loader=loader, generator_for=lambda seed: f"generator({seed})"), loader


async def test_draws_a_png_with_every_setting_passed_through() -> None:
    pipe = FakePipeline()
    engine, loader = sd15(pipe)

    png = await engine.generate(PARAMS)

    image = Image.open(io.BytesIO(png))
    assert image.format == "PNG" and image.size == (768, 576)
    assert pipe.calls == [
        {
            "prompt": PARAMS.prompt,
            "negative_prompt": PARAMS.negative_prompt,
            "width": 768,
            "height": 576,
            "num_inference_steps": 20,
            "guidance_scale": 6.0,
            "generator": "generator(7)",  # the character's seed keeps it recognisable across pages
        }
    ]
    assert loader.loaded == [(PARAMS.model, PARAMS.model_revision, "dpmpp_2m_karras")]


async def test_the_model_is_loaded_once_and_again_only_when_the_settings_change() -> None:
    engine, loader = sd15(FakePipeline())
    await engine.generate(PARAMS)
    await engine.generate(dataclasses.replace(PARAMS, seed=8, prompt="another page"))
    assert len(loader.loaded) == 1
    await engine.generate(dataclasses.replace(PARAMS, sampler="euler_a"))
    assert len(loader.loaded) == 2


async def test_a_picture_the_safety_checker_flags_is_rejected_not_returned() -> None:
    engine, _ = sd15(FakePipeline(nsfw=True))
    with pytest.raises(ImageRejectedError):
        await engine.generate(PARAMS)


@pytest.mark.parametrize(
    "failure",
    [OutOfMemoryError("CUDA out of memory. Tried to allocate 1 GiB"), OSError("connection reset")],
)
async def test_running_out_of_gpu_memory_or_a_failed_download_is_worth_a_retry(failure: Exception) -> None:
    engine, _ = sd15(FakePipeline(fail=failure))
    with pytest.raises(ImageUnavailableError):
        await engine.generate(PARAMS)


async def test_a_programming_error_is_not_disguised_as_a_retryable_one() -> None:
    engine, _ = sd15(FakePipeline(fail=TypeError("unexpected keyword")))
    with pytest.raises(TypeError):
        await engine.generate(PARAMS)


async def test_an_unknown_sampler_fails_loudly() -> None:
    engine, loader = sd15(FakePipeline())
    with pytest.raises(ValueError, match="IMAGE_SAMPLER"):
        await engine.generate(dataclasses.replace(PARAMS, sampler="mystery"))
    assert loader.loaded == []


def _settings(**values: Any) -> Settings:
    return Settings(storyweaver_api_key="k" * 32, **values)  # type: ignore[arg-type]


def test_sd15_gets_its_own_pinned_model_and_size_unless_they_are_set() -> None:
    settings = _settings(image_engine="sd15")
    assert settings.image_model == SD15_DEFAULTS["image_model"]
    assert settings.image_model_revision == SD15_DEFAULTS["image_model_revision"]
    assert (settings.image_width, settings.image_height) == (768, 576)

    chosen = _settings(image_engine="sd15", image_width=512, image_height=512)
    assert (chosen.image_width, chosen.image_height) == (512, 512)

    placeholder = _settings(image_engine="placeholder")
    assert placeholder.image_model == "stabilityai/stable-diffusion-xl-base-1.0"


def test_sizes_must_be_multiples_of_eight() -> None:
    with pytest.raises(ValueError, match="multiple"):
        _settings(image_width=770)


def test_the_illustrator_is_sd15_only_locally_with_a_gpu(monkeypatch: pytest.MonkeyPatch) -> None:
    blobs: Any = object()
    monkeypatch.setattr("storyweaver.services.cuda_available", lambda: True)
    assert isinstance(build_illustrator(_settings(image_engine="sd15"), blobs), LocalIllustrator)
    assert build_illustrator(_settings(image_engine="placeholder"), blobs) is None
    assert build_illustrator(_settings(image_engine="sd15", app_env="prod"), blobs) is None

    monkeypatch.setattr("storyweaver.services.cuda_available", lambda: False)
    assert build_illustrator(_settings(image_engine="sd15"), blobs) is None  # placeholders, not CPU minutes
