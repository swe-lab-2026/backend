from uuid import UUID
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.api.deps import get_db, require_active_user
from app.db.models import Order, OrderItem, User
from app.schemas.orders import OrderCreateRequest, OrderItemResponse, OrderResponse
from app.services import orders as service

router = APIRouter(prefix="/orders", tags=["orders"])

async def _response(db: AsyncSession, order: Order) -> OrderResponse:
    rows = (await db.execute(select(OrderItem).where(OrderItem.order_id == order.id).order_by(OrderItem.created_at))).scalars().all()
    quantities: dict[tuple, int] = {}
    for row in rows:
        key = (row.ticket_type_id, row.ticket_type_name, row.unit_price, row.final_price)
        quantities[key] = quantities.get(key, 0) + 1
    return OrderResponse(id=order.id, event_id=order.event_id, status=order.status.value,
        currency=order.currency, subtotal_amount=order.subtotal_amount, total_amount=order.total_amount,
        expires_at=order.expires_at, items=[OrderItemResponse(ticket_type_id=k[0], ticket_type_name=k[1], quantity=q, unit_price=k[2], final_price=k[3]) for k, q in quantities.items()])

@router.post("", response_model=OrderResponse, status_code=201)
async def create_order(payload: OrderCreateRequest, user: User = Depends(require_active_user), db: AsyncSession = Depends(get_db)) -> OrderResponse:
    order = await service.create_order(db, user, payload.event_id, payload.items)
    return await _response(db, order)

@router.get("/{order_id}", response_model=OrderResponse)
async def read_order(order_id: UUID, user: User = Depends(require_active_user), db: AsyncSession = Depends(get_db)) -> OrderResponse:
    await service.expire_holds(db)
    order = await service.get_order(db, order_id, user)
    return await _response(db, order)
