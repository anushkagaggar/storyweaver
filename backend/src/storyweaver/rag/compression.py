"""Context compression (V4, plan §9 step 5): each retrieved passage becomes a short inspiration note.

Three passages of about 120 words are 500 tokens of prompt and an invitation to copy. Three notes of about 40
words are a third of that, and they carry what a story can borrow (who, what happens, how it turns out), not
the wording. One batched call to gpt-oss-120b summarises whatever is not cached yet; its separate free-tier
quota keeps the planner's gpt-oss-20b budget for planning.

Summaries are cached in Redis, one key per passage: `rag:summary:v<version>:<chunk id>:<text hash>`. The text
hash matters because the loader keeps a chunk's id when its text changes, and the version is bumped when this
prompt or its limits change, so a stale note is never served; an old key just expires.

It degrades, never fails. When the cache is down the passage is treated as a miss; when the model is
rate-limited, out of quota, down or answers badly, the note is the passage's own first sentences cut to the
same length (an "extract", not cached, so the next retrieval tries the model again). A model note that copies
eight or more words in a row from the passage is treated as bad too: "inspiration, don't copy" starts here.

Economics, honestly (measured live, 27 Nov 2026): a 97-word passage is about 130 tokens and its note about
50, so a cache hit saves roughly 80 tokens per passage; a miss costs about 300 tokens per passage (907 for a
batch of three). A passage must therefore be served about four times from the cache before compression has
paid for itself in tokens. Over a big corpus that reuse may never come, so the case for compression is mostly
the other one: a short, own-words note nudges the planner to borrow ideas, not wording. If the token budget
matters more, warm the cache offline for the corpus (gpt-oss-120b has its own free pool); the cache key and
`compress` work for either.
"""

import hashlib
import json
import logging
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from storyweaver.llm.client import ChatMessage, LLMClient, LLMError
from storyweaver.llm.schemas import strict_json_schema
from storyweaver.rag.retriever import RetrievedChunk
from storyweaver.routing.router import QuotaExhaustedError

logger = logging.getLogger(__name__)

_PROMPT = Path(__file__).resolve().parents[1] / "llm" / "prompts" / "context_compression.md"
SCHEMA_NAME = "passage_summaries"

SUMMARY_VERSION = 1  # bump when the prompt or the limits below change
MAX_WORDS = 45  # what the prompt asks for
HARD_MAX_WORDS = 70  # a longer note is not a compression: treated as bad
COPY_RUN = 8  # a note repeating this many words in a row from its passage is a copy, not a summary
CACHE_TTL_SECONDS = 90 * 24 * 3600
BASE_TOKENS = 400  # headroom for the model's reasoning ...
TOKENS_PER_PASSAGE = 120  # ... plus room for each note

Source = Literal["cache", "model", "extract"]

_CURLY_QUOTES = {0x2018: "'", 0x2019: "'"}
_UNWANTED = re.compile(r"[^\w\s'\-,.;:!?]|_")
_SPACES = re.compile(r"\s+")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_WORD = re.compile(r"[a-z0-9']+")


class _Note(BaseModel):
    model_config = ConfigDict(extra="forbid")

    n: int
    summary: str


class _Notes(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summaries: list[_Note]


NOTES_SCHEMA = strict_json_schema(_Notes)


# ----------------------------------------------------------------------------------------------- the cache


def cache_key(chunk: RetrievedChunk) -> str:
    digest = hashlib.sha256(chunk.text.encode("utf-8")).hexdigest()[:16]
    return f"rag:summary:v{SUMMARY_VERSION}:{chunk.id}:{digest}"


class SummaryCache(Protocol):
    async def get_many(self, keys: Sequence[str]) -> dict[str, str]: ...

    async def set_many(self, items: Mapping[str, str]) -> None: ...


class RedisSummaryCache:
    """One MGET to read and one pipelined round trip to write (Upstash's free tier counts commands)."""

    def __init__(self, redis: Redis, *, ttl_seconds: int = CACHE_TTL_SECONDS) -> None:
        self._redis = redis
        self._ttl = ttl_seconds

    async def get_many(self, keys: Sequence[str]) -> dict[str, str]:
        if not keys:
            return {}
        values = await self._redis.mget(list(keys))
        return {
            key: value.decode("utf-8") if isinstance(value, bytes) else str(value)
            for key, value in zip(keys, values, strict=True)
            if value is not None
        }

    async def set_many(self, items: Mapping[str, str]) -> None:
        if not items:
            return
        async with self._redis.pipeline(transaction=False) as pipe:
            for key, value in items.items():
                pipe.set(key, value, ex=self._ttl)
            await pipe.execute()


class InMemorySummaryCache:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get_many(self, keys: Sequence[str]) -> dict[str, str]:
        return {key: self.data[key] for key in keys if key in self.data}

    async def set_many(self, items: Mapping[str, str]) -> None:
        self.data.update(items)


# ----------------------------------------------------------------------------------------- text helpers


def _words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def copies_source(summary: str, source: str, run: int = COPY_RUN) -> bool:
    """True when `summary` repeats `run` or more consecutive words of `source`."""
    source_words = _words(source)
    grams = {tuple(source_words[i : i + run]) for i in range(len(source_words) - run + 1)}
    summary_words = _words(summary)
    return any(tuple(summary_words[i : i + run]) in grams for i in range(len(summary_words) - run + 1))


def extract(text: str, max_words: int = MAX_WORDS) -> str:
    """The passage's first sentences, up to `max_words` (the fallback when no model note is available)."""
    taken: list[str] = []
    count = 0
    for sentence in _SENTENCE_END.split(_SPACES.sub(" ", text).strip()):
        size = len(sentence.split())
        if taken and count + size > max_words:
            break
        if not taken and size > max_words:
            return " ".join(sentence.split()[:max_words]) + "..."
        taken.append(sentence)
        count += size
    return " ".join(taken)


def _clean(note: str) -> str:
    note = note.translate(_CURLY_QUOTES)  # a curly apostrophe is an apostrophe, not noise
    return _SPACES.sub(" ", _UNWANTED.sub(" ", note)).strip()


# --------------------------------------------------------------------------------------------- the work


@dataclass(frozen=True)
class CompressedChunk:
    chunk: RetrievedChunk
    summary: str
    source: Source


@dataclass(frozen=True)
class Compression:
    items: list[CompressedChunk]
    tokens: int = 0  # free-tier tokens the model call used
    model_calls: int = 0

    def count(self, source: Source) -> int:
        return sum(item.source == source for item in self.items)


class ContextCompressor:
    def __init__(
        self,
        llm: LLMClient,
        cache: SummaryCache,
        *,
        model: str = "openai/gpt-oss-120b",
        max_words: int = MAX_WORDS,
    ) -> None:
        self._llm = llm
        self._cache = cache
        self._model = model
        self._max_words = max_words
        self._system = _PROMPT.read_text(encoding="utf-8").replace("{max_words}", str(max_words))

    async def compress(self, chunks: Sequence[RetrievedChunk]) -> Compression:
        """A note for every chunk, in the same order: from the cache, the model, or an extract."""
        keys = {chunk.id: cache_key(chunk) for chunk in chunks}
        cached = await self._read_cache(list(dict.fromkeys(keys.values())))
        missing = [c for c in dict.fromkeys(chunks) if keys[c.id] not in cached]
        fresh, tokens, calls = await self._summarise(missing) if missing else ({}, 0, 0)
        if fresh:
            await self._write_cache({keys[chunk_id]: note for chunk_id, note in fresh.items()})
        items: list[CompressedChunk] = []
        for chunk in chunks:
            if keys[chunk.id] in cached:
                items.append(CompressedChunk(chunk, cached[keys[chunk.id]], "cache"))
            elif chunk.id in fresh:
                items.append(CompressedChunk(chunk, fresh[chunk.id], "model"))
            else:
                items.append(CompressedChunk(chunk, extract(chunk.text, self._max_words), "extract"))
        return Compression(items, tokens, calls)

    async def _read_cache(self, keys: list[str]) -> dict[str, str]:
        try:
            return await self._cache.get_many(keys)
        except (RedisError, OSError) as exc:
            logger.warning("Summary cache unreadable (%s); treating every passage as new.", exc)
            return {}

    async def _write_cache(self, items: Mapping[str, str]) -> None:
        try:
            await self._cache.set_many(items)
        except (RedisError, OSError) as exc:
            logger.warning("Summary cache not written (%s); the notes are used this time only.", exc)

    async def _summarise(self, chunks: Sequence[RetrievedChunk]) -> tuple[dict[uuid.UUID, str], int, int]:
        """Notes by chunk id for the passages the model summarised well; the rest are left to `extract`."""
        passages = "\n\n".join(
            f'<passage n="{n}">\n{chunk.text}\n</passage>' for n, chunk in enumerate(chunks, start=1)
        )
        messages = [ChatMessage("system", self._system), ChatMessage("user", passages)]
        try:
            result = await self._llm.complete_json(
                model=self._model,
                messages=messages,
                schema_name=SCHEMA_NAME,
                schema=NOTES_SCHEMA,
                max_tokens=BASE_TOKENS + TOKENS_PER_PASSAGE * len(chunks),
                temperature=0.0,
            )
        except (LLMError, QuotaExhaustedError) as exc:
            logger.warning("Context compression skipped (%s); using extracts.", exc)
            return {}, 0, 0
        tokens = result.prompt_tokens + result.completion_tokens
        try:
            notes = _Notes.model_validate(json.loads(result.content)).summaries
        except (json.JSONDecodeError, ValidationError) as exc:
            logger.warning("Context compression gave unusable output (%s); using extracts.", exc)
            return {}, tokens, 1
        by_number: dict[int, str] = {}
        for note in notes:
            by_number.setdefault(note.n, _clean(note.summary))  # the first note for a number wins
        good: dict[uuid.UUID, str] = {}
        for n, chunk in enumerate(chunks, start=1):
            summary = by_number.get(n, "")
            problem = self._problem(summary, chunk.text)
            if problem:
                logger.warning("Note for passage %d rejected (%s); using an extract.", n, problem)
            else:
                good[chunk.id] = summary
        return good, tokens, 1

    def _problem(self, summary: str, source: str) -> str:
        if not summary:
            return "missing or empty"
        if len(summary.split()) > HARD_MAX_WORDS:
            return "too long to be a summary"
        if copies_source(summary, source):
            return f"copies {COPY_RUN}+ words of the passage"
        return ""
