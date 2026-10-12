import logging
from pathlib import Path

import anyio
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings
from app.db.models import Base

logger = logging.getLogger("app.db")

# Session-level advisory-lock key serializing concurrent migration runners.
_MIGRATION_LOCK = "biletflow_schema_migrations"

# Baselines that predate the versioned runner. The old bootstrapper executed
# its files in one transaction without recording anything, so a database that
# already contains these tables must have them in full; the versions below are
# then recorded without re-executing (migrate_001 is not safely re-runnable).
# Each rule is (version, tables that must all exist).
_BASELINE_RULES: tuple[tuple[str, frozenset[str]], ...] = (
    ("migrate_001.sql", frozenset({"users"})),
    ("migrate_002_auth.sql", frozenset({"refresh_sessions", "email_verification_tokens"})),
)


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


def _tracking_table_ddl(dialect_name: str) -> str:
    if dialect_name == "postgresql":
        return (
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version VARCHAR(255) PRIMARY KEY, "
            "applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
        )
    return (
        "CREATE TABLE IF NOT EXISTS schema_migrations (version VARCHAR(255) PRIMARY KEY, "
        "applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )


async def _recorded_versions(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(text("SELECT version FROM schema_migrations"))
        return set(rows.scalars().all())


async def _backfill_baseline(engine: AsyncEngine, versions: list[str]) -> list[str]:
    """Record pre-runner baselines without executing them.

    Only versions from the configured file list that are not yet recorded are
    considered, and only when every marker table for the rule already exists.
    Returns the versions that were recorded.
    """
    async with engine.connect() as conn:
        tables = await conn.run_sync(lambda sync_conn: set(inspect(sync_conn).get_table_names()))

    recorded = await _recorded_versions(engine)
    to_mark = [
        version
        for version, markers in _BASELINE_RULES
        if version in versions and version not in recorded and markers <= tables
    ]
    if not to_mark:
        return []
    async with engine.begin() as conn:
        for version in to_mark:
            await conn.execute(
                text("INSERT INTO schema_migrations (version) VALUES (:version)"),
                {"version": version},
            )
    logger.info("migrations backfilled without execution: %s", ", ".join(to_mark))
    return to_mark


async def apply_migration_files(engine: AsyncEngine, files: list[str]) -> list[str]:
    """Apply pending migration files exactly once, in the given order.

    Each file runs in its own transaction and its version (the file name) is
    recorded in ``schema_migrations`` only after the file completes. A failed
    file rolls back, is not recorded, and aborts the run so the failure is
    visible instead of silently skipped. On PostgreSQL the whole run holds a
    session-level advisory lock, so concurrent runners serialize and the
    second one becomes a no-op. Returns the versions applied by this call.
    """
    versions = [Path(schema_file).name for schema_file in files]
    is_postgres = engine.dialect.name == "postgresql"

    async def _run() -> list[str]:
        async with engine.begin() as conn:
            await conn.exec_driver_sql(_tracking_table_ddl(engine.dialect.name))
        recorded = await _recorded_versions(engine)
        recorded.update(await _backfill_baseline(engine, versions))

        applied: list[str] = []
        for schema_file, version in zip(files, versions, strict=True):
            if version in recorded:
                continue
            sql = await anyio.Path(schema_file).read_text(encoding="utf-8")
            async with engine.begin() as conn:
                for statement in _split_statements(sql):
                    await conn.exec_driver_sql(statement)
                await conn.execute(
                    text("INSERT INTO schema_migrations (version) VALUES (:version)"),
                    {"version": version},
                )
            recorded.add(version)
            applied.append(version)
            logger.info("migration applied: %s", version)
        return applied

    if not is_postgres:
        return await _run()

    async with engine.connect() as lock_conn:
        await lock_conn.exec_driver_sql(f"SELECT pg_advisory_lock(hashtext('{_MIGRATION_LOCK}'))")
        try:
            return await _run()
        finally:
            await lock_conn.exec_driver_sql(
                f"SELECT pg_advisory_unlock(hashtext('{_MIGRATION_LOCK}'))"
            )


async def apply_schema(engine: AsyncEngine, settings: Settings) -> None:
    """Idempotently apply the persisted schema.

    Postgres runs the checked-in SQL migrations through the versioned runner:
    every file executes exactly once, in ``schema_files`` order, with applied
    versions tracked in ``schema_migrations``. Databases bootstrapped by the
    previous untracked mechanism are detected via their tables and have the
    baseline versions recorded without re-execution, so existing state stays
    intact. Other dialects (tests) fall back to the SQLAlchemy metadata, which
    mirrors the same tables.
    """
    if not settings.apply_schema_on_startup:
        return

    if engine.dialect.name == "postgresql":
        applied = await apply_migration_files(engine, settings.schema_files)
        logger.info(
            "schema migrations applied: %s",
            ", ".join(applied) if applied else "none (already up to date)",
        )
    else:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("schema applied from SQLAlchemy metadata")
