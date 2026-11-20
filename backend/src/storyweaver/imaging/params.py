"""Everything that decides what an image looks like, and the content-addressed cache key derived from it.

The cache key hashes *every* field of `GenerationParams`, not just the prompt: two requests share a cached
image only when the model, its revision, the LoRA, the size, the sampler, the steps, the guidance, the seed
and the IP-Adapter reference all match. Adding a field to the dataclass adds it to the key automatically.
Bump `PIPELINE_VERSION` when code changes the output without changing a field (e.g. a new VAE fix).
"""

import hashlib
import json
from dataclasses import asdict, dataclass

PIPELINE_VERSION = 1
# The look of every picture, put first so it is never cut by CLIP's 77-token limit. The planner is asked for a
# children's-book style too, but often leaves it out, and SD 1.5's default is then a photograph (16 Nov 2026).
STYLE_PREFIX = (
    "children's picture book illustration, soft watercolour, gentle pastel colours, cute, simple shapes"
)
NEGATIVE_PROMPT = (
    "photo, photograph, photorealistic, realistic, 3d render, "
    "text, words, letters, watermark, signature, scary, dark, blurry, deformed, extra limbs"
)


def styled_prompt(image_prompt: str) -> str:
    return f"{STYLE_PREFIX}, {image_prompt}"


@dataclass(frozen=True)
class GenerationParams:
    prompt: str
    seed: int
    model: str
    model_revision: str
    width: int
    height: int
    sampler: str
    steps: int
    guidance: float
    negative_prompt: str = NEGATIVE_PROMPT
    lora: str | None = None
    lora_revision: str | None = None
    lora_scale: float | None = None
    reference_sha256: str | None = None  # the IP-Adapter reference image (scene 1's), by content
    reference_scale: float | None = None
    reference_model: str | None = None  # the IP-Adapter weights (repo@revision:file)
    # The picture self-check (imaging/picture_check.py). With `check_model` set, the engine captions each
    # picture and redraws a failed one with the next seed, up to `max_redraws` times, so these settings
    # decide which picture is kept. `subject` is the main character's fixed look that must be visible.
    subject: str | None = None
    check_model: str | None = None
    check_threshold: float | None = None
    max_redraws: int = 0
    pipeline_version: int = PIPELINE_VERSION

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def from_json(cls, data: str) -> "GenerationParams":
        return cls(**json.loads(data))


# Redraw n uses seed + n * REDRAW_SEED_STEP: far from the next page's seed (seed + 1), so a redraw never
# repeats a neighbouring page's composition.
REDRAW_SEED_STEP = 1009


def redraw_seed(params: GenerationParams, redraw: int) -> int:
    return params.seed + redraw * REDRAW_SEED_STEP


def cache_key(params: GenerationParams) -> str:
    """sha256 of the canonical JSON of every generation setting."""
    return hashlib.sha256(params.to_json().encode()).hexdigest()
