import pytest

from storyweaver.storage.db import normalize_database_url

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (  # Neon, as pasted from its console
            "postgresql://user:pw@"  # pragma: allowlist secret
            "ep-cool-1234.eu-central-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require",
            "postgresql+asyncpg://user:pw@"  # pragma: allowlist secret
            "ep-cool-1234.eu-central-1.aws.neon.tech/neondb?ssl=require",
        ),
        ("postgres://u:p@host:5432/db", "postgresql+asyncpg://u:p@host:5432/db"),
        (  # local docker compose form: unchanged
            "postgresql+asyncpg://storyweaver:pw@postgres:5432/storyweaver",
            "postgresql+asyncpg://storyweaver:pw@postgres:5432/storyweaver",
        ),
        ("postgresql://u:p@h/db?sslmode=disable", "postgresql+asyncpg://u:p@h/db"),
        ("postgresql://u:p@h/db?sslmode=verify-full", "postgresql+asyncpg://u:p@h/db?ssl=verify-full"),
        ("postgresql+asyncpg://u:p@h/db?ssl=require", "postgresql+asyncpg://u:p@h/db?ssl=require"),
    ],
)
def test_normalize_database_url(given: str, expected: str) -> None:
    assert normalize_database_url(given) == expected
