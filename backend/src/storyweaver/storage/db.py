"""Async engine and session factory. Callers own transactions: `async with session.begin(): ...`."""

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

# libpq options that asyncpg does not accept as URL parameters.
_LIBPQ_ONLY = {"sslmode", "channel_binding"}


def normalize_database_url(url: str) -> str:
    """Accept a provider URL as pasted (e.g. Neon's `postgresql://...?sslmode=require&channel_binding=require`)
    and return the asyncpg form SQLAlchemy needs (`postgresql+asyncpg://...?ssl=require`).

    channel_binding is dropped (asyncpg negotiates SCRAM itself). Normalised URLs are returned unchanged.
    """
    parts = urlsplit(url.strip())
    scheme = parts.scheme
    if scheme in ("postgres", "postgresql"):
        scheme = "postgresql+asyncpg"
    query = parse_qsl(parts.query, keep_blank_values=True)
    sslmode = next((value for key, value in query if key == "sslmode"), None)
    kept = [(key, value) for key, value in query if key not in _LIBPQ_ONLY]
    if sslmode and sslmode != "disable" and not any(key == "ssl" for key, _ in kept):
        kept.append(("ssl", "require" if sslmode in ("require", "prefer", "allow") else sslmode))
    return urlunsplit((scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment))


def create_engine(database_url: str) -> AsyncEngine:
    # pool_pre_ping: Neon scales to zero and drops idle connections (plan §3). Small pool: the free tier
    # allows few connections and the Space runs one process.
    return create_async_engine(
        normalize_database_url(database_url), pool_pre_ping=True, pool_size=3, max_overflow=2
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
