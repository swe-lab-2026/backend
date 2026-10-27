import io
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import qrcode
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from app.schemas.tickets import TicketResponse

_FONT_CANDIDATES = (
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "DejaVuSans"),
    ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf", "LiberationSans"),
    ("/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf", "NotoSans"),
)
_BOLD_CANDIDATES = (
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "DejaVuSans-Bold"),
    ("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf", "LiberationSans-Bold"),
    ("/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf", "NotoSans-Bold"),
)
_fonts_registered: tuple[str, str] | None = None


def _register_unicode_fonts() -> tuple[str, str]:
    """Register a UTF-8 font so Cyrillic event and attendee names render.

    ReportLab's built-in Helvetica only covers Latin-1. The container images
    ship DejaVu; fall back to Helvetica on systems without any of the common
    Unicode fonts.
    """
    global _fonts_registered
    if _fonts_registered is not None:
        return _fonts_registered

    regular = "Helvetica"
    bold = "Helvetica-Bold"
    for path, name in _FONT_CANDIDATES:
        if Path(path).is_file():
            pdfmetrics.registerFont(TTFont(name, path))
            regular = name
            break
    for path, name in _BOLD_CANDIDATES:
        if Path(path).is_file():
            pdfmetrics.registerFont(TTFont(name, path))
            bold = name
            break

    _fonts_registered = (regular, bold)
    return _fonts_registered


def _format_event_time(value: datetime, timezone_name: str) -> str:
    try:
        tz = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo("UTC"))
    return value.astimezone(tz).strftime("%d %b %Y, %H:%M %Z")


def _qr_image(payload: str) -> ImageReader:
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=2,
    )
    qr.add_data(payload)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    buffer.seek(0)
    return ImageReader(buffer)


def _wrap(text: str, max_chars: int = 78) -> list[str]:
    words = text.replace("\r", " ").replace("\n", " ").split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [""]


def generate_ticket_pdf(ticket: TicketResponse) -> bytes:
    regular, bold = _register_unicode_fonts()
    buffer = io.BytesIO()
    page_width, page_height = A4
    pdf = canvas.Canvas(buffer, pagesize=A4)
    pdf.setTitle(f"BiletFlow ticket {ticket.public_id}")
    pdf.setAuthor("BiletFlow")

    margin = 18 * mm
    box_left = margin
    box_right = page_width - margin
    box_top = page_height - margin
    box_bottom = margin
    box_width = box_right - box_left
    box_height = box_top - box_bottom

    pdf.setStrokeColorRGB(0, 0, 0)
    pdf.setFillColorRGB(1, 1, 1)
    pdf.roundRect(box_left, box_bottom, box_width, box_height, 8 * mm, stroke=1, fill=1)

    x = box_left + 12 * mm
    y = box_top - 22 * mm

    pdf.setFillColorRGB(0, 0, 0)
    pdf.setFont(bold, 22)
    pdf.drawString(x, y, "BiletFlow")
    pdf.setFont(regular, 10)
    pdf.drawString(x, y - 6 * mm, "ADMISSION TICKET")

    y -= 20 * mm
    pdf.setFont(bold, 19)
    for line in _wrap(ticket.event_title, 38):
        pdf.drawString(x, y, line)
        y -= 8 * mm

    y -= 2 * mm
    pdf.setFont(regular, 11)
    details = [
        ("Event time", _format_event_time(ticket.event_starts_at, ticket.timezone)),
        ("Venue", ticket.venue_name or "Venue to be announced"),
        ("Address", ticket.venue_address or ticket.city or "See event page"),
        ("Ticket type", ticket.ticket_type_name),
        (
            "Attendee",
            ticket.attendee_name or ticket.attendee_email,
        ),
        ("Ticket ID", str(ticket.public_id)),
        ("Order", f"#{ticket.order_number}"),
    ]
    for label, value in details:
        pdf.setFont(bold, 10)
        pdf.drawString(x, y, f"{label}:")
        pdf.setFont(regular, 10)
        for line in _wrap(str(value), 52):
            pdf.drawString(x + 32 * mm, y, line)
            y -= 5 * mm
        y -= 3 * mm

    qr_size = 58 * mm
    qr_x = box_right - qr_size - 14 * mm
    qr_y = box_bottom + 18 * mm
    pdf.drawImage(
        _qr_image(ticket.qr_payload),
        qr_x,
        qr_y,
        width=qr_size,
        height=qr_size,
        preserveAspectRatio=True,
        mask="auto",
    )
    pdf.setFont(regular, 8)
    pdf.drawCentredString(qr_x + qr_size / 2, qr_y - 5 * mm, "Scan at the entrance")

    pdf.setFont(regular, 8)
    pdf.drawString(
        x,
        box_bottom + 10 * mm,
        "Present this ticket (printed or on screen). One admission per ticket.",
    )
    pdf.drawString(
        x,
        box_bottom + 6 * mm,
        "Ticket status is verified by BiletFlow at the entrance.",
    )

    pdf.showPage()
    pdf.save()
    return buffer.getvalue()
