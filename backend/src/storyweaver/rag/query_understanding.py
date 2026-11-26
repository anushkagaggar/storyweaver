"""Query understanding (V4, plan §9 step 2): turn a parent's story idea into search aids for the retriever.

One small strict-JSON call to gpt-oss-20b returns `{themes[], characters[], moral, setting}`; they are added
to the search text (`RetrievalQuery.search_text`). It names the idea behind a request ("fear of the dark",
"courage") that a tale on the shelf may express in other words, which both searches then have a chance to hit.

It is an enhancement, never a dependency. Any failure (rate limit, no quota, provider down, malformed or
nonsensical output) gives back the plain idea with nothing added, so the story still gets written. The call
costs about 550 free-tier tokens of the planner model's daily budget, so a story that cannot afford it simply
goes without. The idea is untrusted text: the prompt says so, and the answer is only ever used as search text
(never shown, never run), after being cleaned and capped here.
"""

import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

from storyweaver.llm.client import ChatMessage, LLMClient, LLMError
from storyweaver.llm.schemas import strict_json_schema
from storyweaver.rag.retriever import RetrievalQuery
from storyweaver.routing.router import QuotaExhaustedError

logger = logging.getLogger(__name__)

_PROMPT = Path(__file__).resolve().parents[1] / "llm" / "prompts" / "query_understanding.md"
SCHEMA_NAME = "retrieval_query"

MAX_IDEA_CHARS = 1000  # an idea is a sentence or two; more is not sent (and not paid for)
MAX_THEMES = 4
MAX_CHARACTERS = 4
MAX_ITEM_CHARS = 40
MAX_MORAL_CHARS = 200
MAX_SETTING_CHARS = 100
DEFAULT_MAX_TOKENS = 400  # headroom for the model's reasoning as well as the short answer

_UNWANTED = re.compile(r"[^\w\s'\-,.]|_")  # anything that is not a word, space, apostrophe, hyphen or , .
_SPACES = re.compile(r"\s+")


class UnderstoodQuery(BaseModel):
    """What the model returns (the strict schema is built from this)."""

    model_config = ConfigDict(extra="forbid")

    themes: list[str]
    characters: list[str]
    moral: str
    setting: str


UNDERSTOOD_QUERY_SCHEMA = strict_json_schema(UnderstoodQuery)


def _clean(value: str, limit: int) -> str:
    cleaned = _SPACES.sub(" ", _UNWANTED.sub(" ", value)).strip()
    return cleaned[:limit].rstrip()


def _clean_items(values: Iterable[str], count: int) -> list[str]:
    seen: set[str] = set()
    items: list[str] = []
    for value in values:
        item = _clean(value, MAX_ITEM_CHARS)
        if item and item.lower() not in seen:
            seen.add(item.lower())
            items.append(item)
        if len(items) == count:
            break
    return items


@dataclass(frozen=True)
class Understanding:
    query: RetrievalQuery
    understood: bool  # False: the model could not help and `query` is just the idea
    tokens: int = 0  # what the call cost, for the quota bookkeeping and the evaluation
    reason: str = ""  # why it fell back


class QueryUnderstanding:
    def __init__(
        self,
        llm: LLMClient,
        *,
        model: str,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = 0.0,
    ) -> None:
        self._llm = llm
        self._model = model
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._system = _PROMPT.read_text(encoding="utf-8")

    async def understand(self, idea: str) -> Understanding:
        """The retrieval query for `idea`: enriched when the model answers, the plain idea when it cannot."""
        idea = idea.strip()
        plain = RetrievalQuery(idea=idea)
        if not idea:
            return Understanding(plain, False, reason="empty idea")
        messages = [
            ChatMessage("system", self._system),
            ChatMessage("user", f"Story idea from the parent:\n<idea>\n{idea[:MAX_IDEA_CHARS]}\n</idea>"),
        ]
        try:
            result = await self._llm.complete_json(
                model=self._model,
                messages=messages,
                schema_name=SCHEMA_NAME,
                schema=UNDERSTOOD_QUERY_SCHEMA,
                max_tokens=self._max_tokens,
                temperature=self._temperature,
            )
        except (LLMError, QuotaExhaustedError) as exc:
            logger.warning("Query understanding skipped (%s); searching with the plain idea.", exc)
            return Understanding(plain, False, reason=type(exc).__name__)
        tokens = result.prompt_tokens + result.completion_tokens
        try:
            understood = UnderstoodQuery.model_validate(json.loads(result.content))
        except (json.JSONDecodeError, ValidationError) as exc:
            logger.warning("Query understanding gave unusable output (%s); using the plain idea.", exc)
            return Understanding(plain, False, tokens, reason="unusable output")
        query = RetrievalQuery(
            idea=idea,
            themes=_clean_items(understood.themes, MAX_THEMES),
            characters=_clean_items(understood.characters, MAX_CHARACTERS),
            moral=_clean(understood.moral, MAX_MORAL_CHARS),
            setting=_clean(understood.setting, MAX_SETTING_CHARS),
        )
        if not (query.themes or query.characters or query.moral or query.setting):
            return Understanding(plain, False, tokens, reason="nothing extracted")
        return Understanding(query, True, tokens)
