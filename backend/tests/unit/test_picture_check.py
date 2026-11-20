"""The picture self-check: the verdict logic (no models) and the engine's redraw loop (fake models)."""

import dataclasses
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image

from storyweaver.config import SD15_DEFAULTS, Settings
from storyweaver.imaging.generator import ImageRejectedError, ImageUnavailableError
from storyweaver.imaging.params import GenerationParams, redraw_seed
from storyweaver.imaging.picture_check import (
    CHECK_MODEL,
    CheckModels,
    PictureChecker,
    judge,
    mentions_subject,
    subject_nouns,
    unsafe_terms,
)
from storyweaver.imaging.sd15 import SD15Local
from storyweaver.orchestrator.illustration_jobs import scene_params
from storyweaver.services import image_settings

pytestmark = pytest.mark.unit

FOX = "a small orange fox wearing a green hat"
CHECKED = GenerationParams(
    prompt=f"watercolour, {FOX}, in a meadow",
    seed=100,
    model=str(SD15_DEFAULTS["image_model"]),
    model_revision=str(SD15_DEFAULTS["image_model_revision"]),
    width=64,
    height=64,
    sampler="dpmpp_2m_karras",
    steps=2,
    guidance=6.0,
    subject=FOX,
    check_model=CHECK_MODEL,
    check_threshold=0.5,
    max_redraws=2,
)


# --- the verdict, without models ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("subject", "nouns"),
    [
        (FOX, {"fox"}),
        ("Pip, a tiny blue bird with a red scarf", {"pip", "bird"}),
        ("a brave little girl named Mia who loves puddles", {"girl"}),
        ("two cheerful bunnies holding balloons", {"bunny"}),
    ],
)
def test_subject_nouns_are_the_head_nouns(subject: str, nouns: set[str]) -> None:
    assert subject_nouns(subject) == nouns


def test_a_caption_naming_the_character_mentions_it_even_in_the_plural() -> None:
    assert mentions_subject("Two foxes play in a field of daisies.", FOX)
    assert not mentions_subject("A girl holding a green hat in a meadow.", FOX)


@pytest.mark.parametrize(
    "caption",
    [
        "A fox holding a knife in a kitchen.",
        "A cartoon of a bloody bandage.",
        "A man smoking a cigarette.",
        "A naked doll on a shelf.",
    ],
)
def test_unsafe_captions_are_caught(caption: str) -> None:
    assert unsafe_terms(caption)
    assert judge(caption, 0.99, FOX, 0.5).reason == "unsafe"


@pytest.mark.parametrize(
    "caption",
    [
        "A knight with a shiny sword beside a friendly dragon.",
        "A pirate ship with a skull and crossbones flag.",
        "A shooting star over a sleepy village.",
        "A killer whale jumping out of the sea.",
    ],
)
def test_fairy_tale_props_are_not_unsafe(caption: str) -> None:
    assert unsafe_terms(caption) == []


def test_either_signal_passes_the_subject() -> None:
    assert judge("A fox in a hat.", 0.01, FOX, 0.5).ok  # the caption names it
    assert judge("A small orange animal in a hat.", 0.9, FOX, 0.5).ok  # SigLIP sees it
    missed = judge("An empty meadow with daisies.", 0.0001, FOX, 0.5)
    assert not missed.ok and missed.reason == "off_subject" and not missed.unsafe


def test_without_a_subject_only_safety_is_checked() -> None:
    assert judge("An empty meadow with daisies.", None, None, 0.5).ok


# --- the engine's redraw loop, with fake models ------------------------------------------------------------


class SeedPipeline:
    """Draws a picture whose colour encodes the seed, and flags the seeds it is told to."""

    def __init__(self, flagged: set[int] | None = None) -> None:
        self.seeds: list[int] = []
        self.flagged = flagged or set()

    def set_ip_adapter_scale(self, scale: float) -> None:
        pass

    def __call__(self, **kwargs: Any) -> SimpleNamespace:
        seed = kwargs["generator"]
        self.seeds.append(seed)
        image = Image.new("RGB", (kwargs["width"], kwargs["height"]), (seed % 256, 0, 0))
        return SimpleNamespace(images=[image], nsfw_content_detected=[seed in self.flagged])


def fake_models(captions: dict[int, str], scores: dict[int, float]) -> CheckModels:
    """Answers by the red channel, i.e. by the seed that drew the picture."""

    def seed_of(image: Any) -> int:
        red: int = image.getpixel((0, 0))[0]
        return next(seed for seed in (*captions, *scores) if seed % 256 == red)

    return CheckModels(
        caption=lambda image: captions.get(seed_of(image), "A picture."),
        similarity=lambda image, text: scores.get(seed_of(image), 0.0),
    )


def engine(pipe: SeedPipeline, models: CheckModels | None) -> SD15Local:
    checker = PictureChecker(loader=lambda device: models) if models is not None else None  # type: ignore[arg-type,return-value]
    return SD15Local(loader=lambda *_: pipe, generator_for=lambda seed: seed, checker=checker)


def seeds(n: int) -> list[int]:
    return [redraw_seed(CHECKED, i) for i in range(n)]


def red(png: bytes) -> int:
    import io

    value: int = Image.open(io.BytesIO(png)).getpixel((0, 0))[0]
    return value


async def test_a_passing_picture_is_kept_without_redraws() -> None:
    first = seeds(1)[0]
    pipe = SeedPipeline()
    png = await engine(pipe, fake_models({first: "A fox in a green hat."}, {first: 0.99})).generate(CHECKED)
    assert pipe.seeds == [first] and red(png) == first % 256


async def test_an_off_subject_picture_is_redrawn_with_the_next_seed() -> None:
    s0, s1 = seeds(2)
    pipe = SeedPipeline()
    models = fake_models({s0: "An empty meadow.", s1: "A fox in a hat."}, {s0: 0.0, s1: 0.98})
    png = await engine(pipe, models).generate(CHECKED)
    assert pipe.seeds == [s0, s1] and red(png) == s1 % 256


async def test_when_nothing_passes_the_closest_safe_picture_is_kept() -> None:
    s0, s1, s2 = seeds(3)
    pipe = SeedPipeline()
    models = fake_models(
        {s0: "A meadow.", s1: "A meadow with a knife.", s2: "A meadow."}, {s0: 0.2, s1: 0.45, s2: 0.3}
    )
    png = await engine(pipe, models).generate(CHECKED)
    assert pipe.seeds == [s0, s1, s2]
    assert red(png) == s2 % 256  # s1 scored higher but is unsafe


async def test_when_every_drawing_is_unsafe_the_picture_is_rejected() -> None:
    s0, s1, s2 = seeds(3)
    pipe = SeedPipeline(flagged={s0})
    models = fake_models({s1: "A gun.", s2: "Blood on the grass."}, {s1: 0.9, s2: 0.9})
    with pytest.raises(ImageRejectedError):
        await engine(pipe, models).generate(CHECKED)
    assert pipe.seeds == [s0, s1, s2]  # one try plus two redraws, never more


async def test_without_a_check_a_flagged_picture_is_rejected_at_once() -> None:
    unchecked = dataclasses.replace(
        CHECKED, subject=None, check_model=None, check_threshold=None, max_redraws=0
    )
    pipe = SeedPipeline(flagged={unchecked.seed})
    with pytest.raises(ImageRejectedError):
        await engine(pipe, None).generate(unchecked)
    assert pipe.seeds == [unchecked.seed]


async def test_a_checked_request_to_an_engine_without_a_checker_is_retried_not_drawn() -> None:
    pipe = SeedPipeline()
    with pytest.raises(ImageUnavailableError):
        await engine(pipe, None).generate(CHECKED)
    assert pipe.seeds == []


async def test_a_checker_that_cannot_load_is_worth_a_retry() -> None:
    def broken(device: str) -> CheckModels:
        raise OSError("connection reset while downloading")

    pipe = SeedPipeline()
    checked_engine = SD15Local(
        loader=lambda *_: pipe, generator_for=lambda seed: seed, checker=PictureChecker(loader=broken)
    )
    with pytest.raises(ImageUnavailableError):
        await checked_engine.generate(CHECKED)


# --- settings -------------------------------------------------------------------------------------


def test_scene_params_name_the_main_character_as_the_subject() -> None:
    settings = image_settings(Settings(image_engine="sdxl", _env_file=None))  # type: ignore[call-arg]
    character = SimpleNamespace(char_key="fox", seed=7, visual_desc=f" {FOX} ")
    scene = SimpleNamespace(idx=2, image_prompt="a fox in a meadow", character_keys=["fox"])
    params = scene_params(scene, [character], settings)  # type: ignore[arg-type]
    assert params.subject == FOX and params.check_model == CHECK_MODEL
    assert params.check_threshold == 0.5 and params.max_redraws == 2


def test_the_check_can_be_turned_off_and_never_applies_to_placeholders() -> None:
    off = image_settings(Settings(image_engine="sdxl", image_check=False, _env_file=None))  # type: ignore[call-arg]
    placeholder = image_settings(Settings(image_engine="placeholder", _env_file=None))  # type: ignore[call-arg]
    for settings in (off, placeholder):
        assert settings.check_model is None and settings.max_redraws == 0
        character = SimpleNamespace(char_key="fox", seed=7, visual_desc=FOX)
        scene = SimpleNamespace(idx=1, image_prompt="a fox", character_keys=["fox"])
        params = scene_params(scene, [character], settings)  # type: ignore[arg-type]
        assert params.subject is None and params.check_model is None
