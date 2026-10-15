import json
from typing import Any

import pytest

from story_samples import make_planned_story
from storyweaver.llm.client import ChatMessage, LLMResult
from storyweaver.llm.planner import PlanningError, StoryPlanner, StoryRequest
from storyweaver.llm.schemas import PLANNED_STORY_SCHEMA

pytestmark = pytest.mark.unit


class FakeLLM:
    """Returns queued responses and records every call."""

    def __init__(self, *responses: str) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def complete_json(
        self,
        *,
        model: str,
        messages: list[ChatMessage],
        schema_name: str,
        schema: dict[str, Any],
        max_tokens: int,
        temperature: float,
    ) -> LLMResult:
        self.calls.append(
            {"model": model, "messages": messages, "schema_name": schema_name, "schema": schema}
        )
        return LLMResult(
            content=self.responses.pop(0),
            model=model,
            prompt_tokens=100,
            completion_tokens=200,
            latency_ms=5.0,
            finish_reason="stop",
        )


def _planner(llm: FakeLLM) -> StoryPlanner:
    return StoryPlanner(
        llm, planner_model="planner-model", repair_model="teacher-model", max_tokens=4000, temperature=0.7
    )


async def test_valid_first_attempt_needs_no_repair(planned_story: dict[str, Any]) -> None:
    llm = FakeLLM(json.dumps(planned_story))

    result = await _planner(llm).plan(StoryRequest(prompt="a brave dragon"), story_key="s1")

    assert not result.repaired
    assert result.story.title == "Pip and the Night Lights"
    assert len(llm.calls) == 1
    call = llm.calls[0]
    assert call["model"] == "planner-model"
    assert call["schema"] is PLANNED_STORY_SCHEMA
    system, user = call["messages"]
    assert "Exactly 5 scenes" in system.content and "40-120 words" in system.content
    assert "<idea>\na brave dragon\n</idea>" in user.content


async def test_rule_violation_triggers_one_repair_with_teacher(planned_story: dict[str, Any]) -> None:
    broken = json.loads(json.dumps(planned_story))
    broken["scenes"][0]["text"] = "Too short."
    llm = FakeLLM(json.dumps(broken), json.dumps(planned_story))

    result = await _planner(llm).plan(StoryRequest(prompt="x"), story_key="s1")

    assert result.repaired
    assert [c["model"] for c in llm.calls] == ["planner-model", "teacher-model"]
    repair_messages = llm.calls[1]["messages"]
    assert repair_messages[2].role == "assistant"
    assert "Scene 1 text has 2 words" in repair_messages[3].content


async def test_still_invalid_after_repair_raises(planned_story: dict[str, Any]) -> None:
    broken = json.loads(json.dumps(planned_story))
    broken["scenes"].pop()
    llm = FakeLLM(json.dumps(broken), json.dumps(broken))

    with pytest.raises(PlanningError) as info:
        await _planner(llm).plan(StoryRequest(prompt="x"), story_key="s1")

    assert any("exactly 5 scenes" in p for p in info.value.problems)
    assert len(info.value.calls) == 2


async def test_unparseable_json_is_repaired(planned_story: dict[str, Any]) -> None:
    llm = FakeLLM("{not json", json.dumps(planned_story))

    result = await _planner(llm).plan(StoryRequest(prompt="x"), story_key="s1")

    assert result.repaired
    assert "Invalid JSON" in llm.calls[1]["messages"][3].content


async def test_six_scenes_requested(planned_story: dict[str, Any]) -> None:
    llm = FakeLLM(json.dumps(make_planned_story(6)))

    result = await _planner(llm).plan(StoryRequest(prompt="x", num_scenes=6), story_key="s1")

    assert len(result.story.scenes) == 6
    assert "Exactly 6 scenes" in llm.calls[0]["messages"][0].content


async def test_provider_json_failure_goes_through_the_repair_call(planned_story: dict[str, Any]) -> None:
    from storyweaver.llm.client import LLMInvalidOutputError

    class FailsFirst(FakeLLM):
        async def complete_json(self, **kwargs: Any) -> LLMResult:  # type: ignore[override]
            if not self.calls:
                self.calls.append({"model": kwargs["model"], "messages": kwargs["messages"]})
                raise LLMInvalidOutputError("json_validate_failed", '{"title": "Half')
            return await super().complete_json(**kwargs)

    llm = FailsFirst(json.dumps(planned_story))

    result = await _planner(llm).plan(StoryRequest(prompt="x"), story_key="s1")

    assert result.repaired
    assert [c["model"] for c in llm.calls] == ["planner-model", "teacher-model"]
    repair = llm.calls[1]["messages"]
    assert repair[2].content == '{"title": "Half'  # the partial generation is sent back for repair
    assert "Invalid JSON" in repair[3].content


async def test_adapted_idea_gets_explicit_instruction(planned_story: dict[str, Any]) -> None:
    llm = FakeLLM(json.dumps(planned_story))

    await _planner(llm).plan(StoryRequest(prompt="Tom and Jerry", adaptation="brands"), story_key="s1")

    user = llm.calls[0]["messages"][1].content
    assert "names brands or trademarked characters" in user and "invent original characters" in user


async def test_ordinary_idea_has_no_brand_instruction(planned_story: dict[str, Any]) -> None:
    llm = FakeLLM(json.dumps(planned_story))

    await _planner(llm).plan(StoryRequest(prompt="a brave dragon"), story_key="s1")

    assert "trademarked" not in llm.calls[0]["messages"][1].content


async def test_borrowed_character_name_is_sent_to_repair(planned_story: dict[str, Any]) -> None:
    # The sample story's dragon is called Pip: an adapted idea naming "Pip" must not keep that name.
    llm = FakeLLM(json.dumps(planned_story), json.dumps(planned_story))

    with pytest.raises(PlanningError) as caught:
        await _planner(llm).plan(StoryRequest(prompt="Pip the dragon", adaptation="brands"), story_key="s1")

    assert any("uses ['pip'] from the idea" in problem for problem in caught.value.problems)
    assert "Invent a new, original name" in llm.calls[1]["messages"][3].content
