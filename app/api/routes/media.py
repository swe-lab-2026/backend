"""Public read access to uploaded media (event images)."""

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse

from app.api.deps import get_app_settings
from app.core.config import Settings
from app.core.errors import ApiError
from app.services import media as media_service

router = APIRouter(prefix="/media", tags=["media"])


@router.get("/{key:path}", response_class=FileResponse)
async def get_media(key: str, settings: Settings = Depends(get_app_settings)) -> FileResponse:
    resolved = media_service.resolve_key(settings.media_root, key)
    if resolved is None:
        raise ApiError(404, "MEDIA_NOT_FOUND", "File not found")
    path, content_type = resolved
    # Keys are random and never reused, so the content is immutable.
    return FileResponse(
        path,
        media_type=content_type,
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
        },
    )
