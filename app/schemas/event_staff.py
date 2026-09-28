from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, EmailStr


class EventAdminAssignRequest(BaseModel):
    email: EmailStr


class EventAdminResponse(BaseModel):
    user_id: UUID
    email: str
    first_name: str
    last_name: str
    role: str
    permissions: dict[str, Any]
    assigned_at: datetime


class AssignedEventResponse(BaseModel):
    event_id: UUID
    title: str
    status: str
    visibility: str
    starts_at: datetime
    ends_at: datetime
    venue_name: str | None
    city: str | None
    timezone: str