"""Project Gutenberg catalogue -> the list of children's tales worth harvesting (plan §7, V4).

Gutenberg publishes its whole catalogue as one CSV (about 15 MB, rebuilt daily). This script downloads it
once, keeps the English children's tales, and writes them as a JSONL list for the harvest step (slice 3).
It never touches the website's pages: the catalogue feed and, later, `/robot/harvest` are the routes Gutenberg
documents for programs, and its search pages block bots.

Being a good guest:
- **One request per run, and usually none.** The CSV is cached; a copy under 7 days old is used as it is, and
  an older one is re-checked with `If-None-Match`, so an unchanged catalogue costs a 304 and no download.
- **When the site says "not now", stop.** A 429/503 with `Retry-After` ends the run with that time shown;
  there is no retry loop. (On 25 Nov 2026 the site answered 503 "down for maintenance", Retry-After 6 h.)
- **Offline works.** `--catalogue PATH` reads a CSV you already have and makes no request at all.

Run from backend/ (it uses the backend's venv):

    uv run python ../data_pipeline/flows/gutenberg_catalogue.py
    uv run python ../data_pipeline/flows/gutenberg_catalogue.py --catalogue D:/lenovo/data/pg_catalog.csv

Outputs, in data_pipeline/data/raw/gutenberg/ (git-ignored; DVC tracks it from slice 3):
    pg_catalog.csv       the downloaded catalogue (+ pg_catalog.etag)
    tales.jsonl          one selected tale per line
    tales.meta.json      the catalogue's SHA-256, row counts and the selection rules' version (lineage)
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[2]
logger = logging.getLogger("gutenberg_catalogue")

CATALOGUE_URL = "https://www.gutenberg.org/cache/epub/feeds/pg_catalog.csv"
USER_AGENT = "StoryWeaver-corpus-builder/0.1 (non-commercial learning project; cached, one request per run)"
DEFAULT_OUT = REPO / "data_pipeline" / "data" / "raw" / "gutenberg"
MAX_AGE_DAYS = 7

# Bump when the rules below change, so a tales.jsonl always says which rules made it.
SELECTION_RULES_VERSION = 1

# The columns the selection reads. The catalogue is read by column name, so a reordered file still works and a
# renamed column stops the run with a clear message instead of selecting from the wrong data.
REQUIRED_COLUMNS = ("Text#", "Type", "Title", "Language", "Authors", "Subjects", "Bookshelves")

# A book is a candidate tale when its Subjects or Bookshelves mention one of these (case-insensitive).
TALE_MARKERS = (
    "fairy tales",
    "fables",
    "folklore",
    "folk tales",
    "children's stories",
    "children's fiction",
    "children's literature",
    "children's myths",
    "juvenile fiction",
)
# ...unless it is a book *about* tales rather than tales themselves.
EXCLUDED_MARKERS = ("history and criticism", "criticism and interpretation", "bibliography")

# Gutenberg's books are public domain in the United States; some are not elsewhere. The licence says so.
LICENCE = "public-domain-us"


class CatalogueFormatError(Exception):
    """The catalogue lacks a column this script needs; Gutenberg changed its format."""


class SiteBusyError(Exception):
    """Gutenberg answered 429 or 503: it asked us to come back later."""

    def __init__(self, status: int, retry_after_seconds: int | None) -> None:
        when = (
            f"after {retry_after_seconds} s ({retry_after_seconds / 3600:.1f} h)"
            if retry_after_seconds
            else "later"
        )
        super().__init__(f"Project Gutenberg answered {status}; try again {when}.")
        self.status = status
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class Book:
    gutenberg_id: int
    kind: str  # "Text", "Sound", ...
    title: str
    languages: tuple[str, ...]
    authors: tuple[str, ...]
    subjects: tuple[str, ...]
    bookshelves: tuple[str, ...]
    issued: str

    @property
    def tale_id(self) -> str:
        return f"gutenberg:{self.gutenberg_id}"

    def to_record(self) -> dict[str, object]:
        return {
            "source": "gutenberg",
            "tale_id": self.tale_id,
            "gutenberg_id": self.gutenberg_id,
            "title": self.title,
            "authors": list(self.authors),
            "subjects": list(self.subjects),
            "bookshelves": list(self.bookshelves),
            "issued": self.issued,
            "licence": LICENCE,
        }


def _split(value: str) -> tuple[str, ...]:
    """Catalogue lists ("a; b; c") become tuples."""
    return tuple(part.strip() for part in value.split(";") if part.strip())


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


# ---------------------------------------------------------------------------------------------- download


def _retry_after(response: httpx.Response) -> int | None:
    value = response.headers.get("retry-after", "").strip()
    return int(value) if value.isdigit() else None  # an HTTP-date form is rare here; treat it as unknown


def fetch_catalogue(
    dest: Path,
    *,
    refresh: bool = False,
    max_age_days: float = MAX_AGE_DAYS,
    client: httpx.Client | None = None,
    now: Callable[[], float] = time.time,
) -> Path:
    """Make sure `dest` holds a recent catalogue, asking Gutenberg for as little as possible."""
    etag_file = dest.with_suffix(".etag")
    if dest.exists() and not refresh and now() - dest.stat().st_mtime < max_age_days * 86_400:
        logger.info("Using the cached catalogue (%s).", dest)
        return dest

    headers = {"User-Agent": USER_AGENT}
    if dest.exists() and etag_file.exists():
        headers["If-None-Match"] = etag_file.read_text(encoding="utf-8").strip()

    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_suffix(".part")
    own_client = client is None
    http = client or httpx.Client(timeout=120.0, follow_redirects=True)
    try:
        with http.stream("GET", CATALOGUE_URL, headers=headers) as response:
            if response.status_code == 304:
                os.utime(dest)  # still current: restart the freshness clock
                logger.info("The catalogue has not changed (304).")
                return dest
            if response.status_code in (429, 503):
                raise SiteBusyError(response.status_code, _retry_after(response))
            response.raise_for_status()
            with partial.open("wb") as handle:
                for block in response.iter_bytes(1 << 20):
                    handle.write(block)
            etag = response.headers.get("etag")
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    finally:
        if own_client:
            http.close()
    partial.replace(dest)
    if etag:
        etag_file.write_text(etag, encoding="utf-8")
    else:
        etag_file.unlink(missing_ok=True)
    return dest


# -------------------------------------------------------------------------------------------- selection


def read_catalogue(path: Path) -> Iterator[Book]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        missing = [column for column in REQUIRED_COLUMNS if column not in (reader.fieldnames or [])]
        if missing:
            raise CatalogueFormatError(
                f"{path.name} has no column {missing}; its columns are {reader.fieldnames}. "
                "Gutenberg may have changed the format: check the file, then update REQUIRED_COLUMNS."
            )
        for row in reader:
            try:
                number = int(row["Text#"])
            except (TypeError, ValueError):
                continue  # a blank or damaged row
            yield Book(
                gutenberg_id=number,
                kind=(row["Type"] or "").strip(),
                title=" ".join((row["Title"] or "").split()),  # titles can hold line breaks
                languages=_split(row["Language"] or ""),
                authors=_split(row["Authors"] or ""),
                subjects=_split(row["Subjects"] or ""),
                bookshelves=_split(row["Bookshelves"] or ""),
                issued=(row.get("Issued") or "").strip(),
            )


def is_children_tale(book: Book) -> bool:
    """An English text whose subjects or bookshelves name a tale genre, and which isn't about tales."""
    if book.kind != "Text" or book.languages != ("en",) or not book.title:
        return False
    labels = [label.lower() for label in (*book.subjects, *book.bookshelves)]
    if any(marker in label for label in labels for marker in EXCLUDED_MARKERS):
        return False
    return any(marker in label for label in labels for marker in TALE_MARKERS)


def select_tales(books: Iterator[Book]) -> list[Book]:
    return sorted((book for book in books if is_children_tale(book)), key=lambda book: book.gutenberg_id)


def write_selection(tales: list[Book], out_dir: Path, *, catalogue: Path, catalogue_rows: int) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    tales_path = out_dir / "tales.jsonl"
    with tales_path.open("w", encoding="utf-8", newline="\n") as handle:
        for book in tales:
            handle.write(json.dumps(book.to_record(), ensure_ascii=False) + "\n")
    meta = {
        "source": "gutenberg",
        "catalogue_file": catalogue.name,
        "catalogue_sha256": sha256_of(catalogue),
        "catalogue_rows": catalogue_rows,
        "selected": len(tales),
        "selection_rules_version": SELECTION_RULES_VERSION,
        "tale_markers": list(TALE_MARKERS),
        "excluded_markers": list(EXCLUDED_MARKERS),
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    (out_dir / "tales.meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return tales_path


def run(out_dir: Path, *, catalogue: Path | None, refresh: bool) -> int:
    path = catalogue or fetch_catalogue(out_dir / "pg_catalog.csv", refresh=refresh)
    books = list(read_catalogue(path))
    tales = select_tales(iter(books))
    tales_path = write_selection(tales, out_dir, catalogue=path, catalogue_rows=len(books))
    logger.info("%d tales selected from %d catalogue rows -> %s", len(tales), len(books), tales_path)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0] if __doc__ else None)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output folder")
    parser.add_argument("--catalogue", type=Path, help="use this CSV instead of downloading (no request)")
    parser.add_argument(
        "--refresh", action="store_true", help="re-check with Gutenberg even if the copy is fresh"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        return run(args.out, catalogue=args.catalogue, refresh=args.refresh)
    except SiteBusyError as exc:
        logger.error("%s", exc)
        return 2
    except CatalogueFormatError as exc:
        logger.error("%s", exc)
        return 3


if __name__ == "__main__":
    sys.exit(main())
