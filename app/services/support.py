"""Support cases and asynchronous messages (SCRUM-16).

Routing
    ATTENDEE_ORGANIZER  a customer <-> organizer staff of one event. Always has
                        an event. Handlers: the event owner and event staff
                        with a support role; Platform Admins only once the
                        case is explicitly escalated.
    PLATFORM            an organizer (or any user with an account/technical
                        issue) <-> Platform Admins. Event staff never see it.

Authorization is relationship based and re-evaluated against PostgreSQL on
every request (removing an EventStaff row revokes access immediately). The
assignee field never grants access on its own. A caller with no relationship
to an existing case gets 403 with no case metadata; listings never include
inaccessible cases because the visibility rule is part of the SQL WHERE.

Lifecycle history (creation, status changes, assignment, escalation) is
written to the existing append-only ``audit_logs`` in the same transaction,
so the case activity feed and the event timeline share one source of truth.
Messages live in ``support_messages`` with a per-case gap-free ``sequence``
assigned under the case row lock: deterministic order and a polling cursor.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.errors import ApiError
from app.db import repositories as repo
from app.db.models import (
    AuditLog,
    Event,
    EventStaff,
    EventStaffRole,
    Order,
    SupportCase,
    SupportCaseStatus,
    SupportCaseType,
    SupportMessage,
    Ticket,
    User,
    UserRole,
    UserStatus,
)
from app.schemas.support import (
    SupportActivityPage,
    SupportActivityResponse,
    SupportAssignableUser,
    SupportCaseAccessResponse,
    SupportCaseCreate,
    SupportCasePage,
    SupportCaseResponse,
    SupportMessagePage,
    SupportMessageResponse,
    SupportMessageSender,
    SupportParticipant,
)
from app.services import events as event_service
from app.services import notifications

logger = logging.getLogger("app.support")

# Event staff roles that may handle attendee support for their event.
# CHECK_IN_STAFF only admits guests and has no support access.
SUPPORT_STAFF_ROLES = (EventStaffRole.EVENT_ADMIN, EventStaffRole.ORGANIZER_STAFF)

REL_REQUESTER = "REQUESTER"
REL_ORGANIZER = "ORGANIZER"
REL_STAFF = "STAFF"
REL_PLATFORM_ADMIN = "PLATFORM_ADMIN"

_AO = SupportCaseType.ATTENDEE_ORGANIZER.value
_PLATFORM = SupportCaseType.PLATFORM.value

S = SupportCaseStatus
_TRANSITIONS: dict[SupportCaseStatus, set[SupportCaseStatus]] = {
    S.OPEN: {S.IN_PROGRESS, S.WAITING_FOR_CUSTOMER, S.RESOLVED},
    S.IN_PROGRESS: {S.WAITING_FOR_CUSTOMER, S.RESOLVED},
    S.WAITING_FOR_CUSTOMER: {S.IN_PROGRESS, S.RESOLVED},
    S.RESOLVED: {S.OPEN, S.IN_PROGRESS},
}

_AUDIT_ACTIONS = (
    "support_case.created",
    "support_case.status_changed",
    "support_case.assigned",
    "support_case.unassigned",
    "support_case.escalated",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def display_name(user: User | None) -> str | None:
    if user is None:
        return None
    return f"{user.first_name} {user.last_name}".strip() or None


# ---------- Access ----------

@dataclass(frozen=True)
class CaseAccess:
    is_requester: bool
    handler_role: str | None  # ORGANIZER | STAFF | PLATFORM_ADMIN

    @property
    def can_view(self) -> bool:
        return self.is_requester or self.handler_role is not None

    @property
    def is_handler(self) -> bool:
        return self.handler_role is not None

    @property
    def relation(self) -> str:
        # The requester speaks as the customer even if they also manage it.
        if self.is_requester:
            return REL_REQUESTER
        assert self.handler_role is not None
        return self.handler_role


def _is_admin(user: User) -> bool:
    return user.global_role == UserRole.PLATFORM_ADMIN


async def resolve_access(db: AsyncSession, user: User, case: SupportCase) -> CaseAccess:
    is_requester = case.requester_user_id == user.id
    handler: str | None = None
    if case.case_type == _AO and case.event_id is not None:
        event = await repo.get_event_by_id(db, case.event_id)
        if event is not None and event.owner_user_id == user.id:
            handler = REL_ORGANIZER
        else:
            staff = await repo.get_event_staff(db, event_id=case.event_id, user_id=user.id)
            if staff is not None and staff.role in SUPPORT_STAFF_ROLES:
                handler = REL_STAFF
        if handler is None and case.escalated_at is not None and _is_admin(user):
            handler = REL_PLATFORM_ADMIN
    elif case.case_type == _PLATFORM and _is_admin(user):
        handler = REL_PLATFORM_ADMIN
    return CaseAccess(is_requester=is_requester, handler_role=handler)


async def _deny(db: AsyncSession, user: User, case: SupportCase) -> None:
    # Deliberately no event_id: a denied probe must not appear on the event
    # timeline, and the response carries no case metadata.
    await repo.add_audit_entry(
        db,
        actor_user_id=user.id,
        entity_type="support_case",
        entity_id=case.id,
        action="support_case.access_denied",
    )
    await db.commit()
    raise ApiError(
        403, "SUPPORT_CASE_ACCESS_DENIED", "You do not have access to this support case"
    )


async def _load_case(
    db: AsyncSession, user: User, case_id: UUID, *, for_update: bool = False
) -> tuple[SupportCase, CaseAccess]:
    case = await db.get(SupportCase, case_id)
    if case is None:
        raise ApiError(404, "SUPPORT_CASE_NOT_FOUND", "Support case not found")
    access = await resolve_access(db, user, case)
    if not access.can_view:
        await _deny(db, user, case)
    if for_update:
        # Serializes writers (message sequence, status, assignment) per case.
        locked = await db.scalar(
            select(SupportCase)
            .where(SupportCase.id == case_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        assert locked is not None
        case = locked
    return case, access


def _visibility_clause(user: User) -> Any:
    """SQL predicate: the cases ``user`` may see. Mirrors ``resolve_access``."""
    owned_events = select(Event.id).where(Event.owner_user_id == user.id)
    staffed_events = select(EventStaff.event_id).where(
        EventStaff.user_id == user.id, EventStaff.role.in_(SUPPORT_STAFF_ROLES)
    )
    clauses = [
        SupportCase.requester_user_id == user.id,
        and_(
            SupportCase.case_type == _AO,
            or_(
                SupportCase.event_id.in_(owned_events),
                SupportCase.event_id.in_(staffed_events),
            ),
        ),
    ]
    if _is_admin(user):
        clauses.append(SupportCase.case_type == _PLATFORM)
        clauses.append(
            and_(SupportCase.case_type == _AO, SupportCase.escalated_at.is_not(None))
        )
    return or_(*clauses)


def _handler_clause(user: User) -> Any:
    """Cases the user handles (excludes cases they merely requested)."""
    owned_events = select(Event.id).where(Event.owner_user_id == user.id)
    staffed_events = select(EventStaff.event_id).where(
        EventStaff.user_id == user.id, EventStaff.role.in_(SUPPORT_STAFF_ROLES)
    )
    clauses = [
        and_(
            SupportCase.case_type == _AO,
            or_(
                SupportCase.event_id.in_(owned_events),
                SupportCase.event_id.in_(staffed_events),
            ),
        )
    ]
    if _is_admin(user):
        clauses.append(SupportCase.case_type == _PLATFORM)
        clauses.append(
            and_(SupportCase.case_type == _AO, SupportCase.escalated_at.is_not(None))
        )
    return and_(or_(*clauses), SupportCase.requester_user_id.is_distinct_from(user.id))


# ---------- Participants (for assignment and notifications) ----------

async def _active_admin_ids(db: AsyncSession) -> list[UUID]:
    rows = await db.execute(
        select(User.id).where(
            User.global_role == UserRole.PLATFORM_ADMIN, User.status == UserStatus.ACTIVE
        )
    )
    return list(rows.scalars().all())


async def _organizer_handler_ids(db: AsyncSession, event_id: UUID) -> list[UUID]:
    """Event owner plus support staff of the event."""
    owner = await db.scalar(select(Event.owner_user_id).where(Event.id == event_id))
    staff = (
        await db.execute(
            select(EventStaff.user_id).where(
                EventStaff.event_id == event_id, EventStaff.role.in_(SUPPORT_STAFF_ROLES)
            )
        )
    ).scalars().all()
    return [user_id for user_id in [owner, *staff] if user_id is not None]


async def _handler_ids(db: AsyncSession, case: SupportCase) -> set[UUID]:
    """Everyone currently authorized to handle the case."""
    if case.case_type == _AO and case.event_id is not None:
        handlers = set(await _organizer_handler_ids(db, case.event_id))
        if case.escalated_at is not None:
            handlers.update(await _active_admin_ids(db))
        return handlers
    if case.case_type == _PLATFORM:
        return set(await _active_admin_ids(db))
    return set()


async def _notify(
    db: AsyncSession,
    case: SupportCase,
    *,
    kind: str,
    recipients: set[UUID | None],
    actor: User,
    extra: dict[str, Any] | None = None,
) -> list[UUID]:
    """Notify ``recipients`` that are still authorized for the case."""
    authorized = await _handler_ids(db, case)
    if case.requester_user_id is not None:
        authorized.add(case.requester_user_id)
    allowed = {user_id for user_id in recipients if user_id in authorized}
    payload: dict[str, Any] = {
        "case_id": str(case.id),
        "subject": case.subject,
        "case_type": case.case_type,
        "status": case.status.value,
        "event_id": str(case.event_id) if case.event_id else None,
        "actor_user_id": str(actor.id),
    }
    payload.update(extra or {})
    return await notifications.notify_users(
        db, user_ids=allowed, kind=kind, payload=payload, exclude=[actor.id]
    )


async def _counterpart_ids(
    db: AsyncSession, case: SupportCase, access: CaseAccess
) -> set[UUID | None]:
    """Who should hear about an action: the 'other side' of the conversation."""
    if access.is_requester and not access.is_handler:
        if case.assignee_user_id is not None:
            return {case.assignee_user_id}
        if case.case_type == _AO and case.event_id is not None:
            return set(await _organizer_handler_ids(db, case.event_id))
        return set(await _active_admin_ids(db))
    return {case.requester_user_id, case.assignee_user_id}


def _actor_type(access: CaseAccess) -> str:
    if access.relation == REL_PLATFORM_ADMIN:
        return "PLATFORM_ADMIN"
    if access.relation in (REL_ORGANIZER, REL_STAFF):
        return "ORGANIZER"
    return "CUSTOMER"


async def _audit(
    db: AsyncSession,
    case: SupportCase,
    *,
    actor: User,
    actor_type: str,
    action: str,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    await repo.add_audit_entry(
        db,
        actor_user_id=actor.id,
        actor_type=actor_type,
        event_id=case.event_id,
        entity_type="support_case",
        entity_id=case.id,
        action=action,
        before_data=before,
        after_data=after,
        metadata={"case_type": case.case_type, **(metadata or {})},
    )


# ---------- Context resolution ----------

@dataclass
class _Context:
    event: Event | None = None
    order: Order | None = None
    ticket: Ticket | None = None


def _context_not_found(kind: str) -> ApiError:
    # Missing and not-yours are indistinguishable (no IDOR oracle).
    return ApiError(404, "SUPPORT_CONTEXT_NOT_FOUND", f"Referenced {kind} not found")


def _context_mismatch(message: str) -> ApiError:
    return ApiError(422, "SUPPORT_CONTEXT_MISMATCH", message)


async def _resolve_context(
    db: AsyncSession, user: User, payload: SupportCaseCreate
) -> tuple[_Context, str]:
    """Resolve ticket -> order -> event from the database and pick the route.

    The most specific identifier is authoritative; any other identifier
    supplied must agree with it. Ownership is checked on the authoritative
    entity before consistency, so mismatch errors never reveal foreign data.
    """
    ctx = _Context()
    if payload.ticket_id is not None:
        ticket = await db.get(Ticket, payload.ticket_id)
        row = (
            await repo.get_ticket_order_item(db, ticket) if ticket is not None else None
        )
        if ticket is None or row is None:
            raise _context_not_found("ticket")
        _item, ctx.order, ctx.event, _ticket_type = row
        ctx.ticket = ticket
    elif payload.order_id is not None:
        ctx.order = await repo.get_order_by_id(db, payload.order_id)
        if ctx.order is None:
            raise _context_not_found("order")
        ctx.event = await repo.get_event_by_id(db, ctx.order.event_id)
    elif payload.event_id is not None:
        ctx.event = await repo.get_event_by_id(db, payload.event_id)
        if ctx.event is None:
            raise _context_not_found("event")

    requested = payload.case_type.value if payload.case_type else None
    if ctx.event is None:
        if requested == _AO:
            raise ApiError(
                422,
                "SUPPORT_CONTEXT_REQUIRED",
                "An attendee case needs an event, order or ticket",
            )
        return ctx, _PLATFORM

    is_owner = ctx.event.owner_user_id == user.id
    case_type = requested or (_PLATFORM if is_owner else _AO)
    primary = "ticket" if ctx.ticket else "order" if ctx.order else "event"

    # Ownership of the authoritative entity.
    if case_type == _PLATFORM:
        # Organizer-to-platform with event context: only the event's owner.
        if not is_owner:
            raise _context_not_found(primary)
    else:
        if is_owner:
            raise ApiError(
                422,
                "SUPPORT_ROUTE_INVALID",
                "Organizers contact Platform support about their own events",
            )
        if ctx.order is not None:
            if ctx.order.buyer_user_id != user.id:
                raise _context_not_found(primary)
        else:
            has_order = await db.scalar(
                select(func.count())
                .select_from(Order)
                .where(Order.event_id == ctx.event.id, Order.buyer_user_id == user.id)
            )
            if not has_order and not event_service.is_publicly_visible(ctx.event):
                raise _context_not_found("event")

    # Consistency of the remaining identifiers.
    if ctx.ticket is not None and payload.order_id is not None:
        assert ctx.order is not None
        if payload.order_id != ctx.order.id:
            raise _context_mismatch("Ticket does not belong to the given order")
    if payload.event_id is not None and payload.event_id != ctx.event.id:
        raise _context_mismatch(f"The {primary} does not belong to the given event")
    return ctx, case_type


# ---------- Responses ----------

def _case_query() -> tuple[Select[Any], Any, Any]:
    requester = aliased(User)
    assignee = aliased(User)
    message_count = (
        select(func.coalesce(func.max(SupportMessage.sequence), 0))
        .where(SupportMessage.case_id == SupportCase.id)
        .correlate(SupportCase)
        .scalar_subquery()
    )
    stmt = (
        select(SupportCase, Event.title, Order.order_number, requester, assignee, message_count)
        .outerjoin(Event, Event.id == SupportCase.event_id)
        .outerjoin(Order, Order.id == SupportCase.order_id)
        .outerjoin(requester, requester.id == SupportCase.requester_user_id)
        .outerjoin(assignee, assignee.id == SupportCase.assignee_user_id)
    )
    return stmt, requester, assignee


def _participant(user: User | None) -> SupportParticipant | None:
    if user is None:
        return None
    return SupportParticipant(user_id=user.id, display_name=display_name(user) or "")


def _case_response(row: Any, access: CaseAccess | None = None) -> SupportCaseResponse:
    case, event_title, order_number, requester, assignee, message_count = row
    status = case.status.value
    return SupportCaseResponse(
        id=case.id,
        case_type=case.case_type,
        category=case.category,
        subject=case.subject,
        status=status,
        requester=_participant(requester),
        assignee=_participant(assignee),
        event_id=case.event_id,
        event_title=event_title,
        order_id=case.order_id,
        order_number=order_number,
        ticket_id=case.ticket_id,
        is_escalated=case.escalated_at is not None,
        escalated_at=case.escalated_at,
        escalation_reason=case.escalation_reason,
        message_count=int(message_count or 0),
        created_at=case.created_at,
        updated_at=case.updated_at,
        resolved_at=case.resolved_at,
        access=_access_response(case, access) if access else None,
    )


def _access_response(case: SupportCase, access: CaseAccess) -> SupportCaseAccessResponse:
    return SupportCaseAccessResponse(
        relation=access.relation,
        can_manage=access.is_handler,
        can_escalate=_can_escalate(case, access),
    )


def _can_escalate(case: SupportCase, access: CaseAccess) -> bool:
    return (
        case.case_type == _AO
        and case.escalated_at is None
        and case.status != SupportCaseStatus.RESOLVED
        and (access.is_requester or access.handler_role in (REL_ORGANIZER, REL_STAFF))
    )


async def build_case_response(
    db: AsyncSession, case: SupportCase, access: CaseAccess
) -> SupportCaseResponse:
    stmt, _r, _a = _case_query()
    row = (await db.execute(stmt.where(SupportCase.id == case.id))).one()
    return _case_response(row, access)


# ---------- Use cases ----------

async def create_case(
    db: AsyncSession, user: User, payload: SupportCaseCreate
) -> tuple[SupportCase, CaseAccess]:
    ctx, case_type = await _resolve_context(db, user, payload)
    now = _now()
    case = SupportCase(
        requester_user_id=user.id,
        case_type=case_type,
        category=payload.category.value,
        subject=payload.subject,
        status=SupportCaseStatus.OPEN,
        event_id=ctx.event.id if ctx.event else None,
        order_id=ctx.order.id if ctx.order else None,
        ticket_id=ctx.ticket.id if ctx.ticket else None,
        created_at=now,
        updated_at=now,
    )
    db.add(case)
    await db.flush()
    access = await resolve_access(db, user, case)
    db.add(
        SupportMessage(
            case_id=case.id,
            sender_user_id=user.id,
            sender_role=access.relation,
            sequence=1,
            body=payload.message,
            created_at=now,
        )
    )
    await _audit(
        db, case, actor=user, actor_type=_actor_type(access), action="support_case.created",
        after={"status": case.status.value},
        metadata={
            "category": case.category,
            "order_id": str(case.order_id) if case.order_id else None,
            "ticket_id": str(case.ticket_id) if case.ticket_id else None,
        },
    )
    await db.flush()
    await _notify(
        db, case, kind=notifications.KIND_SUPPORT_CASE_CREATED,
        recipients=set(await _handler_ids(db, case)), actor=user,
    )
    logger.info("support case %s (%s) opened by %s", case.id, case_type, user.id)
    return case, access


async def get_case(
    db: AsyncSession, user: User, case_id: UUID
) -> tuple[SupportCase, CaseAccess]:
    return await _load_case(db, user, case_id)


async def list_cases(
    db: AsyncSession,
    user: User,
    *,
    status: list[SupportCaseStatus] | None = None,
    category: str | None = None,
    case_type: str | None = None,
    event_id: UUID | None = None,
    escalated: bool | None = None,
    assigned_to_me: bool = False,
    role: str | None = None,
    q: str | None = None,
    sort: str = "-updated_at",
    limit: int = 20,
    offset: int = 0,
) -> SupportCasePage:
    """Accessible cases only; the visibility rule is part of the SQL."""
    conditions: list[Any] = [_visibility_clause(user)]
    if status:
        conditions.append(SupportCase.status.in_(status))
    if category:
        conditions.append(SupportCase.category == category)
    if case_type:
        conditions.append(SupportCase.case_type == case_type)
    if event_id is not None:
        conditions.append(SupportCase.event_id == event_id)
    if escalated is True:
        conditions.append(SupportCase.escalated_at.is_not(None))
    elif escalated is False:
        conditions.append(SupportCase.escalated_at.is_(None))
    if assigned_to_me:
        conditions.append(SupportCase.assignee_user_id == user.id)
    if role == "requester":
        conditions.append(SupportCase.requester_user_id == user.id)
    elif role == "handler":
        conditions.append(_handler_clause(user))
    if q:
        pattern = f"%{repo._escape_like(q.strip())}%"
        conditions.append(SupportCase.subject.ilike(pattern, escape="\\"))

    total = int(
        await db.scalar(
            select(func.count()).select_from(SupportCase).where(*conditions)
        )
        or 0
    )
    order_column = {
        "updated_at": SupportCase.updated_at,
        "created_at": SupportCase.created_at,
    }[sort.lstrip("-")]
    ordering = order_column.desc() if sort.startswith("-") else order_column.asc()
    id_order = SupportCase.id.desc() if sort.startswith("-") else SupportCase.id.asc()
    stmt, _r, _a = _case_query()
    rows = (
        await db.execute(
            stmt.where(*conditions).order_by(ordering, id_order).limit(limit).offset(offset)
        )
    ).all()
    # Per-case ``access`` is only computed on the detail endpoint, keeping the
    # listing at a fixed number of queries.
    items = [_case_response(row) for row in rows]
    return SupportCasePage(items=items, total=total, limit=limit, offset=offset)


async def list_messages(
    db: AsyncSession,
    user: User,
    case_id: UUID,
    *,
    after_sequence: int | None,
    before_sequence: int | None,
    since: datetime | None,
    limit: int,
) -> SupportMessagePage:
    case, _access = await _load_case(db, user, case_id)
    if after_sequence is not None and before_sequence is not None:
        raise ApiError(
            422, "VALIDATION_ERROR", "Use either after_sequence or before_sequence, not both"
        )
    latest = int(
        await db.scalar(
            select(func.coalesce(func.max(SupportMessage.sequence), 0)).where(
                SupportMessage.case_id == case.id
            )
        )
        or 0
    )
    sender = aliased(User)
    stmt = (
        select(SupportMessage, sender)
        .outerjoin(sender, sender.id == SupportMessage.sender_user_id)
        .where(SupportMessage.case_id == case.id)
    )
    if since is not None:
        stmt = stmt.where(SupportMessage.created_at > since)
    backwards = before_sequence is not None
    if after_sequence is not None:
        stmt = stmt.where(SupportMessage.sequence > after_sequence)
    if backwards:
        stmt = stmt.where(SupportMessage.sequence < before_sequence)
        stmt = stmt.order_by(SupportMessage.sequence.desc())
    else:
        stmt = stmt.order_by(SupportMessage.sequence.asc())
    rows = list((await db.execute(stmt.limit(limit + 1))).all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    if backwards:
        rows.reverse()
    items = [
        SupportMessageResponse(
            id=message.id,
            case_id=message.case_id,
            sequence=message.sequence,
            sender=SupportMessageSender(
                user_id=message.sender_user_id,
                display_name=display_name(author),
                role=message.sender_role,
            ),
            body=message.body,
            created_at=message.created_at,
        )
        for message, author in rows
    ]
    first = items[0].sequence if items else None
    last = items[-1].sequence if items else None
    return SupportMessagePage(
        items=items,
        has_more=has_more,
        # Forward polling continues from the newest returned message; with no
        # new messages the client keeps its cursor (latest_sequence).
        next_cursor=last if last is not None else (after_sequence or latest or None),
        prev_cursor=first,
        latest_sequence=latest,
    )


async def _change_status(
    db: AsyncSession,
    case: SupportCase,
    *,
    actor: User,
    access: CaseAccess,
    target: SupportCaseStatus,
    note: str | None,
    reason: str | None = None,
) -> SupportCaseStatus:
    previous = case.status
    now = _now()
    case.status = target
    case.updated_at = now
    if target == SupportCaseStatus.RESOLVED:
        case.resolved_at = now
    elif previous == SupportCaseStatus.RESOLVED:
        case.resolved_at = None
    await _audit(
        db, case, actor=actor, actor_type=_actor_type(access),
        action="support_case.status_changed",
        before={"status": previous.value}, after={"status": target.value},
        metadata={"note": note, "reason": reason},
    )
    return previous


async def post_message(
    db: AsyncSession, user: User, case_id: UUID, body: str
) -> SupportMessageResponse:
    case, access = await _load_case(db, user, case_id, for_update=True)
    if case.status == SupportCaseStatus.RESOLVED:
        raise ApiError(
            409, "SUPPORT_CASE_RESOLVED", "Reopen the case before sending new messages"
        )
    if case.status == SupportCaseStatus.CLOSED:
        raise ApiError(409, "SUPPORT_CASE_CLOSED", "This support case is closed")
    sequence = int(
        await db.scalar(
            select(func.coalesce(func.max(SupportMessage.sequence), 0)).where(
                SupportMessage.case_id == case.id
            )
        )
        or 0
    ) + 1
    now = _now()
    message = SupportMessage(
        case_id=case.id,
        sender_user_id=user.id,
        sender_role=access.relation,
        sequence=sequence,
        body=body,
        created_at=now,
    )
    db.add(message)
    case.updated_at = now
    # A customer reply hands the case back to the staff.
    if (
        access.is_requester
        and not access.is_handler
        and case.status == SupportCaseStatus.WAITING_FOR_CUSTOMER
    ):
        await _change_status(
            db, case, actor=user, access=access, target=SupportCaseStatus.IN_PROGRESS,
            note=None, reason="customer_replied",
        )
    await db.flush()
    await _notify(
        db, case, kind=notifications.KIND_SUPPORT_MESSAGE_RECEIVED,
        recipients=await _counterpart_ids(db, case, access), actor=user,
        extra={"message_id": str(message.id), "sequence": sequence},
    )
    return SupportMessageResponse(
        id=message.id,
        case_id=case.id,
        sequence=sequence,
        sender=SupportMessageSender(
            user_id=user.id, display_name=display_name(user), role=access.relation
        ),
        body=message.body,
        created_at=message.created_at,
    )


def _status_allowed(
    access: CaseAccess, current: SupportCaseStatus, target: SupportCaseStatus
) -> bool:
    if target not in _TRANSITIONS.get(current, set()):
        return False
    if access.is_handler:
        return True
    # A requester may resolve their own case or reopen it.
    return target == SupportCaseStatus.RESOLVED or (
        current == SupportCaseStatus.RESOLVED and target == SupportCaseStatus.OPEN
    )


async def update_status(
    db: AsyncSession, user: User, case_id: UUID, target: SupportCaseStatus, note: str | None
) -> tuple[SupportCase, CaseAccess]:
    case, access = await _load_case(db, user, case_id, for_update=True)
    if case.status == target:
        return case, access  # idempotent no-op
    if target not in _TRANSITIONS.get(case.status, set()):
        raise ApiError(
            409,
            "INVALID_STATUS_TRANSITION",
            f"Cannot move a support case from {case.status.value} to {target.value}",
        )
    if not _status_allowed(access, case.status, target):
        raise ApiError(
            403, "SUPPORT_STATUS_FORBIDDEN", "You cannot set this status on the case"
        )
    previous = await _change_status(
        db, case, actor=user, access=access, target=target, note=note
    )
    await db.flush()
    significant = target in (
        SupportCaseStatus.WAITING_FOR_CUSTOMER, SupportCaseStatus.RESOLVED
    ) or previous == SupportCaseStatus.RESOLVED
    if significant:
        kind = (
            notifications.KIND_SUPPORT_CASE_RESOLVED
            if target == SupportCaseStatus.RESOLVED
            else notifications.KIND_SUPPORT_CASE_STATUS_CHANGED
        )
        await _notify(
            db, case, kind=kind, recipients=await _counterpart_ids(db, case, access),
            actor=user, extra={"from_status": previous.value, "to_status": target.value},
        )
    return case, access


async def eligible_assignees(db: AsyncSession, case: SupportCase) -> list[tuple[User, str]]:
    """Users the case may be assigned to, with their handler role."""
    result: list[tuple[User, str]] = []
    seen: set[UUID] = set()
    if case.case_type == _AO and case.event_id is not None:
        event = await repo.get_event_by_id(db, case.event_id)
        if event is not None:
            owner = await repo.get_user_by_id(db, event.owner_user_id)
            if owner is not None and owner.status == UserStatus.ACTIVE:
                result.append((owner, REL_ORGANIZER))
                seen.add(owner.id)
            staff_rows = (
                await db.execute(
                    select(User)
                    .join(EventStaff, EventStaff.user_id == User.id)
                    .where(
                        EventStaff.event_id == case.event_id,
                        EventStaff.role.in_(SUPPORT_STAFF_ROLES),
                        User.status == UserStatus.ACTIVE,
                    )
                    .order_by(EventStaff.created_at.asc())
                )
            ).scalars().all()
            for staff_user in staff_rows:
                if staff_user.id not in seen:
                    result.append((staff_user, REL_STAFF))
                    seen.add(staff_user.id)
    if case.case_type == _PLATFORM or case.escalated_at is not None:
        admins = (
            await db.execute(
                select(User)
                .where(
                    User.global_role == UserRole.PLATFORM_ADMIN,
                    User.status == UserStatus.ACTIVE,
                )
                .order_by(User.created_at.asc())
            )
        ).scalars().all()
        for admin in admins:
            if admin.id not in seen:
                result.append((admin, REL_PLATFORM_ADMIN))
                seen.add(admin.id)
    return result


async def list_assignable(
    db: AsyncSession, user: User, case_id: UUID
) -> list[SupportAssignableUser]:
    case, access = await _load_case(db, user, case_id)
    if not access.is_handler:
        raise ApiError(403, "SUPPORT_ASSIGN_FORBIDDEN", "Only case handlers can assign cases")
    return [
        SupportAssignableUser(user_id=u.id, display_name=display_name(u) or "", role=role)
        for u, role in await eligible_assignees(db, case)
    ]


async def assign_case(
    db: AsyncSession, user: User, case_id: UUID, assignee_user_id: UUID | None
) -> tuple[SupportCase, CaseAccess]:
    case, access = await _load_case(db, user, case_id, for_update=True)
    if not access.is_handler:
        raise ApiError(403, "SUPPORT_ASSIGN_FORBIDDEN", "Only case handlers can assign cases")
    previous = case.assignee_user_id
    if previous == assignee_user_id:
        return case, access
    if assignee_user_id is not None:
        eligible = {u.id for u, _role in await eligible_assignees(db, case)}
        if assignee_user_id not in eligible:
            # Same error for unknown users and users without event permission.
            raise ApiError(
                422,
                "SUPPORT_ASSIGNEE_NOT_ELIGIBLE",
                "The user is not authorized to handle this support case",
            )
    case.assignee_user_id = assignee_user_id
    case.updated_at = _now()
    await _audit(
        db, case, actor=user, actor_type=_actor_type(access),
        action="support_case.assigned" if assignee_user_id else "support_case.unassigned",
        before={"assignee_user_id": str(previous) if previous else None},
        after={"assignee_user_id": str(assignee_user_id) if assignee_user_id else None},
    )
    await db.flush()
    if assignee_user_id is not None:
        await _notify(
            db, case, kind=notifications.KIND_SUPPORT_CASE_ASSIGNED,
            recipients={assignee_user_id}, actor=user,
            extra={"assignee_user_id": str(assignee_user_id)},
        )
    if previous is not None:
        await _notify(
            db, case, kind=notifications.KIND_SUPPORT_CASE_UNASSIGNED,
            recipients={previous}, actor=user,
            extra={"previous_assignee_user_id": str(previous)},
        )
    return case, access


async def escalate_case(
    db: AsyncSession, user: User, case_id: UUID, reason: str
) -> tuple[SupportCase, CaseAccess]:
    case, access = await _load_case(db, user, case_id, for_update=True)
    if case.case_type != _AO:
        raise ApiError(
            409, "SUPPORT_CASE_NOT_ESCALATABLE", "Platform cases are already handled by admins"
        )
    if case.escalated_at is not None:
        raise ApiError(409, "SUPPORT_CASE_ALREADY_ESCALATED", "Case is already escalated")
    if case.status == SupportCaseStatus.RESOLVED:
        raise ApiError(409, "SUPPORT_CASE_RESOLVED", "Reopen the case before escalating it")
    if not _can_escalate(case, access):
        raise ApiError(403, "SUPPORT_ESCALATE_FORBIDDEN", "You cannot escalate this case")
    now = _now()
    case.escalated_at = now
    case.escalated_by_user_id = user.id
    case.escalation_reason = reason
    case.updated_at = now
    await _audit(
        db, case, actor=user, actor_type=_actor_type(access),
        action="support_case.escalated",
        after={"escalated": True}, metadata={"reason": reason},
    )
    await db.flush()
    recipients: set[UUID | None] = set(await _active_admin_ids(db))
    recipients |= await _counterpart_ids(db, case, access)
    await _notify(
        db, case, kind=notifications.KIND_SUPPORT_CASE_ESCALATED,
        recipients=recipients, actor=user, extra={"reason": reason},
    )
    return case, access


def _describe(action: str, before: dict[str, Any], after: dict[str, Any]) -> str:
    if action == "support_case.created":
        return "Support case opened"
    if action == "support_case.status_changed":
        return f"Status changed from {before.get('status')} to {after.get('status')}"
    if action == "support_case.assigned":
        return "Case assigned" if not before.get("assignee_user_id") else "Case reassigned"
    if action == "support_case.unassigned":
        return "Case unassigned"
    if action == "support_case.escalated":
        return "Case escalated to Platform Admins"
    return action


async def list_activity(
    db: AsyncSession, user: User, case_id: UUID, *, limit: int, offset: int
) -> SupportActivityPage:
    case, _access = await _load_case(db, user, case_id)
    conditions = (
        AuditLog.entity_type == "support_case",
        AuditLog.entity_id == case.id,
        AuditLog.action.in_(_AUDIT_ACTIONS),
    )
    total = int(
        await db.scalar(select(func.count()).select_from(AuditLog).where(*conditions)) or 0
    )
    actor = aliased(User)
    rows = (
        await db.execute(
            select(AuditLog, actor)
            .outerjoin(actor, actor.id == AuditLog.actor_user_id)
            .where(*conditions)
            .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    items = []
    for entry, author in rows:
        before = entry.before_data or {}
        after = entry.after_data or {}
        meta = entry.meta or {}
        items.append(
            SupportActivityResponse(
                id=entry.id,
                timestamp=entry.created_at,
                action=entry.action,
                actor=SupportMessageSender(
                    user_id=entry.actor_user_id,
                    display_name=display_name(author),
                    role=entry.actor_type,
                ),
                description=_describe(entry.action, before, after),
                from_status=before.get("status"),
                to_status=after.get("status"),
                from_assignee_user_id=before.get("assignee_user_id"),
                to_assignee_user_id=after.get("assignee_user_id"),
                note=meta.get("note") or meta.get("reason"),
            )
        )
    return SupportActivityPage(items=items, total=total, limit=limit, offset=offset)

