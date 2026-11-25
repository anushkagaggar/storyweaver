"""Cleaning pipeline: raw Project Gutenberg texts -> clean, scene-sized, graded chunks (plan §7, V4).

Input (written by the harvest step, slice 4):
    data_pipeline/data/raw/gutenberg/tales.jsonl        which tales (from gutenberg_catalogue.py)
    data_pipeline/data/raw/gutenberg/texts/<id>.txt     one raw ebook per tale, exactly as Gutenberg serves it
Output:
    data_pipeline/data/clean/gutenberg/chunks.jsonl     one chunk per line, ready to embed and load
    data_pipeline/data/clean/gutenberg/chunks.meta.json what each step dropped (the data card's numbers)

The steps, in order (plan §7; the numbers there are the ones in brackets):
 [1] Unicode NFKC, whitespace fixes, and the Gutenberg licence header/footer cut off. A text whose header
     marker cannot be found is skipped whole, never guessed at: the licence text must not leak into stories.
     Hard-wrapped lines are rejoined into paragraphs; headings, credits, tables of contents and [Illustration]
     tags are removed.
 [2] English only (a stop-word ratio test on each chunk; the catalogue already filtered by language).
 [3] Near-duplicate chunks removed with MinHash + banded LSH, estimated Jaccard >= 0.8. The first copy wins.
 [4] Flesch-Kincaid grade per chunk. Classic tales are *kept*, not cut at grade 4; the grade becomes the
     age band, so retrieval can choose.
 [5] A first safety screen: chunks with severe violence or adult content are dropped. This is a word-list
     screen only; the gpt-oss-safeguard sample check runs later (it needs Groq quota).
 [6] Split into scenes of 80-150 words, at sentence ends, preferring paragraph ends. Theme and moral tags come
     from gpt-oss-20b in a later, quota-paced step: `theme` and `moral` are empty here.

Run from backend/ (it uses the backend's venv and code):

    uv run python ../data_pipeline/flows/clean.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
import unicodedata
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend" / "src"))

from storyweaver.domain.readability import flesch_kincaid_grade  # noqa: E402

logger = logging.getLogger("clean")

# Bump when any rule below changes, so chunks.jsonl always says which rules made it.
CLEANING_VERSION = 1

RAW_DIR = REPO / "data_pipeline" / "data" / "raw" / "gutenberg"
CLEAN_DIR = REPO / "data_pipeline" / "data" / "clean" / "gutenberg"

MIN_WORDS = 80
MAX_WORDS = 150
PARAGRAPH_FLUSH_WORDS = MIN_WORDS  # a chunk ends at a paragraph end once it has at least this many words

MIN_ENGLISH_RATIO = 0.25  # share of words that are very common English words (real English prose: ~0.4-0.5)
MAX_WORDS_PER_SENTENCE = 50  # a "chunk" with longer sentences than this is a table, a list or verse
JACCARD_THRESHOLD = 0.8

# Flesch-Kincaid grade -> age band. "4-6" is the band the planner writes for (grade <= 4, see StoryPlanner);
# the harder bands exist so classic tales can be kept for retrieval and tagged, not thrown away.
BANDS = ((4.0, "4-6"), (7.0, "7-9"))
TOP_BAND = "10+"


class CleanError(Exception):
    """A raw text cannot be cleaned safely (reported per tale, never fatal to the run)."""


# ------------------------------------------------------------------------------------------ [1] text

_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿"))
_START = re.compile(
    r"^[ \t]*\*{3}[ \t]*START OF (?:THE|THIS) PROJECT GUTENBERG EBOOK[^\n]*", re.IGNORECASE | re.MULTILINE
)
_END = re.compile(
    r"^[ \t]*\*{3}[ \t]*END OF (?:THE|THIS) PROJECT GUTENBERG EBOOK[^\n]*", re.IGNORECASE | re.MULTILINE
)
# Older files (before ~2002) use other markers.
_START_OLD = re.compile(r"^[ \t]*\*END\*THE SMALL PRINT[^\n]*", re.IGNORECASE | re.MULTILINE)
_END_OLD = re.compile(
    r"^[ \t]*End of (?:the )?Project Gutenberg(?:'s)? (?:Etext|EBook)", re.IGNORECASE | re.MULTILINE
)
_BRACKET_NOTE = re.compile(r"\[(?:Illustration|Transcriber|Footnote|Note)[^\]]*\]", re.IGNORECASE)
_FOOTNOTE_MARK = re.compile(r"\[\d{1,3}\]")
_ITALIC = re.compile(r"(?<!\w)_([^_\n]+)_(?!\w)")
_CREDIT = re.compile(
    r"^(?:produced by|transcribed|transcriber|e-?text prepared|this e-?book|updated|release date|language:|"
    r"character set|credits|illustrated by|illustrations? by|images? (?:generously )?(?:provided|courtesy)|"
    r"the online distributed)",
    re.IGNORECASE,
)
_HEADING_END = re.compile(r"[.!?…\"”’']$")  # noqa: RUF001 (curly quotes)
_SENTENCE_MARK = re.compile(r"[.!?]")


def normalise(text: str) -> str:
    """Unicode NFKC, one kind of line break and space, no zero-width characters, no markup noise."""
    text = unicodedata.normalize("NFKC", text).translate(_ZERO_WIDTH)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\f", "\n\n")
    text = _BRACKET_NOTE.sub(" ", text)  # [Illustration: ...] may span lines
    text = _FOOTNOTE_MARK.sub("", text)
    text = _ITALIC.sub(r"\1", text)  # _word_ marks italics in plain-text ebooks
    text = text.replace("--", "—")
    return re.sub(r"[ \t]+", " ", text)


def strip_boilerplate(text: str) -> str | None:
    """The ebook's own text, between Gutenberg's header and footer; None when the header is not found."""
    start = _START.search(text) or _START_OLD.search(text)
    if start is None:
        return None
    body = text[start.end() :]
    end = _END.search(body) or _END_OLD.search(body)
    return body[: end.start()] if end else body


def paragraphs(body: str) -> list[str]:
    """Hard-wrapped lines joined into paragraphs, without headings, credits or lists that are not prose."""
    found: list[str] = []
    for block in re.split(r"\n\s*\n", body):
        paragraph = " ".join(block.split())
        if not paragraph:
            continue
        words = paragraph.split()
        if len(found) < 8 and _CREDIT.match(paragraph):
            continue
        if len(words) <= 10 and not _HEADING_END.search(paragraph):
            continue  # "CHAPTER I", "THE FROG PRINCE", "Contents"
        if len(words) >= 25 and not _SENTENCE_MARK.search(paragraph):
            continue  # a table of contents or a list of names, run together
        found.append(paragraph)
    return found


# -------------------------------------------------------------------------------------- [6] scenes

_BOUNDARY = re.compile(r"[.!?]+[\"”’')\]]*\s+")  # noqa: RUF001 (curly quotes)
_ABBREVIATIONS = frozenset(
    "mr mrs ms dr st mt prof capt col gen sir vs etc no vol viz".split()
)  # a full stop after these does not end a sentence


def split_sentences(paragraph: str) -> list[str]:
    parts: list[str] = []
    start = 0
    for match in _BOUNDARY.finditer(paragraph):
        before = paragraph[start : match.start()].split()
        word = before[-1] if before else ""
        if match.group(0).startswith(".") and (
            word.lower() in _ABBREVIATIONS or (len(word) == 1 and word.isupper())
        ):
            continue  # "Mr. Fox", "J. M. Barrie"
        following = paragraph[match.end() : match.end() + 1]
        if following.islower():
            continue  # "...!" he said": the sentence goes on
        parts.append(paragraph[start : match.end()].strip())
        start = match.end()
    tail = paragraph[start:].strip()
    if tail:
        parts.append(tail)
    return parts


@dataclass(frozen=True)
class _Sentence:
    text: str
    words: int
    ends_paragraph: bool


def _words(sentences: Iterable[_Sentence]) -> int:
    return sum(s.words for s in sentences)


def split_scenes(paragraph_list: list[str]) -> list[str]:
    """Scene-sized chunks: 80-150 words, ending at sentence ends (and paragraph ends where possible).

    Exceptions: a tale shorter than 80 words is one chunk, and one sentence longer than 150 words is its own
    chunk (the prose check then drops it). No word is lost or repeated.
    """
    units: list[_Sentence] = []
    for paragraph in paragraph_list:
        sentences = split_sentences(paragraph)
        for index, sentence in enumerate(sentences):
            units.append(_Sentence(sentence, len(sentence.split()), index == len(sentences) - 1))

    chunks: list[list[_Sentence]] = []
    current: list[_Sentence] = []
    for unit in units:
        if current and _words(current) + unit.words > MAX_WORDS and _words(current) >= MIN_WORDS:
            chunks.append(current)
            current = []
        current.append(unit)
        if unit.ends_paragraph and _words(current) >= PARAGRAPH_FLUSH_WORDS:
            chunks.append(current)
            current = []
    if current:
        chunks.append(current)

    if len(chunks) >= 2 and _words(chunks[-1]) < MIN_WORDS:  # a short tail: merge it, or share it out evenly
        pool = chunks[-2] + chunks[-1]
        if _words(pool) <= MAX_WORDS:
            chunks[-2:] = [pool]
        else:
            cut = min(range(1, len(pool)), key=lambda k: abs(_words(pool[:k]) - _words(pool[k:])))
            chunks[-2:] = [pool[:cut], pool[cut:]]
    return [" ".join(s.text for s in chunk) for chunk in chunks]


# ----------------------------------------------------------------------------------- [2] [4] [5] checks

_STOPWORDS = frozenset(
    "the and of to a in that is was he she it for with as his her on be at by had they not but are from or "
    "this have an said which you were all there their so if what when one them would who been will no out up "
    "him me my we do can then into could than its has more some other only now over like our your".split()
)
_WORD = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")


def english_ratio(text: str) -> float:
    words = [w.lower() for w in _WORD.findall(text)]
    return sum(w in _STOPWORDS for w in words) / len(words) if words else 0.0


def looks_like_english(text: str) -> bool:
    return english_ratio(text) >= MIN_ENGLISH_RATIO


def looks_like_prose(text: str) -> bool:
    """Sentences of a believable length, made of words (not a table of contents, a list, or verse)."""
    sentences = split_sentences(text)
    return bool(sentences) and len(text.split()) / len(sentences) <= MAX_WORDS_PER_SENTENCE


def age_band_for(grade: float) -> str:
    for limit, band in BANDS:
        if grade <= limit:
            return band
    return TOP_BAND


# Dropped on one hit: violence beyond mild, and adult content.
_SEVERE = re.compile(
    r"\b(?:murder\w*|slaughter\w*|massacre\w*|stab(?:bed|bing|s)?|behead\w*|beheaded|corpses?|tortur\w+|"
    r"gallows|hanged|bloodshed|bloody|butcher\w*|suicide|rape[sd]?|whores?|brothel\w*|disembowel\w*)\b",
    re.IGNORECASE,
)
# Fine alone, a worry in numbers: a chunk with this many is mostly about death.
_MILD = re.compile(
    r"\b(?:kill\w*|dead|die[ds]?|dying|blood|wound\w*|slay\w*|slain|grave|coffin)\b", re.IGNORECASE
)
MILD_LIMIT = 3


def is_unsafe(text: str) -> bool:
    return bool(_SEVERE.search(text)) or len(_MILD.findall(text)) >= MILD_LIMIT


def leaks_licence(text: str) -> bool:
    return "project gutenberg" in text.lower()


# ------------------------------------------------------------------------------------------ [3] duplicates

_SHINGLE_WORDS = 5
_NUM_PERM = 64
_BANDS = (
    8  # 8 bands of 8 rows: a pair of chunks at Jaccard 0.8 shares a band with probability ~0.84, 0.9 -> ~0.99
)
_ROWS = _NUM_PERM // _BANDS
_SEED = 20261125


def _mix(x: NDArray[np.uint64]) -> NDArray[np.uint64]:
    """splitmix64's finaliser: scrambles 64-bit numbers (arithmetic wraps around, as intended)."""
    x = x ^ (x >> np.uint64(30))
    x = x * np.uint64(0xBF58476D1CE4E5B9)
    x = x ^ (x >> np.uint64(27))
    x = x * np.uint64(0x94D049BB133111EB)
    return x ^ (x >> np.uint64(31))


def _shingles(text: str) -> NDArray[np.uint64]:
    words = [w.lower() for w in _WORD.findall(text)]
    grams = [" ".join(words[i : i + _SHINGLE_WORDS]) for i in range(max(1, len(words) - _SHINGLE_WORDS + 1))]
    return np.array(
        [int.from_bytes(hashlib.blake2b(g.encode(), digest_size=8).digest(), "little") for g in set(grams)],
        dtype=np.uint64,
    )


class NearDuplicateIndex:
    """MinHash signatures with banded locality-sensitive hashing: finds chunks that are nearly the same text.

    Each chunk becomes 64 numbers such that two chunks agree on a share of them equal to their Jaccard
    similarity (the share of 5-word phrases they have in common). Chunks that agree on a whole band of 8
    numbers become candidates, so a new chunk is compared with a few, not with all. Deterministic.
    """

    def __init__(self, threshold: float = JACCARD_THRESHOLD) -> None:
        self._threshold = threshold
        self._seeds = np.random.default_rng(_SEED).integers(0, 2**63, size=_NUM_PERM, dtype=np.uint64)
        self._signatures: list[NDArray[np.uint32]] = []
        self._buckets: dict[tuple[int, bytes], list[int]] = {}

    def _signature(self, text: str) -> NDArray[np.uint32]:
        mixed = _mix(_shingles(text)[:, None] ^ self._seeds[None, :])
        return mixed.min(axis=0).astype(np.uint32)

    def add_if_new(self, text: str) -> bool:
        """Remember `text` and return True, unless a near-duplicate was already seen (then False)."""
        signature = self._signature(text)
        keys = [(b, signature[b * _ROWS : (b + 1) * _ROWS].tobytes()) for b in range(_BANDS)]
        candidates = {i for key in keys for i in self._buckets.get(key, ())}
        if any(float(np.mean(self._signatures[i] == signature)) >= self._threshold for i in candidates):
            return False
        index = len(self._signatures)
        self._signatures.append(signature)
        for key in keys:
            self._buckets.setdefault(key, []).append(index)
        return True


# ---------------------------------------------------------------------------------------------- the flow


@dataclass(frozen=True)
class Chunk:
    tale_id: str
    position: int
    title: str
    licence: str
    text: str
    grade: float

    def to_record(self) -> dict[str, object]:
        return {
            "source": "gutenberg",
            "tale_id": self.tale_id,
            "position": self.position,
            "title": self.title[:200],
            "theme": None,  # tagged later by gpt-oss-20b
            "moral": None,
            "age_band": age_band_for(self.grade),
            "grade": round(self.grade, 2),
            "licence": self.licence,
            "text": self.text,
            "words": len(self.text.split()),
            "cleaning_version": CLEANING_VERSION,
        }


def clean_text(raw: str) -> list[str]:
    """One raw ebook -> its scene-sized chunks (steps [1] and [6]); CleanError when it cannot be cleaned."""
    body = strip_boilerplate(normalise(raw))
    if body is None:
        raise CleanError("no Project Gutenberg start marker")
    scenes = split_scenes(paragraphs(body))
    if not scenes:
        raise CleanError("no prose left after cleaning")
    return scenes


def drop_reason(scene: str, index: NearDuplicateIndex) -> str | None:
    """Why this chunk must not be kept, or None. Cheap checks first; the duplicate check runs last and is the
    only one that remembers the chunk, so a chunk dropped for another reason never shadows a later copy."""
    if leaks_licence(scene):
        return "dropped_licence_text"
    if not looks_like_english(scene):
        return "dropped_not_english"
    if not looks_like_prose(scene):
        return "dropped_not_prose"
    if is_unsafe(scene):
        return "dropped_unsafe"
    if not index.add_if_new(scene):
        return "dropped_near_duplicate"
    return None


def clean_corpus(raw_dir: Path, out_dir: Path) -> Counter[str]:
    tales = [json.loads(line) for line in (raw_dir / "tales.jsonl").read_text(encoding="utf-8").splitlines()]
    index = NearDuplicateIndex()
    counts: Counter[str] = Counter()
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "chunks.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
        for tale in sorted(tales, key=lambda t: t["gutenberg_id"]):
            counts["tales_in"] += 1
            path = raw_dir / "texts" / f"{tale['gutenberg_id']}.txt"
            if not path.exists():
                counts["tales_no_text_file"] += 1
                continue
            try:
                scenes = clean_text(path.read_text(encoding="utf-8-sig", errors="replace"))
            except CleanError as exc:
                logger.warning("%s skipped: %s", tale["tale_id"], exc)
                counts["tales_skipped_" + ("no_marker" if "marker" in str(exc) else "no_prose")] += 1
                continue
            counts["tales_cleaned"] += 1
            for position, scene in enumerate(scenes):
                counts["chunks_split"] += 1
                reason = drop_reason(scene, index)
                if reason:
                    counts[reason] += 1
                    continue
                chunk = Chunk(
                    tale_id=tale["tale_id"],
                    position=position,
                    title=tale["title"],
                    licence=tale["licence"],
                    text=scene,
                    grade=flesch_kincaid_grade(scene),
                )
                handle.write(json.dumps(chunk.to_record(), ensure_ascii=False) + "\n")
                counts["chunks_kept"] += 1
                counts["band_" + age_band_for(chunk.grade)] += 1
    meta = {
        "source": "gutenberg",
        "cleaning_version": CLEANING_VERSION,
        "tales_file_sha256": hashlib.sha256((raw_dir / "tales.jsonl").read_bytes()).hexdigest(),
        "counts": dict(sorted(counts.items())),
        "words_per_chunk": [MIN_WORDS, MAX_WORDS],
        "min_english_ratio": MIN_ENGLISH_RATIO,
        "jaccard_threshold": JACCARD_THRESHOLD,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    (out_dir / "chunks.meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Clean raw Gutenberg texts into scene-sized chunks.")
    parser.add_argument("--raw", type=Path, default=RAW_DIR, help="folder with tales.jsonl and texts/")
    parser.add_argument("--out", type=Path, default=CLEAN_DIR, help="output folder")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if not (args.raw / "tales.jsonl").exists() or not (args.raw / "texts").is_dir():
        logger.error(
            "Nothing to clean: %s needs tales.jsonl and a texts/ folder (run the harvest first).", args.raw
        )
        return 4
    counts = clean_corpus(args.raw, args.out)
    for name, number in sorted(counts.items()):
        logger.info("%-28s %d", name, number)
    return 0


if __name__ == "__main__":
    sys.exit(main())
