import copy
from typing import Any

import pytest

from story_samples import make_planned_story


@pytest.fixture
def planned_story() -> dict[str, Any]:
    return copy.deepcopy(make_planned_story())
