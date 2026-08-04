"""Shared pytest fixtures for AXE tests."""

import os
from collections.abc import AsyncGenerator
from pathlib import Path

from cryptography.fernet import Fernet

# Set a valid encryption key *before* any AXE module builds a cached Settings
# instance from the checked-in .env placeholder.
os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from axe.config import get_settings
from axe.db.base import Base
from axe.security.encryption import EncryptedJSON, generate_fernet_key

# If something already created a cached Settings instance with the placeholder,
# drop it so the env var above takes effect.
get_settings.cache_clear()


@pytest.fixture(scope="session")
def test_db_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Return a temporary SQLite DB path."""
    return tmp_path_factory.mktemp("axe_test_db") / "test.db"


@pytest.fixture(scope="session")
def db_url(test_db_path: Path) -> str:
    return f"sqlite+aiosqlite:///{test_db_path}"


@pytest.fixture(scope="session")
def encryption_key() -> str:
    """Generate a per-test-run Fernet key for EncryptedJSON columns."""
    return generate_fernet_key()


@pytest_asyncio.fixture(scope="session")
async def engine(db_url: str, encryption_key: str):
    """Create an async engine with WAL + FK pragmas and encrypted columns configured."""
    EncryptedJSON.configure(encryption_key)
    engine = create_async_engine(db_url, future=True)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("PRAGMA journal_mode=WAL;"))
        await conn.execute(text("PRAGMA foreign_keys=ON;"))

    yield engine

    await engine.dispose()


@pytest_asyncio.fixture
async def connection(engine) -> AsyncGenerator[AsyncConnection, None]:
    """Provide a transactional database connection for a single test.

    Uses a savepoint so code under test can commit without escaping the
    test-scoped transaction; the outermost savepoint is rolled back at the
    end of each test to keep tests isolated.
    """
    async with engine.connect() as connection:
        transaction = await connection.begin_nested()
        yield connection
        await transaction.rollback()


@pytest_asyncio.fixture
async def db_session(connection: AsyncConnection) -> AsyncGenerator[AsyncSession, None]:
    """Provide a transactional session that rolls back after each test."""
    session_maker = async_sessionmaker(
        connection,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
        autocommit=False,
    )
    session = session_maker()
    yield session
    await session.close()


@pytest_asyncio.fixture
async def db_session_factory(connection: AsyncConnection) -> async_sessionmaker[AsyncSession]:
    """Return a sessionmaker bound to the test connection/transaction."""
    return async_sessionmaker(
        connection,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
        autocommit=False,
    )


@pytest.fixture(autouse=True)
def _restore_encrypted_json_key(encryption_key: str):
    """Keep EncryptedJSON configured with the session key across tests.

    Some compliance tests intentionally mutate ``EncryptedJSON._key`` to
    exercise the env-var fallback; this fixture prevents that pollution from
    leaking into schema/integration tests.
    """
    previous = EncryptedJSON._key
    EncryptedJSON.configure(encryption_key)
    yield
    EncryptedJSON._key = previous
