from pydantic import BaseModel, EmailStr, Field, field_validator


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)

    @field_validator("password")
    @classmethod
    def _password_complexity(cls, value: str) -> str:
        # Unicode-safe: checks are on characters, and max_length is on
        # characters too, so a long multibyte password is never silently
        # truncated.
        if not any(c.islower() for c in value):
            raise ValueError("password must contain a lowercase letter")
        if not any(c.isupper() for c in value):
            raise ValueError("password must contain an uppercase letter")
        if not any(c.isdigit() for c in value):
            raise ValueError("password must contain a digit")
        return value


class RegisterResponse(BaseModel):
    email: str
    first_name: str
    last_name: str
    email_verified: bool = False
    code_expires_in: int


class VerifyEmailRequest(BaseModel):
    email: EmailStr
    code: str = Field(min_length=6, max_length=6, pattern="^[0-9]{6}$")


class VerifyEmailResponse(BaseModel):
    email: str
    email_verified: bool = True


class ResendVerificationRequest(BaseModel):
    email: EmailStr


class ResendVerificationResponse(BaseModel):
    email: str
    code_expires_in: int
    retry_after: int


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str


class LogoutResponse(BaseModel):
    ok: bool = True