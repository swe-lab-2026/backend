from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.db.models import (
    Event, EventSalesActivation, OrganizerProfile, Payment, PaymentStatus,
    PayoutAccount, PayoutStatus, VerificationStatus, Order, OrderStatus, User,
)
from app.services import orders as order_service

ACTIVATION_FEE_KZT = Decimal("1000.00")
PROCESSING_FEE_RATE = Decimal("0.03")
SIMULATION_PROVIDER = "internal_simulation"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


async def get_or_create_activation(db: AsyncSession, event: Event) -> EventSalesActivation:
    row = await db.get(EventSalesActivation, event.id)
    if row is None:
        row = EventSalesActivation(event_id=event.id, activation_fee_amount=ACTIVATION_FEE_KZT)
        db.add(row)
        await db.flush()
    return row


async def activation_status(db: AsyncSession, event: Event) -> dict[str, object]:
    activation = await get_or_create_activation(db, event)
    profile = await db.get(OrganizerProfile, event.owner_user_id)
    payout = (await db.execute(
        select(PayoutAccount).where(
            PayoutAccount.organizer_user_id == event.owner_user_id,
            PayoutAccount.status == PayoutStatus.ACTIVE,
        ).limit(1)
    )).scalar_one_or_none()
    verified = profile is not None and profile.verification_status == VerificationStatus.VERIFIED
    payout_ok = payout is not None
    return {
        "event_id": event.id,
        "verification_complete": verified,
        "payout_account_complete": payout_ok,
        "activation_fee_amount": activation.activation_fee_amount,
        "activation_fee_paid": activation.activation_fee_paid_at is not None,
        "terms_accepted": activation.terms_accepted_at is not None,
        "is_activated": activation.activated_at is not None,
        "activated_at": activation.activated_at,
    }


async def accept_terms(db: AsyncSession, event: Event, accepted: bool) -> EventSalesActivation:
    if not accepted:
        raise ApiError(400, "TERMS_NOT_ACCEPTED", "Terms must be explicitly accepted")
    row = await get_or_create_activation(db, event)
    row.terms_accepted_at = now_utc()
    row.updated_at = now_utc()
    await db.flush()
    return row


async def simulate_activation_fee(db: AsyncSession, event: Event) -> EventSalesActivation:
    row = await get_or_create_activation(db, event)
    row.activation_fee_paid_at = now_utc()
    row.updated_at = now_utc()
    await db.flush()
    return row


async def activate_event(db: AsyncSession, event: Event) -> dict[str, object]:
    row = await get_or_create_activation(db, event)
    status = await activation_status(db, event)
    missing = [
        name for name, key in (
            ("organizer verification", "verification_complete"),
            ("active payout account", "payout_account_complete"),
            ("activation fee", "activation_fee_paid"),
            ("terms acceptance", "terms_accepted"),
        ) if not status[key]
    ]
    if missing:
        raise ApiError(409, "ACTIVATION_CHECKLIST_INCOMPLETE",
                       "Complete the event activation checklist",
                       details={"missing": missing})
    row.activated_at = row.activated_at or now_utc()
    row.updated_at = now_utc()
    await db.flush()
    return await activation_status(db, event)


async def start_simulated_payment(db: AsyncSession, user: User, order_id: UUID) -> Payment:
    order = await db.scalar(select(Order).where(Order.id == order_id).with_for_update())
    if order is None:
        raise ApiError(404, "ORDER_NOT_FOUND", "Order not found")
    if order.buyer_user_id != user.id:
        raise ApiError(403, "INSUFFICIENT_PERMISSIONS", "Only the buyer can pay this order")
    if order.status != OrderStatus.PENDING:
        raise ApiError(409, "ORDER_NOT_PAYABLE", "Order is not pending payment")
    if order.currency != "KZT":
        raise ApiError(409, "UNSUPPORTED_CURRENCY", "Payment simulation supports KZT only")
    if order.expires_at is not None and order.expires_at <= now_utc():
        raise ApiError(409, "ORDER_EXPIRED", "Order reservation has expired")
    event = await db.get(Event, order.event_id)
    activation = await db.get(EventSalesActivation, order.event_id)
    if event is None or activation is None or activation.activated_at is None:
        raise ApiError(409, "PAID_SALES_NOT_ACTIVATED",
                       "Paid sales are not activated for this event")
    existing = await db.scalar(select(Payment).where(
        Payment.order_id == order.id, Payment.provider == SIMULATION_PROVIDER
    ).with_for_update())
    if existing is not None:
        return existing
    payment = Payment(
        order_id=order.id, provider=SIMULATION_PROVIDER,
        provider_payment_ref=f"demo-{order.id}", amount=order.total_amount,
        currency="KZT", status=PaymentStatus.PENDING, is_simulated=True,
    )
    db.add(payment)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        existing = await db.scalar(select(Payment).where(
            Payment.order_id == order.id, Payment.provider == SIMULATION_PROVIDER
        ))
        if existing is None:
            raise
        return existing
    return payment


async def resolve_simulated_payment(
    db: AsyncSession, user: User, payment_id: UUID, outcome: str, *, qr_secret: str
) -> tuple[Payment, Order]:
    payment = await db.scalar(select(Payment).where(Payment.id == payment_id).with_for_update())
    if payment is None or payment.provider != SIMULATION_PROVIDER or not payment.is_simulated:
        raise ApiError(404, "PAYMENT_NOT_FOUND", "Simulated payment not found")
    order = await db.scalar(select(Order).where(Order.id == payment.order_id).with_for_update())
    if order is None:
        raise ApiError(404, "ORDER_NOT_FOUND", "Order not found")
    if order.buyer_user_id != user.id:
        raise ApiError(403, "INSUFFICIENT_PERMISSIONS", "Only the buyer can resolve this payment")
    if payment.status == PaymentStatus.SUCCEEDED:
        return payment, order
    if payment.status in {PaymentStatus.FAILED, PaymentStatus.CANCELLED}:
        return payment, order
    if outcome == "success":
        if payment.amount != order.total_amount or payment.currency != order.currency:
            raise ApiError(409, "PAYMENT_AMOUNT_MISMATCH", "Payment amount does not match order")
        await order_service.confirm_order(db, user, order.id, qr_secret=qr_secret)
        payment.status = PaymentStatus.SUCCEEDED
        payment.paid_at = now_utc()
    else:
        payment.status = PaymentStatus.FAILED if outcome == "failed" else PaymentStatus.CANCELLED
        payment.failure_code = "SIMULATED_FAILURE" if outcome == "failed" else "SIMULATED_ABANDONED"
        payment.failure_message = "Demo payment was not completed"
        if order.status == OrderStatus.PENDING:
            await order_service.cancel_order(db, user, order.id)
    payment.updated_at = now_utc()
    await db.flush()
    return payment, order
