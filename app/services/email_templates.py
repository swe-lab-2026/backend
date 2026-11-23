"""Reusable HTML/text templates for transactional email.

All user-provided text (event names, venues, attendee names, ticket type
names) is escaped before it reaches HTML. The English copy is the safe
fallback for every unsupported or missing locale.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.email import NotificationEmail, OrderConfirmationEmail


@dataclass(frozen=True)
class EmailContent:
    subject: str
    text: str
    html: str


def _safe_subject(value: str) -> str:
    # A newline in a subject can be interpreted as a header boundary by some
    # intermediaries. Brevo uses JSON, but stripping keeps the value safe for
    # every downstream transport as well.
    return " ".join(value.split())


def _format_datetime(value: datetime, locale: str) -> str:
    del locale
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _page(title: str, body_html: str) -> str:
    return (
        "<!doctype html><html><body style=\"font-family:Arial,sans-serif;"
        "line-height:1.5;color:#222\">"
        f"<h1 style=\"font-size:22px\">{escape(title)}</h1>"
        f"{body_html}"
        "<p style=\"color:#666;font-size:12px\">BiletFlow</p>"
        "</body></html>"
    )


def render_verification_email(*, code: str, locale: str = "en") -> EmailContent:
    del locale
    subject = "Your BiletFlow verification code"
    text = (
        "Your BiletFlow verification code is:\n\n"
        f"{code}\n\n"
        "Enter it in the app to verify your email address. "
        "If you did not create a BiletFlow account, you can ignore this message."
    )
    body = (
        "<p>Your BiletFlow verification code is:</p>"
        f"<p style=\"font-size:28px;font-weight:bold;letter-spacing:6px\">{escape(code)}</p>"
        "<p>Enter it in the app to verify your email address. "
        "If you did not create a BiletFlow account, you can ignore this message.</p>"
    )
    return EmailContent(subject=_safe_subject(subject), text=text, html=_page(subject, body))


def render_password_reset_email(*, token: str, locale: str = "en") -> EmailContent:
    del locale
    subject = "Reset your BiletFlow password"
    text = (
        "A password reset was requested for your BiletFlow account.\n\n"
        f"Reset token:\n\n{token}\n\n"
        "Use this token in the app to choose a new password. "
        "If you did not request a reset, you can ignore this message; "
        "your current password remains valid."
    )
    body = (
        "<p>A password reset was requested for your BiletFlow account.</p>"
        "<p>Reset token:</p>"
        "<p style=\"font-family:monospace;font-size:14px;"
        f"word-break:break-all\">{escape(token)}</p>"
        "<p>Use this token in the app to choose a new password. "
        "If you did not request a reset, you can ignore this message; "
        "your current password remains valid.</p>"
    )
    return EmailContent(subject=_safe_subject(subject), text=text, html=_page(subject, body))


def render_order_confirmation_email(
    message: OrderConfirmationEmail,
) -> EmailContent:
    when = _format_datetime(message.event_starts_at, message.locale)
    venue = message.venue or "Venue to be announced"
    subject = _safe_subject(f"Order #{message.order_number} confirmed - {message.event_name}")

    ticket_lines: list[str] = []
    ticket_html: list[str] = []
    for ticket in message.tickets:
        attendee = ticket.attendee_name or "Ticket holder"
        ticket_lines.append(
            f"- {ticket.ticket_type_name} ({ticket.public_id}) - {attendee}\n"
            f"  Download: {ticket.download_url}"
        )
        ticket_html.append(
            "<li>"
            f"<strong>{escape(ticket.ticket_type_name)}</strong> "
            f"({escape(ticket.public_id)}) - {escape(attendee)}"
            f"<br><a href=\"{escape(ticket.download_url, quote=True)}\">Download ticket PDF</a>"
            "</li>"
        )

    if not ticket_lines:
        ticket_lines.append("- Your tickets will be available in your BiletFlow account.")
        ticket_html.append("<li>Your tickets will be available in your BiletFlow account.</li>")

    text = (
        f"Your order #{message.order_number} is confirmed.\n\n"
        f"Event: {message.event_name}\n"
        f"Date: {when}\n"
        f"Venue: {venue}\n\n"
        "Tickets:\n"
        f"{chr(10).join(ticket_lines)}\n\n"
        "Sign in to BiletFlow with the email address that placed the order to "
        "open the ticket download links."
    )
    body = (
        f"<p>Your order <strong>#{escape(str(message.order_number))}</strong> is confirmed.</p>"
        "<table style=\"border-collapse:collapse\">"
        f"<tr><td style=\"padding:2px 12px 2px 0\"><strong>Event</strong></td>"
        f"<td>{escape(message.event_name)}</td></tr>"
        f"<tr><td style=\"padding:2px 12px 2px 0\"><strong>Date</strong></td>"
        f"<td>{escape(when)}</td></tr>"
        f"<tr><td style=\"padding:2px 12px 2px 0\"><strong>Venue</strong></td>"
        f"<td>{escape(venue)}</td></tr>"
        "</table>"
        "<h2 style=\"font-size:18px;margin-top:20px\">Tickets</h2>"
        f"<ul>{''.join(ticket_html)}</ul>"
        "<p>Sign in to BiletFlow with the email address that placed the order to "
        "open the ticket download links.</p>"
    )
    return EmailContent(subject=subject, text=text, html=_page(subject, body))


_NOTIFICATION_COPY: dict[str, tuple[str, str]] = {
    # kind: (subject prefix, lead sentence)
    "PAYMENT_FAILED": (
        "Payment failed",
        "Your payment for this order was not completed, so no tickets were issued. "
        "Any reserved tickets have been released; you can start a new checkout.",
    ),
    "EVENT_UPDATED": (
        "Event details updated",
        "The organizer changed details of an event you have tickets for. "
        "Your tickets remain valid.",
    ),
    "EVENT_CANCELLED": (
        "Event cancelled",
        "The organizer has cancelled this event. Your tickets are no longer valid "
        "for admission.",
    ),
    "REFUND_COMPLETED": (
        "Refund completed",
        "Your refund has been processed and your tickets have been cancelled.",
    ),
}

_DETAIL_LABELS = {
    "status": "Status",
    "reason": "Reason",
    "failure": "Failure",
    "amount": "Amount",
    "refund_amount": "Refund amount",
    "changed": "Changed",
    "cancelled_at": "Cancelled at",
    "refunded_at": "Refunded at",
    "updated_at": "Updated at",
    "notice": "Note",
}


def render_notification_email(message: NotificationEmail) -> EmailContent:
    prefix, lead = _NOTIFICATION_COPY.get(
        message.kind, ("Order update", "There is an update to your order.")
    )
    when = _format_datetime(message.event_starts_at, message.locale)
    venue = message.venue or "Venue to be announced"
    subject = _safe_subject(f"{prefix} - {message.event_name} (order #{message.order_number})")
    rows = [
        ("Event", message.event_name),
        ("Date", when),
        ("Venue", venue),
        ("Order", f"#{message.order_number}"),
    ] + [(_DETAIL_LABELS.get(key, key), value) for key, value in message.details.items()]
    text = f"{lead}\n\n" + "\n".join(f"{label}: {value}" for label, value in rows)
    body = (
        f"<p>{escape(lead)}</p><table style=\"border-collapse:collapse\">"
        + "".join(
            f"<tr><td style=\"padding:2px 12px 2px 0\"><strong>{escape(label)}</strong></td>"
            f"<td>{escape(value)}</td></tr>"
            for label, value in rows
        )
        + "</table>"
    )
    return EmailContent(subject=subject, text=text, html=_page(prefix, body))
