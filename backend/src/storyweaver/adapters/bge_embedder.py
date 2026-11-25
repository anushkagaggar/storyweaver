"""bge-small-en-v1.5 implementation of `Embedder` on onnxruntime (CPU, no torch).

Weights: BAAI/bge-small-en-v1.5 (MIT, 33M parameters, 384-number vectors), the ONNX export from the model's
own repository. The files are fetched from one pinned git revision (so "latest" can never change them under
us), checked against SHA-256 digests, and cached in EMBEDDER_DIR. They download on first use, not at startup,
so cold starts stay fast. Embedding runs in a thread, one batch at a time, like Kokoro.

How bge-small turns text into numbers (the model card's recipe, which the retrieval quality depends on):
the tokenizer splits text into at most 512 word-pieces; the network produces one vector per piece; the
vector of the first piece (`[CLS]`) stands for the whole text; it is scaled to length 1, so the cosine
distance pgvector computes is a plain dot product. Queries get a fixed instruction in front; passages don't.
"""

import asyncio
import hashlib
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import numpy as np
from numpy.typing import NDArray

from storyweaver.rag.embeddings import EmbeddingError, EmbeddingUnavailableError

logger = logging.getLogger(__name__)

MODEL_NAME = "bge-small-en-v1.5"
DIMENSION = 384
MAX_TOKENS = 512
# The model card's instruction for short search queries; passages are embedded as they are.
QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "

# One immutable revision of BAAI/bge-small-en-v1.5 (checked 25 Nov 2026).
REVISION = "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a"  # pragma: allowlist secret (a public git commit)
BASE_URL = f"https://huggingface.co/BAAI/bge-small-en-v1.5/resolve/{REVISION}"


@dataclass(frozen=True)
class PinnedFile:
    path: str  # path inside the model repository
    sha256: str

    @property
    def name(self) -> str:
        return Path(self.path).name


# Public file checksums, not secrets (hence the allowlist pragmas for detect-secrets). The model's is the
# digest Hugging Face publishes for its LFS file; the tokenizer's is measured from the pinned revision.
MODEL_FILE = PinnedFile(
    "onnx/model.onnx",
    "828e1496d7fabb79cfa4dcd84fa38625c0d3d21da474a00f08db0f559940cf35",  # pragma: allowlist secret
)
TOKENIZER_FILE = PinnedFile(
    "tokenizer.json",
    "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66",  # pragma: allowlist secret
)

# What the loader returns: (tokenizer, onnxruntime session). Injected in tests.
ModelLoader = Callable[[], tuple[Any, Any]]


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def ensure_file(directory: Path, spec: PinnedFile, *, base_url: str = BASE_URL) -> Path:
    """Download `spec` into `directory` once, verifying its SHA-256. A bad download is deleted."""
    target = directory / spec.name
    if target.exists() and sha256_of(target) == spec.sha256:
        return target
    directory.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".part")
    logger.info("Downloading %s ...", spec.name)
    with httpx.stream("GET", f"{base_url}/{spec.path}", follow_redirects=True, timeout=120.0) as response:
        response.raise_for_status()
        with partial.open("wb") as handle:
            for block in response.iter_bytes(1 << 20):
                handle.write(block)
    if sha256_of(partial) != spec.sha256:
        partial.unlink(missing_ok=True)
        raise EmbeddingUnavailableError(f"{spec.name} failed its SHA-256 check.")
    partial.replace(target)
    return target


def cls_pool_and_normalise(hidden: NDArray[np.float32]) -> NDArray[np.float32]:
    """(batch, tokens, 384) network output -> (batch, 384) vectors of length 1, from each first token."""
    first = hidden[:, 0, :]
    lengths = np.linalg.norm(first, axis=1, keepdims=True)
    return (first / np.maximum(lengths, 1e-12)).astype(np.float32)


def load_onnx_model(directory: Path) -> tuple[Any, Any]:
    try:
        import onnxruntime
        from tokenizers import Tokenizer
    except ImportError as exc:
        raise EmbeddingUnavailableError("onnxruntime or tokenizers is not installed.") from exc
    model = ensure_file(directory, MODEL_FILE)
    tokenizer = Tokenizer.from_file(str(ensure_file(directory, TOKENIZER_FILE)))
    tokenizer.enable_truncation(max_length=MAX_TOKENS)
    tokenizer.enable_padding()  # to the longest text in each batch; the attention mask hides the padding
    session = onnxruntime.InferenceSession(str(model), providers=["CPUExecutionProvider"])
    return tokenizer, session


class BgeSmallEmbedder:
    name = MODEL_NAME
    dimension = DIMENSION

    def __init__(self, *, directory: Path, batch_size: int = 32, loader: ModelLoader | None = None) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        self._batch_size = batch_size
        self._loader = loader or (lambda: load_onnx_model(directory))
        self._loaded: tuple[Any, Any] | None = None
        self._lock = asyncio.Lock()

    async def _model(self) -> tuple[Any, Any]:
        if self._loaded is None:
            async with self._lock:
                if self._loaded is None:
                    started = time.perf_counter()
                    try:
                        self._loaded = await asyncio.to_thread(self._loader)
                    except EmbeddingError:
                        raise
                    except Exception as exc:  # download or load failure
                        raise EmbeddingUnavailableError(
                            f"Could not load {MODEL_NAME}: {type(exc).__name__}"
                        ) from exc
                    logger.info("Loaded %s in %.1f s.", MODEL_NAME, time.perf_counter() - started)
        return self._loaded

    def _run(self, tokenizer: Any, session: Any, texts: list[str]) -> NDArray[np.float32]:
        encodings = tokenizer.encode_batch(texts)
        inputs = {
            "input_ids": np.array([e.ids for e in encodings], dtype=np.int64),
            "attention_mask": np.array([e.attention_mask for e in encodings], dtype=np.int64),
        }
        if "token_type_ids" in {i.name for i in session.get_inputs()}:
            inputs["token_type_ids"] = np.zeros_like(inputs["input_ids"])
        hidden = np.asarray(session.run(None, inputs)[0], dtype=np.float32)
        if hidden.ndim != 3 or hidden.shape[2] != DIMENSION:
            raise EmbeddingUnavailableError(
                f"{MODEL_NAME} returned shape {hidden.shape}, expected (batch, tokens, {DIMENSION})."
            )
        return cls_pool_and_normalise(hidden)

    async def _embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        tokenizer, session = await self._model()
        vectors: list[list[float]] = []
        async with self._lock:  # one batch at a time on the shared CPU
            for start in range(0, len(texts), self._batch_size):
                batch = list(texts[start : start + self._batch_size])
                try:
                    result = await asyncio.to_thread(self._run, tokenizer, session, batch)
                except EmbeddingError:
                    raise
                except Exception as exc:
                    raise EmbeddingUnavailableError(f"{MODEL_NAME} failed: {type(exc).__name__}") from exc
                vectors.extend(result.tolist())
        return vectors

    async def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        return await self._embed(texts)

    async def embed_query(self, text: str) -> list[float]:
        return (await self._embed([QUERY_INSTRUCTION + text]))[0]
