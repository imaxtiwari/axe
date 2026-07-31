"""Async SQLAlchemy engine and session setup for AXE."""

from collections.abc import AsyncGenerator

from sqlalchemy.event import listens_for
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from axe.config import get_settings

settings = get_settings()

async_engine = create_async_engine(
    settings.database_url,
    echo=settings.app_env == "development",
    future=True,
)

AsyncSessionLocal = async_sessionmaker(
    bind=async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield an async database session."""
    async with AsyncSessionLocal() as session:
        yield session


@listens_for(async_engine.sync_engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, connection_record):  # type: ignore[no-untyped-def]
    """Enable SQLite WAL mode and foreign keys on each connection."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA foreign_keys=ON;")
    finally:
        cursor.close()
