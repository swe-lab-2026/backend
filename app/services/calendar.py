"""iCalendar (RFC 5545) export for one event."""

from datetime import datetime, timezone

from app.db.models import Event, EventStatus

PRODID = "-//BiletFlow//Events//EN"


def _utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )


def _fold(line: str) -> str:
    """Fold a content line to at most 75 octets (UTF-8 safe), RFC 5545 3.1."""
    encoded = line.encode("utf-8")
    if len(encoded) <= 75:
        return line
    parts: list[str] = []
    current = b""
    limit = 75
    for char in line:
        piece = char.encode("utf-8")
        if len(current) + len(piece) > limit:
            parts.append(current.decode("utf-8"))
            current = b""
            limit = 74  # continuation lines start with one space
        current += piece
    parts.append(current.decode("utf-8"))
    return "\r\n ".join(parts)


def build_event_ics(event: Event, *, url: str | None, now: datetime | None = None) -> str:
    stamp = now or datetime.now(timezone.utc)
    location = ", ".join(
        part for part in (event.venue_name, event.venue_address, event.city) if part
    )
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{event.id}@biletflow",
        f"DTSTAMP:{_utc(stamp)}",
        f"DTSTART:{_utc(event.starts_at)}",
        f"DTEND:{_utc(event.ends_at)}",
        f"LAST-MODIFIED:{_utc(event.updated_at)}",
        f"SUMMARY:{_escape(event.title)}",
    ]
    if event.description:
        lines.append(f"DESCRIPTION:{_escape(event.description)}")
    if location:
        lines.append(f"LOCATION:{_escape(location)}")
    if event.latitude is not None and event.longitude is not None:
        lines.append(f"GEO:{event.latitude};{event.longitude}")
    if url:
        lines.append(f"URL:{url}")
    lines.append(
        "STATUS:CANCELLED" if event.status == EventStatus.CANCELLED else "STATUS:CONFIRMED"
    )
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"
