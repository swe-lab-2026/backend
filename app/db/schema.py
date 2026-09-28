import logging

import anyio
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings
from app.db.models import Base

logger = logging.getLogger("app.db")


def _split_statements(sql: str) -> list[str]:
    """Split a SQL script on top-level semicolons.

    Handles `--` line comments, `'...'` single-quoted strings (with `''`
    escaping) and `$$...$$` / `$tag$...$tag$` dollar-quoted bodies, so the
    `CREATE FUNCTION` trigger helper in migrate_001 survives intact.
    """
    statements: list[str] = []
    current: list[str] = []
    i = 0
    n = len(sql)

    while i < n:
        char = sql[i]

        if char == "-" and i + 1 < n and sql[i + 1] == "-":
            while i < n and sql[i] != "\n":
                i += 1
            continue

        if char == "'":
            current.append(char)
            i += 1
            while i < n:
                current.append(sql[i])
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        current.append(sql[i + 1])
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue

        if char == "$":
            close = sql.find("$", i + 1)
            consumed = False
            while close != -1:
                tag = sql[i + 1 : close]
                if tag.isidentifier() or tag == "":
                    delimiter = f"${tag}$"
                    end = sql.find(delimiter, close)
                    if end == -1:
                        current.append(sql[i:])
                        i = n
                    else:
                        current.append(sql[i : end + len(delimiter)])
                        i = end + len(delimiter)
                    consumed = True
                    break
                close = sql.find("$", close + 1)
            if consumed:
                continue
            current.append(char)
            i += 1
            continue

        if char == ";":
            stmt = "".join(current).strip()
            if stmt:
                statements.append(stmt)
            current = []
            i += 1
            continue

        current.append(char)
        i += 1

    tail = "".join(current).strip()
    if tail:
        statements.append(tail)
    return statements


async def apply_schema(engine: AsyncEngine, settings: Settings) -> None:
    """Idempotently apply the persisted schema.

    Postgres runs the checked-in SQL migrations (the same contract the
    frontend repo keeps); other dialects (tests) fall back to the SQLAlchemy
    metadata, which mirrors the same tables.

    migrate_001.sql is a baseline that targets an empty database; its enum
    types and trigger function are guarded so it can run repeatedly. The whole
    file is skipped once the `users` table exists (startup on a populated
    database).
    """
    if not settings.apply_schema_on_startup:
        return

    if engine.dialect.name == "postgresql":
        async with engine.begin() as conn:
            has_users = await conn.run_sync(lambda sync_conn: inspect(sync_conn).has_table("users"))
            files = [] if has_users else settings.schema_files
            for schema_file in files:
                sql = await anyio.Path(schema_file).read_text(encoding="utf-8")
                for statement in _split_statements(sql):
                    await conn.exec_driver_sql(statement)
        logger.info("schema applied from %s", ", ".join(files) or "none (already bootstrapped)")
    else:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("schema applied from SQLAlchemy metadata")