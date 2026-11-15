"""The ImageGenerator interface (plan §4.1) and the errors its adapters raise.

Adapters: `PlaceholderImage` (V3 step 2, CPU, always available), then `SD15Local` and `SDXLZeroGPU`. A
generator only turns settings into PNG bytes; storing and recording the image is the job's business.
"""

from typing import Protocol

from storyweaver.imaging.params import GenerationParams

PNG = "image/png"


class ImageError(Exception):
    """An image could not be generated."""


class ImageQuotaExhaustedError(ImageError):
    """No GPU budget left (ours or ZeroGPU's). Retrying soon won't help; use the placeholder."""

    def __init__(self, retry_after_seconds: float, reason: str) -> None:
        super().__init__(reason)
        self.retry_after_seconds = retry_after_seconds


class ImageUnavailableError(ImageError):
    """A transient failure (GPU busy, endpoint down, timeout): worth a retry with backoff."""


class ImageGenerator(Protocol):
    @property
    def name(self) -> str: ...

    async def generate(self, params: GenerationParams) -> bytes:
        """PNG bytes for `params`. Raises ImageQuotaExhaustedError or ImageUnavailableError."""
        ...
