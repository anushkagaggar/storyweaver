"""Story planner (plan §6.1): one strict-JSON call, then at most one repair call to the teacher model."""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from storyweaver.domain.story import (
    MAX_SCENE_WORDS,
    MIN_SCENE_WORDS,
    AgeBand,
    SceneCount,
    Story,
    story_problems,
)
from storyweaver.llm.client import ChatMessage, LLMClient, LLMResult
from storyweaver.llm.schemas import PLANNED_STORY_SCHEMA, PlannedStory

logger = logging.getLogger(__name__)

_PROMPTS = Path(__file__).parent / "prompts"
_SCHEMA_NAME = "story"


@dataclass(frozen=True)
class StoryRequest:
    prompt: str
    age_band: AgeBand = "4-6"
    num_scenes: SceneCount = 5


@dataclass(frozen=True)
class PlanResult:
    story: Story
    calls: list[LLMResult]

    @property
    def repaired(self) -> bool:
        return len(self.calls) > 1


@dataclass
class PlanningError(Exception):
    """The story still broke the rules after the repair call."""

    problems: list[str]
    calls: list[LLMResult] = field(default_factory=list)

    def __str__(self) -> str:
        return "Story planning failed: " + "; ".join(self.problems)


def _format_validation_error(error: ValidationError) -> list[str]:
    return [
        f"{'.'.join(str(part) for part in item['loc']) or 'story'}: {item['msg']}" for item in error.errors()
    ]


class StoryPlanner:
    def __init__(
        self,
        llm: LLMClient,
        *,
        planner_model: str,
        repair_model: str,
        max_tokens: int,
        temperature: float,
    ) -> None:
        self._llm = llm
        self._planner_model = planner_model
        self._repair_model = repair_model
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._system_template = (_PROMPTS / "planner_system.md").read_text(encoding="utf-8")
        self._repair_template = (_PROMPTS / "planner_repair.md").read_text(encoding="utf-8")

    async def plan(self, request: StoryRequest, *, story_key: str) -> PlanResult:
        """Plan a story. `story_key` (e.g. the story id) makes character seeds deterministic."""
        messages = [
            ChatMessage("system", self._system_prompt(request)),
            ChatMessage("user", f"Story idea from the parent:\n<idea>\n{request.prompt}\n</idea>"),
        ]
        first = await self._call(self._planner_model, messages)
        story, problems = self._evaluate(first.content, request, story_key)
        if story is not None and not problems:
            return PlanResult(story=story, calls=[first])

        logger.info("Planner output rejected; sending one repair call. Problems: %s", problems)
        repair_messages = [
            *messages,
            ChatMessage("assistant", first.content),
            ChatMessage(
                "user",
                self._repair_template.format(
                    problems="\n".join(f"- {problem}" for problem in problems),
                    num_scenes=request.num_scenes,
                    min_words=MIN_SCENE_WORDS,
                    max_words=MAX_SCENE_WORDS,
                ),
            ),
        ]
        second = await self._call(self._repair_model, repair_messages)
        story, problems = self._evaluate(second.content, request, story_key)
        if story is not None and not problems:
            return PlanResult(story=story, calls=[first, second])

        logger.warning("Planner output still rejected after repair. Problems: %s", problems)
        raise PlanningError(problems=problems, calls=[first, second])

    def _system_prompt(self, request: StoryRequest) -> str:
        return self._system_template.format(
            age_band=request.age_band,
            num_scenes=request.num_scenes,
            min_words=MIN_SCENE_WORDS,
            max_words=MAX_SCENE_WORDS,
        )

    async def _call(self, model: str, messages: list[ChatMessage]) -> LLMResult:
        result = await self._llm.complete_json(
            model=model,
            messages=messages,
            schema_name=_SCHEMA_NAME,
            schema=PLANNED_STORY_SCHEMA,
            max_tokens=self._max_tokens,
            temperature=self._temperature,
        )
        logger.info(
            "Planner call: model=%s prompt_tokens=%d completion_tokens=%d latency_ms=%.0f",
            result.model,
            result.prompt_tokens,
            result.completion_tokens,
            result.latency_ms,
        )
        return result

    @staticmethod
    def _evaluate(content: str, request: StoryRequest, story_key: str) -> tuple[Story | None, list[str]]:
        try:
            planned = PlannedStory.model_validate_json(content)
        except ValidationError as error:
            return None, _format_validation_error(error)
        try:
            story = planned.to_story(age_band=request.age_band, story_key=story_key)
        except ValidationError as error:
            return None, _format_validation_error(error)
        return story, story_problems(story, request.num_scenes)


def story_to_json(story: Story) -> str:
    return json.dumps(story.model_dump(mode="json"), ensure_ascii=False)
