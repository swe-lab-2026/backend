from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    get_app_settings,
    get_db,
    get_email_provider,
    get_store,
    require_active_user,
)
from app.core.config import Settings
from app.db.models import User
from app.kv.store import TTLStore
from app.schemas.auth import (
    LoginRequest,
    LogoutRequest,
    LogoutResponse,
    RefreshRequest,
    RegisterRequest,
    RegisterResponse,
    ResendVerificationRequest,
    ResendVerificationResponse,
    TokenResponse,
    VerifyEmailRequest,
    VerifyEmailResponse,
)
from app.services import auth as auth_service
from app.services.auth import normalize_email
from app.services.email import EmailProvider

router = APIRouter(prefix="/auth", tags=["auth"])


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _user_agent(request: Request) -> str | None:
    return request.headers.get("user-agent")


def _token_response(pair: auth_service.TokenPair) -> TokenResponse:
    return TokenResponse(
        access_token=pair.access_token,
        refresh_token=pair.refresh_token,
        expires_in=pair.expires_in,
    )


@router.post("/register", response_model=RegisterResponse, status_code=201)
async def register(
    body: RegisterRequest,
    db: AsyncSession = Depends(get_db),
    provider: EmailProvider = Depends(get_email_provider),
    settings: Settings = Depends(get_app_settings),
) -> RegisterResponse:
    user, expires_in = await auth_service.register(
        db,
        provider,
        settings,
        email=body.email,
        password=body.password,
        first_name=body.first_name,
        last_name=body.last_name,
    )
    return RegisterResponse(
        email=user.email,
        first_name=user.first_name,
        last_name=user.last_name,
        code_expires_in=expires_in,
    )


@router.post("/verify-email", response_model=VerifyEmailResponse)
async def verify_email(
    body: VerifyEmailRequest,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> VerifyEmailResponse:
    user = await auth_service.verify_email(db, settings, email=body.email, code=body.code)
    return VerifyEmailResponse(email=user.email)


@router.post("/resend-verification", response_model=ResendVerificationResponse)
async def resend_verification(
    body: ResendVerificationRequest,
    db: AsyncSession = Depends(get_db),
    provider: EmailProvider = Depends(get_email_provider),
    store: TTLStore = Depends(get_store),
    settings: Settings = Depends(get_app_settings),
) -> ResendVerificationResponse:
    expires_in, retry_after = await auth_service.resend_verification(
        db, provider, store, settings, email=body.email
    )
    return ResendVerificationResponse(
        email=normalize_email(body.email),
        code_expires_in=expires_in,
        retry_after=retry_after,
    )


@router.post("/login", response_model=TokenResponse)
async def login(
    request: Request,
    body: LoginRequest,
    db: AsyncSession = Depends(get_db),
    store: TTLStore = Depends(get_store),
    settings: Settings = Depends(get_app_settings),
) -> TokenResponse:
    _, pair = await auth_service.login(
        db,
        store,
        settings,
        email=body.email,
        password=body.password,
        user_agent=_user_agent(request),
        ip_address=_client_ip(request),
    )
    return _token_response(pair)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    request: Request,
    body: RefreshRequest,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> TokenResponse:
    pair = await auth_service.rotate_refresh_token(
        db,
        settings,
        body.refresh_token,
        user_agent=_user_agent(request),
        ip_address=_client_ip(request),
    )
    return _token_response(pair)


@router.post("/logout", response_model=LogoutResponse)
async def logout(
    body: LogoutRequest,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> LogoutResponse:
    await auth_service.logout(db, settings, body.refresh_token)
    return LogoutResponse()


@router.post("/logout-all", response_model=LogoutResponse)
async def logout_all(
    user: User = Depends(require_active_user),
    db: AsyncSession = Depends(get_db),
) -> LogoutResponse:
    await auth_service.logout_all(db, user.id)
    return LogoutResponse()