from typing import Any

import pytest

from storyweaver.llm.schemas import PLANNED_STORY_SCHEMA

pytestmark = pytest.mark.unit

_CONSTRAINT_KEYWORDS = {"minLength", "maxLength", "pattern", "minItems", "maxItems", "minimum", "maximum"}


def _objects(node: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        if node.get("type") == "object" and "properties" in node:
            found.append(node)
        for value in node.values():
            found.extend(_objects(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_objects(item))
    return found


def _keys(node: Any) -> set[str]:
    if isinstance(node, dict):
        return set(node) | {k for v in node.values() for k in _keys(v)}
    if isinstance(node, list):
        return {k for item in node for k in _keys(item)}
    return set()


def test_every_object_is_strict() -> None:
    objects = _objects(PLANNED_STORY_SCHEMA)

    assert len(objects) == 4  # story, character, scene, line
    for obj in objects:
        assert obj["additionalProperties"] is False
        assert obj["required"] == list(obj["properties"])


def test_schema_has_no_constraint_or_server_owned_fields() -> None:
    keys = _keys(PLANNED_STORY_SCHEMA)

    assert not keys & _CONSTRAINT_KEYWORDS
    assert "title" in PLANNED_STORY_SCHEMA["properties"]  # a property named "title" survives
    assert not {"seed", "voice", "age_band"} & set(PLANNED_STORY_SCHEMA["properties"])
