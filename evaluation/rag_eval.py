"""V4 retrieval evaluation (plan §9 step 7, §10): does a child-style request find a passage of the right tale?

The gold set (`evaluation/data/rag_gold_v1.jsonl`) is 50 hand-written requests, 18 that name the tale's own
things ("the gingerbread man runs away") and 32 that describe it without its words ("a musician rids a town of
vermin ..."). A passage counts as **relevant** to a request when its text matches every pattern the request
lists (`all_of`, case-insensitive regular expressions). That is a judgement about the passage, not about a
book id or a title, so it holds for any edition and any chunking of the corpus. Three things follow:

- a request whose tale is **not in the corpus** (no passage matches) is left out of the scores and listed, so
  a thin corpus lowers the count of judged requests rather than the score;
- a request whose only matching passages are in an age band the story may not draw on is listed as **filtered
  by age band**, and also left out: that is the band policy working, and the list shows what it costs;
- the audit prints how many passages each request matches, so a pattern that is far too loose (or too tight)
  is visible before anyone trusts a number.

Scores (k = 5 by default):
- **recall@5** is the share of judged requests with at least one relevant passage in the top 5;
- **MRR@5** is the mean of 1/rank of the first relevant passage, counting 0 when it is not in the top 5.

The same requests are run through each retriever setup (hybrid search as the story path uses it, the same with
up to five passages per book, and with the re-ranker when `--reranker-dir` is given), so a setup earns its
place by the numbers. What this measures: whether the machinery finds a tale it is asked for by description.
It does not measure whether the retrieved tales make a story better; the with-and-without-RAG judge does.

Run from the repo root (needs the pgvector Postgres with the real chunks loaded, and the embedder files):
    uv run --directory backend python ../evaluation/rag_eval.py --database-url postgresql+asyncpg://...
Add `--reranker-dir D:/lenovo/models/bge-reranker` to include the re-ranker.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from sqlalchemy import select  # noqa: E402

from storyweaver.adapters.bge_embedder import BgeSmallEmbedder  # noqa: E402
from storyweaver.adapters.bge_reranker import BgeReranker  # noqa: E402
from storyweaver.config import Settings  # noqa: E402
from storyweaver.rag.retriever import HybridRetriever, RetrievalQuery, bands_for  # noqa: E402
from storyweaver.storage.db import create_engine, create_session_factory  # noqa: E402
from storyweaver.storage.models import TaleChunkRow  # noqa: E402

GOLD = ROOT / "evaluation" / "data" / "rag_gold_v1.jsonl"
RESULTS = ROOT / "evaluation" / "results" / "rag_eval_v1.jsonl"
SUMMARY = ROOT / "evaluation" / "results" / "rag_eval_v1.md"
TARGET = 0.8  # plan §10, V4 exit criterion: recall@5 >= 0.8
KINDS = ("named", "described")
TOO_LOOSE = 60  # a request matching more passages than this probably has a pattern that is too general


class GoldError(ValueError):
    """The gold file is malformed (the message says which line)."""


@dataclass(frozen=True)
class GoldQuery:
    id: str
    kind: str
    query: str
    patterns: tuple[re.Pattern[str], ...]

    def matches(self, text: str) -> bool:
        """True when `text` is a passage this request is about: every pattern is found in it."""
        return all(pattern.search(text) for pattern in self.patterns)


def parse_gold(lines: Sequence[str]) -> list[GoldQuery]:
    queries: list[GoldQuery] = []
    seen: set[str] = set()
    for number, line in enumerate(lines, start=1):
        if not line.strip() or line.startswith("#"):
            continue
        try:
            raw = json.loads(line)
            qid, kind, query, all_of = raw["id"], raw["kind"], raw["query"], raw["all_of"]
            if not isinstance(qid, str) or not qid or not isinstance(query, str) or not query.strip():
                raise ValueError("id and query must be non-empty strings")
            if kind not in KINDS:
                raise ValueError(f"kind must be one of {KINDS}")
            if (
                not isinstance(all_of, list)
                or not all_of
                or not all(isinstance(p, str) and p for p in all_of)
            ):
                raise ValueError("all_of must be a non-empty list of patterns")
            patterns = tuple(re.compile(p, re.IGNORECASE) for p in all_of)
        except (json.JSONDecodeError, KeyError, ValueError, re.error) as exc:
            raise GoldError(f"gold line {number}: {exc}") from exc
        if qid in seen:
            raise GoldError(f"gold line {number}: duplicate id {qid!r}")
        seen.add(qid)
        queries.append(GoldQuery(qid, kind, query.strip(), patterns))
    return queries


def load_gold(path: Path = GOLD) -> list[GoldQuery]:
    return parse_gold(path.read_text(encoding="utf-8").splitlines())


# ------------------------------------------------------------------------------------------------ the corpus


@dataclass(frozen=True)
class CorpusChunk:
    id: Any
    tale_id: str
    title: str
    age_band: str
    text: str


@dataclass(frozen=True)
class Judgement:
    """What the corpus says about one request: which passages are relevant, and whether it can be scored."""

    query: GoldQuery
    relevant: frozenset[Any]  # chunk ids relevant AND in an allowed band
    matching: int  # passages matching in any band
    tales: int  # distinct tales among the allowed-band matches

    @property
    def status(self) -> str:
        if self.matching == 0:
            return "not_in_corpus"
        if not self.relevant:
            return "filtered_by_age_band"
        return "judged"


def judge(queries: Sequence[GoldQuery], corpus: Sequence[CorpusChunk], age_band: str) -> list[Judgement]:
    allowed = set(bands_for(age_band))
    out: list[Judgement] = []
    for query in queries:
        matching = [chunk for chunk in corpus if query.matches(chunk.text)]
        reachable = [chunk for chunk in matching if chunk.age_band in allowed]
        out.append(
            Judgement(
                query=query,
                relevant=frozenset(chunk.id for chunk in reachable),
                matching=len(matching),
                tales=len({chunk.tale_id for chunk in reachable}),
            )
        )
    return out


# ------------------------------------------------------------------------------------------------- scoring


def first_relevant_rank(ranked_ids: Sequence[Any], relevant: frozenset[Any], k: int) -> int | None:
    """1-based rank of the first relevant passage within the top k, or None."""
    for rank, chunk_id in enumerate(ranked_ids[:k], start=1):
        if chunk_id in relevant:
            return rank
    return None


@dataclass
class Score:
    judged: int = 0
    hits: int = 0
    reciprocal_sum: float = 0.0
    first_place: int = 0

    def add(self, rank: int | None) -> None:
        self.judged += 1
        if rank is not None:
            self.hits += 1
            self.reciprocal_sum += 1 / rank
            self.first_place += rank == 1

    @property
    def recall(self) -> float:
        return self.hits / self.judged if self.judged else 0.0

    @property
    def mrr(self) -> float:
        return self.reciprocal_sum / self.judged if self.judged else 0.0


@dataclass
class ConfigResult:
    name: str
    seconds: float = 0.0
    overall: Score = field(default_factory=Score)
    by_kind: dict[str, Score] = field(default_factory=dict)
    ranks: dict[str, int | None] = field(default_factory=dict)
    top: dict[str, list[str]] = field(default_factory=dict)  # query id -> "title: first words" of the top 3

    def record(self, query: GoldQuery, rank: int | None, top: list[str]) -> None:
        self.overall.add(rank)
        self.by_kind.setdefault(query.kind, Score()).add(rank)
        self.ranks[query.id] = rank
        self.top[query.id] = top


# -------------------------------------------------------------------------------------------------- the run


def _gist(chunk: Any) -> str:
    return f"{chunk.title[:40]}: {' '.join(chunk.text.split()[:8])}"


async def evaluate(
    name: str,
    retriever: HybridRetriever,
    judgements: Sequence[Judgement],
    *,
    age_band: str,
    k: int,
) -> ConfigResult:
    result = ConfigResult(name)
    started = time.perf_counter()
    for judgement in judgements:
        if judgement.status != "judged":
            continue
        found = await retriever.search(RetrievalQuery(idea=judgement.query.query), age_band=age_band, limit=k)
        rank = first_relevant_rank([chunk.id for chunk in found], judgement.relevant, k)
        result.record(judgement.query, rank, [_gist(chunk) for chunk in found[:3]])
    result.seconds = time.perf_counter() - started
    return result


async def read_corpus(factory: Any) -> list[CorpusChunk]:
    async with factory() as session:
        rows = await session.execute(
            select(
                TaleChunkRow.id,
                TaleChunkRow.tale_id,
                TaleChunkRow.title,
                TaleChunkRow.age_band,
                TaleChunkRow.text,
            )
        )
        return [CorpusChunk(*row) for row in rows.all()]


def _percent(score: Score) -> str:
    return f"{score.recall:.0%} ({score.hits}/{score.judged})" if score.judged else "n/a"


def render_summary(
    judgements: Sequence[Judgement],
    results: Sequence[ConfigResult],
    *,
    corpus: Sequence[CorpusChunk],
    age_band: str,
    k: int,
) -> str:
    counts = Counter(j.status for j in judgements)
    judged = counts["judged"]
    tales = len({chunk.tale_id for chunk in corpus})
    default = results[0] if results else None
    complete = judged >= 0.6 * len(
        judgements
    )  # a number over a few survivors is not an exit-criterion answer
    if default is None or not judged:
        verdict = "NOT MEASURED"
    elif not complete:
        verdict = f"INCONCLUSIVE (only {judged} of {len(judgements)} requests are in the corpus)"
    else:
        verdict = "PASS" if default.overall.recall >= TARGET else "FAIL"
    lines = [
        "# V4 retrieval eval (gold set v1)",
        "",
        f"- **Result:** {verdict}; target recall@{k} >= {TARGET:.0%} for the story path's setup"
        + (f" ({default.name}: {default.overall.recall:.0%})" if default and judged else ""),
        f"- **Corpus:** {len(corpus)} passages from {tales} books; story age band {age_band!r}, "
        f"which may draw on {', '.join(bands_for(age_band))}",
        f"- **Requests:** {len(judgements)} in the gold set; **{judged} judged**, "
        f"{counts['not_in_corpus']} not in the corpus, "
        f"{counts['filtered_by_age_band']} only in a band the story may not use",
        f"- **Last updated:** {datetime.now(UTC).isoformat(timespec='seconds')}",
        "",
        f"## Scores over the {judged} judged requests",
        "",
        f"| Setup | recall@{k} | MRR@{k} | right passage first | named recall | described recall | seconds |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for result in results:
        named, described = (result.by_kind.get(kind, Score()) for kind in KINDS)
        lines.append(
            f"| {result.name} | {result.overall.recall:.0%} ({result.overall.hits}/{result.overall.judged}) "
            f"| {result.overall.mrr:.2f} | {result.overall.first_place} "
            f"| {_percent(named)} | {_percent(described)} | {result.seconds:.0f} |"
        )
    if default is not None:
        lines += ["", f"## Misses for {default.name!r}", ""]
        misses = [j for j in judgements if j.status == "judged" and default.ranks.get(j.query.id) is None]
        if not misses:
            lines.append("None.")
        for j in misses:
            lines.append(f"- **{j.query.id}** ({j.query.kind}) {j.query.query!r}")
            lines.append(f"  - relevant passages: {len(j.relevant)} in {j.tales} book(s); top 3 returned:")
            lines += [f"  - {gist}" for gist in default.top.get(j.query.id, [])]
    lines += [
        "",
        "## Audit: how many passages each request matches",
        "",
        "| Request | Status | Passages | Books |",
        "| --- | --- | --- | --- |",
    ]
    for j in judgements:
        flag = " (very loose pattern)" if j.matching > TOO_LOOSE else ""
        lines.append(f"| {j.query.id} {j.query.query[:60]!r} | {j.status}{flag} | {j.matching} | {j.tales} |")
    return "\n".join(lines) + "\n"


def write_results(
    judgements: Sequence[Judgement], results: Sequence[ConfigResult], summary: str, *, jsonl: Path, md: Path
) -> None:
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    with jsonl.open("w", encoding="utf-8", newline="\n") as handle:
        for j in judgements:
            row: dict[str, Any] = {
                "id": j.query.id,
                "kind": j.query.kind,
                "query": j.query.query,
                "status": j.status,
                "matching_passages": j.matching,
                "books": j.tales,
                "rank": {r.name: r.ranks.get(j.query.id) for r in results},
            }
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    md.write_text(summary, encoding="utf-8", newline="\n")


async def run(args: argparse.Namespace, database_url: str) -> int:
    gold = load_gold(args.gold)  # a malformed gold file stops the run before anything is loaded or connected
    default_dir = Settings.model_fields["embedder_dir"].default
    embedder = BgeSmallEmbedder(
        directory=args.embedder_dir or Path(os.getenv("EMBEDDER_DIR", str(default_dir)))
    )
    engine = create_engine(database_url)
    factory = create_session_factory(engine)
    try:
        corpus = await read_corpus(factory)
        if not corpus:
            print("tale_chunks is empty: load chunks first (data_pipeline/flows/load_chunks.py).")
            return 3
        judgements = judge(gold, corpus, args.age_band)
        setups: list[tuple[str, HybridRetriever]] = [
            ("hybrid, 1 passage per book", HybridRetriever(session_factory=factory, embedder=embedder)),
            (
                "hybrid, up to 5 per book",
                HybridRetriever(session_factory=factory, embedder=embedder, per_tale=5),
            ),
        ]
        if args.reranker_dir:
            reranker = BgeReranker(directory=args.reranker_dir)
            setups.append(
                (
                    "hybrid + re-ranker",
                    HybridRetriever(
                        session_factory=factory, embedder=embedder, reranker=reranker, fused_k=max(10, args.k)
                    ),
                )
            )
        results = []
        for name, retriever in setups:
            print(f"running: {name}")
            results.append(await evaluate(name, retriever, judgements, age_band=args.age_band, k=args.k))
    finally:
        await engine.dispose()
    summary = render_summary(judgements, results, corpus=corpus, age_band=args.age_band, k=args.k)
    write_results(judgements, results, summary, jsonl=args.out_jsonl, md=args.out_md)
    print(summary.split("## Misses")[0])
    print(f"Full report: {args.out_md}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database-url", default=None, help="default: DATABASE_URL from the environment")
    parser.add_argument("--embedder-dir", type=Path, default=None)
    parser.add_argument("--reranker-dir", type=Path, default=None, help="also evaluate with the re-ranker")
    parser.add_argument("--age-band", default="4-6")
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--gold", type=Path, default=GOLD)
    parser.add_argument("--out-jsonl", type=Path, default=RESULTS)
    parser.add_argument("--out-md", type=Path, default=SUMMARY)
    args = parser.parse_args(argv)
    database_url = args.database_url or os.getenv("DATABASE_URL")
    if not database_url:
        print("No database: pass --database-url or set DATABASE_URL.")
        return 2
    try:
        return asyncio.run(run(args, database_url))
    except GoldError as exc:
        print(f"Gold set problem: {exc}")
        return 4


if __name__ == "__main__":
    sys.exit(main())
