from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.db.models import SupportCaseType, SupportCategory

MESSAGE_MAX_LENGTH = 5000


class SupportCaseStatusValue(str, Enum):
    """Statuses accepted and returned by the API (legacy CLOSED is excluded)."""

    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    WAITING_FOR_CUSTOMER = "WAITING_FOR_CUSTOMER"
    RESOLVED = "RESOLVED"


def _clean_text(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("must not be blank")
    return cleaned


class SupportCaseCreate(BaseModel):
    """Open a support case.

    Context is optional and resolved by the server: a ``ticket_id`` resolves
    its order and event, an ``order_id`` its event. Ownership and consistency
    are always checked against the database; nothing about ownership is taken
    from the client. ``case_type`` defaults to ATTENDEE_ORGANIZER for someone
    else's event and PLATFORM otherwise (own event, or no event context).
    """

    subject: str = Field(min_length=3, max_length=240)
    category: SupportCategory
    message: str = Field(min_length=1, max_length=MESSAGE_MAX_LENGTH)
    case_type: SupportCaseType | None = None
    event_id: UUID | None = None
    order_id: UUID | None = None
    ticket_id: UUID | None = None

    @field_validator("subject", "message")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        return _clean_text(value)


class SupportMessageCreate(BaseModel):
    body: str = Field(min_length=1, max_length=MESSAGE_MAX_LENGTH)

    @field_validator("body")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        return _clean_text(value)


class SupportStatusUpdate(BaseModel):
    status: SupportCaseStatusValue
    note: str | None = Field(default=None, max_length=1000)


class SupportAssignmentUpdate(BaseModel):
    # null unassigns the case.
    assignee_user_id: UUID | None


class SupportEscalationRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=1000)

    @field_validator("reason")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        return _clean_text(value)


class SupportParticipant(BaseModel):
    user_id: UUID
    display_name: str


class SupportCaseAccessResponse(BaseModel):
    """How the caller relates to the case and what they may do."""

    relation: str  # REQUESTER | ORGANIZER | STAFF | PLATFORM_ADMIN
    can_manage: bool  # change any status, assign
    can_escalate: bool


class SupportCaseResponse(BaseModel):
    id: UUID
    case_type: str
    category: str
    subject: str
    status: str
    requester: SupportParticipant | None
    assignee: SupportParticipant | None
    event_id: UUID | None
    event_title: str | None
    order_id: UUID | None
    order_number: int | None
    ticket_id: UUID | None
    is_escalated: bool
    escalated_at: datetime | None
    escalation_reason: str | None
    message_count: int
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None
    access: SupportCaseAccessResponse | None = None


class SupportCasePage(BaseModel):
    items: list[SupportCaseResponse]
    total: int
    limit: int
    offset: int


class SupportMessageSender(BaseModel):
    user_id: UUID | None
    display_name: str | None
    role: str | None  # REQUESTER | ORGANIZER | STAFF | PLATFORM_ADMIN


class SupportMessageResponse(BaseModel):
    id: UUID
    case_id: UUID
    sequence: int
    sender: SupportMessageSender
    body: str
    created_at: datetime


class SupportMessagePage(BaseModel):
    """Chronological (oldest first) page of a conversation.

    Poll for new messages with ``after_sequence=next_cursor``; page backwards
    through history with ``before_sequence=prev_cursor``.
    """

    items: list[SupportMessageResponse]
    has_more: bool
    next_cursor: int | None
    prev_cursor: int | None
    latest_sequence: int


class SupportActivityResponse(BaseModel):
    id: int
    timestamp: datetime
    action: str
    actor: SupportMessageSender
    description: str
    from_status: str | None = None
    to_status: str | None = None
    from_assignee_user_id: UUID | None = None
    to_assignee_user_id: UUID | None = None
    note: str | None = None


class SupportActivityPage(BaseModel):
    items: list[SupportActivityResponse]
    total: int
    limit: int
    offset: int


class SupportAssignableUser(BaseModel):
    user_id: UUID
    display_name: str
    role: str  # ORGANIZER | STAFF | PLATFORM_ADMIN


class NotificationResponse(BaseModel):
    id: UUID
    type: str
    channel: str
    payload: dict[str, object]
    status: str
    created_at: datetime
    read_at: datetime | None


class NotificationPage(BaseModel):
    items: list[NotificationResponse]
    total: int
    unread_count: int
    limit: int
    offset: int
