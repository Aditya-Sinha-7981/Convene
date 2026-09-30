"""The minutes email itself (ADR-33): subject, HTML and plain-text body, and the attachment's file name.

Written in the mascot's voice (the same light lines as the join page and the dashboard pop-ins), and kept to what
the attachment does not already say: the meeting title, its date, and why this person got it. Pure functions, so the
wording is testable without sending anything.
"""
import html
import re
from datetime import datetime

SUBJECT = "Your words, delivered: {title}"
PARTS = {"summary": "the summary", "action_items": "the action items",
         "transcript": "the full transcript with who said what"}
BRAND = "#4338ca"


def subject(title: str) -> str:
    return SUBJECT.format(title=title)


def attachment_name(title: str) -> str:
    """``Convene minutes - <title>.docx``, with characters that trip up mail clients and file systems replaced."""
    safe = re.sub(r"[^\w .()-]+", "-", title, flags=re.UNICODE).strip(" .-") or "meeting"
    return f"Convene minutes - {safe[:80]}.docx"


def meeting_date(started_at: str | None, created_at: str) -> str:
    """The meeting's day in the laptop's local time zone, for example ``30 September 2026``."""
    stamp = datetime.fromisoformat((started_at or created_at).replace("Z", "+00:00")).astimezone()
    return f"{stamp.day} {stamp:%B %Y}"


def contents(sections) -> str:
    """What the attachment holds, in template order: "the summary and the action items"."""
    parts = [PARTS[key] for key in PARTS if key in sections]
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + (", and " if len(parts) > 2 else " and ") + parts[-1]


def bodies(name: str, title: str, date: str, sections=tuple(PARTS)) -> tuple[str, str]:
    """The (HTML, plain text) bodies. ``sections`` are the parts in this person's attachment (ADR-34). Every value
    from the meeting is escaped in the HTML."""
    attached = contents(sections)
    text = (f"Hi {name},\n\n"
            f"I sat in on \"{title}\" ({date}) and wrote everything down, so you didn't have to.\n\n"
            f"The minutes are attached: {attached}.\n\n"
            "Every line is exactly as it was said. I didn't put any words in your mouth. Promise.\n\n"
            "- Convene\n\n"
            "You're getting this because you added your email when you joined this meeting.\n")
    n, t, d = html.escape(name), html.escape(title), html.escape(date)
    markup = f"""<!doctype html>
<html><body style="margin:0;padding:24px;background:#f7f5f0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;color:#1f1d1a">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:520px;margin:0 auto;background:#ffffff;border:1px solid #e7e3da;border-radius:16px">
<tr><td style="padding:28px 28px 8px">
<p style="margin:0 0 6px;color:{BRAND};font-size:13px;font-weight:600">Meeting minutes</p>
<h1 style="margin:0 0 4px;font-size:22px;line-height:1.25">{t}</h1>
<p style="margin:0;color:#6b665d;font-size:14px">{d}</p>
</td></tr>
<tr><td style="padding:16px 28px 8px;font-size:15px;line-height:1.6">
<p style="margin:0 0 12px">Hi {n},</p>
<p style="margin:0 0 12px">I sat in on this one and wrote everything down, so you didn't have to.</p>
<p style="margin:0 0 12px">The minutes are attached: {html.escape(attached)}.</p>
<p style="margin:0 0 12px">Every line is exactly as it was said. I didn't put any words in your mouth. Promise.</p>
<p style="margin:0;font-weight:600;color:{BRAND}">- Convene</p>
</td></tr>
<tr><td style="padding:16px 28px 24px;color:#8a857b;font-size:12px;line-height:1.5">You're getting this because you added your email when you joined this meeting.</td></tr>
</table></body></html>"""
    return markup, text
