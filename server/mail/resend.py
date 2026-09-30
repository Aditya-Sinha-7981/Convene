"""Resend email connector for sending the minutes (ADR-33). Stdlib HTTP only, so it adds no dependency.

Configured only by the operator's environment when the server starts: ``RESEND_API_KEY`` and ``CONVENE_MAIL_FROM``
(for example ``Convene <minutes@example.com>`` on a domain verified with Resend). Without them the feature is
reported as not configured and nothing is ever sent. Sending happens only when someone presses the send button on
the post-meeting page; nothing sends on its own.

The key travels only in the ``Authorization`` header. It is never logged, stored, or put in an error message.
"""
import base64
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

ENDPOINT = "https://api.resend.com/emails"
REQUEST_TIMEOUT_S = 20.0
RETRY_STATUSES = (429, 500, 502, 503)
RETRY_DELAY_S = 1.5
# Resend's API sits behind a filter that may refuse Python's default User-Agent, so the connector names itself.
USER_AGENT = "convene-minutes/1.0"
# Resend's two forms: ``address`` or ``Name <address>``. A bare ``<address>`` (no name) is refused by Resend.
_SENDER = re.compile(r"^(?:[^<>@\s][^<>@]*\s<[^<>@\s]+@[^<>@\s]+\.[^<>@\s]+>|[^<>@\s]+@[^<>@\s]+\.[^<>@\s]+)$")


class MailConfigError(ValueError):
    """The environment asks for email but is incomplete or malformed."""


class MailError(RuntimeError):
    """One message could not be sent. The message names the HTTP status and Resend's reason, never the key."""


@dataclass(frozen=True)
class Attachment:
    filename: str
    content: bytes


class ResendMailer:
    def __init__(self, api_key: str, sender: str, *, opener=None, sleep=time.sleep):
        if not api_key:
            raise MailConfigError("RESEND_API_KEY is empty")
        self._key, self.sender = api_key, sender
        self._open = opener or urllib.request.urlopen
        self._sleep = sleep

    def __repr__(self) -> str:  # never show the key
        return f"ResendMailer(sender={self.sender!r})"

    def send(self, to: str, subject: str, html: str, text: str, attachment: Attachment | None = None) -> str:
        """Send one message to one address and return Resend's message id. One retry on a rate limit or 5xx."""
        message = {"from": self.sender, "to": [to], "subject": subject, "html": html, "text": text}
        if attachment is not None:
            message["attachments"] = [{"filename": attachment.filename,
                                       "content": base64.b64encode(attachment.content).decode("ascii")}]
        body = json.dumps(message).encode("utf-8")
        for attempt in (1, 2):
            request = urllib.request.Request(ENDPOINT, data=body, method="POST", headers={
                "Content-Type": "application/json", "Authorization": f"Bearer {self._key}", "User-Agent": USER_AGENT})
            try:
                with self._open(request, timeout=REQUEST_TIMEOUT_S) as response:
                    reply = json.loads(response.read().decode("utf-8") or "{}")
                    return str(reply.get("id") or "")
            except urllib.error.HTTPError as exc:
                if exc.code in RETRY_STATUSES and attempt == 1:
                    self._sleep(RETRY_DELAY_S)
                    continue
                raise MailError(f"Resend HTTP {exc.code}: {_reason(exc)}") from None
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise MailError(f"could not reach Resend ({type(exc).__name__}); check the internet connection") from None
        raise MailError("Resend did not accept the message")  # unreachable: the loop returns or raises


def _reason(exc: urllib.error.HTTPError) -> str:
    try:
        detail = json.loads(exc.read().decode("utf-8")).get("message", "")
    except Exception:
        detail = ""
    return (detail or exc.reason or "no detail")[:300]


def mailer_from_environment(environ) -> ResendMailer | None:
    """The configured mailer, or None when ``RESEND_API_KEY`` is unset. A key without a valid sender is an error."""
    key = (environ.get("RESEND_API_KEY") or "").strip()
    if not key:
        return None
    sender = (environ.get("CONVENE_MAIL_FROM") or "").strip()
    if not sender:
        raise MailConfigError("RESEND_API_KEY is set but CONVENE_MAIL_FROM is not, for example "
                              "'Convene <minutes@your-verified-domain>'")
    if not _SENDER.match(sender):
        raise MailConfigError(f"CONVENE_MAIL_FROM must be 'address' or 'Name <address>', got {sender!r}")
    return ResendMailer(key, sender)
