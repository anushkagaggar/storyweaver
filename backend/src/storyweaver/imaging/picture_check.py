"""The picture self-check (plan §6.5 character consistency, §6.6 image safety; V3).

After each picture, Florence-2 writes a caption and SigLIP scores how well the picture shows the page's main
character (its fixed `visual` description). A picture fails when:
- **unsafe:** its caption names something a young child's book must not show (blood, weapons, nudity, ...).
  This sits behind the negative prompt and the diffusers safety checker, which only looks for nudity;
- **off subject:** SigLIP's score is under the threshold *and* the caption names none of the character's
  head nouns ("fox" for "a small orange fox wearing a green hat"). Either signal alone passes it, because a
  needless redraw costs GPU time while a slightly-off character only costs a little consistency.

The engine redraws a failed picture with the next seed, up to IMAGE_MAX_REDRAWS times (imaging/sd15.py).

Measured 20 Nov 2026 on the SDXL test-Space pictures (CPU, scratch spike): SigLIP gave 0.997-0.999 for the
fox's own description and 0.0000 for a girl, a dragon and an elephant (0.24 s); Florence-2's detailed
caption named "a fox wearing a green hat" on both pages (~10 s on the CPU, well under 1 s on a GPU).

Both models are small, MIT / Apache-2.0 and not gated, pinned to commits (checked 20 Nov 2026):
- `florence-community/Florence-2-base`: Microsoft's Florence-2 base (0.23 B) converted to transformers'
  native classes, so no `trust_remote_code`;
- `google/siglip-base-patch16-224` (0.2 B). Its tokenizer class needs sentencepiece, which isn't installed,
  so its `tokenizer.json` loads through the fast tokenizer instead (pad and end token `</s>`, lower case).

torch and transformers come from the `gpu` extra and are imported only when the checker loads.
"""

import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from storyweaver.imaging.sd15 import download_weights

logger = logging.getLogger(__name__)

FLORENCE_REPO = "florence-community/Florence-2-base"
FLORENCE_REVISION = "00921df66db728a9ceb750f5eca43e5c203a2051"  # pragma: allowlist secret
FLORENCE_FILES = ["*.json", "*.txt", "*.safetensors"]
SIGLIP_REPO = "google/siglip-base-patch16-224"
SIGLIP_REVISION = "7fd15f0689c79d79e38b1c2e2e2370a7bf2761ed"  # pragma: allowlist secret
SIGLIP_FILES = ["*.json", "model.safetensors"]
# Bump when the checking logic changes what passes: it is in the cache key (GenerationParams.check_model).
CHECK_VERSION = 1
CHECK_MODEL = (
    f"{FLORENCE_REPO}@{FLORENCE_REVISION[:12]}+{SIGLIP_REPO}@{SIGLIP_REVISION[:12]}+check-v{CHECK_VERSION}"
)

CAPTION_TASK = "<DETAILED_CAPTION>"
CAPTION_MAX_TOKENS = 64
SIGLIP_TEXT_TOKENS = 64  # SigLIP was trained with text padded to 64 tokens
SUBJECT_TEMPLATE = "a picture book illustration of {}"

# Words a caption of a picture for 4-6 year olds must never contain. Narrow on purpose, like safety/rules.py:
# fairy-tale props ("sword", "dragon", "skull and crossbones" on a pirate flag) stay allowed.
_UNSAFE = re.compile(
    r"(?i)\b(?:blood(?:y|ied)?|bleed(?:s|ing)?|gore|gun(?:s)?|pistol(?:s)?|rifle(?:s)?|firearm(?:s)?|"
    r"knife|knives|dagger(?:s)?|weapon(?:s)?|corpse(?:s)?|dead body|dead bodies|"
    r"wound(?:s|ed)?|injur(?:y|ies|ed)|"
    r"naked|nude|nudity|topless|underwear|lingerie|cigarette(?:s)?|cigar(?:s)?|syringe(?:s)?|"
    r"beer|alcohol|drunk|kill(?:s|ed|ing)?|murder\w*|suicide|torture\w*|sex(?:y|ual)?)\b"
)
# Words that end a character's head noun: "a small orange fox | wearing a green hat".
_CONNECTORS = {"wearing", "with", "who", "that", "in", "on", "holding", "carrying", "named", "called", "and"}
_WORD = re.compile(r"[a-z]+")


@dataclass(frozen=True)
class PictureVerdict:
    ok: bool
    reason: str  # "ok", "unsafe" or "off_subject"
    similarity: float | None  # SigLIP's probability that the picture shows the subject; None: no subject
    caption: str

    @property
    def unsafe(self) -> bool:
        return self.reason == "unsafe"


def unsafe_terms(caption: str) -> list[str]:
    return sorted({match.group(0).lower() for match in _UNSAFE.finditer(caption)})


def _singular(word: str) -> str:
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith(("ches", "shes", "xes", "sses")):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        return word[:-1]
    return word


def subject_nouns(subject: str) -> set[str]:
    """The head noun of each comma-separated part: "Pip, a tiny blue bird with a red scarf" -> {pip, bird}."""
    nouns: set[str] = set()
    for part in subject.lower().split(","):
        words: list[str] = []
        for word in _WORD.findall(part):
            if word in _CONNECTORS:
                break
            words.append(word)
        if words:
            nouns.add(_singular(words[-1]))
    return nouns


def mentions_subject(caption: str, subject: str) -> bool:
    caption_words = {_singular(word) for word in _WORD.findall(caption.lower())}
    return bool(subject_nouns(subject) & caption_words)


def judge(caption: str, similarity: float | None, subject: str | None, threshold: float) -> PictureVerdict:
    """The verdict from a caption and a SigLIP score (pure, so it is tested without models)."""
    if unsafe_terms(caption):
        return PictureVerdict(False, "unsafe", similarity, caption)
    if subject is None or similarity is None:
        return PictureVerdict(True, "ok", similarity, caption)
    if similarity >= threshold or mentions_subject(caption, subject):
        return PictureVerdict(True, "ok", similarity, caption)
    return PictureVerdict(False, "off_subject", similarity, caption)


# (device) -> the loaded models. Injected in tests.
CheckLoader = Callable[[str], "CheckModels"]


@dataclass
class CheckModels:
    caption: Callable[[Any], str]  # PIL image -> caption
    similarity: Callable[[Any, str], float]  # (PIL image, text) -> probability


def load_check_models(device: str) -> CheckModels:
    """Florence-2 and SigLIP on `device` (fp16 on a GPU, fp32 on the CPU)."""
    import torch
    from transformers import (
        AutoModel,
        AutoProcessor,
        Florence2ForConditionalGeneration,
        PreTrainedTokenizerFast,
        SiglipImageProcessor,
    )

    dtype = torch.float16 if device.startswith("cuda") else torch.float32
    florence_path = download_weights(FLORENCE_REPO, FLORENCE_REVISION, files=FLORENCE_FILES)
    processor = AutoProcessor.from_pretrained(florence_path)
    florence = Florence2ForConditionalGeneration.from_pretrained(florence_path, dtype=dtype).to(device).eval()
    siglip_path = download_weights(SIGLIP_REPO, SIGLIP_REVISION, files=SIGLIP_FILES)
    special = {"pad_token": "</s>", "eos_token": "</s>", "unk_token": "<unk>"}  # SiglipTokenizer's own
    tokenizer = PreTrainedTokenizerFast(tokenizer_file=f"{siglip_path}/tokenizer.json", **special)
    image_processor = SiglipImageProcessor.from_pretrained(siglip_path)
    siglip = AutoModel.from_pretrained(siglip_path, dtype=dtype).to(device).eval()

    def caption(image: Any) -> str:
        inputs = processor(text=CAPTION_TASK, images=image, return_tensors="pt").to(device, dtype)
        with torch.no_grad():
            ids = florence.generate(**inputs, max_new_tokens=CAPTION_MAX_TOKENS, num_beams=1, do_sample=False)
        raw = processor.batch_decode(ids, skip_special_tokens=False)[0]
        parsed = processor.post_process_generation(raw, task=CAPTION_TASK, image_size=image.size)
        return str(parsed[CAPTION_TASK]).strip()

    def similarity(image: Any, text: str) -> float:
        tokens = tokenizer(
            [SUBJECT_TEMPLATE.format(text).lower()],
            padding="max_length",
            max_length=SIGLIP_TEXT_TOKENS,
            truncation=True,
            return_tensors="pt",
        )
        pixels = image_processor(images=image, return_tensors="pt").pixel_values.to(device, dtype)
        with torch.no_grad():
            logits = siglip(input_ids=tokens.input_ids.to(device), pixel_values=pixels).logits_per_image
        return float(torch.sigmoid(logits.float())[0][0])

    return CheckModels(caption=caption, similarity=similarity)


class PictureChecker:
    """Loads its two models once (at start-up on ZeroGPU, like the picture model), then checks pictures.

    Called by the engine while it holds its own lock, so one check runs at a time.
    """

    def __init__(self, device: str = "cpu", loader: CheckLoader = load_check_models) -> None:
        self._device = device
        self._loader = loader
        self._models: CheckModels | None = None
        self._lock = threading.Lock()

    @property
    def device(self) -> str:
        return self._device

    def load(self) -> None:
        with self._lock:
            if self._models is None:
                started = time.monotonic()
                self._models = self._loader(self._device)
                logger.info("Picture check loaded on %s in %.1f s.", self._device, time.monotonic() - started)

    def check(self, image: Any, subject: str | None, threshold: float) -> PictureVerdict:
        self.load()
        assert self._models is not None  # noqa: S101  # load() just set it
        started = time.monotonic()
        caption = self._models.caption(image)
        similarity = self._models.similarity(image, subject) if subject else None
        verdict = judge(caption, similarity, subject, threshold)
        logger.info(
            "Picture check: %s (similarity %s, %.1f s)%s",
            verdict.reason,
            "-" if similarity is None else f"{similarity:.3f}",
            time.monotonic() - started,
            f"; unsafe words: {', '.join(unsafe_terms(caption))}" if verdict.unsafe else "",
        )
        return verdict
