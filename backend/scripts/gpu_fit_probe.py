"""Does SD 1.5 + IP-Adapter Plus fit entirely on this GPU (no CPU offload)? Prints VRAM, RAM and timings.

Run inside the api image (uses the cached weights):
    docker compose -f infra/docker-compose.yml --env-file .env run --rm --no-deps \
        -v "D:/lenovo/work/storyweaver/backend/scripts:/app/scripts:ro" api python scripts/gpu_fit_probe.py
"""

import resource
import time

import torch
from diffusers import DPMSolverMultistepScheduler, StableDiffusionPipeline
from PIL import Image

from storyweaver.config import SD15_DEFAULTS
from storyweaver.imaging.sd15 import (
    IP_ADAPTER_FILES,
    IP_ADAPTER_REPO,
    IP_ADAPTER_REVISION,
    IP_ADAPTER_WEIGHTS,
    download_weights,
)


def report(label: str) -> None:
    peak = torch.cuda.max_memory_allocated() / 2**30
    reserved = torch.cuda.memory_reserved() / 2**30
    free, total = (x / 2**30 for x in torch.cuda.mem_get_info())
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**20
    print(f"{label}: peak {peak:.2f} GB, reserved {reserved:.2f} GB, free {free:.2f}/{total:.2f} GB")
    print(f"{label}: RAM peak {rss:.2f} GB")


started = time.monotonic()
path = download_weights(str(SD15_DEFAULTS["image_model"]), str(SD15_DEFAULTS["image_model_revision"]))
pipe = StableDiffusionPipeline.from_pretrained(
    path, dtype=torch.float16, variant="fp16", use_safetensors=True
)
pipe.scheduler = DPMSolverMultistepScheduler.from_config(pipe.scheduler.config, use_karras_sigmas=True)
pipe.load_ip_adapter(
    download_weights(IP_ADAPTER_REPO, IP_ADAPTER_REVISION, files=IP_ADAPTER_FILES),
    subfolder="models",
    weight_name=IP_ADAPTER_WEIGHTS,
)
pipe.to("cuda")
pipe.vae.enable_tiling()
pipe.set_progress_bar_config(disable=True)
print(f"loaded in {time.monotonic() - started:.1f} s")
report("after load")

reference = Image.new("RGB", (768, 576), (200, 120, 90))
for run in range(3):
    pipe.set_ip_adapter_scale(0.5)
    t = time.monotonic()
    pipe(
        prompt="children's picture book illustration, soft watercolour, a small hedgehog with a red scarf",
        ip_adapter_image=reference,
        negative_prompt="photo",
        width=768,
        height=576,
        num_inference_steps=25,
        guidance_scale=6.0,
        generator=torch.Generator("cpu").manual_seed(run),
    )
    torch.cuda.synchronize()
    print(f"picture {run + 1}: {time.monotonic() - t:.1f} s")
report("after 3 pictures")
