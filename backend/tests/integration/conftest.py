"""Integration fixtures: a throwaway `<db>_test` database on the local Postgres, migrated with Alembic.

Set TEST_DATABASE_URL, e.g. postgresql+asyncpg://storyweaver:<password>@localhost:5432/storyweaver_test
(the database is dropped and recreated on each run; never point this at real data).
"""

import asyncio
import os
from collections.abc import AsyncIterator

import asyncpg
import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from db_helpers import alembic_config
from storyweaver.storage.db import create_engine, create_session_factory


async def _recreate_database(url: str) -> None:
    target = make_url(url)
    if not (target.database or "").endswith("_test"):
        raise RuntimeError("TEST_DATABASE_URL must name a database ending in '_test'.")
    admin = target.set(drivername="postgresql", database="postgres").render_as_string(hide_password=False)
    connection = await asyncpg.connect(admin)
    try:
        await connection.execute(f'DROP DATABASE IF EXISTS "{target.database}" WITH (FORCE)')
        await connection.execute(f'CREATE DATABASE "{target.database}"')
    finally:
        await connection.close()


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    asyncio.run(_recreate_database(url))
    command.upgrade(alembic_config(url), "head")
    return url


@pytest.fixture
async def session(database_url: str) -> AsyncIterator[AsyncSession]:
    engine = create_engine(database_url)
    factory = create_session_factory(engine)
    async with factory() as db_session:
        yield db_session
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE stories, jobs CASCADE"))
    await engine.dispose()
