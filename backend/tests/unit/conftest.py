import copy
from typing import Any

import pytest

SCENE_TEXT = (
    "Pip the little dragon sat in his warm cave. The night was dark and the wind was cold. "
    "Pip did not like the dark one bit. He held his green scarf tight. Then he saw a small light "
    "by the door. It was a firefly named Glow. Glow said hello and gave a big smile. Pip felt a bit "
    "braver now."
)


def _scene(idx: int) -> dict[str, Any]:
    return {
        "idx": idx,
        "text": SCENE_TEXT,
        "image_prompt": (
            "Pip, a small purple dragon with tiny yellow wings and a green striped scarf, in a cosy cave"
        ),
        "characters": ["pip", "glow"],
        "lines": [
            {"speaker": "narrator", "text": "Pip the little dragon sat in his warm cave."},
            {"speaker": "glow", "text": "Hello, Pip!"},
        ],
    }


def make_planned_story(num_scenes: int = 5) -> dict[str, Any]:
    return {
        "title": "Pip and the Night Lights",
        "moral": "Being brave means trying even when you are scared.",
        "setting": "A cosy cave on a hill above a sleepy village.",
        "characters": [
            {
                "id": "pip",
                "name": "Pip",
                "species": "dragon",
                "visual": "small purple dragon, round belly, tiny yellow wings, green striped scarf",
            },
            {
                "id": "glow",
                "name": "Glow",
                "species": "firefly",
                "visual": "tiny firefly with a soft golden glow",
            },
        ],
        "scenes": [_scene(i) for i in range(1, num_scenes + 1)],
    }


@pytest.fixture
def planned_story() -> dict[str, Any]:
    return copy.deepcopy(make_planned_story())
