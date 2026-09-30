"""Send the meeting's DOCX minutes to the participants who gave an email address at join (ADR-33).

Who gets what is chosen per person (ADR-34): any of the participants with an address, each with a non-empty subset of
summary, action items and transcript. Without a choice, everyone gets everything.

Only an explicit request sends (the post-meeting page's button); nothing is sent when a meeting ends. The meeting
must have ended and a ready summary must exist: the attachment is the same current, non-stale DOCX that
``GET /api/meetings/{id}/export`` serves, rendered first if needed. Each recipient gets their own message, so no
one sees another person's address. One failed address does not stop the others. Addresses never enter an audit
payload or a log line; the audit event records participant ids only.
"""
import asyncio

from ..audit import emit
from ..errors import MeetingNotEndedError, ValidationError
from ..export.docx_renderer import SECTIONS
from ..export.service import ExportRenderError
from ..repositories import meetings, participant_emails
from ..timeutil import utc_now
from . import message
from .resend import Attachment, MailError

SEND_GAP_S = 0.6  # Resend's default limit is 2 requests per second


class MailNotConfiguredError(Exception):
    """No Resend key and sender were given to this server process (``409 mail_not_configured``)."""


class NoRecipientsError(Exception):
    """No participant of the meeting gave an address (``409 no_recipients``)."""


class EmailInProgressError(Exception):
    """A send for this meeting is already running (``409 email_in_progress``)."""


def mask(email: str) -> str:
    """``pr•••@example.com``: enough for the room to recognise an address on a shared screen, not to copy it."""
    local, _, domain = email.partition("@")
    return f"{local[:2]}•••@{domain}"


def _recipient_view(row: dict) -> dict:
    return {"participant_id": row["participant_id"], "display_name": row["display_name"],
            "email_masked": mask(row["email"]), "last_sent_at": row["last_sent_at"]}


def parse_plan(body: dict) -> list[tuple[str, tuple[str, ...]]] | None:
    """The request's ``recipients``: who gets which sections (ADR-34). None (absent) means everyone, everything.

    Each entry is ``{"participant_id", "sections"}``; sections are a non-empty subset of ``SECTIONS``, returned in
    template order. A participant may appear once. Whether each one gave an address is checked against the meeting.
    """
    raw = body.get("recipients")
    if raw is None:
        return None
    if not isinstance(raw, list) or not raw:
        raise ValidationError("recipients must be a non-empty list")
    plan, seen = [], set()
    for entry in raw:
        if not isinstance(entry, dict) or not isinstance(entry.get("participant_id"), str):
            raise ValidationError("each recipient needs a participant_id")
        sections = entry.get("sections")
        if not isinstance(sections, list) or not sections or not all(isinstance(k, str) for k in sections) \
                or not set(sections) <= set(SECTIONS):
            raise ValidationError(f"sections must be a non-empty list drawn from {', '.join(SECTIONS)}")
        if entry["participant_id"] in seen:
            raise ValidationError("a participant may appear only once in recipients")
        seen.add(entry["participant_id"])
        plan.append((entry["participant_id"], tuple(key for key in SECTIONS if key in sections)))
    return plan


class EmailReportService:
    def __init__(self, db, export, mailer, *, send_gap_s: float = SEND_GAP_S):
        self.db, self.export, self.mailer, self.send_gap_s = db, export, mailer, send_gap_s
        self._sending: set[str] = set()

    async def status(self, meeting_id: str) -> dict:
        def read(tx):
            meeting = meetings.require(tx.conn, meeting_id)
            return meeting, participant_emails.list_for_meeting(tx.conn, meeting_id)
        meeting, rows = await self.db.run(read)
        return {"configured": self.mailer is not None, "sender": self.mailer.sender if self.mailer else None,
                "meeting_ended": meeting.status == "ended", "sending": meeting_id in self._sending,
                "sections": list(SECTIONS),
                "recipients": [_recipient_view(row) for row in rows]}

    async def send(self, meeting_id: str, plan: list[tuple[str, tuple[str, ...]]] | None = None) -> dict:
        """Send to ``plan`` (from ``parse_plan``), or to everyone with every section when it is None."""
        if meeting_id in self._sending:
            raise EmailInProgressError("the minutes are already being sent for this meeting")
        self._sending.add(meeting_id)
        try:
            return await self._send(meeting_id, plan)
        finally:
            self._sending.discard(meeting_id)

    async def _send(self, meeting_id: str, plan) -> dict:
        def read(tx):
            meeting = meetings.require(tx.conn, meeting_id)
            return meeting, participant_emails.list_for_meeting(tx.conn, meeting_id)
        meeting, rows = await self.db.run(read)
        if meeting.status != "ended":
            raise MeetingNotEndedError("the minutes can be emailed once the meeting has ended")
        if self.mailer is None:
            raise MailNotConfiguredError("email is not set up on this laptop (RESEND_API_KEY and CONVENE_MAIL_FROM)")
        if not rows:
            raise NoRecipientsError("no one in this meeting added an email address")
        by_id = {row["participant_id"]: row for row in rows}
        if plan is None:
            plan = [(row["participant_id"], SECTIONS) for row in rows]
        missing = [pid for pid, _ in plan if pid not in by_id]
        if missing:
            raise ValidationError(f"participant {missing[0]} did not add an email address in this meeting")

        # The full file is the exported DOCX itself (rendered first if needed), as before. Any other selection is
        # rendered once per distinct set of sections, in memory, from the same stored rows. Everything is rendered
        # before the first message goes out, so a render failure sends nothing.
        export = await self.export.ensure(meeting_id)  # ExportRenderError propagates; the route maps it
        files: dict[tuple[str, ...], bytes] = {}
        for sections in dict.fromkeys(sections for _, sections in plan):
            if sections == SECTIONS:
                try:
                    files[sections] = self.export._path(export).read_bytes()
                except OSError as exc:
                    raise ExportRenderError("the DOCX file could not be read") from exc
            else:
                files[sections] = await self.export.render_sections(meeting_id, sections)
        name = message.attachment_name(meeting.title)
        subject = message.subject(meeting.title)
        date = message.meeting_date(meeting.started_at, meeting.created_at)

        sent, failed = [], []
        for index, (participant_id, sections) in enumerate(plan):
            row = by_id[participant_id]
            if index:
                await asyncio.sleep(self.send_gap_s)
            html, text = message.bodies(row["display_name"], meeting.title, date, sections)
            attachment = Attachment(name, files[sections])
            try:
                await asyncio.to_thread(self.mailer.send, row["email"], subject, html, text, attachment)
                sent.append(row)
            except MailError as exc:
                failed.append((row, str(exc)))
            except Exception as exc:  # one bad address or bug must not stop the rest
                failed.append((row, f"{type(exc).__name__}: could not send"))

        now = utc_now()
        def record(tx):
            for row in sent:
                participant_emails.mark_sent(tx.conn, row["participant_id"], now)
            emit(tx, "minutes_emailed", "export", {
                "export_id": export.export_id,
                "sent_participant_ids": [row["participant_id"] for row in sent],
                "failed_participant_ids": [row["participant_id"] for row, _ in failed],
                "sections": {participant_id: list(sections) for participant_id, sections in plan}},
                meeting_id=meeting_id, timestamp=now)
            return participant_emails.list_for_meeting(tx.conn, meeting_id)
        rows_after = await self.db.run(record)
        return {"export_id": export.export_id, "sent_count": len(sent),
                "failed": [{"participant_id": row["participant_id"], "display_name": row["display_name"],
                            "email_masked": mask(row["email"]), "error": error} for row, error in failed],
                "recipients": [_recipient_view(row) for row in rows_after]}
