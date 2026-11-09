from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    get_app_settings,
    get_db,
    get_email_provider,
    require_active_user,
)
from app.core.config import Settings
from app.db import repositories as repo
from app.db.models import Order, User
from app.schemas.orders import OrderCreate, OrderResponse
from app.schemas.paid_sales import (
    PaymentSimulationStartResponse, PaymentSimulationResultRequest, PaymentResponse,
)
from app.schemas.tickets import TicketResponse
from app.services import notifications
from app.services import orders as order_service
from app.services import paid_sales
from app.services.email import EmailProvider

router = APIRouter(prefix="/orders", tags=["orders"])


async def _response(
    db: AsyncSession,
    order: Order,
    settings: Settings,
) -> OrderResponse:
    return await order_service.build_order_response(
        db,
        order,
        qr_secret=settings.effective_ticket_qr_secret,
        download_base=settings.public_api_base,
    )


async def _deliver_email(
    db: AsyncSession,
    order_id: UUID,
    settings: Settings,
    provider: EmailProvider,
) -> None:
    await db.commit()
    await notifications.deliver_order_confirmation(
        db,
        order_id=order_id,
        provider=provider,
        download_base=settings.public_api_base,
    )
    await db.commit()


@router.post("", response_model=OrderResponse, status_code=201)
async def create_order(
    payload: OrderCreate,
    user: User = Depends(require_active_user),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
    provider: EmailProvider = Depends(get_email_provider),
) -> OrderResponse:
    order = await order_service.create_order(
        db,
        user,
        payload,
        qr_secret=settings.effective_ticket_qr_secret,
        reservation_ttl_minutes=settings.order_reservation_ttl_minutes,
    )
    if order.paid_at is not None:
        await _deliver_email(db, order.id, settings, provider)
    return await _response(db, order, settings)


@router.post(
    "/{order_id}/payments/simulate",
    response_model=PaymentSimulationStartResponse,
    status_code=201,
)
async def start_payment_simulation(
    order_id: UUID,
    user: User = Depends(require_active_user),
    db: AsyncSession = Depends(get_db),
) -> PaymentSimulationStartResponse:
    payment = await paid_sales.start_simulated_payment(db, user, order_id)
    return PaymentSimulationStartResponse(
        payment_id=payment.id, order_id=payment.order_id, amount=payment.amount,
        currency=payment.currency, status=payment.status.value,
    )


@router.post("/payments/{payment_id}/simulate-result", response_model=PaymentResponse)
async def resolve_payment_simulation(
    payment_id: UUID,
    payload: PaymentSimulationResultRequest,
    user: User = Depends(require_active_user),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
    provider: EmailProvider = Depends(get_email_provider),
) -> PaymentResponse:
    payment, order = await paid_sales.resolve_simulated_payment(
        db, user, payment_id, payload.outcome,
        qr_secret=settings.effective_ticket_qr_secret,
    )
    await db.commit()
    if payment.status.value == "SUCCEEDED":
        await notifications.deliver_order_confirmation(
            db, order_id=order.id, provider=provider,
            download_base=settings.public_api_base,
        )
        await db.commit()
    return PaymentResponse(
        id=payment.id, order_id=payment.order_id, amount=payment.amount,
        currency=payment.currency, status=payment.status.value,
        is_simulated=payment.is_simulated, provider=payment.provider,
        failure_code=payment.failure_code, created_at=payment.created_at,
        paid_at=payment.paid_at,
    )


@router.get("", response_model=list[OrderResponse])
async def list_orders(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(require_active_user),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> list[OrderResponse]:
    orders = await repo.list_user_orders(db, user.id, limit=limit, offset=offset)
    return [await _response(db, order, settings) for order in orders]


@router.get("/{order_id}/tickets", response_model=list[TicketResponse])
async def list_order_tickets(
    order_id: UUID,
    user: User = Depends(require_active_user),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> list[TicketResponse]:
    order = await order_service.get_order_for_view(db, user, order_id)
    response = await order_service.build_order_response(
        db,
        order,
        qr_secret=settings.effective_ticket_qr_secret,
        download_base=settings.public_api_base,
    )
    return response.tickets


@router.get("/{order_id}", response_model=OrderResponse)
async def get_order(
    order_id: UUID,
    user: User = Depends(require_active_user),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> OrderResponse:
    await order_service.expire_holds(db)
    order = await order_service.get_order(db, order_id, user)
    return await _response(db, order, settings)


@router.post("/{order_id}/confirm", response_model=OrderResponse)
async def confirm_order(
    order_id: UUID,
    user: User = Depends(require_active_user),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> OrderResponse:
    order = await order_service.get_order(db, order_id, user)
    # Keep committed free/paid orders idempotently readable, but never let a
    # pending paid order bypass the payment confirmation flow.
    if order.status.value in {"PAID", "CONFIRMED"}:
        return await _response(db, order, settings)
    from app.core.errors import ApiError
    raise ApiError(409, "PAYMENT_REQUIRED",
                   "Paid orders must be confirmed through the payment confirmation flow")


@router.post("/{order_id}/checkout", response_model=OrderResponse)
async def checkout_order(
    order_id: UUID,
    user: User = Depends(require_active_user),
) -> OrderResponse:
    del order_id, user
    from app.core.errors import ApiError
    raise ApiError(409, "PAYMENT_REQUIRED",
                   "Use the payment simulation flow to complete paid checkout")


@router.post("/{order_id}/cancel", response_model=OrderResponse)
async def cancel_order(
    order_id: UUID,
    user: User = Depends(require_active_user),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> OrderResponse:
    order = await order_service.cancel_order(db, user, order_id)
    return await _response(db, order, settings)
