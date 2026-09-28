from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, field_validator


def _strip_required(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("must not be blank")
    return stripped


class OrganizerProfileCreate(BaseModel):
    display_name: str = Field(min_length=1, max_length=160)
    legal_name: str | None = Field(default=None, max_length=200)
    contact_email: EmailStr | None = None
    contact_phone: str | None = Field(default=None, max_length=32)
    terms_accepted: bool = False

    @field_validator("display_name")
    @classmethod
    def _display_name_required(cls, value: str) -> str:
        return _strip_required(value)


class OrganizerProfileUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=160)
    legal_name: str | None = Field(default=None, max_length=200)
    contact_email: EmailStr | None = None
    contact_phone: str | None = Field(default=None, max_length=32)

    @field_validator("display_name")
    @classmethod
    def _display_name_not_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _strip_required(value)


class OrganizerProfileResponse(BaseModel):
    user_id: UUID
    display_name: str
    legal_name: str | None
    contact_email: str | None
    contact_phone: str | None
    verification_status: str
    terms_accepted_at: datetime | None
    created_at: datetime
    updated_at: datetime


class PayoutAccountCreate(BaseModel):
    provider: str = Field(min_length=1, max_length=50)
    external_account_ref: str = Field(min_length=1, max_length=200)
    is_default: bool = False

    @field_validator("provider", "external_account_ref")
    @classmethod
    def _required_not_blank(cls, value: str) -> str:
        return _strip_required(value)


class PayoutAccountResponse(BaseModel):
    id: UUID
    provider: str
    external_account_ref: str
    status: str
    is_default: bool
    created_at: datetime
    updated_at: datetime