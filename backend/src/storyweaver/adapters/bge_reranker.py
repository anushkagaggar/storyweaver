"""bge-reranker-base implementation of `Reranker` on onnxruntime (CPU, no torch).

Weights: BAAI/bge-reranker-base (MIT, 278M parameters, XLM-RoBERTa), as an int8-quantised ONNX export by the
Hugging Face `Xenova` account (279 MB, against 1.1 GB for the full-precision file; the original model is
MIT-licensed and the export is not gated). Like the embedder, the files come from one pinned git revision,
are checked against SHA-256 digests, and download on first use into RERANKER_DIR.

How it scores: the tokenizer joins the query and a passage into one sequence (`<s> query </s></s> passage
</s>`, at most 512 word-pieces, the passage is the part that gets cut); the network reads the whole pair and
outputs one number (a "logit"): higher means the passage answers the query better. The numbers have no fixed
scale; they only order the passages of one query.
"""

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from storyweaver.adapters.bge_embedder import PinnedFile, ensure_file
from storyweaver.rag.reranking import RerankError, RerankUnavailableError

logger = logging.getLogger(__name__)

MODEL_NAME = "bge-reranker-base"
MAX_TOKENS = 512

# One immutable revision of Xenova/bge-reranker-base (checked 26 Nov 2026).
REVISION = "280bcc27a84e0b898c251e06fddb25171bd9b101"  # pragma: allowlist secret (a public git commit)
BASE_URL = f"https://huggingface.co/Xenova/bge-reranker-base/resolve/{REVISION}"

# Public file checksums, not secrets (hence the allowlist pragmas for detect-secrets): the digests Hugging
# Face publishes for these LFS files at the revision above.
MODEL_FILE = PinnedFile(
    "onnx/model_int8.onnx",
    "2059d8ef0b6e935b4845e11b38c9af9e9e2e7b91f69fc99efe03254e0a7da8d3",  # pragma: allowlist secret
)
TOKENIZER_FILE = PinnedFile(
    "tokenizer.json",
    "48564c5c7d3fa64d85d95e65414a542385f88b0f128fd8d4163fd7a57f2be05c",  # pragma: allowlist secret
)

# What the loader returns: (tokenizer, onnxruntime session). Injected in tests.
ModelLoader = Callable[[], tuple[Any, Any]]


def load_onnx_model(directory: Path, model_file: PinnedFile = MODEL_FILE) -> tuple[Any, Any]:
    try:
        import onnxruntime
        from tokenizers import Tokenizer
    except ImportError as exc:
        raise RerankUnavailableError("onnxruntime or tokenizers is not installed.") from exc
    model = ensure_file(directory, model_file, base_url=BASE_URL)
    tokenizer = Tokenizer.from_file(str(ensure_file(directory, TOKENIZER_FILE, base_url=BASE_URL)))
    tokenizer.enable_truncation(max_length=MAX_TOKENS)
    pad_id = tokenizer.token_to_id("<pad>")  # XLM-RoBERTa pads with id 1, not 0
    tokenizer.enable_padding(pad_id=1 if pad_id is None else pad_id, pad_token="<pad>")  # noqa: S106
    session = onnxruntime.InferenceSession(str(model), providers=["CPUExecutionProvider"])
    return tokenizer, session


class BgeReranker:
    name = MODEL_NAME

    def __init__(
        self,
        *,
        directory: Path,
        batch_size: int = 16,
        loader: ModelLoader | None = None,
        model_file: PinnedFile = MODEL_FILE,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        self._batch_size = batch_size
        self._loader = loader or (lambda: load_onnx_model(directory, model_file))
        self._loaded: tuple[Any, Any] | None = None
        self._lock = asyncio.Lock()

    async def _model(self) -> tuple[Any, Any]:
        if self._loaded is None:
            async with self._lock:
                if self._loaded is None:
                    started = time.perf_counter()
                    try:
                        self._loaded = await asyncio.to_thread(self._loader)
                    except RerankError:
                        raise
                    except Exception as exc:  # download or load failure
                        raise RerankUnavailableError(
                            f"Could not load {MODEL_NAME}: {type(exc).__name__}"
                        ) from exc
                    logger.info("Loaded %s in %.1f s.", MODEL_NAME, time.perf_counter() - started)
        return self._loaded

    @staticmethod
    def _run(tokenizer: Any, session: Any, query: str, passages: list[str]) -> NDArray[np.float32]:
        encodings = tokenizer.encode_batch([(query, passage) for passage in passages])
        inputs = {
            "input_ids": np.array([e.ids for e in encodings], dtype=np.int64),
            "attention_mask": np.array([e.attention_mask for e in encodings], dtype=np.int64),
        }
        if "token_type_ids" in {i.name for i in session.get_inputs()}:
            inputs["token_type_ids"] = np.zeros_like(inputs["input_ids"])
        logits = np.asarray(session.run(None, inputs)[0], dtype=np.float32)
        if logits.ndim == 2 and logits.shape[1] == 1:
            logits = logits[:, 0]
        if logits.shape != (len(passages),):
            raise RerankUnavailableError(
                f"{MODEL_NAME} returned shape {logits.shape}, expected one score per passage."
            )
        return logits

    async def rerank(self, query: str, passages: Sequence[str]) -> list[float]:
        if not passages:
            return []
        tokenizer, session = await self._model()
        scores: list[float] = []
        async with self._lock:  # one batch at a time on the shared CPU
            for start in range(0, len(passages), self._batch_size):
                batch = list(passages[start : start + self._batch_size])
                try:
                    result = await asyncio.to_thread(self._run, tokenizer, session, query, batch)
                except RerankError:
                    raise
                except Exception as exc:
                    raise RerankUnavailableError(f"{MODEL_NAME} failed: {type(exc).__name__}") from exc
                scores.extend(result.tolist())
        return scores
