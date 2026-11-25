"""Reusable HTML/text templates for transactional email.

All user-provided text (event names, venues, attendee names, ticket type
names) is escaped before it reaches HTML. Copy exists in English (``en``),
Russian (``ru``) and Kazakh (``kk``); English is the safe fallback for every
unsupported or missing locale and for any string a locale does not define.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.email import NotificationEmail, OrderConfirmationEmail


SUPPORTED_LOCALES = ("en", "ru", "kk")
DEFAULT_LOCALE = "en"


@dataclass(frozen=True)
class EmailContent:
    subject: str
    text: str
    html: str


# ---------- Copy ----------

_COPY: dict[str, dict[str, str]] = {
    "en": {
        "venue_tba": "Venue to be announced",
        "ticket_holder": "Ticket holder",
        "verify.subject": "Your BiletFlow verification code",
        "verify.intro": "Your BiletFlow verification code is:",
        "verify.outro": (
            "Enter it in the app to verify your email address. "
            "If you did not create a BiletFlow account, you can ignore this message."
        ),
        "reset.subject": "Reset your BiletFlow password",
        "reset.intro": "A password reset was requested for your BiletFlow account.",
        "reset.token": "Reset token:",
        "reset.outro": (
            "Use this token in the app to choose a new password. "
            "If you did not request a reset, you can ignore this message; "
            "your current password remains valid."
        ),
        "order.subject": "Order #{number} confirmed - {event}",
        "order.lead": "Your order #{number} is confirmed.",
        "order.download": "Download",
        "order.download_pdf": "Download ticket PDF",
        "order.no_tickets": "Your tickets will be available in your BiletFlow account.",
        "order.sign_in": (
            "Sign in to BiletFlow with the email address that placed the order to "
            "open the ticket download links."
        ),
        "label.event": "Event",
        "label.date": "Date",
        "label.venue": "Venue",
        "label.order": "Order",
        "label.tickets": "Tickets",
        "notify.subject": "{prefix} - {event} (order #{number})",
        "notify.order_ref": "#{number}",
        "notify.default.prefix": "Order update",
        "notify.default.lead": "There is an update to your order.",
        "notify.PAYMENT_FAILED.prefix": "Payment failed",
        "notify.PAYMENT_FAILED.lead": (
            "Your payment for this order was not completed, so no tickets were issued. "
            "Any reserved tickets have been released; you can start a new checkout."
        ),
        "notify.EVENT_UPDATED.prefix": "Event details updated",
        "notify.EVENT_UPDATED.lead": (
            "The organizer changed details of an event you have tickets for. "
            "Your tickets remain valid."
        ),
        "notify.EVENT_CANCELLED.prefix": "Event cancelled",
        "notify.EVENT_CANCELLED.lead": (
            "The organizer has cancelled this event. Your tickets are no longer valid "
            "for admission."
        ),
        "notify.REFUND_COMPLETED.prefix": "Refund completed",
        "notify.REFUND_COMPLETED.lead": (
            "Your refund has been processed and your tickets have been cancelled."
        ),
        "detail.status": "Status",
        "detail.reason": "Reason",
        "detail.failure": "Failure",
        "detail.amount": "Amount",
        "detail.refund_amount": "Refund amount",
        "detail.changed": "Changed",
        "detail.cancelled_at": "Cancelled at",
        "detail.refunded_at": "Refunded at",
        "detail.updated_at": "Updated at",
        "detail.notice": "Note",
    },
    "ru": {
        "venue_tba": "Место проведения будет объявлено",
        "ticket_holder": "Владелец билета",
        "verify.subject": "Ваш код подтверждения BiletFlow",
        "verify.intro": "Ваш код подтверждения BiletFlow:",
        "verify.outro": (
            "Введите его в приложении, чтобы подтвердить адрес электронной почты. "
            "Если вы не создавали аккаунт BiletFlow, просто проигнорируйте это письмо."
        ),
        "reset.subject": "Сброс пароля BiletFlow",
        "reset.intro": "Для вашего аккаунта BiletFlow запрошен сброс пароля.",
        "reset.token": "Токен для сброса:",
        "reset.outro": (
            "Используйте этот токен в приложении, чтобы задать новый пароль. "
            "Если вы не запрашивали сброс, проигнорируйте это письмо: "
            "текущий пароль останется действительным."
        ),
        "order.subject": "Заказ №{number} подтверждён - {event}",
        "order.lead": "Ваш заказ №{number} подтверждён.",
        "order.download": "Скачать",
        "order.download_pdf": "Скачать билет (PDF)",
        "order.no_tickets": "Ваши билеты будут доступны в аккаунте BiletFlow.",
        "order.sign_in": (
            "Чтобы открыть ссылки для скачивания билетов, войдите в BiletFlow "
            "с адресом электронной почты, на который оформлен заказ."
        ),
        "label.event": "Мероприятие",
        "label.date": "Дата",
        "label.venue": "Место",
        "label.order": "Заказ",
        "label.tickets": "Билеты",
        "notify.subject": "{prefix} - {event} (заказ №{number})",
        "notify.order_ref": "№{number}",
        "notify.default.prefix": "Обновление заказа",
        "notify.default.lead": "По вашему заказу есть обновление.",
        "notify.PAYMENT_FAILED.prefix": "Оплата не прошла",
        "notify.PAYMENT_FAILED.lead": (
            "Оплата этого заказа не была завершена, поэтому билеты не выпущены. "
            "Зарезервированные билеты освобождены; вы можете оформить заказ заново."
        ),
        "notify.EVENT_UPDATED.prefix": "Детали мероприятия изменены",
        "notify.EVENT_UPDATED.lead": (
            "Организатор изменил детали мероприятия, на которое у вас есть билеты. "
            "Ваши билеты остаются действительными."
        ),
        "notify.EVENT_CANCELLED.prefix": "Мероприятие отменено",
        "notify.EVENT_CANCELLED.lead": (
            "Организатор отменил это мероприятие. Ваши билеты больше не действительны "
            "для входа."
        ),
        "notify.REFUND_COMPLETED.prefix": "Возврат выполнен",
        "notify.REFUND_COMPLETED.lead": (
            "Ваш возврат обработан, билеты аннулированы."
        ),
        "detail.status": "Статус",
        "detail.reason": "Причина",
        "detail.failure": "Ошибка",
        "detail.amount": "Сумма",
        "detail.refund_amount": "Сумма возврата",
        "detail.changed": "Изменено",
        "detail.cancelled_at": "Время отмены",
        "detail.refunded_at": "Время возврата",
        "detail.updated_at": "Время изменения",
        "detail.notice": "Примечание",
    },
    "kk": {
        "venue_tba": "Өтетін орны кейін хабарланады",
        "ticket_holder": "Билет иесі",
        "verify.subject": "BiletFlow растау кодыңыз",
        "verify.intro": "BiletFlow растау кодыңыз:",
        "verify.outro": (
            "Электрондық пошта мекенжайыңызды растау үшін оны қолданбаға енгізіңіз. "
            "Егер сіз BiletFlow аккаунтын ашпаған болсаңыз, бұл хатты елемеуге болады."
        ),
        "reset.subject": "BiletFlow құпиясөзін қалпына келтіру",
        "reset.intro": "BiletFlow аккаунтыңыз үшін құпиясөзді қалпына келтіру сұралды.",
        "reset.token": "Қалпына келтіру токені:",
        "reset.outro": (
            "Жаңа құпиясөз орнату үшін осы токенді қолданбада пайдаланыңыз. "
            "Егер сіз қалпына келтіруді сұрамаған болсаңыз, бұл хатты елемеңіз: "
            "қазіргі құпиясөзіңіз жарамды болып қала береді."
        ),
        "order.subject": "№{number} тапсырыс расталды - {event}",
        "order.lead": "Сіздің №{number} тапсырысыңыз расталды.",
        "order.download": "Жүктеу",
        "order.download_pdf": "Билетті жүктеу (PDF)",
        "order.no_tickets": "Билеттеріңіз BiletFlow аккаунтыңызда қолжетімді болады.",
        "order.sign_in": (
            "Билетті жүктеу сілтемелерін ашу үшін тапсырыс берілген электрондық "
            "пошта мекенжайымен BiletFlow-қа кіріңіз."
        ),
        "label.event": "Іс-шара",
        "label.date": "Күні",
        "label.venue": "Өтетін орны",
        "label.order": "Тапсырыс",
        "label.tickets": "Билеттер",
        "notify.subject": "{prefix} - {event} (№{number} тапсырыс)",
        "notify.order_ref": "№{number}",
        "notify.default.prefix": "Тапсырыс жаңартылды",
        "notify.default.lead": "Тапсырысыңыз бойынша жаңарту бар.",
        "notify.PAYMENT_FAILED.prefix": "Төлем өтпеді",
        "notify.PAYMENT_FAILED.lead": (
            "Бұл тапсырыс бойынша төлем аяқталмады, сондықтан билеттер берілмеді. "
            "Брондалған билеттер босатылды; жаңа тапсырысты қайта рәсімдей аласыз."
        ),
        "notify.EVENT_UPDATED.prefix": "Іс-шара мәліметтері өзгерді",
        "notify.EVENT_UPDATED.lead": (
            "Ұйымдастырушы сізде билеті бар іс-шараның мәліметтерін өзгертті. "
            "Билеттеріңіз жарамды болып қала береді."
        ),
        "notify.EVENT_CANCELLED.prefix": "Іс-шара тоқтатылды",
        "notify.EVENT_CANCELLED.lead": (
            "Ұйымдастырушы бұл іс-шараны тоқтатты. Билеттеріңіз енді кіру үшін жарамсыз."
        ),
        "notify.REFUND_COMPLETED.prefix": "Қаражат қайтарылды",
        "notify.REFUND_COMPLETED.lead": (
            "Қаражатыңыз қайтарылды, билеттеріңіздің күші жойылды."
        ),
        "detail.status": "Күйі",
        "detail.reason": "Себебі",
        "detail.failure": "Қате",
        "detail.amount": "Сомасы",
        "detail.refund_amount": "Қайтарылған сома",
        "detail.changed": "Өзгертілгені",
        "detail.cancelled_at": "Тоқтатылған уақыты",
        "detail.refunded_at": "Қайтарылған уақыты",
        "detail.updated_at": "Жаңартылған уақыты",
        "detail.notice": "Ескертпе",
    },
}


def normalize_locale(locale: str | None) -> str:
    """Map ``ru-RU``/``KK``/None/unknown onto a supported locale."""
    if not locale:
        return DEFAULT_LOCALE
    base = locale.strip().replace("_", "-").split("-", 1)[0].lower()
    return base if base in SUPPORTED_LOCALES else DEFAULT_LOCALE


def _t(locale: str, key: str, **values: object) -> str:
    template = _COPY[locale].get(key) or _COPY[DEFAULT_LOCALE][key]
    return template.format(**values) if values else template


def _has(key: str) -> bool:
    return key in _COPY[DEFAULT_LOCALE]


# ---------- Layout helpers ----------

def _safe_subject(value: str) -> str:
    # A newline in a subject can be interpreted as a header boundary by some
    # intermediaries. Brevo uses JSON, but stripping keeps the value safe for
    # every downstream transport as well.
    return " ".join(value.split())


def _format_datetime(value: datetime, locale: str) -> str:
    # ISO-like UTC timestamps read the same in every supported language.
    del locale
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _page(title: str, body_html: str, locale: str = DEFAULT_LOCALE) -> str:
    return (
        f"<!doctype html><html lang=\"{locale}\"><head><meta charset=\"utf-8\"></head>"
        "<body style=\"font-family:Arial,sans-serif;line-height:1.5;color:#222\">"
        f"<h1 style=\"font-size:22px\">{escape(title)}</h1>"
        f"{body_html}"
        "<p style=\"color:#666;font-size:12px\">BiletFlow</p>"
        "</body></html>"
    )


def _table(rows: list[tuple[str, str]]) -> str:
    return (
        "<table style=\"border-collapse:collapse\">"
        + "".join(
            f"<tr><td style=\"padding:2px 12px 2px 0\"><strong>{escape(label)}</strong></td>"
            f"<td>{escape(value)}</td></tr>"
            for label, value in rows
        )
        + "</table>"
    )


# ---------- Account email ----------

def render_verification_email(*, code: str, locale: str = DEFAULT_LOCALE) -> EmailContent:
    loc = normalize_locale(locale)
    subject = _t(loc, "verify.subject")
    intro, outro = _t(loc, "verify.intro"), _t(loc, "verify.outro")
    text = f"{intro}\n\n{code}\n\n{outro}"
    body = (
        f"<p>{escape(intro)}</p>"
        f"<p style=\"font-size:28px;font-weight:bold;letter-spacing:6px\">{escape(code)}</p>"
        f"<p>{escape(outro)}</p>"
    )
    return EmailContent(
        subject=_safe_subject(subject), text=text, html=_page(subject, body, loc)
    )


def render_password_reset_email(*, token: str, locale: str = DEFAULT_LOCALE) -> EmailContent:
    loc = normalize_locale(locale)
    subject = _t(loc, "reset.subject")
    intro, label, outro = _t(loc, "reset.intro"), _t(loc, "reset.token"), _t(loc, "reset.outro")
    text = f"{intro}\n\n{label}\n\n{token}\n\n{outro}"
    body = (
        f"<p>{escape(intro)}</p>"
        f"<p>{escape(label)}</p>"
        "<p style=\"font-family:monospace;font-size:14px;"
        f"word-break:break-all\">{escape(token)}</p>"
        f"<p>{escape(outro)}</p>"
    )
    return EmailContent(
        subject=_safe_subject(subject), text=text, html=_page(subject, body, loc)
    )


# ---------- Order email ----------

def render_order_confirmation_email(
    message: OrderConfirmationEmail,
) -> EmailContent:
    loc = normalize_locale(message.locale)
    when = _format_datetime(message.event_starts_at, loc)
    venue = message.venue or _t(loc, "venue_tba")
    subject = _safe_subject(
        _t(loc, "order.subject", number=message.order_number, event=message.event_name)
    )
    download, download_pdf = _t(loc, "order.download"), _t(loc, "order.download_pdf")

    ticket_lines: list[str] = []
    ticket_html: list[str] = []
    for ticket in message.tickets:
        attendee = ticket.attendee_name or _t(loc, "ticket_holder")
        ticket_lines.append(
            f"- {ticket.ticket_type_name} ({ticket.public_id}) - {attendee}\n"
            f"  {download}: {ticket.download_url}"
        )
        ticket_html.append(
            "<li>"
            f"<strong>{escape(ticket.ticket_type_name)}</strong> "
            f"({escape(ticket.public_id)}) - {escape(attendee)}"
            f"<br><a href=\"{escape(ticket.download_url, quote=True)}\">"
            f"{escape(download_pdf)}</a>"
            "</li>"
        )

    if not ticket_lines:
        no_tickets = _t(loc, "order.no_tickets")
        ticket_lines.append(f"- {no_tickets}")
        ticket_html.append(f"<li>{escape(no_tickets)}</li>")

    lead = _t(loc, "order.lead", number=message.order_number)
    sign_in = _t(loc, "order.sign_in")
    tickets_label = _t(loc, "label.tickets")
    rows = [
        (_t(loc, "label.event"), message.event_name),
        (_t(loc, "label.date"), when),
        (_t(loc, "label.venue"), venue),
    ]
    text = (
        f"{lead}\n\n"
        + "\n".join(f"{label}: {value}" for label, value in rows)
        + f"\n\n{tickets_label}:\n"
        f"{chr(10).join(ticket_lines)}\n\n"
        f"{sign_in}"
    )
    body = (
        f"<p>{escape(lead)}</p>"
        + _table(rows)
        + f"<h2 style=\"font-size:18px;margin-top:20px\">{escape(tickets_label)}</h2>"
        f"<ul>{''.join(ticket_html)}</ul>"
        f"<p>{escape(sign_in)}</p>"
    )
    return EmailContent(subject=subject, text=text, html=_page(subject, body, loc))


NOTIFICATION_KINDS = ("PAYMENT_FAILED", "EVENT_UPDATED", "EVENT_CANCELLED", "REFUND_COMPLETED")


def render_notification_email(message: NotificationEmail) -> EmailContent:
    loc = normalize_locale(message.locale)
    kind = message.kind if message.kind in NOTIFICATION_KINDS else "default"
    prefix = _t(loc, f"notify.{kind}.prefix")
    lead = _t(loc, f"notify.{kind}.lead")
    when = _format_datetime(message.event_starts_at, loc)
    venue = message.venue or _t(loc, "venue_tba")
    subject = _safe_subject(
        _t(loc, "notify.subject", prefix=prefix, event=message.event_name,
           number=message.order_number)
    )
    rows = [
        (_t(loc, "label.event"), message.event_name),
        (_t(loc, "label.date"), when),
        (_t(loc, "label.venue"), venue),
        (_t(loc, "label.order"), _t(loc, "notify.order_ref", number=message.order_number)),
    ] + [
        (_t(loc, f"detail.{key}") if _has(f"detail.{key}") else key, value)
        for key, value in message.details.items()
    ]
    text = f"{lead}\n\n" + "\n".join(f"{label}: {value}" for label, value in rows)
    body = f"<p>{escape(lead)}</p>" + _table(rows)
    return EmailContent(subject=subject, text=text, html=_page(prefix, body, loc))
