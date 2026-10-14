"""Input and output safety checks (plan §6.6).

Input (before a story is created), in order, stopping at the first block:
  1. rules (safety/rules.py), 2. Prompt Guard 2 (prompt injection), 3. gpt-oss-safeguard against policy.md.
Output (after planning): gpt-oss-safeguard on the finished story.

Fail closed: if a guard model is unavailable or out of quota, nothing is let through; callers turn the
exception into a "try again later" response.
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from storyweaver.domain.story import Story
from storyweaver.llm.client import ChatMessage, LLMClient
from storyweaver.llm.schemas import strict_json_schema
from storyweaver.safety.rules import check_rules

logger = logging.getLogger(__name__)

POLICY = (Path(__file__).parent / "policy.md").read_text(encoding="utf-8")

Stage = Literal["rules", "prompt_guard", "policy"]

INPUT_REJECTED_MESSAGE = "This story idea can't be used. Try a gentle, kind idea."
OUTPUT_REJECTED_MESSAGE = "This story didn't pass our safety check. Please try a different idea."


@dataclass(frozen=True)
class SafetyVerdict:
    allowed: bool
    stage: Stage | None = None
    category: str | None = None
    reason: str | None = None


ALLOWED = SafetyVerdict(allowed=True)


class SafetyCheckError(Exception):
    """A guard model returned something unusable. Treated as unavailable (fail closed)."""


class PolicyVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    violation: Literal[0, 1]
    category: str | None
    rationale: str


POLICY_VERDICT_SCHEMA = strict_json_schema(PolicyVerdict)


@dataclass(frozen=True)
class GuardModels:
    prompt_guard: str = "meta-llama/llama-prompt-guard-2-22m"
    safeguard: str = "openai/gpt-oss-safeguard-20b"
    prompt_guard_threshold: float = 0.5
    safeguard_max_tokens: int = 300


class PolicyChecker:
    def __init__(self, llm: LLMClient, models: GuardModels) -> None:
        self._llm = llm
        self._models = models

    async def check(self, content: str, *, kind: Literal["story idea", "finished story"]) -> SafetyVerdict:
        result = await self._llm.complete_json(
            model=self._models.safeguard,
            messages=[
                ChatMessage("system", POLICY),
                ChatMessage("user", f"Content type: {kind}\nContent to classify:\n{content}"),
            ],
            schema_name="policy_verdict",
            schema=POLICY_VERDICT_SCHEMA,
            max_tokens=self._models.safeguard_max_tokens,
            temperature=0.0,
        )
        try:
            verdict = PolicyVerdict.model_validate_json(result.content)
        except ValidationError as exc:
            raise SafetyCheckError("Safeguard returned an invalid verdict.") from exc
        if verdict.violation == 1:
            logger.info("Policy check rejected a %s (category=%s).", kind, verdict.category)
            return SafetyVerdict(False, "policy", verdict.category or "policy", verdict.rationale)
        return ALLOWED


class InputGuard:
    def __init__(self, llm: LLMClient, policy: PolicyChecker, models: GuardModels) -> None:
        self._llm = llm
        self._policy = policy
        self._models = models

    async def check(self, prompt: str) -> SafetyVerdict:
        rule = check_rules(prompt)
        if rule is not None:
            logger.info("Rules rejected a story idea (category=%s).", rule.category)
            return SafetyVerdict(False, "rules", rule.category, rule.reason)

        score = await self._injection_score(prompt)
        if score >= self._models.prompt_guard_threshold:
            logger.info("Prompt Guard flagged a story idea (score=%.3f).", score)
            return SafetyVerdict(False, "prompt_guard", "prompt_injection", "Possible prompt injection.")

        return await self._policy.check(prompt, kind="story idea")

    async def _injection_score(self, text: str) -> float:
        # Prompt Guard 2 returns the probability that the text is a prompt attack (context: 512 tokens; story
        # ideas are capped at 500 characters, well within it).
        result = await self._llm.complete_text(
            model=self._models.prompt_guard, messages=[ChatMessage("user", text)], max_tokens=16
        )
        try:
            return float(result.content.strip())
        except ValueError as exc:
            raise SafetyCheckError("Prompt Guard returned a non-numeric score.") from exc


class OutputGuard:
    def __init__(self, policy: PolicyChecker) -> None:
        self._policy = policy

    async def check(self, story: Story) -> SafetyVerdict:
        return await self._policy.check(story_for_review(story), kind="finished story")


def story_for_review(story: Story) -> str:
    """The text a reader sees. Image prompts are checked by the image safety stage (V3), keeping this call
    inside safeguard's 2,000-tokens-per-minute budget."""
    characters = "; ".join(f"{c.name} ({c.species}): {c.visual}" for c in story.characters)
    scenes = "\n\n".join(f"Scene {s.idx}: {s.text}" for s in story.scenes)
    header = f"Title: {story.title}\nMoral: {story.moral}\nSetting: {story.setting}"
    return f"{header}\nCharacters: {characters}\n\n{scenes}"
