import logging
import re
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import repositories as repo
from app.db.models import (
    Event,
    Notification,
    NotificationChannel,
    NotificationStatus,
    Order,
    OrderNotification,
    OrderStatus,
    User,
    UserStatus,
)
from app.services.email import (
    EmailDeliveryError,
    EmailProvider,
    EmailTicket,
    NotificationEmail,
    OrderConfirmationEmail,
)

logger = logging.getLogger("app.notifications")

KIND_ORDER_CONFIRMATION = "ORDER_CONFIRMATION"
KIND_PAYMENT_FAILED = "PAYMENT_FAILED"
KIND_EVENT_UPDATED = "EVENT_UPDATED"
KIND_EVENT_CANCELLED = "EVENT_CANCELLED"
KIND_REFUND_COMPLETED = "REFUND_COMPLETED"
LIFECYCLE_KINDS = frozenset(
    {KIND_PAYMENT_FAILED, KIND_EVENT_UPDATED, KIND_EVENT_CANCELLED, KIND_REFUND_COMPLETED}
)
# Event fields whose change is worth telling ticket holders about.
EVENT_UPDATE_NOTIFY_FIELDS = (
    "title", "starts_at", "ends_at", "timezone", "venue_name", "venue_address", "city",
)
_COMMITTED_STATUSES = {OrderStatus.PAID, OrderStatus.CONFIRMED}
_PERMANENT_PREFIX = "permanent: "
_RETRY_AFTER_RE = re.compile(r"retry_after=(\d+)")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _failure_message(exc: Exception) -> str:
    """Return a short, non-secret error description for the outbox row.

    Provider exceptions only contain HTTP status/provider codes, never API keys,
    verification codes, reset tokens or message bodies.
    """
    prefix = ""
    message = str(exc) or type(exc).__name__
    if isinstance(exc, EmailDeliveryError):
        if not exc.retryable:
            prefix = _PERMANENT_PREFIX
        elif exc.ambiguous:
            prefix = "ambiguous: "
        if exc.retry_after_seconds is not None:
            message = f"{message} retry_after={exc.retry_after_seconds}"
    return f"{prefix}{message}"[:500]


def _retry_delay_seconds(
    notification: OrderNotification, *, base_seconds: int, max_seconds: int
) -> int:
    if notification.attempts <= 0:
        return 0
    if base_seconds <= 0:
        delay = 0
    else:
        exponent = min(notification.attempts - 1, 16)
        delay = min(base_seconds * (2**exponent), max_seconds)
    # Honour a provider Retry-After value captured with the failure.
    match = _RETRY_AFTER_RE.search(notification.last_error or "")
    if match:
        delay = max(delay, int(match.group(1)))
    return delay


def _retry_ready(
    notification: OrderNotification,
    *,
    now: datetime,
    base_seconds: int,
    max_seconds: int,
) -> bool:
    delay = _retry_delay_seconds(
        notification, base_seconds=base_seconds, max_seconds=max_seconds
    )
    if delay <= 0:
        return True
    return _ensure_utc(notification.updated_at) + timedelta(seconds=delay) <= now


async def enqueue_order_confirmation(
    db: AsyncSession, *, order_id: UUID
) -> OrderNotification:
    order = await repo.get_order_by_id(db, order_id)
    if order is None or order.status not in _COMMITTED_STATUSES:
        raise ValueError("order confirmation can only be enqueued for a confirmed order")
    if not await repo.list_order_tickets(db, order.id):
        raise ValueError("order confirmation requires at least one issued ticket")

    existing = await repo.get_order_notification(
        db, order_id=order_id, kind=KIND_ORDER_CONFIRMATION
    )
    if existing is not None:
        return existing
    try:
        async with db.begin_nested():
            return await repo.create_order_notification(
                db, order_id=order_id, kind=KIND_ORDER_CONFIRMATION
            )
    except IntegrityError:
        existing = await repo.get_order_notification(
            db, order_id=order_id, kind=KIND_ORDER_CONFIRMATION
        )
        if existing is not None:
            return existing
        raise


async def enqueue(
    db: AsyncSession,
    *,
    order_id: UUID,
    kind: str,
    payload: dict[str, Any] | None = None,
    dedupe_key: str = "",
) -> OrderNotification:
    """Idempotently add a lifecycle notification to the transactional outbox.

    Written in the caller's transaction, so the email exists if and only if
    the business change commits. ``(order_id, kind, dedupe_key)`` is unique:
    retried operations never queue a second copy.
    """
    if kind not in LIFECYCLE_KINDS:
        raise ValueError(f"unsupported notification kind {kind!r}")
    existing = await repo.get_order_notification(
        db, order_id=order_id, kind=kind, dedupe_key=dedupe_key
    )
    if existing is not None:
        return existing
    try:
        async with db.begin_nested():
            return await repo.create_order_notification(
                db, order_id=order_id, kind=kind, dedupe_key=dedupe_key, payload=payload
            )
    except IntegrityError:
        existing = await repo.get_order_notification(
            db, order_id=order_id, kind=kind, dedupe_key=dedupe_key
        )
        if existing is not None:
            return existing
        raise


async def enqueue_event_updated(
    db: AsyncSession, event: Event, changed_fields: list[str], *, update_key: str
) -> int:
    """Queue one EVENT_UPDATED email per confirmed order of the event."""
    order_ids = (
        await db.execute(
            select(Order.id).where(
                Order.event_id == event.id, Order.status.in_(tuple(_COMMITTED_STATUSES))
            )
        )
    ).scalars().all()
    for order_id in order_ids:
        await enqueue(
            db,
            order_id=order_id,
            kind=KIND_EVENT_UPDATED,
            dedupe_key=update_key,
            payload={"changed": changed_fields, "updated_at": _now().isoformat()},
        )
    return len(order_ids)


def _format_detail(key: str, value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(str(item).replace("_", " ") for item in value)
    if key.endswith("_at") and isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return value
        return _ensure_utc(parsed).strftime("%Y-%m-%d %H:%M UTC")
    return str(value)


async def _buyer_locale(db: AsyncSession, order: Order) -> str:
    """The buyer's preferred email language; English for guests or unknown."""
    if order.buyer_user_id is None:
        return "en"
    buyer = await repo.get_user_by_id(db, order.buyer_user_id)
    return buyer.locale if buyer is not None and buyer.locale else "en"


async def _deliver_lifecycle(
    db: AsyncSession, notification: OrderNotification, provider: EmailProvider
) -> bool:
    order = await repo.get_order_by_id(db, notification.order_id)
    event = await repo.get_event_by_id(db, order.event_id) if order is not None else None
    now = _now()
    notification.attempts += 1
    notification.updated_at = now
    if order is None or event is None:
        notification.status = "FAILED"
        notification.last_error = f"{_PERMANENT_PREFIX}order or event not found"
        return False
    details = {
        key: _format_detail(key, value)
        for key, value in (notification.payload or {}).items()
        if value is not None
    }
    message = NotificationEmail(
        email=order.buyer_email,
        kind=notification.kind,
        order_number=order.order_number,
        event_name=event.title,
        event_starts_at=event.starts_at,
        venue=event.venue_name or event.city,
        details=details,
        locale=await _buyer_locale(db, order),
    )
    try:
        await provider.send_notification(message)
    except Exception as exc:
        notification.status = "FAILED"
        notification.last_error = _failure_message(exc)
        logger.warning(
            "%s email failed for order %s (attempt %d)",
            notification.kind, order.id, notification.attempts,
        )
        return False
    notification.status = "SENT"
    notification.sent_at = now
    notification.last_error = None
    return True


async def deliver_notification(
    db: AsyncSession,
    notification: OrderNotification,
    provider: EmailProvider,
    *,
    download_base: str,
) -> bool:
    if notification.status == "SENT":
        return False
    if notification.kind in LIFECYCLE_KINDS:
        return await _deliver_lifecycle(db, notification, provider)

    order = await repo.get_order_by_id(db, notification.order_id)
    if order is None or order.status not in _COMMITTED_STATUSES:
        return False
    event = await repo.get_event_by_id(db, order.event_id)
    if event is None:
        notification.status = "FAILED"
        notification.attempts += 1
        notification.last_error = "event not found"
        notification.updated_at = _now()
        return False

    tickets = await repo.list_order_tickets(db, order.id)
    if not tickets:
        notification.status = "FAILED"
        notification.attempts += 1
        notification.last_error = "no admission tickets issued"
        notification.updated_at = _now()
        return False

    items = await repo.list_order_items(db, order.id)
    items_by_id = {item.id: item for item in items}
    ticket_messages: list[EmailTicket] = []
    for ticket in tickets:
        item = items_by_id.get(ticket.order_item_id)
        attendee = (
            await repo.get_attendee(db, item.attendee_id)
            if item is not None and item.attendee_id is not None
            else None
        )
        ticket_messages.append(
            EmailTicket(
                public_id=str(ticket.public_id),
                ticket_type_name=item.ticket_type_name if item else "Ticket",
                attendee_name=attendee.full_name if attendee else None,
                download_url=f"{download_base.rstrip('/')}/tickets/{ticket.public_id}/pdf",
            )
        )

    message = OrderConfirmationEmail(
        email=order.buyer_email,
        order_number=order.order_number,
        event_name=event.title,
        event_starts_at=event.starts_at,
        venue=event.venue_name or event.city,
        tickets=ticket_messages,
        locale=await _buyer_locale(db, order),
    )
    now = _now()
    notification.attempts += 1
    notification.updated_at = now
    try:
        await provider.send_order_confirmation(message)
    except Exception as exc:
        notification.status = "FAILED"
        notification.last_error = _failure_message(exc)
        if isinstance(exc, EmailDeliveryError) and not exc.retryable:
            logger.error(
                "order confirmation email has a permanent delivery error for order %s",
                order.id,
            )
        else:
            logger.warning(
                "order confirmation email failed for order %s (attempt %d)",
                order.id,
                notification.attempts,
            )
        return False

    notification.status = "SENT"
    notification.sent_at = now
    notification.last_error = None
    return True


async def deliver_order_confirmation(
    db: AsyncSession,
    *,
    order_id: UUID,
    provider: EmailProvider,
    download_base: str,
) -> bool:
    notification = await repo.get_order_notification(
        db, order_id=order_id, kind=KIND_ORDER_CONFIRMATION, for_update=True
    )
    if notification is None:
        notification = await enqueue_order_confirmation(db, order_id=order_id)
    return await deliver_notification(
        db, notification, provider, download_base=download_base
    )


async def deliver_pending_for_order(
    db: AsyncSession, *, order_id: UUID, provider: EmailProvider, download_base: str
) -> int:
    """Best-effort immediate delivery after the business transaction committed.

    Failures stay in the outbox for the retry worker; nothing here can roll
    back an order, payment or refund.
    """
    sent = 0
    for notification in await repo.list_order_pending_notifications(db, order_id):
        if await deliver_notification(db, notification, provider, download_base=download_base):
            sent += 1
    return sent


async def process_pending_notifications(
    db: AsyncSession,
    *,
    provider: EmailProvider,
    download_base: str,
    max_attempts: int,
    limit: int = 100,
    retry_base_seconds: int = 0,
    retry_max_seconds: int = 3600,
) -> int:
    notifications = await repo.list_retryable_order_notifications(
        db, max_attempts=max_attempts, limit=limit
    )
    now = _now()
    sent = 0
    for notification in notifications:
        if not _retry_ready(
            notification,
            now=now,
            base_seconds=retry_base_seconds,
            max_seconds=retry_max_seconds,
        ):
            continue
        if await deliver_notification(
            db, notification, provider, download_base=download_base
        ):
            sent += 1
    return sent


# ---------- In-app notifications (SCRUM-16) ----------
#
# Support activity is not tied to an order, so it cannot use the order email
# outbox above. It uses the pre-existing ``notifications`` table instead:
# one IN_APP row per recipient, created in the caller's transaction (so it
# exists if and only if the business change commits) and read via
# ``GET /users/me/notifications``.

KIND_SUPPORT_CASE_CREATED = "SUPPORT_CASE_CREATED"
KIND_SUPPORT_MESSAGE_RECEIVED = "SUPPORT_MESSAGE_RECEIVED"
KIND_SUPPORT_CASE_ASSIGNED = "SUPPORT_CASE_ASSIGNED"
KIND_SUPPORT_CASE_UNASSIGNED = "SUPPORT_CASE_UNASSIGNED"
KIND_SUPPORT_CASE_STATUS_CHANGED = "SUPPORT_CASE_STATUS_CHANGED"
KIND_SUPPORT_CASE_RESOLVED = "SUPPORT_CASE_RESOLVED"
KIND_SUPPORT_CASE_ESCALATED = "SUPPORT_CASE_ESCALATED"


async def notify_users(
    db: AsyncSession,
    *,
    user_ids: Iterable[UUID | None],
    kind: str,
    payload: dict[str, Any],
    exclude: Iterable[UUID | None] = (),
) -> list[UUID]:
    """Create one in-app notification per distinct, active recipient.

    ``exclude`` (typically the actor) is removed, duplicates collapse to one
    row, and blocked/deleted accounts are skipped. Returns the recipients.
    """
    excluded = {user_id for user_id in exclude if user_id is not None}
    wanted = {user_id for user_id in user_ids if user_id is not None} - excluded
    if not wanted:
        return []
    active = (
        await db.execute(
            select(User.id).where(User.id.in_(wanted), User.status == UserStatus.ACTIVE)
        )
    ).scalars().all()
    now = _now()
    recipients = sorted(active, key=str)
    db.add_all(
        [
            Notification(
                user_id=user_id,
                type=kind,
                channel=NotificationChannel.IN_APP,
                payload=payload,
                # In-app rows are delivered by being stored.
                status=NotificationStatus.SENT,
                sent_at=now,
                created_at=now,
            )
            for user_id in recipients
        ]
    )
    return list(recipients)


async def list_user_notifications(
    db: AsyncSession, user_id: UUID, *, unread_only: bool, limit: int, offset: int
) -> tuple[list[Notification], int, int]:
    """Return (page, total, unread_count) of the user's in-app notifications."""
    base = select(Notification).where(
        Notification.user_id == user_id, Notification.channel == NotificationChannel.IN_APP
    )
    if unread_only:
        base = base.where(Notification.read_at.is_(None))
    total = int(
        await db.scalar(select(func.count()).select_from(base.subquery())) or 0
    )
    unread = int(
        await db.scalar(
            select(func.count())
            .select_from(Notification)
            .where(
                Notification.user_id == user_id,
                Notification.channel == NotificationChannel.IN_APP,
                Notification.read_at.is_(None),
            )
        )
        or 0
    )
    rows = (
        await db.execute(
            base.order_by(Notification.created_at.desc(), Notification.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()
    return list(rows), total, unread


async def mark_notification_read(
    db: AsyncSession, user_id: UUID, notification_id: UUID
) -> Notification | None:
    """Mark one of the caller's notifications read (idempotent)."""
    notification = await db.scalar(
        select(Notification).where(
            Notification.id == notification_id, Notification.user_id == user_id
        )
    )
    if notification is None:
        return None
    if notification.read_at is None:
        notification.read_at = _now()
        notification.status = NotificationStatus.READ
        await db.flush()
    return notification
