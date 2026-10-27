"""Database migration CLI: ``python -m app.db.migrate`` (also ``make migrate``).

Applies the checked-in SQL migrations (see ``Settings.schema_files``) against
``DATABASE_URL``. Safe to run repeatedly: applied migrations are recorded in
``schema_migrations`` and skipped on later runs.
"""

import asyncio
import logging

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.schema import apply_schema
from app.db.session import create_engine_and_session_factory

logger = logging.getLogger("app.migrate")


async def main() -> int:
    settings = get_settings()
    configure_logging("DEBUG" if settings.app_debug else "INFO")
    engine, _ = create_engine_and_session_factory(settings.database_url)
    try:
        await apply_schema(engine, settings)
    finally:
        await engine.dispose()
    logger.info("migrations complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
