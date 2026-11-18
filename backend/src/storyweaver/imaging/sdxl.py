"""SDXL on the ZeroGPU Space (plan V3 step 4, IMAGE_ENGINE=sdxl).

Stable Diffusion XL base 1.0 (pinned in config.SDXL_DEFAULTS) runs through `SD15Local` with this loader. On
ZeroGPU it is loaded and moved to "cuda" at start-up, before `launch()`; each picture is then drawn inside a
`@spaces.GPU` call (imaging/gpu_drawing.py), reached only through the `illustrate` Gradio endpoint.

- fp16 weights only (~7 GB of the repo's ~70 GB). SDXL's VAE overflows in fp16, so diffusers upcasts it to
  fp32 for the decode (`force_upcast` in its config).
- IP-Adapter Plus for SDXL (`h94/IP-Adapter`, Apache-2.0, the same pinned commit as SD 1.5's) with the same
  ViT-H image encoder, for character consistency (plan §6.5).
- SDXL has no safety checker of its own, so the one shipped with the pinned SD 1.5 repo (CreativeML
  OpenRAIL-M, already used by sd15) checks every picture; a flagged one raises ImageRejectedError.

Too big for the local 6 GB card with Docker's 7.35 GB of RAM (about 9 GB with the IP-Adapter): locally, use
sd15. Fetch the weights ahead of time with `python -m storyweaver.imaging.sdxl`.
"""

import logging
from typing import Any

from storyweaver.config import SD15_DEFAULTS, SDXL_DEFAULTS
from storyweaver.imaging.sd15 import (
    IP_ADAPTER_REPO,
    IP_ADAPTER_REVISION,
    SAMPLERS,
    download_weights,
)

logger = logging.getLogger(__name__)

SDXL_WEIGHT_FILES = [
    "model_index.json",
    "scheduler/*.json",
    "text_encoder/config.json",
    "text_encoder/model.fp16.safetensors",
    "text_encoder_2/config.json",
    "text_encoder_2/model.fp16.safetensors",
    "tokenizer/*",
    "tokenizer_2/*",
    "unet/config.json",
    "unet/diffusion_pytorch_model.fp16.safetensors",
    "vae/config.json",
    "vae/diffusion_pytorch_model.fp16.safetensors",
]
SDXL_IP_ADAPTER_WEIGHTS = "ip-adapter-plus_sdxl_vit-h.safetensors"
SDXL_IP_ADAPTER_FILES = [
    "models/image_encoder/config.json",
    "models/image_encoder/model.safetensors",
    f"sdxl_models/{SDXL_IP_ADAPTER_WEIGHTS}",
]
SDXL_IP_ADAPTER_MODEL = f"{IP_ADAPTER_REPO}@{IP_ADAPTER_REVISION[:12]}:{SDXL_IP_ADAPTER_WEIGHTS}"
# SD 1.5's safety checker, from the same pinned SD 1.5 commit sd15 uses.
SAFETY_FILES = [
    "safety_checker/config.json",
    "safety_checker/model.fp16.safetensors",
    "feature_extractor/preprocessor_config.json",
]


class CheckedPipeline:
    """The SDXL pipeline plus a safety check, so its result has `nsfw_content_detected` like SD 1.5's."""

    def __init__(self, pipe: Any, checker: Any, extractor: Any) -> None:
        self._pipe = pipe
        self._checker = checker
        self._extractor = extractor

    def __getattr__(self, name: str) -> Any:
        return getattr(self._pipe, name)  # set_ip_adapter_scale, set_progress_bar_config, ...

    def __call__(self, **kwargs: Any) -> Any:
        import numpy as np
        import torch

        result = self._pipe(**kwargs)
        inputs = self._extractor(result.images, return_tensors="pt").to(self._checker.device)
        arrays = [np.asarray(image, dtype=np.float32) / 255.0 for image in result.images]
        with torch.no_grad():
            _, flagged = self._checker(images=arrays, clip_input=inputs.pixel_values.to(self._checker.dtype))
        result.nsfw_content_detected = list(flagged)
        return result


def load_sdxl_pipeline(model: str, revision: str, sampler: str) -> CheckedPipeline:
    """SDXL with the IP-Adapter and the safety checker, all on "cuda" (lent per call on ZeroGPU)."""
    import torch
    from diffusers import StableDiffusionXLPipeline, schedulers
    from diffusers.pipelines.stable_diffusion.safety_checker import StableDiffusionSafetyChecker
    from transformers import CLIPImageProcessor

    if not torch.cuda.is_available():  # always true on ZeroGPU (patched); real elsewhere
        from storyweaver.imaging.generator import ImageUnavailableError

        raise ImageUnavailableError("torch sees no CUDA GPU")
    scheduler_name, options = SAMPLERS[sampler]
    pipe = StableDiffusionXLPipeline.from_pretrained(
        download_weights(model, revision, files=SDXL_WEIGHT_FILES),
        dtype=torch.float16,
        variant="fp16",
        use_safetensors=True,
    )
    pipe.scheduler = getattr(schedulers, scheduler_name).from_config(pipe.scheduler.config, **options)
    pipe.load_ip_adapter(
        download_weights(IP_ADAPTER_REPO, IP_ADAPTER_REVISION, files=SDXL_IP_ADAPTER_FILES),
        subfolder="sdxl_models",
        weight_name=SDXL_IP_ADAPTER_WEIGHTS,
        image_encoder_folder="models/image_encoder",
    )
    pipe.to("cuda")
    pipe.set_progress_bar_config(disable=True)

    safety_path = download_weights(
        str(SD15_DEFAULTS["image_model"]), str(SD15_DEFAULTS["image_model_revision"]), files=SAFETY_FILES
    )
    checker = StableDiffusionSafetyChecker.from_pretrained(
        safety_path, subfolder="safety_checker", dtype=torch.float16, variant="fp16", use_safetensors=True
    ).to("cuda")
    extractor = CLIPImageProcessor.from_pretrained(safety_path, subfolder="feature_extractor")
    return CheckedPipeline(pipe, checker, extractor)


if __name__ == "__main__":
    # Fetch SDXL, its IP-Adapter and the safety checker into the HF cache (see the module docstring).
    logging.basicConfig(level=logging.INFO)
    model, revision = str(SDXL_DEFAULTS["image_model"]), str(SDXL_DEFAULTS["image_model_revision"])
    print(f"SDXL is in {download_weights(model, revision, files=SDXL_WEIGHT_FILES)}")
    adapter = download_weights(IP_ADAPTER_REPO, IP_ADAPTER_REVISION, files=SDXL_IP_ADAPTER_FILES)
    print(f"IP-Adapter {SDXL_IP_ADAPTER_MODEL} is in {adapter}")
    sd15 = str(SD15_DEFAULTS["image_model"]), str(SD15_DEFAULTS["image_model_revision"])
    print(f"The safety checker is in {download_weights(*sd15, files=SAFETY_FILES)}")
