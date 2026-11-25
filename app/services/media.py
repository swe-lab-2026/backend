"""Event image uploads stored on local disk (MEDIA_ROOT).

Uploads are decoded and re-encoded with Pillow, which:
* accepts only real JPEG, PNG and WebP images (the declared content type and
  file name are ignored),
* drops metadata such as EXIF GPS coordinates,
* neutralizes files that are valid images and something else at once.

Keys look like ``events/<event uuid>/<32 hex>.<ext>`` and are the only paths
the media route serves, so a request can never leave MEDIA_ROOT.
"""

import io
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

import anyio
from PIL import Image, UnidentifiedImageError

from app.core.errors import ApiError

MAX_PIXELS = 40_000_000
_FORMATS = {
    "JPEG": ("jpg", "image/jpeg"),
    "PNG": ("png", "image/png"),
    "WEBP": ("webp", "image/webp"),
}
_CONTENT_TYPES = {ext: content_type for ext, content_type in _FORMATS.values()}
KEY_PATTERN = re.compile(
    r"^events/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/[0-9a-f]{32}\.(jpg|png|webp)$"
)


@dataclass(frozen=True)
class StoredImage:
    key: str
    content_type: str
    size_bytes: int
    width: int
    height: int


def _reencode(data: bytes) -> tuple[bytes, str, int, int]:
    try:
        with Image.open(io.BytesIO(data)) as probe:
            fmt = probe.format or ""
            width, height = probe.size
            probe.verify()
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise ApiError(415, "UNSUPPORTED_IMAGE", "Upload a JPEG, PNG or WebP image") from exc
    except Image.DecompressionBombError as exc:
        raise ApiError(413, "IMAGE_TOO_LARGE", "Image dimensions are too large") from exc
    if fmt not in _FORMATS:
        raise ApiError(415, "UNSUPPORTED_IMAGE", "Upload a JPEG, PNG or WebP image")
    if width * height > MAX_PIXELS:
        raise ApiError(413, "IMAGE_TOO_LARGE", "Image dimensions are too large")

    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            out = io.BytesIO()
            if fmt == "JPEG":
                image.convert("RGB").save(out, format="JPEG", quality=90, optimize=True)
            elif fmt == "PNG":
                image.save(out, format="PNG", optimize=True)
            else:
                image.save(out, format="WEBP", quality=90)
    except (OSError, ValueError) as exc:
        raise ApiError(415, "UNSUPPORTED_IMAGE", "Upload a JPEG, PNG or WebP image") from exc
    return out.getvalue(), fmt, width, height


async def save_event_image(media_root: str, event_id: uuid.UUID, data: bytes) -> StoredImage:
    if not data:
        raise ApiError(422, "EMPTY_UPLOAD", "The uploaded file is empty")
    encoded, fmt, width, height = await anyio.to_thread.run_sync(_reencode, data)
    ext, content_type = _FORMATS[fmt]
    key = f"events/{event_id}/{uuid.uuid4().hex}.{ext}"
    path = Path(media_root) / key
    await anyio.Path(path.parent).mkdir(parents=True, exist_ok=True)
    await anyio.Path(path).write_bytes(encoded)
    return StoredImage(
        key=key, content_type=content_type, size_bytes=len(encoded), width=width, height=height
    )


def resolve_key(media_root: str, key: str) -> tuple[Path, str] | None:
    """Map a media key to its file and content type; ``None`` if invalid/missing."""
    if not KEY_PATTERN.match(key):
        return None
    path = Path(media_root) / key
    if not path.is_file():
        return None
    return path, _CONTENT_TYPES[key.rsplit(".", 1)[1]]
