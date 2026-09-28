import os
from collections.abc import AsyncIterator
from typing import Any

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

# The module-level `app = create_app()` in app.main reads the environment, so
# provide valid test secrets before anything imports it.
os.environ.setdefault("JWT_ACCESS_SECRET", "t" * 64)
os.environ.setdefault("JWT_REFRESH_SECRET", "u" * 64)
os.environ.setdefault("APP_ENV", "test")

from app.core.config import Settings
from app.db.models import Base, User, UserStatus
from app.main import create_app

# Separate test database. Set TEST_DATABASE_URL to a PostgreSQL URL to run the
# suite against Postgres; the default sqlite in-memory mirror keeps tests
# runnable without infrastructure (the project's own test fallback).
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")


def test_settings() -> Settings:
    return Settings(
        app_env="test",
        database_url=TEST_DATABASE_URL or "sqlite+aiosqlite://",
        redis_url="",
        jwt_access_secret="t" * 64,
        jwt_refresh_secret="u" * 64,
        apply_schema_on_startup=True,
        login_max_attempts=5,
        email_code_max_attempts=5,
    )


@pytest_asyncio.fixture
async def app():
    app = create_app(test_settings())
    async with app.router.lifespan_context(app):
        yield app
    if TEST_DATABASE_URL:
        async with app.state.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture
async def client(app) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def db(app) -> AsyncIterator[AsyncSession]:
    async with app.state.session_factory() as session:
        yield session


async def get_user(db: AsyncSession, email: str) -> User | None:
    from app.db import repositories as repo

    return await repo.get_user_by_email(db, email)


async def get_user_id(db: AsyncSession, email: str) -> Any:
    user = await get_user(db, email)
    assert user is not None
    return user.id


async def set_user_status(db: AsyncSession, user_id: Any, status: UserStatus) -> None:
    user = await db.get(User, user_id)
    assert user is not None
    user.status = status
    await db.commit()


async def get_code(app, email: str) -> str:
    provider = app.state.email_provider
    code = provider.last_code_for(email)
    assert code is not None, f"no code was sent to {email}"
    return code


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


API = "/api/v1"