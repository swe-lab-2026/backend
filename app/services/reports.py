"""Reported events (SRS 4.12).

Users report publicly visible events; Platform Admins review the reports and
may suspend the event while resolving one. A report is bookkeeping: it never
changes the event on its own. The event owner is not told who reported.
"""

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.errors import ApiError
from app.db import repositories as repo
from app.db.models import Event, EventReport, EventReportStatus, User
from app.schemas.admin import (
    AdminEventReportResponse,
    EventRef,
    EventReportResponse,
    Page,
    PersonRef,
)
from app.services import admin as admin_service
from app.services import notifications
from app.services.events import is_publicly_visible

logger = logging.getLogger("app.reports")

_ACTIVE = (EventReportStatus.OPEN.value, EventReportStatus.UNDER_REVIEW.value)
_TERMINAL = (EventReportStatus.RESOLVED.value, EventReportStatus.DISMISSED.value)
_TRANSITIONS: dict[str, set[str]] = {
    EventReportStatus.OPEN.value: {"UNDER_REVIEW", "RESOLVED", "DISMISSED"},
    EventReportStatus.UNDER_REVIEW.value: {"OPEN", "RESOLVED", "DISMISSED"},
    EventReportStatus.RESOLVED.value: set(),
    EventReportStatus.DISMISSED.value: set(),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _report_response(r: EventReport) -> EventReportResponse:
    return EventReportResponse(
        id=r.id, event_id=r.event_id, reason=r.reason, details=r.details, status=r.status,
        created_at=r.created_at, updated_at=r.updated_at, resolved_at=r.resolved_at,
    )


async def create_report(
    db: AsyncSession, user: User, event_id: UUID, *, reason: str, details: str | None
) -> EventReportResponse:
    event = await repo.get_event_by_id(db, event_id)
    if event is None or not is_publicly_visible(event):
        raise ApiError(404, "EVENT_NOT_FOUND", "Event not found")
    if event.owner_user_id == user.id:
        raise ApiError(409, "CANNOT_REPORT_OWN_EVENT", "You cannot report your own event")
    now = _now()
    report = EventReport(
        event_id=event.id, reporter_user_id=user.id, reason=reason, details=details,
        status=EventReportStatus.OPEN.value, created_at=now, updated_at=now,
    )
    db.add(report)
    try:
        async with db.begin_nested():
            await db.flush()
    except IntegrityError as exc:
        raise ApiError(
            409, "REPORT_ALREADY_OPEN", "You already have an open report for this event"
        ) from exc
    await repo.add_audit_entry(
        db, actor_user_id=user.id, entity_type="event_report", entity_id=report.id,
        event_id=event.id, action="event.reported", after_data={"reason": reason},
    )
    logger.info("event %s reported (%s)", event.id, reason)
    return _report_response(report)


async def list_my_reports(
    db: AsyncSession, user: User, *, limit: int, offset: int
) -> Page[EventReportResponse]:
    condition = EventReport.reporter_user_id == user.id
    total = int(await db.scalar(select(func.count(EventReport.id)).where(condition)) or 0)
    rows = (await db.execute(
        select(EventReport).where(condition)
        .order_by(EventReport.created_at.desc(), EventReport.id.desc())
        .limit(limit).offset(offset)
    )).scalars().all()
    return Page[EventReportResponse](
        items=[_report_response(r) for r in rows], total=total, limit=limit, offset=offset
    )


# ---------- Platform Admin ----------


def _active_count() -> Any:
    other = aliased(EventReport)
    return (
        select(func.count(other.id))
        .where(other.event_id == EventReport.event_id, other.status.in_(_ACTIVE))
        .correlate(EventReport)
        .scalar_subquery()
    )


def _admin_query() -> Any:
    reporter = aliased(User)
    return (
        select(EventReport, Event, reporter, _active_count().label("active"))
        .join(Event, Event.id == EventReport.event_id)
        .outerjoin(reporter, reporter.id == EventReport.reporter_user_id)
    )


def _admin_response(row: Any) -> AdminEventReportResponse:
    report, event, reporter, active = row
    return AdminEventReportResponse(
        **_report_response(report).model_dump(),
        event=EventRef(id=event.id, title=event.title),
        event_is_suspended=event.suspended_at is not None,
        event_active_reports=int(active or 0),
        reporter=(
            PersonRef(id=reporter.id, name=f"{reporter.first_name} {reporter.last_name}".strip())
            if reporter else None
        ),
        reporter_email=reporter.email if reporter else None,
        resolution_note=report.resolution_note,
        resolved_by_user_id=report.resolved_by_user_id,
    )


async def list_reports(
    db: AsyncSession,
    *,
    status: str | None,
    reason: str | None,
    event_id: UUID | None,
    limit: int,
    offset: int,
) -> Page[AdminEventReportResponse]:
    conditions: list[Any] = []
    if status:
        conditions.append(EventReport.status == status)
    if reason:
        conditions.append(EventReport.reason == reason)
    if event_id is not None:
        conditions.append(EventReport.event_id == event_id)
    total = int(await db.scalar(select(func.count(EventReport.id)).where(*conditions)) or 0)
    rows = (await db.execute(
        _admin_query().where(*conditions)
        .order_by(EventReport.created_at.desc(), EventReport.id.desc())
        .limit(limit).offset(offset)
    )).all()
    return Page[AdminEventReportResponse](
        items=[_admin_response(r) for r in rows], total=total, limit=limit, offset=offset
    )


async def get_report(db: AsyncSession, report_id: UUID) -> AdminEventReportResponse:
    row = (await db.execute(_admin_query().where(EventReport.id == report_id))).first()
    if row is None:
        raise ApiError(404, "REPORT_NOT_FOUND", "Report not found")
    return _admin_response(row)


async def update_report(
    db: AsyncSession,
    admin: User,
    report_id: UUID,
    *,
    status: str,
    note: str | None,
    suspend_event: bool,
) -> AdminEventReportResponse:
    report = await db.scalar(
        select(EventReport).where(EventReport.id == report_id).with_for_update()
    )
    if report is None:
        raise ApiError(404, "REPORT_NOT_FOUND", "Report not found")
    if status != report.status:
        if status not in _TRANSITIONS[report.status]:
            raise ApiError(
                409, "INVALID_STATUS_TRANSITION",
                f"Cannot move a report from {report.status} to {status}",
            )
        previous = report.status
        now = _now()
        report.status = status
        if note is not None:
            report.resolution_note = note.strip() or None
        report.updated_at = now
        if status in _TERMINAL:
            report.resolved_at = now
            report.resolved_by_user_id = admin.id
        await admin_service._audit(
            db, admin, action="admin.event_report_updated", entity_type="event_report",
            entity_id=report.id, event_id=report.event_id, reason=note,
            before={"status": previous}, after={"status": status},
        )
        if status in _TERMINAL:
            event = await db.get(Event, report.event_id)
            await notifications.notify_users(
                db, user_ids=[report.reporter_user_id], kind="EVENT_REPORT_REVIEWED",
                payload={
                    "report_id": str(report.id), "event_id": str(report.event_id),
                    "title": event.title if event else None, "status": status,
                },
                exclude=[admin.id],
            )
        await db.flush()
    elif not suspend_event:
        return await get_report(db, report.id)

    if suspend_event:
        event = await db.get(Event, report.event_id)
        assert event is not None
        if event.suspended_at is None:
            assert note is not None  # enforced by the request schema
            await admin_service.suspend_event(db, admin, event.id, note.strip())
    return await get_report(db, report.id)
