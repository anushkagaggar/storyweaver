"""V0 deployment-spike checks: prove the GPU and Groq paths work end to end."""

import time
from typing import Any

import spaces


@spaces.GPU(duration=20)  # type: ignore[untyped-decorator]  # the spaces package ships no type hints
def run_gpu_matmul(size: int = 1024) -> dict[str, Any]:
    """Run a small CUDA matmul. On ZeroGPU a GPU is attached only for the duration of this call.

    Returns a plain dict (picklable) rather than raising, because ZeroGPU runs this in a separate process.
    """
    try:
        import torch
    except ImportError:
        return {"ok": False, "reason": "PyTorch is not installed (install the 'gpu' extra)."}

    if not torch.cuda.is_available():
        return {"ok": False, "reason": "CUDA is not available.", "torch": torch.__version__}

    a = torch.randn(size, size, device="cuda")
    b = torch.randn(size, size, device="cuda")
    torch.cuda.synchronize()
    start = time.perf_counter()
    c = a @ b
    torch.cuda.synchronize()
    elapsed_ms = (time.perf_counter() - start) * 1000

    return {
        "ok": True,
        "device": torch.cuda.get_device_name(0),
        "torch": torch.__version__,
        "cuda": torch.version.cuda,
        "matmul_size": size,
        "matmul_ms": round(elapsed_ms, 2),
        "checksum": float(c.float().sum().item()),
    }
