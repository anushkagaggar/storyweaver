"""PlaceholderImage: a soft, wordless picture used when no GPU image can be made (plan §6.4 step 5).

A pastel sky, a glowing sun or moon and two rolling hills, in colours picked deterministically from the
seed, so the same scene always gets the same placeholder and pages of a story differ. No text: the reader
shows its own "pictures are resting" note. Drawn on the CPU in a thread, in well under a second.
"""

import asyncio
import colorsys
import io
import random

from PIL import Image, ImageDraw, ImageFilter

from storyweaver.imaging.params import GenerationParams

MAX_SIDE = 512  # the browser scales it; a placeholder needn't be full size


def _pastel(rng: random.Random, lightness: float) -> tuple[int, int, int]:
    r, g, b = colorsys.hls_to_rgb(rng.random(), lightness, 0.45 + 0.2 * rng.random())
    return int(r * 255), int(g * 255), int(b * 255)


def _mix(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return (
        round(a[0] + (b[0] - a[0]) * t),
        round(a[1] + (b[1] - a[1]) * t),
        round(a[2] + (b[2] - a[2]) * t),
    )


def draw_placeholder(seed: int, width: int, height: int) -> bytes:
    scale = min(1.0, MAX_SIDE / max(width, height))
    w, h = max(1, round(width * scale)), max(1, round(height * scale))
    rng = random.Random(seed)  # noqa: S311  # colours, not security
    top, bottom = _pastel(rng, 0.82), _pastel(rng, 0.9)

    image = Image.new("RGB", (w, h))
    draw = ImageDraw.Draw(image)
    for y in range(h):
        draw.line([(0, y), (w, y)], fill=_mix(top, bottom, y / max(1, h - 1)))

    radius = round(min(w, h) * (0.1 + 0.05 * rng.random()))
    cx, cy = round(w * (0.2 + 0.6 * rng.random())), round(h * (0.18 + 0.12 * rng.random()))
    draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], fill=(255, 244, 214))

    for depth, lightness in ((0.62, 0.72), (0.78, 0.62)):
        hill = _pastel(rng, lightness)
        crest = round(h * (depth + 0.06 * rng.random()))
        offset = round(w * (rng.random() - 0.5) * 0.6)
        draw.ellipse([offset - w // 2, crest, offset + w * 3 // 2, crest + h], fill=hill)

    image = image.filter(ImageFilter.GaussianBlur(radius=max(1, w // 160)))
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()


class PlaceholderImage:
    name = "placeholder"

    async def generate(self, params: GenerationParams, reference: bytes | None = None) -> bytes:
        return await asyncio.to_thread(draw_placeholder, params.seed, params.width, params.height)
