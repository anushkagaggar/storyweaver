"""Load cleaned tale chunks (`chunks.jsonl`) into `tale_chunks`, with their embeddings (V4, plan §7 and §9).

The loader is built to be run again and again, because the corpus is big (hundreds of thousands of chunks, a
couple of hours of CPU to embed) and runs get interrupted:

- **Idempotent.** The key is (tale_id, position). Running the same file twice changes nothing.
- **Cheap to resume.** Before embedding, a batch is compared with what the table already holds. A chunk
  whose text is unchanged is never embedded again: if its labels (theme, moral, age band, ...) changed, only
  those are updated; if nothing changed, nothing is written. Killing a run at 80% and starting it again
  embeds only the remaining 20%.
- **Strict about bad lines.** A line that is not valid JSON, misses a field, has an unknown age band, or
  repeats a key is skipped, counted and reported (never half-loaded), so the caller can fail loudly.
- **Stale chunks go only on request.** If a tale is re-cleaned into fewer chunks, the leftover positions stay
  until `prune=True`, and pruning is refused when any line was rejected (so a bad file cannot delete good
  rows) or when `limit` cut the run short.

Passages are embedded as they are (no title prefix, no instruction): bge's recipe puts the instruction on the
*query* side only.
"""

import json
import logging
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import Table, bindparam, delete, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from storyweaver.rag.embeddings import Embedder
from storyweaver.storage.models import EMBEDDING_DIM, TaleChunkRow

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 256
MAX_REJECTIONS_SHOWN = 10

# The columns that describe a chunk but are not its text. Changing one never needs a new embedding.
LABEL_FIELDS = ("source", "title", "theme", "moral", "age_band", "grade", "licence")


class ChunkRecord(BaseModel):
    """One line of chunks.jsonl, checked against the limits of the `tale_chunks` columns."""

    # `words`, `cleaning_version` and any later additions are lineage for the file, not for the table.
    model_config = ConfigDict(frozen=True, extra="ignore")

    source: Literal["gutenberg", "standard_ebooks", "tinystories"]
    tale_id: str = Field(min_length=1, max_length=96)
    position: int = Field(ge=0)
    title: str = Field(min_length=1, max_length=200)
    theme: str | None = Field(default=None, max_length=40)
    moral: str | None = Field(default=None, max_length=200)
    age_band: Literal["4-6", "7-9", "10+"]
    grade: float | None = None
    licence: str = Field(min_length=1, max_length=32)
    text: str = Field(min_length=1)

    @field_validator("text")
    @classmethod
    def _text_is_storable(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("text contains a NUL character, which Postgres cannot store")
        if not value.strip():
            raise ValueError("text is blank")
        return value

    @property
    def key(self) -> tuple[str, int]:
        return (self.tale_id, self.position)

    def labels(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in LABEL_FIELDS}


@dataclass
class LoadReport:
    lines: int = 0  # non-blank lines read
    rejected: int = 0
    rejections: list[str] = field(default_factory=list)  # the first few, as "line 12: reason"
    inserted: int = 0  # new chunks (embedded)
    replaced: int = 0  # same key, different text (embedded again)
    retagged: int = 0  # same text, different labels (no embedding needed)
    unchanged: int = 0  # nothing to do
    pruned: int = 0
    prune_skipped_because: str | None = None

    @property
    def embedded(self) -> int:
        return self.inserted + self.replaced

    @property
    def loaded(self) -> int:
        return self.inserted + self.replaced + self.retagged + self.unchanged


class LoadError(Exception):
    """The loader cannot run at all (as opposed to a bad line, which is only counted)."""


def read_chunks(path: Path) -> Iterator[tuple[int, ChunkRecord | str]]:
    """Yield (line number, record) for each good line and (line number, reason) for each bad one."""
    with path.open(encoding="utf-8") as handle:
        for number, raw in enumerate(handle, start=1):
            if not raw.strip():
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                yield number, f"not valid JSON ({exc.msg})"
                continue
            if not isinstance(payload, dict):
                yield number, "not a JSON object"
                continue
            try:
                yield number, ChunkRecord.model_validate(payload)
            except ValidationError as exc:
                first = exc.errors()[0]
                where = ".".join(str(part) for part in first["loc"]) or "record"
                yield number, f"{where}: {first['msg']}"


async def _existing(session: AsyncSession, keys: Sequence[tuple[str, int]]) -> dict[tuple[str, int], Any]:
    columns = (
        TaleChunkRow.tale_id,
        TaleChunkRow.position,
        TaleChunkRow.text,
        *(getattr(TaleChunkRow, name) for name in LABEL_FIELDS),
    )
    rows = await session.execute(
        select(*columns).where(tuple_(TaleChunkRow.tale_id, TaleChunkRow.position).in_(list(keys)))
    )
    return {(row.tale_id, row.position): row for row in rows}


async def _load_batch(
    session_factory: async_sessionmaker[AsyncSession],
    embedder: Embedder,
    batch: list[ChunkRecord],
    report: LoadReport,
) -> None:
    async with session_factory() as session, session.begin():
        existing = await _existing(session, [record.key for record in batch])

        to_embed: list[ChunkRecord] = []
        is_new: list[bool] = []
        relabel: list[dict[str, Any]] = []
        for record in batch:
            row = existing.get(record.key)
            if row is None:
                to_embed.append(record)
                is_new.append(True)
            elif row.text != record.text:
                to_embed.append(record)
                is_new.append(False)
            elif any(getattr(row, name) != value for name, value in record.labels().items()):
                relabel.append(
                    {"b_tale_id": record.tale_id, "b_position": record.position, **record.labels()}
                )
            else:
                report.unchanged += 1

        if to_embed:
            vectors = await embedder.embed_passages([record.text for record in to_embed])
            if len(vectors) != len(to_embed) or any(len(vector) != EMBEDDING_DIM for vector in vectors):
                raise LoadError(
                    f"{embedder.name} returned vectors that do not match the {EMBEDDING_DIM}-wide column."
                )
            rows = [
                {
                    "id": uuid.uuid4(),
                    "tale_id": record.tale_id,
                    "position": record.position,
                    "text": record.text,
                    "embedding": vector,
                    **record.labels(),
                }
                for record, vector in zip(to_embed, vectors, strict=True)
            ]
            statement = pg_insert(TaleChunkRow)
            statement = statement.on_conflict_do_update(
                constraint="uq_tale_chunks_tale_id_position",
                # `tsv` follows `text` by itself; `id` and `created_at` stay as they were.
                set_={name: statement.excluded[name] for name in ("text", "embedding", *LABEL_FIELDS)},
            )
            await session.execute(statement, rows)
            report.inserted += sum(is_new)
            report.replaced += len(is_new) - sum(is_new)

        if relabel:
            connection = await session.connection()
            table = cast(Table, TaleChunkRow.__table__)
            await connection.execute(
                update(table)
                .where(table.c.tale_id == bindparam("b_tale_id"), table.c.position == bindparam("b_position"))
                .values({name: bindparam(name) for name in LABEL_FIELDS}),
                relabel,
            )
            report.retagged += len(relabel)


async def _prune(
    session_factory: async_sessionmaker[AsyncSession], kept: dict[str, set[int]], report: LoadReport
) -> None:
    async with session_factory() as session, session.begin():
        for tale_id, positions in kept.items():
            result = await session.execute(
                delete(TaleChunkRow).where(
                    TaleChunkRow.tale_id == tale_id, TaleChunkRow.position.not_in(sorted(positions))
                )
            )
            report.pruned += result.rowcount or 0  # type: ignore[attr-defined]


def _duplicate_reason(record: ChunkRecord, seen: dict[str, set[int]]) -> str:
    if record.position in seen.get(record.tale_id, ()):
        return f"{record.tale_id} position {record.position} appears more than once"
    return ""


async def load_chunks(
    path: Path,
    *,
    embedder: Embedder,
    session_factory: async_sessionmaker[AsyncSession],
    batch_size: int = DEFAULT_BATCH_SIZE,
    limit: int | None = None,
    prune: bool = False,
    progress: Callable[[LoadReport], None] | None = None,
) -> LoadReport:
    """Load `path` into `tale_chunks`. Returns what happened; bad lines are in the report, not raised."""
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    if embedder.dimension != EMBEDDING_DIM:
        raise LoadError(
            f"{embedder.name} writes {embedder.dimension} numbers per text but tale_chunks.embedding holds "
            f"{EMBEDDING_DIM}. Changing the embedder needs a migration and a full reload."
        )

    report = LoadReport()
    seen: dict[str, set[int]] = {}
    batch: list[ChunkRecord] = []
    accepted = 0

    async def flush() -> None:
        if batch:
            await _load_batch(session_factory, embedder, batch, report)
            batch.clear()
            if progress:
                progress(report)

    for number, item in read_chunks(path):
        report.lines += 1
        reason = item if isinstance(item, str) else _duplicate_reason(item, seen)
        if reason or isinstance(item, str):
            report.rejected += 1
            if len(report.rejections) < MAX_REJECTIONS_SHOWN:
                report.rejections.append(f"line {number}: {reason}")
            continue
        if limit is not None and accepted >= limit:
            break
        seen.setdefault(item.tale_id, set()).add(item.position)
        batch.append(item)
        accepted += 1
        if len(batch) >= batch_size:
            await flush()
    await flush()

    if prune:
        if report.rejected:
            report.prune_skipped_because = "some lines were rejected"
        elif limit is not None:
            report.prune_skipped_because = "the run was limited"
        else:
            await _prune(session_factory, seen, report)
    return report
