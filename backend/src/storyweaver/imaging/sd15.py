"""SD15Local: Stable Diffusion 1.5 on this machine's NVIDIA GPU (plan V3, IMAGE_ENGINE=sd15).

For local Docker on a 6 GB card (the RTX 4050), never for the ZeroGPU Space, where CUDA exists only inside a
`@spaces.GPU` call (that is `SDXLZeroGPU`, step 4). Settings that fit in 6 GB:
- fp16 weights (the repo's `fp16` variant, safetensors only);
- everything on the GPU (4.5 GB peak with the IP-Adapter), or `enable_model_cpu_offload` (one part on the
  GPU at a time) when IMAGE_CPU_OFFLOAD=true or the GPU is too full;
- VAE tiling, so decoding a large picture doesn't spike memory.

The model's own safety checker stays on: a flagged picture raises ImageRejectedError and is thrown away.

Later pages use page 1's picture as an IP-Adapter reference (character consistency, plan §6.5).

The weights (2.6 GB, plus 2.5 GB for the IP-Adapter and its image encoder) download to the Hugging Face
cache on first use, which compose mounts from D:.
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

from PIL import Image

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

# Character consistency (plan §6.5): IP-Adapter Plus for SD 1.5 (Apache-2.0, not gated), pinned to a commit
# (checked 16 Nov 2026). "Plus" keeps a character's likeness more closely than the base adapter. Its CLIP
# ViT-H image encoder is 2.4 GB in fp32 and loads as fp16; with model CPU offload it is on the GPU only
# while it encodes the reference.
IP_ADAPTER_REPO = "h94/IP-Adapter"
IP_ADAPTER_REVISION = "018e402774aeeddd60609b4ecdb7e298259dc729"  # pragma: allowlist secret
IP_ADAPTER_WEIGHTS = "ip-adapter-plus_sd15.safetensors"
IP_ADAPTER_FILES = [
    "models/image_encoder/config.json",
    "models/image_encoder/model.safetensors",
    f"models/{IP_ADAPTER_WEIGHTS}",
]
# Names the weights in GenerationParams.reference_model, so the cache key changes with them.
IP_ADAPTER_MODEL = f"{IP_ADAPTER_REPO}@{IP_ADAPTER_REVISION[:12]}:{IP_ADAPTER_WEIGHTS}"


def download_weights(
    model: str, revision: str, *, files: list[str] = WEIGHT_FILES, attempts: int = DOWNLOAD_ATTEMPTS
) -> str:
    """The local folder holding the model's fp16 weights, downloading what's missing first.

    The first download is 2.6 GB and Hugging Face's CDN can drop a connection part-way (seen 16 Nov 2026), so
    it is retried here with backoff; each try resumes from the files already complete. Raises
    ImageUnavailableError when every try fails, so the job retries later rather than spinning.
    """
    from huggingface_hub import snapshot_download

    for attempt in range(1, attempts + 1):
        try:
            path: str = snapshot_download(model, revision=revision, allow_patterns=files)
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


def load_pipeline(model: str, revision: str, sampler: str, *, offload: bool = False) -> Any:
    """The pipeline with the IP-Adapter, all on the GPU unless `offload` (or the GPU is too full).

    Everything on the GPU (measured on the RTX 4050, 16 Nov 2026, scripts/gpu_fit_probe.py): 4.5 GB peak of
    6 GB, 5.7 s a picture. With model CPU offload, every part also lives in RAM (~4 GB): pictures took
    20-56 s, and with the IP-Adapter the api ran out of Docker's 7.35 GB and was killed.
    """
    import torch
    from diffusers import StableDiffusionPipeline, schedulers

    if not torch.cuda.is_available():
        raise ImageUnavailableError("torch sees no CUDA GPU")
    scheduler_name, options = SAMPLERS[sampler]
    pipe = StableDiffusionPipeline.from_pretrained(
        download_weights(model, revision), dtype=torch.float16, variant="fp16", use_safetensors=True
    )
    pipe.scheduler = getattr(schedulers, scheduler_name).from_config(pipe.scheduler.config, **options)
    # Before any offload, so the adapter's image encoder gets an offload hook like the other parts.
    pipe.load_ip_adapter(
        download_weights(IP_ADAPTER_REPO, IP_ADAPTER_REVISION, files=IP_ADAPTER_FILES),
        subfolder="models",
        weight_name=IP_ADAPTER_WEIGHTS,
    )
    if not offload:
        try:
            pipe.to("cuda")
        except torch.cuda.OutOfMemoryError:
            logger.warning("The pictures model doesn't fit on the GPU; using CPU offload (slower, more RAM).")
            pipe.to("cpu")
            free_gpu_memory()
            offload = True
    if offload:
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


def open_png(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data)).convert("RGB")


def no_reference() -> Image.Image:
    """The blank image passed at strength 0 when a page has no reference."""
    return Image.new("RGB", (224, 224), (255, 255, 255))


class SD15Local:
    """One picture at a time (a lock), in a worker thread so the event loop keeps serving requests.

    It runs whatever diffusers pipeline its loader returns, with an IP-Adapter loaded: SD 1.5 here, SDXL
    with `imaging.sdxl.load_sdxl_pipeline` (named "sdxl").
    """

    def __init__(
        self,
        loader: PipelineLoader = load_pipeline,
        generator_for: Callable[[int], Any] = seeded_generator,
        *,
        name: str = "sd15",
    ) -> None:
        self._loader = loader
        self._generator_for = generator_for
        self._name = name
        self._lock = threading.Lock()
        self._loaded: tuple[tuple[str, str, str], Any] | None = None

    @property
    def name(self) -> str:
        return self._name

    async def generate(self, params: GenerationParams, reference: bytes | None = None) -> bytes:
        self.check(params, reference)
        return await asyncio.to_thread(self._generate, params, reference)

    def draw(self, params: GenerationParams, reference: bytes | None = None) -> bytes:
        """`generate` on the calling thread: inside a ZeroGPU `@spaces.GPU` call (imaging/gpu_drawing.py)."""
        self.check(params, reference)
        return self._generate(params, reference)

    @staticmethod
    def check(params: GenerationParams, reference: bytes | None) -> None:
        if params.sampler not in SAMPLERS:
            raise ValueError(f"Unknown IMAGE_SAMPLER {params.sampler!r}; use one of {sorted(SAMPLERS)}.")
        if (reference is None) != (params.reference_sha256 is None):
            raise ValueError("A reference picture and its sha256 must be given together.")

    def load(self, model: str, revision: str, sampler: str) -> None:
        """Load the model now. On ZeroGPU this must happen at start-up, before `launch()`: the weights are
        moved to "cuda" there and ZeroGPU attaches them to the GPU it lends each call."""
        with self._lock:
            self._pipeline((model, revision, sampler))

    async def warm_up(self, model: str, revision: str, sampler: str) -> None:
        """Load the model and draw one tiny throwaway picture, so a story's first page doesn't wait for it.

        The first picture after a restart took 76 s instead of 8-34 s: loading (22 s), then CUDA's first-run
        set-up. A picture job arriving meanwhile waits on the lock, then finds the model ready.
        """
        if sampler not in SAMPLERS:
            raise ValueError(f"Unknown IMAGE_SAMPLER {sampler!r}; use one of {sorted(SAMPLERS)}.")
        await asyncio.to_thread(self._warm_up, (model, revision, sampler))

    def _warm_up(self, wanted: tuple[str, str, str]) -> None:
        with self._lock:
            started = time.monotonic()
            pipe = self._pipeline(wanted)
            pipe.set_ip_adapter_scale(0.0)
            pipe(
                prompt="a small round pebble",
                ip_adapter_image=no_reference(),
                width=256,
                height=256,
                num_inference_steps=1,
                guidance_scale=1.0,
            )
            logger.info("Pictures warmed up in %.1f s.", time.monotonic() - started)

    def _pipeline(self, wanted: tuple[str, str, str]) -> Any:
        if self._loaded is None or self._loaded[0] != wanted:
            self._loaded = None
            free_gpu_memory()
            started = time.monotonic()
            pipe = self._loader(*wanted)
            logger.info("Loaded %s@%s in %.1f s.", wanted[0], wanted[1][:12], time.monotonic() - started)
            self._loaded = (wanted, pipe)
        return self._loaded[1]

    def _generate(self, params: GenerationParams, reference: bytes | None) -> bytes:
        with self._lock:
            try:
                pipe = self._pipeline((params.model, params.model_revision, params.sampler))
                # With the adapter loaded, every call needs an image; without a reference it is a blank one
                # at strength 0, which adds nothing.
                pipe.set_ip_adapter_scale(params.reference_scale if reference is not None else 0.0)
                started = time.monotonic()
                result = pipe(
                    prompt=params.prompt,
                    ip_adapter_image=open_png(reference) if reference is not None else no_reference(),
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
    adapter = download_weights(IP_ADAPTER_REPO, IP_ADAPTER_REVISION, files=IP_ADAPTER_FILES)
    print(f"IP-Adapter {IP_ADAPTER_MODEL} is in {adapter}")
