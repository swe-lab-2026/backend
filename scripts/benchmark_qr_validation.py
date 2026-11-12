"""Reproducible PostgreSQL benchmark for event QR admission validation.

This is intentionally a standalone script, not a pytest test: latency numbers
are environment-dependent, so they should be reported rather than asserted in
the regular test suite.

Example:
    BENCHMARK_DATABASE_URL=postgresql+asyncpg://biletflow:biletflowpw@localhost:55432/biletflow \
        python3 scripts/benchmark_qr_validation.py --tickets 1000 --iterations 500

The target database must be disposable: the script applies the project's
migrations and removes only the rows it creates.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import os
import platform
import random
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# app.main creates a module-level app before we can pass our benchmark settings.
os.environ.setdefault("JWT_ACCESS_SECRET", "b" * 64)
os.environ.setdefault("JWT_REFRESH_SECRET", "c" * 64)
os.environ.setdefault("APP_ENV", "test")

from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import delete, event, text  # noqa: E402

from app.core.config import Settings  # noqa: E402
from app.core.security import create_access_token  # noqa: E402
from app.db.models import (  # noqa: E402
    Attendee,
    Event,
    EventStatus,
    EventVisibility,
    Order,
    OrderItem,
    OrderStatus,
    OrganizerProfile,
    Ticket,
    TicketStatus,
    TicketType,
    User,
    UserRole,
    UserStatus,
)
from app.main import create_app  # noqa: E402
from app.services.tickets import build_qr_payload, hash_qr_payload  # noqa: E402

API = "/api/v1"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (upper - position) + ordered[upper] * (position - lower)


def _summary(values: list[float]) -> str:
    return (
        f"median={statistics.median(values):7.2f}  "
        f"p95={_percentile(values, 0.95):7.2f}  "
        f"mean={statistics.fmean(values):7.2f}  "
        f"min={min(values):7.2f}  max={max(values):7.2f}"
    )


async def _create_workload(app, *, ticket_count: int):
    settings: Settings = app.state.settings
    secret = settings.effective_ticket_qr_secret
    suffix = uuid4().hex
    now = _now()

    owner = User(
        email=f"bench-owner-{suffix}@example.com",
        password_hash="x" * 60,
        first_name="Bench",
        last_name="Owner",
        global_role=UserRole.USER,
        status=UserStatus.ACTIVE,
        email_verified_at=now,
    )
    buyer = User(
        email=f"bench-buyer-{suffix}@example.com",
        password_hash="x" * 60,
        first_name="Bench",
        last_name="Buyer",
        global_role=UserRole.USER,
        status=UserStatus.ACTIVE,
        email_verified_at=now,
    )

    async with app.state.session_factory() as db:
        db.add_all([owner, buyer])
        await db.flush()

        profile = OrganizerProfile(
            user_id=owner.id,
            display_name="Benchmark Organizer",
            terms_accepted_at=now,
        )
        db.add(profile)
        await db.flush()

        event = Event(
            owner_user_id=owner.id,
            title=f"Benchmark Event {suffix}",
            description="QR validation latency workload",
            category="Benchmark",
            visibility=EventVisibility.PUBLIC,
            status=EventStatus.PUBLISHED,
            venue_name="Benchmark Hall",
            city="Almaty",
            timezone="Asia/Almaty",
            starts_at=now + timedelta(days=1),
            ends_at=now + timedelta(days=1, hours=3),
            capacity=ticket_count,
            published_at=now,
        )
        db.add(event)
        await db.flush()

        ticket_type = TicketType(
            event_id=event.id,
            name="General",
            price=Decimal("0"),
            currency="KZT",
            capacity=ticket_count,
            min_per_order=1,
            max_per_order=ticket_count,
        )
        db.add(ticket_type)
        await db.flush()

        order = Order(
            event_id=event.id,
            buyer_user_id=buyer.id,
            buyer_email=buyer.email,
            status=OrderStatus.PAID,
            currency="KZT",
            subtotal_amount=Decimal("0"),
            discount_amount=Decimal("0"),
            fee_amount=Decimal("0"),
            total_amount=Decimal("0"),
            paid_at=now,
        )
        db.add(order)
        await db.flush()

        attendee = Attendee(
            user_id=buyer.id,
            full_name="Benchmark Buyer",
            email=buyer.email,
        )
        db.add(attendee)
        await db.flush()

        items: list[OrderItem] = []
        tickets: list[Ticket] = []
        payloads: list[str] = []
        for _ in range(ticket_count):
            item_id = uuid4()
            public_id = uuid4()
            payload = build_qr_payload(public_id, secret)
            items.append(
                OrderItem(
                    id=item_id,
                    order_id=order.id,
                    ticket_type_id=ticket_type.id,
                    attendee_id=attendee.id,
                    ticket_type_name=ticket_type.name,
                    unit_price=Decimal("0"),
                    discount_amount=Decimal("0"),
                    final_price=Decimal("0"),
                )
            )
            tickets.append(
                Ticket(
                    order_item_id=item_id,
                    public_id=public_id,
                    qr_token_hash=hash_qr_payload(payload),
                    status=TicketStatus.VALID,
                )
            )
            payloads.append(payload)

        db.add_all(items)
        db.add_all(tickets)
        await db.commit()

    return {
        "owner": owner,
        "buyer": buyer,
        "event": event,
        "ticket_type": ticket_type,
        "order": order,
        "attendee": attendee,
        "items": items,
        "tickets": tickets,
        "payloads": payloads,
    }


async def _cleanup_workload(app, workload: dict) -> None:
    async with app.state.session_factory() as db:
        item_ids = [item.id for item in workload["items"]]
        await db.execute(delete(Ticket).where(Ticket.order_item_id.in_(item_ids)))
        await db.execute(delete(OrderItem).where(OrderItem.id.in_(item_ids)))
        await db.execute(delete(Order).where(Order.id == workload["order"].id))
        await db.execute(delete(Attendee).where(Attendee.id == workload["attendee"].id))
        await db.execute(
            delete(TicketType).where(TicketType.id == workload["ticket_type"].id)
        )
        await db.execute(delete(Event).where(Event.id == workload["event"].id))
        await db.execute(
            delete(OrganizerProfile).where(
                OrganizerProfile.user_id == workload["owner"].id
            )
        )
        await db.execute(
            delete(User).where(
                User.id.in_([workload["owner"].id, workload["buyer"].id])
            )
        )
        await db.commit()


async def _run(args: argparse.Namespace) -> int:
    database_url = os.environ.get("BENCHMARK_DATABASE_URL") or os.environ.get(
        "TEST_DATABASE_URL", ""
    )
    if not database_url.startswith("postgresql"):
        print(
            "Set BENCHMARK_DATABASE_URL (or TEST_DATABASE_URL) to a disposable "
            "PostgreSQL URL, for example:\n"
            "  postgresql+asyncpg://biletflow:biletflowpw@localhost:55432/biletflow",
            file=sys.stderr,
        )
        return 2

    settings = Settings(
        app_env="test",
        database_url=database_url,
        redis_url="",
        jwt_access_secret="b" * 64,
        jwt_refresh_secret="c" * 64,
        apply_schema_on_startup=True,
        public_base_url="http://benchmark.local",
    )
    app = create_app(settings)
    workload: dict | None = None

    async with app.router.lifespan_context(app):
        try:
            workload = await _create_workload(app, ticket_count=args.tickets)
            token = create_access_token(
                settings,
                user_id=str(workload["owner"].id),
                role=workload["owner"].global_role.value,
            )
            headers = {"Authorization": f"Bearer {token}"}
            event_id = workload["event"].id
            payloads = workload["payloads"]

            db_seconds = [0.0]

            def _before_cursor_execute(
                _conn, _cursor, _statement, _parameters, context, _executemany
            ) -> None:
                context._benchmark_started_at = time.perf_counter()

            def _after_cursor_execute(
                _conn, _cursor, _statement, _parameters, context, _executemany
            ) -> None:
                started_at = getattr(context, "_benchmark_started_at", None)
                if started_at is not None:
                    db_seconds[0] += time.perf_counter() - started_at

            sync_engine = app.state.engine.sync_engine
            event.listen(sync_engine, "before_cursor_execute", _before_cursor_execute)
            event.listen(sync_engine, "after_cursor_execute", _after_cursor_execute)

            transport = ASGITransport(app=app)
            request_ms: list[float] = []
            db_ms: list[float] = []
            try:
                async with AsyncClient(transport=transport, base_url="http://test") as client:
                    warmup = min(args.warmup, len(payloads))
                    for index in range(warmup):
                        response = await client.post(
                            f"{API}/events/{event_id}/tickets/validate",
                            json={"qr_payload": payloads[index]},
                            headers=headers,
                        )
                        if response.status_code != 200 or not response.json()["valid"]:
                            raise RuntimeError(
                                f"warmup validation failed: {response.status_code} {response.text}"
                            )

                    rng = random.Random(args.seed)
                    if args.iterations <= len(payloads):
                        sample = rng.sample(payloads, args.iterations)
                    else:
                        sample = [rng.choice(payloads) for _ in range(args.iterations)]

                    for payload in sample:
                        db_seconds[0] = 0.0
                        started_at = time.perf_counter()
                        response = await client.post(
                            f"{API}/events/{event_id}/tickets/validate",
                            json={"qr_payload": payload},
                            headers=headers,
                        )
                        elapsed_ms = (time.perf_counter() - started_at) * 1000
                        if response.status_code != 200 or not response.json()["valid"]:
                            raise RuntimeError(
                                f"validation failed: {response.status_code} {response.text}"
                            )
                        request_ms.append(elapsed_ms)
                        db_ms.append(db_seconds[0] * 1000)
            finally:
                event.remove(sync_engine, "before_cursor_execute", _before_cursor_execute)
                event.remove(sync_engine, "after_cursor_execute", _after_cursor_execute)

            async with app.state.engine.connect() as conn:
                postgres_version = (
                    await conn.execute(text("SHOW server_version"))
                ).scalar_one()

            non_db_ms = [
                max(0.0, total - database)
                for total, database in zip(request_ms, db_ms, strict=True)
            ]

            print("QR validation benchmark (PostgreSQL)")
            print(f"  platform: {platform.platform()}")
            print(f"  python: {platform.python_version()}")
            print(f"  cpu_count: {os.cpu_count()}")
            print(f"  postgres: {postgres_version}")
            print(
                f"  workload: {args.tickets} tickets / 1 event / 1 order, "
                f"{args.iterations} measured requests, {warmup} warmup"
            )
            print(f"  request latency ms:  {_summary(request_ms)}")
            print(f"  db query time ms:    {_summary(db_ms)}")
            print(f"  non-DB time ms:      {_summary(non_db_ms)}")
            print(
                "  note: request latency is in-process ASGI (no network); "
                "db time is SQL execution measured via SQLAlchemy events."
            )
        finally:
            if workload is not None:
                await _cleanup_workload(app, workload)

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tickets", type=int, default=1000, help="tickets in workload")
    parser.add_argument("--iterations", type=int, default=500, help="measured requests")
    parser.add_argument("--warmup", type=int, default=50, help="warmup requests")
    parser.add_argument("--seed", type=int, default=0, help="deterministic sample seed")
    args = parser.parse_args()
    if args.tickets < 1 or args.iterations < 1 or args.warmup < 0:
        parser.error("--tickets and --iterations must be positive; --warmup must be >= 0")
    logging.disable(logging.WARNING)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
