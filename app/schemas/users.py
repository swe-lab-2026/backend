from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class UserResponse(BaseModel):
    user_id: UUID
    email: str
    first_name: str
    last_name: str
    global_role: str
    status: str
    email_verified_at: datetime | None
    created_at: datetime


class UpdateUserRequest(BaseModel):
    first_name: str | None = Field(default=None, min_length=1, max_length=100)
    last_name: str | None = Field(default=None, min_length=1, max_length=100)