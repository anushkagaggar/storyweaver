"""What the planner LLM returns, and the strict JSON Schema sent to Groq.

The LLM output deliberately omits fields the server owns (age_band comes from the request, seeds and voices
are assigned server-side). Groq strict mode needs every property required and `additionalProperties: false`
on every object; length and count limits are enforced afterwards in Python (domain/story.py), so the schema
carries no constraint keywords.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict

from storyweaver.domain.story import AgeBand, Character, Line, Scene, Story, character_seed


class _Planned(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlannedCharacter(_Planned):
    id: str
    name: str
    species: str
    visual: str


class PlannedLine(_Planned):
    speaker: str
    text: str


class PlannedScene(_Planned):
    idx: int
    text: str
    image_prompt: str
    characters: list[str]
    lines: list[PlannedLine]


class PlannedStory(_Planned):
    title: str
    moral: str
    setting: str
    characters: list[PlannedCharacter]
    scenes: list[PlannedScene]

    def to_story(self, *, age_band: AgeBand, story_key: str) -> Story:
        """Build the domain Story. Raises pydantic.ValidationError if field limits are violated."""
        return Story(
            title=self.title,
            age_band=age_band,
            moral=self.moral,
            setting=self.setting,
            characters=[
                Character(
                    id=c.id,
                    name=c.name,
                    species=c.species,
                    visual=c.visual,
                    seed=character_seed(story_key, c.id),
                )
                for c in self.characters
            ],
            scenes=[
                Scene(
                    idx=s.idx,
                    text=s.text,
                    image_prompt=s.image_prompt,
                    characters=s.characters,
                    lines=[Line(speaker=line.speaker, text=line.text) for line in s.lines],
                )
                for s in self.scenes
            ],
        )


_DROP_KEYS = {"title", "description", "default"}


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Pydantic JSON Schema rewritten for Groq strict mode (all properties required, no extra properties)."""

    def rewrite(node: Any) -> Any:
        if isinstance(node, dict):
            out: dict[str, Any] = {}
            for key, value in node.items():
                if key in ("properties", "$defs"):
                    # Keys here are property/definition names (e.g. a property called "title"): keep them all.
                    out[key] = {name: rewrite(sub) for name, sub in value.items()}
                elif key not in _DROP_KEYS:
                    out[key] = rewrite(value)
            if out.get("type") == "object" and "properties" in out:
                out["required"] = list(out["properties"].keys())
                out["additionalProperties"] = False
            return out
        if isinstance(node, list):
            return [rewrite(item) for item in node]
        return node

    result: dict[str, Any] = rewrite(model.model_json_schema())
    return result


PLANNED_STORY_SCHEMA = strict_json_schema(PlannedStory)
