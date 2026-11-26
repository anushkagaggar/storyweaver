"""Shared fixtures for the opt-in live tests that need a database."""

import os

import pytest
from alembic import command
from sqlalchemy.engine import make_url

from db_helpers import alembic_config


@pytest.fixture
def database_url() -> str:
    """The throwaway `*_test` database, brought to the latest migration (it is not recreated here)."""
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    if not (make_url(url).database or "").endswith("_test"):
        raise RuntimeError("TEST_DATABASE_URL must name a database ending in '_test'.")
    command.upgrade(alembic_config(url), "head")
    return url
