"""SD15Local: Stable Diffusion 1.5 on this machine's NVIDIA GPU (plan V3, IMAGE_ENGINE=sd15).

For local Docker on a 6 GB card (the RTX 4050), never for the ZeroGPU Space, where CUDA exists only inside a
`@spaces.GPU` call (that is `SDXLZeroGPU`, step 4). Settings that fit in 6 GB:
- fp16 weights (the repo's `fp16` variant, safetensors only);
- `enable_model_cpu_offload`: one sub-model (text encoder, UNet, VAE) on the GPU at a time;
- VAE tiling, so decoding a large picture doesn't spike memory.

The model's own safety checker stays on: a flagged picture raises ImageRejectedError and is thrown away.

The weights (about 2.6 GB) download to the Hugging Face cache on first use, which compose mounts from D:.
Fetch them ahead of time, so the first story isn't the one that waits:
    docker compose -f infra/docker-compose.yml --env-file .env run --rm --no-deps api \
        python -m storyweaver.imaging.sd15
`diffusers`, `transformers`, `accelerate` and `torch` are in the `gpu` extra and imported only when the first
picture is made, so the app (and CI, which has no `gpu` extra) starts without them.
"""

import asyncio
import io
import logging
import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from storyweaver.imaging.generator import ImageRejectedError, ImageUnavailableError
from storyweaver.imaging.params import GenerationParams

logger = logging.getLogger(__name__)

# Sampler names (IMAGE_SAMPLER, part of the cache key) -> diffusers scheduler class and its options.
SAMPLERS: dict[str, tuple[str, dict[str, Any]]] = {
    "dpmpp_2m_karras": ("DPMSolverMultistepScheduler", {"use_karras_sigmas": True}),
    "dpmpp_2m": ("DPMSolverMultistepScheduler", {}),
    "euler_a": ("EulerAncestralDiscreteScheduler", {}),
    "euler": ("EulerDiscreteScheduler", {}),
    "ddim": ("DDIMScheduler", {}),
}

# (model, revision, sampler) -> a ready pipeline. Injected in tests.
PipelineLoader = Callable[[str, str, str], Any]


def nvidia_gpu_present() -> bool:
    """A cheap start-up check for an NVIDIA GPU: its device files, as Docker's GPU support mounts them.

    It must not import torch: that takes several seconds, and Gradio's `launch()` reports "Cannot find empty
    port" when the server isn't up within about 5 s (seen 16 Nov 2026). torch confirms CUDA on first use.
    """
    return Path("/dev/nvidiactl").exists() or shutil.which("nvidia-smi") is not None


# Only what an fp16 pipeline loads: 2.6 GB instead of the repo's ~45 GB of fp32, .ckpt and EMA files.
WEIGHT_FILES = ["*.json", "*.txt", "*.fp16.safetensors"]
DOWNLOAD_ATTEMPTS = 4


def download_weights(model: str, revision: str, *, attempts: int = DOWNLOAD_ATTEMPTS) -> str:
    """The local folder holding the model's fp16 weights, downloading what's missing first.

    The first download is 2.6 GB and Hugging Face's CDN can drop a connection part-way (seen 16 Nov 2026), so
    it is retried here with backoff; each try resumes from the files already complete. Raises
    ImageUnavailableError when every try fails, so the job retries later rather than spinning.
    """
    from huggingface_hub import snapshot_download

    for attempt in range(1, attempts + 1):
        try:
            path: str = snapshot_download(model, revision=revision, allow_patterns=WEIGHT_FILES)
            return path
        except Exception as exc:  # network, CDN and cache errors all surface differently
            if attempt == attempts:
                raise ImageUnavailableError(f"could not download {model}: {exc}") from exc
            wait = 5 * 2**attempt
            logger.warning(
                "Downloading %s failed (try %d of %d); again in %d s: %s", model, attempt, attempts, wait, exc
            )
            time.sleep(wait)
    raise AssertionError("unreachable")


def load_pipeline(model: str, revision: str, sampler: str) -> Any:
    import torch
    from diffusers import StableDiffusionPipeline, schedulers

    if not torch.cuda.is_available():
        raise ImageUnavailableError("torch sees no CUDA GPU")
    scheduler_name, options = SAMPLERS[sampler]
    pipe = StableDiffusionPipeline.from_pretrained(
        download_weights(model, revision), dtype=torch.float16, variant="fp16", use_safetensors=True
    )
    pipe.scheduler = getattr(schedulers, scheduler_name).from_config(pipe.scheduler.config, **options)
    pipe.enable_model_cpu_offload()
    pipe.vae.enable_tiling()
    pipe.set_progress_bar_config(disable=True)
    return pipe


def is_out_of_memory(exc: BaseException) -> bool:
    return type(exc).__name__ == "OutOfMemoryError" or "out of memory" in str(exc).lower()


def free_gpu_memory() -> None:
    try:
        import torch
    except ImportError:
        return
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


def seeded_generator(seed: int) -> Any:
    """A CPU generator: the same seed gives the same picture whichever sub-model is on the GPU."""
    import torch

    return torch.Generator("cpu").manual_seed(seed)


def to_png(image: Any) -> bytes:
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()


class SD15Local:
    """One picture at a time (a lock), in a worker thread so the event loop keeps serving requests."""

    name = "sd15"

    def __init__(
        self,
        loader: PipelineLoader = load_pipeline,
        generator_for: Callable[[int], Any] = seeded_generator,
    ) -> None:
        self._loader = loader
        self._generator_for = generator_for
        self._lock = threading.Lock()
        self._loaded: tuple[tuple[str, str, str], Any] | None = None

    async def generate(self, params: GenerationParams) -> bytes:
        if params.sampler not in SAMPLERS:
            raise ValueError(f"Unknown IMAGE_SAMPLER {params.sampler!r}; use one of {sorted(SAMPLERS)}.")
        return await asyncio.to_thread(self._generate, params)

    def _pipeline(self, params: GenerationParams) -> Any:
        wanted = (params.model, params.model_revision, params.sampler)
        if self._loaded is None or self._loaded[0] != wanted:
            self._loaded = None
            free_gpu_memory()
            started = time.monotonic()
            pipe = self._loader(*wanted)
            logger.info(
                "Loaded %s@%s in %.1f s.",
                params.model,
                params.model_revision[:12],
                time.monotonic() - started,
            )
            self._loaded = (wanted, pipe)
        return self._loaded[1]

    def _generate(self, params: GenerationParams) -> bytes:
        with self._lock:
            try:
                pipe = self._pipeline(params)
                started = time.monotonic()
                result = pipe(
                    prompt=params.prompt,
                    negative_prompt=params.negative_prompt,
                    width=params.width,
                    height=params.height,
                    num_inference_steps=params.steps,
                    guidance_scale=params.guidance,
                    generator=self._generator_for(params.seed),
                )
            except Exception as exc:
                if is_out_of_memory(exc):
                    free_gpu_memory()
                    raise ImageUnavailableError("the GPU ran out of memory") from exc
                if isinstance(exc, OSError):  # a download or a cache file failed
                    raise ImageUnavailableError(f"could not load {params.model}: {exc}") from exc
                raise
            if any(getattr(result, "nsfw_content_detected", None) or []):
                raise ImageRejectedError("flagged by the model's safety checker")
            logger.info(
                "Drew a %dx%d picture in %.1f s (%d steps).",
                params.width,
                params.height,
                time.monotonic() - started,
                params.steps,
            )
            return to_png(result.images[0])


if __name__ == "__main__":
    # Fetch the configured model's weights into the HF cache (see the module docstring).
    from storyweaver.config import SD15_DEFAULTS

    logging.basicConfig(level=logging.INFO)
    model, revision = str(SD15_DEFAULTS["image_model"]), str(SD15_DEFAULTS["image_model_revision"])
    print(f"Weights for {model}@{revision[:12]} are in {download_weights(model, revision)}")
