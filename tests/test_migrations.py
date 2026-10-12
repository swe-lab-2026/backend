import os
from pathlib import Path

import anyio
import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError

from app.db.models import Base
from app.db.schema import _split_statements, apply_migration_files
from app.db.session import create_engine_and_session_factory

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")
SQL_DIR = Path(__file__).resolve().parent.parent / "sql"
REAL_FILES = [str(SQL_DIR / name) for name in (
    "migrate_001.sql",
    "migrate_002_auth.sql",
    "migrate_003_events.sql",
    "migrate_004_password_reset.sql",
)]
REAL_VERSIONS = [Path(f).name for f in REAL_FILES]

needs_pg = pytest.mark.skipif(not TEST_DATABASE_URL, reason="requires real PostgreSQL")


def _write(path: Path, sql: str) -> str:
    path.write_text(sql, encoding="utf-8")
    return str(path)


async def _fresh_sqlite_engine():
    engine, _ = create_engine_and_session_factory("sqlite+aiosqlite://")
    return engine


async def _recorded(engine) -> set[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(text("SELECT version FROM schema_migrations"))
        return set(rows.scalars().all())


async def _tables(engine) -> set[str]:
    async with engine.connect() as conn:
        return await conn.run_sync(lambda s: set(inspect(s).get_table_names()))


# ---------- Statement splitter (pure, dialect-independent) ----------

async def test_split_statements_basics():
    assert _split_statements("SELECT 1; SELECT 2;") == ["SELECT 1", "SELECT 2"]
    assert _split_statements("  ; ; ") == []
    assert _split_statements("SELECT 1") == ["SELECT 1"]


async def test_split_statements_ignores_semicolons_in_strings_and_comments():
    sql = "INSERT INTO t VALUES ('a;b'';c'); -- trailing; comment\nSELECT 2;"
    assert _split_statements(sql) == [
        "INSERT INTO t VALUES ('a;b'';c')",
        "SELECT 2",
    ]


async def test_split_statements_keeps_dollar_quoted_body():
    sql = "CREATE FUNCTION f() RETURNS TRIGGER LANGUAGE plpgsql AS $$ BEGIN a;b; END; $$;"
    assert _split_statements(sql) == [sql[:-1]]


# ---------- Runner core on portable fixture migrations ----------

async def test_runner_applies_once_in_order(tmp_path):
    engine = await _fresh_sqlite_engine()
    try:
        a = _write(tmp_path / "migrate_010_alpha.sql", (
            "CREATE TABLE mig_log (version TEXT PRIMARY KEY, n INTEGER); "
            "INSERT INTO mig_log VALUES ('alpha', 1);"
        ))
        b = _write(tmp_path / "migrate_011_beta.sql", "INSERT INTO mig_log VALUES ('beta', 2);")

        applied = await apply_migration_files(engine, [a, b])
        assert applied == ["migrate_010_alpha.sql", "migrate_011_beta.sql"]

        async with engine.connect() as conn:
            rows = (
                await conn.execute(text("SELECT version FROM mig_log ORDER BY n ASC"))
            ).scalars().all()
        assert rows == ["alpha", "beta"]

        # Repeated execution is a no-op and duplicates nothing.
        assert await apply_migration_files(engine, [a, b]) == []
        async with engine.connect() as conn:
            count = (await conn.execute(text("SELECT COUNT(*) FROM mig_log"))).scalar_one()
        assert count == 2
        assert await _recorded(engine) == {"migrate_010_alpha.sql", "migrate_011_beta.sql"}
    finally:
        await engine.dispose()


async def test_runner_failure_is_not_recorded(tmp_path):
    engine = await _fresh_sqlite_engine()
    try:
        good = _write(
            tmp_path / "migrate_020_good.sql",
            "CREATE TABLE mig_probe (id INTEGER PRIMARY KEY);",
        )
        bad = _write(
            tmp_path / "migrate_021_bad.sql",
            "INSERT INTO mig_probe VALUES (1); INSERT INTO missing_table_xyz VALUES (1);",
        )
        with pytest.raises(DBAPIError):
            await apply_migration_files(engine, [good, bad])

        recorded = await _recorded(engine)
        assert recorded == {"migrate_020_good.sql"}
        # The failed file rolled back: its partial INSERT left no rows.
        async with engine.connect() as conn:
            count = (await conn.execute(text("SELECT COUNT(*) FROM mig_probe"))).scalar_one()
        assert count == 0

        fixed = _write(tmp_path / "migrate_021_bad.sql", "INSERT INTO mig_probe VALUES (1);")
        assert await apply_migration_files(engine, [good, fixed]) == ["migrate_021_bad.sql"]
        async with engine.connect() as conn:
            count = (await conn.execute(text("SELECT COUNT(*) FROM mig_probe"))).scalar_one()
        assert count == 1
    finally:
        await engine.dispose()


async def test_runner_backfills_legacy_baseline_without_executing(tmp_path):
    engine = await _fresh_sqlite_engine()
    try:
        # Simulate a database bootstrapped by the old untracked mechanism:
        # the marker tables exist, but nothing is recorded.
        async with engine.begin() as conn:
            await conn.exec_driver_sql("CREATE TABLE users (id INTEGER PRIMARY KEY)")
            await conn.exec_driver_sql("CREATE TABLE refresh_sessions (id INTEGER PRIMARY KEY)")
            await conn.exec_driver_sql(
                "CREATE TABLE email_verification_tokens (id INTEGER PRIMARY KEY)"
            )
        witness_001 = _write(
            tmp_path / "migrate_001.sql", "CREATE TABLE backfill_witness_001 (id INTEGER);"
        )
        witness_002 = _write(
            tmp_path / "migrate_002_auth.sql", "CREATE TABLE backfill_witness_002 (id INTEGER);"
        )
        later = _write(
            tmp_path / "migrate_003_later.sql", "CREATE TABLE backfill_later (id INTEGER);"
        )

        applied = await apply_migration_files(engine, [witness_001, witness_002, later])
        # Baselines recorded without execution; only the later file ran.
        assert applied == ["migrate_003_later.sql"]
        assert await _recorded(engine) == {
            "migrate_001.sql",
            "migrate_002_auth.sql",
            "migrate_003_later.sql",
        }
        tables = await _tables(engine)
        assert "backfill_witness_001" not in tables
        assert "backfill_witness_002" not in tables
        assert "backfill_later" in tables
    finally:
        await engine.dispose()


# ---------- Real migrations on real PostgreSQL ----------

async def _pg_engine():
    engine, _ = create_engine_and_session_factory(TEST_DATABASE_URL)
    return engine


async def _pg_cleanup(engine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def _run_raw_statements(engine, files: list[str]) -> None:
    """Execute migration files without recording, like the legacy bootstrapper."""
    for schema_file in files:
        sql = await anyio.Path(schema_file).read_text(encoding="utf-8")
        async with engine.begin() as conn:
            for statement in _split_statements(sql):
                await conn.exec_driver_sql(statement)


@needs_pg
async def test_pg_fresh_database_applies_all_migrations():
    engine = await _pg_engine()
    try:
        applied = await apply_migration_files(engine, REAL_FILES)
        assert applied == REAL_VERSIONS
        tables = await _tables(engine)
        assert {"users", "password_reset_tokens", "schema_migrations", "orders"} <= tables
        assert await apply_migration_files(engine, REAL_FILES) == []
        assert await _recorded(engine) == set(REAL_VERSIONS)
    finally:
        await _pg_cleanup(engine)


@needs_pg
async def test_pg_legacy_database_is_backfilled_and_extended():
    engine = await _pg_engine()
    try:
        # Legacy state: 001+002 executed by the old mechanism, nothing recorded.
        await _run_raw_statements(engine, REAL_FILES[:2])
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO users (id, email, password_hash, first_name, last_name) "
                    "VALUES (gen_random_uuid(), 'legacy@example.com', 'x', 'Legacy', 'User')"
                )
            )

        applied = await apply_migration_files(engine, REAL_FILES)
        assert applied == ["migrate_003_events.sql", "migrate_004_password_reset.sql"]
        assert await _recorded(engine) == set(REAL_VERSIONS)

        # Existing state is intact: the legacy row survives and new objects exist.
        async with engine.connect() as conn:
            count = (
                await conn.execute(
                    text("SELECT COUNT(*) FROM users WHERE email = 'legacy@example.com'")
                )
            ).scalar_one()
            assert count == 1
            index = (
                await conn.execute(
                    text(
                        "SELECT COUNT(*) FROM pg_indexes "
                        "WHERE indexname = 'idx_order_items_ticket_type'"
                    )
                )
            ).scalar_one()
            assert index == 1
    finally:
        await _pg_cleanup(engine)


@needs_pg
async def test_pg_partial_history_converges_without_reexecution():
    engine = await _pg_engine()
    try:
        # Partial history: 001+002 executed and recorded, 003 marked applied
        # without being executed, 004 pending.
        await _run_raw_statements(engine, REAL_FILES[:2])
        async with engine.begin() as conn:
            await conn.exec_driver_sql(
                "CREATE TABLE IF NOT EXISTS schema_migrations (version VARCHAR(255) PRIMARY KEY, "
                "applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
            )
            for version in REAL_VERSIONS[:3]:
                await conn.execute(
                    text("INSERT INTO schema_migrations (version) VALUES (:version)"),
                    {"version": version},
                )

        applied = await apply_migration_files(engine, REAL_FILES)
        assert applied == ["migrate_004_password_reset.sql"]

        # Recorded-but-unexecuted 003 was honored, not re-run.
        async with engine.connect() as conn:
            index = (
                await conn.execute(
                    text(
                        "SELECT COUNT(*) FROM pg_indexes "
                        "WHERE indexname = 'idx_order_items_ticket_type'"
                    )
                )
            ).scalar_one()
            assert index == 0
            tokens = "password_reset_tokens" in await conn.run_sync(
                lambda s: set(inspect(s).get_table_names())
            )
            assert tokens
    finally:
        await _pg_cleanup(engine)


@needs_pg
async def test_pg_failed_migration_is_not_recorded(tmp_path):
    engine = await _pg_engine()
    try:
        bad = anyio.Path(tmp_path / "mig_bad_probe.sql")
        await bad.write_text(
            "CREATE TABLE mig_fail_probe (id INTEGER PRIMARY KEY); "
            "INSERT INTO missing_table_xyz VALUES (1);",
            encoding="utf-8",
        )
        with pytest.raises(DBAPIError):
            await apply_migration_files(engine, [*REAL_FILES[:2], str(bad)])

        recorded = await _recorded(engine)
        assert recorded == {"migrate_001.sql", "migrate_002_auth.sql"}
        # Unlike SQLite, PostgreSQL rolls DDL back with the failed file.
        assert "mig_fail_probe" not in await _tables(engine)
    finally:
        await _pg_cleanup(engine)


@needs_pg
async def test_pg_concurrent_runners_apply_exactly_once():
    import asyncio

    engine_a = await _pg_engine()
    engine_b = await _pg_engine()
    try:
        first, second = await asyncio.gather(
            apply_migration_files(engine_a, REAL_FILES),
            apply_migration_files(engine_b, REAL_FILES),
        )
        assert sorted(first + second) == sorted(REAL_VERSIONS)
        assert await _recorded(engine_a) == set(REAL_VERSIONS)
    finally:
        await _pg_cleanup(engine_a)
        await engine_b.dispose()
