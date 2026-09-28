from fastapi import APIRouter, Depends

from app.api.deps import require_roles
from app.db.models import User

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/example")
async def admin_example(
    user: User = Depends(require_roles("PLATFORM_ADMIN")),
) -> dict[str, object]:
    return {"ok": True, "user_id": str(user.id)}