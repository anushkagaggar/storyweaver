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
NEGATIVE_PROMPT = "text, words, letters, watermark, signature, scary, dark, blurry, deformed, extra limbs"


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
    pipeline_version: int = PIPELINE_VERSION

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def from_json(cls, data: str) -> "GenerationParams":
        return cls(**json.loads(data))


def cache_key(params: GenerationParams) -> str:
    """sha256 of the canonical JSON of every generation setting."""
    return hashlib.sha256(params.to_json().encode()).hexdigest()
