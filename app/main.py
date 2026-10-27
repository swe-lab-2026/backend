import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

import redis.asyncio as redis_async
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.routes import admin, auth, events, orders, organizers, tickets, users
from app.core.config import Settings, get_settings
from app.core.errors import (
    ApiError,
    api_error_handler,
    http_exception_handler,
    unhandled_error_handler,
    validation_error_handler,
)
from app.core.logging import RequestContextMiddleware, configure_logging
from app.db.schema import apply_schema
from app.db.session import create_engine_and_session_factory
from app.kv.store import MemoryTTLStore, RedisTTLStore, TTLStore
from app.services import notifications
from app.services import orders as order_service
from app.services.email import EmailProvider, build_email_provider

logger = logging.getLogger("app")


def _create_store(settings: Settings) -> tuple[TTLStore, redis_async.Redis | None]:
    if not settings.redis_url:
        logger.warning("REDIS_URL not set - using in-process TTL store (dev only)")
        return MemoryTTLStore(), None
    redis = redis_async.from_url(settings.redis_url, decode_responses=True)
    return RedisTTLStore(redis), redis


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging("DEBUG" if settings.app_debug else "INFO")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine, session_factory = create_engine_and_session_factory(settings.database_url)
        store, redis = _create_store(settings)
        email_provider: EmailProvider = build_email_provider()

        await apply_schema(engine, settings)

        app.state.settings = settings
        app.state.engine = engine
        app.state.session_factory = session_factory
        app.state.store = store
        app.state.redis = redis
        app.state.email_provider = email_provider

        async def reservation_loop() -> None:
            while True:
                await asyncio.sleep(settings.reservation_cleanup_interval_seconds)
                try:
                    async with session_factory() as session:
                        await order_service.expire_holds(session)
                        await session.commit()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("reservation cleanup failed")

        async def notification_loop() -> None:
            while True:
                await asyncio.sleep(settings.notification_retry_interval_seconds)
                try:
                    async with session_factory() as session:
                        await notifications.process_pending_notifications(
                            session,
                            provider=email_provider,
                            download_base=settings.public_api_base,
                            max_attempts=settings.notification_max_attempts,
                        )
                        await session.commit()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("notification retry failed")

        background_tasks: list[asyncio.Task[None]] = []
        if settings.app_env != "test":
            if settings.reservation_cleanup_enabled:
                background_tasks.append(asyncio.create_task(reservation_loop()))
            background_tasks.append(asyncio.create_task(notification_loop()))

        logger.info("%s started (env=%s)", settings.app_name, settings.app_env)
        try:
            yield
        finally:
            for task in background_tasks:
                task.cancel()
            for task in background_tasks:
                with suppress(asyncio.CancelledError):
                    await task
            await store.aclose()
            await engine.dispose()

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        lifespan=lifespan,
    )

    app.add_exception_handler(ApiError, api_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(
        RequestValidationError, validation_error_handler  # type: ignore[arg-type]
    )
    app.add_exception_handler(
        StarletteHTTPException, http_exception_handler  # type: ignore[arg-type]
    )
    app.add_exception_handler(Exception, unhandled_error_handler)

    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list or ["*"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
        expose_headers=["X-Request-ID"],
    )

    app.include_router(auth.router, prefix=settings.api_prefix)
    app.include_router(users.router, prefix=settings.api_prefix)
    app.include_router(admin.router, prefix=settings.api_prefix)
    app.include_router(organizers.router, prefix=settings.api_prefix)
    app.include_router(events.router, prefix=settings.api_prefix)
    app.include_router(orders.router, prefix=settings.api_prefix)
    app.include_router(tickets.router, prefix=settings.api_prefix)
    app.include_router(tickets.checkin_router, prefix=settings.api_prefix)

    @app.get("/")
    async def root() -> dict[str, str]:
        return {"service": settings.app_name, "status": "ok"}

    return app


app = create_app()