import base64
import hashlib
import hmac
import logging
import uuid
from datetime import datetime, timezone
from typing import cast
from uuid import UUID

from sqlalchemy import CursorResult, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.db import repositories as repo
from app.db.models import (
    CheckinAction,
    CheckinRecord,
    Event,
    Order,
    OrderItem,
    OrderStatus,
    Ticket,
    TicketStatus,
    TicketType,
    User,
    UserRole,
)
from app.schemas.tickets import TicketResponse

logger = logging.getLogger("app.tickets")

_QR_VERSION = "v1"
_COMMITTED_ORDER_STATUSES = {OrderStatus.PAID, OrderStatus.CONFIRMED}
_SIGNATURE_BYTES = 32


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sign(value: str, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def build_qr_payload(public_id: UUID, secret: str) -> str:
    """Return the stable, signed payload encoded in a ticket QR code.

    The payload contains only the ticket's public identifier and an HMAC. It is
    deterministic, so regenerating a PDF always renders the same QR data.
    """
    subject = f"{_QR_VERSION}.{public_id}"
    return f"{subject}.{_sign(subject, secret)}"


def hash_qr_payload(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _verify_qr_payload(payload: str, secret: str) -> UUID:
    parts = payload.strip().split(".")
    if len(parts) != 3 or parts[0] != _QR_VERSION:
        raise ApiError(401, "INVALID_TICKET_QR", "Invalid ticket QR code")
    try:
        public_id = UUID(parts[1])
    except ValueError as exc:
        raise ApiError(401, "INVALID_TICKET_QR", "Invalid ticket QR code") from exc

    subject = f"{_QR_VERSION}.{public_id}"
    expected = _sign(subject, secret)
    if not hmac.compare_digest(expected, parts[2]):
        raise ApiError(401, "INVALID_TICKET_QR", "Invalid ticket QR code")
    return public_id


async def _resolve_ticket(
    db: AsyncSession, payload: str, secret: str
) -> Ticket:
    """Resolve a scanned payload to its ticket.

    Newly issued tickets carry a signed ``v1`` payload. Opaque random tokens
    from pre-existing databases remain valid through a SHA-256 lookup, which is
    the same defense: the database only stores the token's hash.
    """
    payload = payload.strip()
    try:
        public_id = _verify_qr_payload(payload, secret)
    except ApiError:
        public_id = None

    if public_id is not None:
        ticket = await repo.get_ticket_by_public_id(db, public_id)
        if ticket is None:
            raise ApiError(404, "TICKET_NOT_FOUND", "Ticket not found")
        if hmac.compare_digest(ticket.qr_token_hash, hash_qr_payload(payload)):
            return ticket
        raise ApiError(401, "INVALID_TICKET_QR", "Invalid ticket QR code")

    ticket = await repo.get_ticket_by_qr_hash(db, hash_qr_payload(payload))
    if ticket is None:
        raise ApiError(401, "INVALID_TICKET_QR", "Invalid ticket QR code")
    return ticket


def _attendee_name(user: User | None, fallback_email: str) -> str:
    if user is None:
        return fallback_email
    name = f"{user.first_name} {user.last_name}".strip()
    return name or fallback_email


async def _attendee_for_item(
    db: AsyncSession, item: OrderItem, order: Order
) -> UUID:
    if item.attendee_id is not None:
        return item.attendee_id

    user = (
        await repo.get_user_by_id(db, order.buyer_user_id)
        if order.buyer_user_id is not None
        else None
    )
    attendee = await repo.create_attendee(
        db,
        user_id=user.id if user else None,
        full_name=_attendee_name(user, order.buyer_email),
        email=user.email if user else order.buyer_email,
        phone=user.phone if user else None,
    )
    item.attendee_id = attendee.id
    return attendee.id


async def ensure_order_tickets(
    db: AsyncSession, order: Order, *, qr_secret: str
) -> list[Ticket]:
    """Create exactly one ticket per order item, skipping ones already issued.

    Order item uniqueness on ``tickets.order_item_id`` is the database-level
    guard against duplicate issuance. The nested transaction lets a concurrent
    loser recover the winner's row instead of aborting the whole request.
    """
    if order.status not in _COMMITTED_ORDER_STATUSES:
        raise ApiError(
            409,
            "ORDER_NOT_CONFIRMED",
            "Admission tickets can only be issued for confirmed orders",
        )

    items = await repo.list_order_items(db, order.id)
    existing = await repo.list_order_tickets(db, order.id)
    tickets_by_item = {ticket.order_item_id: ticket for ticket in existing}

    for item in items:
        if item.id in tickets_by_item:
            continue
        await _attendee_for_item(db, item, order)
        public_id = uuid.uuid4()
        payload = build_qr_payload(public_id, qr_secret)
        ticket = Ticket(
            order_item_id=item.id,
            public_id=public_id,
            qr_token_hash=hash_qr_payload(payload),
        )
        try:
            async with db.begin_nested():
                db.add(ticket)
                await db.flush()
        except IntegrityError:
            winner = await repo.list_order_tickets(db, order.id)
            tickets_by_item = {row.order_item_id: row for row in winner}
            continue
        tickets_by_item[item.id] = ticket

    return sorted(
        tickets_by_item.values(),
        key=lambda row: (row.issued_at, str(row.id)),
    )


async def build_ticket_response(
    db: AsyncSession,
    ticket: Ticket,
    *,
    qr_secret: str,
    download_base: str,
) -> TicketResponse:
    context = await repo.get_ticket_order_item(db, ticket)
    if context is None:
        raise ApiError(404, "TICKET_NOT_FOUND", "Ticket not found")
    item, order, event, ticket_type = context
    attendee = (
        await repo.get_attendee(db, item.attendee_id)
        if item.attendee_id is not None
        else None
    )
    payload = build_qr_payload(ticket.public_id, qr_secret)
    return TicketResponse(
        ticket_id=ticket.id,
        public_id=ticket.public_id,
        order_id=order.id,
        order_number=order.order_number,
        event_id=event.id,
        event_title=event.title,
        event_starts_at=event.starts_at,
        event_ends_at=event.ends_at,
        venue_name=event.venue_name,
        venue_address=event.venue_address,
        city=event.city,
        timezone=event.timezone,
        ticket_type_id=ticket_type.id,
        ticket_type_name=item.ticket_type_name,
        attendee_name=attendee.full_name if attendee else None,
        attendee_email=attendee.email if attendee else order.buyer_email,
        status=ticket.status.value,
        issued_at=ticket.issued_at,
        checked_in_at=ticket.checked_in_at,
        cancelled_at=ticket.cancelled_at,
        qr_payload=payload,
        download_url=f"{download_base.rstrip('/')}/tickets/{ticket.public_id}/pdf",
    )


async def list_order_ticket_responses(
    db: AsyncSession,
    order: Order,
    *,
    qr_secret: str,
    download_base: str,
) -> list[TicketResponse]:
    tickets = await repo.list_order_tickets(db, order.id)
    return [
        await build_ticket_response(
            db, ticket, qr_secret=qr_secret, download_base=download_base
        )
        for ticket in tickets
    ]


def _can_view_ticket(user: User, order: Order, event: Event) -> bool:
    if user.global_role == UserRole.PLATFORM_ADMIN:
        return True
    if order.buyer_user_id == user.id:
        return True
    return event.owner_user_id == user.id


async def get_ticket_context(
    db: AsyncSession, user: User, public_id: UUID
) -> tuple[Ticket, Order, Event, TicketType]:
    ticket = await repo.get_ticket_by_public_id(db, public_id)
    if ticket is None:
        raise ApiError(404, "TICKET_NOT_FOUND", "Ticket not found")
    context = await repo.get_ticket_order_item(db, ticket)
    if context is None:
        raise ApiError(404, "TICKET_NOT_FOUND", "Ticket not found")
    _item, order, event, ticket_type = context
    if not _can_view_ticket(user, order, event):
        raise ApiError(403, "INSUFFICIENT_PERMISSIONS", "Insufficient permissions")
    return ticket, order, event, ticket_type


async def validate_ticket_qr(
    db: AsyncSession,
    *,
    event: Event,
    qr_payload: str,
    qr_secret: str,
    download_base: str,
) -> tuple[TicketResponse, str | None]:
    ticket = await _resolve_ticket(db, qr_payload, qr_secret)

    context = await repo.get_ticket_order_item(db, ticket)
    if context is None or context[2].id != event.id:
        raise ApiError(404, "TICKET_NOT_FOUND", "Ticket not found")
    response = await build_ticket_response(
        db, ticket, qr_secret=qr_secret, download_base=download_base
    )
    if ticket.status != TicketStatus.VALID:
        return response, f"TICKET_{ticket.status.value}"
    return response, None


async def check_in_ticket(
    db: AsyncSession,
    *,
    event: Event,
    qr_payload: str,
    performed_by: User,
    qr_secret: str,
    download_base: str,
) -> TicketResponse:
    ticket = await _resolve_ticket(db, qr_payload, qr_secret)

    context = await repo.get_ticket_order_item(db, ticket)
    if context is None or context[2].id != event.id:
        raise ApiError(404, "TICKET_NOT_FOUND", "Ticket not found")

    now = _now()
    result = await db.execute(
        update(Ticket)
        .where(Ticket.id == ticket.id, Ticket.status == TicketStatus.VALID)
        .values(
            status=TicketStatus.CHECKED_IN,
            checked_in_at=now,
            updated_at=now,
        )
    )
    if (cast(CursorResult, result).rowcount or 0) != 1:
        await db.refresh(ticket)
        if ticket.status == TicketStatus.CHECKED_IN:
            checked_in_at = (
                ticket.checked_in_at.isoformat() if ticket.checked_in_at else None
            )
            raise ApiError(
                409,
                "TICKET_ALREADY_CHECKED_IN",
                "Ticket has already been checked in",
                details={"checked_in_at": checked_in_at},
            )
        raise ApiError(
            409,
            "TICKET_NOT_VALID",
            f"Ticket cannot be admitted in status {ticket.status.value}",
        )

    db.add(
        CheckinRecord(
            ticket_id=ticket.id,
            event_id=event.id,
            performed_by=performed_by.id,
            action=CheckinAction.CHECK_IN,
        )
    )
    await repo.add_audit_entry(
        db,
        actor_user_id=performed_by.id,
        event_id=event.id,
        entity_type="ticket",
        entity_id=ticket.id,
        action="ticket.checked_in",
        after_data={"status": TicketStatus.CHECKED_IN.value},
    )
    await db.flush()
    await db.refresh(ticket)
    logger.info("ticket %s checked in for event %s", ticket.id, event.id)
    return await build_ticket_response(
        db, ticket, qr_secret=qr_secret, download_base=download_base
    )
