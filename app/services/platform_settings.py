"""Platform-wide commercial settings (SRS 4.12): activation fee and processing fee.

The settings live in a single row (``platform_settings.id = 1``). Reads create
the row with the historical defaults when it is missing (the SQLite test
fallback has no seed migration). Only new charges use changed values: paid
activation fees and the fees stored on existing orders are never rewritten.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import repositories as repo
from app.db.models import PlatformSettings, User

SETTINGS_ID = 1
DEFAULT_ACTIVATION_FEE = Decimal("1000.00")
DEFAULT_PROCESSING_FEE_PERCENT = Decimal("3.00")
CENT = Decimal("0.01")


async def get_settings_row(db: AsyncSession) -> PlatformSettings:
    row = await db.get(PlatformSettings, SETTINGS_ID)
    if row is None:
        row = PlatformSettings(
            id=SETTINGS_ID,
            activation_fee_amount=DEFAULT_ACTIVATION_FEE,
            activation_fee_currency="KZT",
            processing_fee_percent=DEFAULT_PROCESSING_FEE_PERCENT,
            updated_at=datetime.now(timezone.utc),
        )
        db.add(row)
        await db.flush()
    return row


async def processing_fee_rate(db: AsyncSession) -> Decimal:
    """Current processing fee as a fraction (3.00% -> 0.03)."""
    row = await get_settings_row(db)
    return Decimal(row.processing_fee_percent) / Decimal(100)


async def activation_fee_amount(db: AsyncSession) -> Decimal:
    row = await get_settings_row(db)
    return Decimal(row.activation_fee_amount).quantize(CENT)


def snapshot(row: PlatformSettings) -> dict[str, Any]:
    return {
        "activation_fee_amount": str(Decimal(row.activation_fee_amount).quantize(CENT)),
        "activation_fee_currency": row.activation_fee_currency,
        "processing_fee_percent": str(Decimal(row.processing_fee_percent).quantize(CENT)),
    }


async def update_settings(
    db: AsyncSession,
    admin: User,
    *,
    activation_fee_amount: Decimal | None,
    processing_fee_percent: Decimal | None,
    reason: str | None,
) -> PlatformSettings:
    row = await get_settings_row(db)
    before = snapshot(row)
    if activation_fee_amount is not None:
        row.activation_fee_amount = activation_fee_amount.quantize(CENT)
    if processing_fee_percent is not None:
        row.processing_fee_percent = processing_fee_percent.quantize(CENT)
    after = snapshot(row)
    if after == before:
        return row
    row.updated_by_user_id = admin.id
    row.updated_at = datetime.now(timezone.utc)
    await repo.add_audit_entry(
        db,
        actor_user_id=admin.id,
        actor_type="PLATFORM_ADMIN",
        entity_type="platform_settings",
        entity_id=None,
        action="admin.platform_settings_updated",
        before_data=before,
        after_data=after,
        metadata={"reason": reason},
    )
    await db.flush()
    return row
