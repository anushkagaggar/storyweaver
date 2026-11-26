"""chunks.jsonl -> embeddings -> the `tale_chunks` table (plan §7 and §9, V4).

The last step of the data pipeline: it reads the cleaned chunks, embeds them with bge-small on your CPU, and
upserts them into Postgres. It is safe to run again and to interrupt (see `storyweaver/rag/loader.py`):
unchanged chunks are never embedded twice.

Run from backend/ (it uses the backend's venv and code):

    uv run python ../data_pipeline/flows/load_chunks.py --limit 200      # a small trial first
    uv run python ../data_pipeline/flows/load_chunks.py                  # everything
    uv run python ../data_pipeline/flows/load_chunks.py --prune          # also drop chunks no longer in the file

The target database is DATABASE_URL (from the environment or .env), or `--database-url`. The host and database
name are printed before anything is written. The first run downloads the bge-small files (about 130 MB) into
EMBEDDER_DIR and checks their SHA-256.

Exit codes: 0 ok; 2 no database URL; 3 chunks file missing; 5 some lines were rejected (the good ones are
loaded); 6 the embedder or database could not be used.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend" / "src"))

from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.exc import SQLAlchemyError  # noqa: E402

from storyweaver.adapters.bge_embedder import BgeSmallEmbedder  # noqa: E402
from storyweaver.config import Settings  # noqa: E402
from storyweaver.rag.embeddings import EmbeddingError  # noqa: E402
from storyweaver.rag.loader import DEFAULT_BATCH_SIZE, LoadError, LoadReport, load_chunks  # noqa: E402
from storyweaver.storage.db import create_engine, create_session_factory, normalize_database_url  # noqa: E402

logger = logging.getLogger("load_chunks")

DEFAULT_CHUNKS = REPO / "data_pipeline" / "data" / "clean" / "gutenberg" / "chunks.jsonl"


def _settings() -> Settings | None:
    """The app's settings (it reads .env), or None when required ones such as the API key are missing."""
    try:
        return Settings()  # type: ignore[call-arg]
    except Exception:  # any config problem just means "use the plain defaults"
        return None


def describe_target(database_url: str) -> str:
    """'host:port/database', never the password."""
    url = make_url(normalize_database_url(database_url))
    return f"{url.host}:{url.port or 5432}/{url.database}"


def _print_report(report: LoadReport) -> None:
    logger.info("lines read        %d", report.lines)
    logger.info("rejected          %d", report.rejected)
    for reason in report.rejections:
        logger.info("  %s", reason)
    logger.info("inserted          %d (embedded)", report.inserted)
    logger.info("replaced          %d (text changed, embedded again)", report.replaced)
    logger.info("retagged          %d (labels changed, not embedded)", report.retagged)
    logger.info("unchanged         %d", report.unchanged)
    logger.info("pruned            %d", report.pruned)
    if report.prune_skipped_because:
        logger.warning("prune skipped: %s", report.prune_skipped_because)


async def run(args: argparse.Namespace, database_url: str) -> LoadReport:
    settings = _settings()
    directory = args.embedder_dir or (settings.embedder_dir if settings else Settings.model_fields["embedder_dir"].default)
    embedder = BgeSmallEmbedder(
        directory=directory, batch_size=settings.embedder_batch_size if settings else 32
    )
    engine = create_engine(database_url)
    started = time.perf_counter()

    def progress(report: LoadReport) -> None:
        done = report.loaded
        logger.info("%d chunks done (%d embedded) in %.0f s", done, report.embedded, time.perf_counter() - started)

    try:
        return await load_chunks(
            args.chunks,
            embedder=embedder,
            session_factory=create_session_factory(engine),
            batch_size=args.batch_size,
            limit=args.limit,
            prune=args.prune,
            progress=progress,
        )
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Embed cleaned chunks and load them into tale_chunks.")
    parser.add_argument("--chunks", type=Path, default=DEFAULT_CHUNKS, help="the chunks.jsonl to load")
    parser.add_argument("--database-url", default=None, help="default: DATABASE_URL from the environment or .env")
    parser.add_argument("--embedder-dir", type=Path, default=None, help="where the bge-small files live")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="chunks per transaction")
    parser.add_argument("--limit", type=int, default=None, help="load only the first N good chunks (a trial)")
    parser.add_argument("--prune", action="store_true", help="delete rows of these tales that are not in the file")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    settings = _settings()
    database_url = args.database_url or os.getenv("DATABASE_URL") or (settings.database_url if settings else None)
    if not database_url:
        logger.error("No database: set DATABASE_URL (e.g. in .env) or pass --database-url.")
        return 2
    if not args.chunks.is_file():
        logger.error("%s does not exist. Run the cleaning step first.", args.chunks)
        return 3

    logger.info("Loading %s into %s", args.chunks, describe_target(database_url))
    try:
        report = asyncio.run(run(args, database_url))
    except (LoadError, EmbeddingError, SQLAlchemyError, OSError) as exc:
        logger.error("Could not load: %s", exc)
        return 6
    _print_report(report)
    return 5 if report.rejected else 0


if __name__ == "__main__":
    sys.exit(main())
