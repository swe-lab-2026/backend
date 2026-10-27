import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import repositories as repo
from app.db.models import OrderNotification, OrderStatus
from app.services.email import EmailProvider, EmailTicket, OrderConfirmationEmail

logger = logging.getLogger("app.notifications")

KIND_ORDER_CONFIRMATION = "ORDER_CONFIRMATION"
_COMMITTED_STATUSES = {OrderStatus.PAID, OrderStatus.CONFIRMED}


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def enqueue_order_confirmation(
    db: AsyncSession, *, order_id: UUID
) -> OrderNotification:
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


async def deliver_notification(
    db: AsyncSession,
    notification: OrderNotification,
    provider: EmailProvider,
    *,
    download_base: str,
) -> bool:
    if notification.status == "SENT":
        return False

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
    )
    now = _now()
    notification.attempts += 1
    notification.updated_at = now
    try:
        await provider.send_order_confirmation(message)
    except Exception as exc:
        notification.status = "FAILED"
        notification.last_error = str(exc)[:500]
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


async def process_pending_notifications(
    db: AsyncSession,
    *,
    provider: EmailProvider,
    download_base: str,
    max_attempts: int,
    limit: int = 100,
) -> int:
    notifications = await repo.list_retryable_order_notifications(
        db, max_attempts=max_attempts, limit=limit
    )
    sent = 0
    for notification in notifications:
        if await deliver_notification(
            db, notification, provider, download_base=download_base
        ):
            sent += 1
    return sent
