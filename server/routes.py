"""HTTP routes: the REST API and the served pages (docs/api.md). Handlers stay thin; logic is in meetings.py."""
import json
import logging
import re
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, FastAPI, Request, WebSocket, UploadFile, File, Form
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import meetings as service
from .action_items import service as action_items, views as action_item_views
from .attribution.views import utterance_view
from .errors import (ActionItemNotFoundError, AmbiguousDisplayNameError, ColorTakenError, DatabaseBusyError, DeviceConflictError, DeviceNotFoundError,
                     MeetingActiveError, MeetingEndedError, MeetingNotEndedError, MeetingNotFoundError, ParticipantNotFoundError, StorageError,
                     SummaryInProgressError, SummaryNotFoundError, TranscriptEmptyError, UtteranceNotFoundError,
                     ValidationError)
from .ids import is_uuid4
from .timeutil import utc_now
from .repositories import audit_events, meetings as meetings_repo, utterances, policies
from .summary.views import summary_payload
from .export.service import ExportRenderError, MIME
from .transport.signaling import signaling_endpoint
from .policies.extract import ExtractionError
from .policies.service import PolicyVersionNotFailed

log = logging.getLogger("convene.api")

CLIENT_DIR = Path(__file__).resolve().parents[1] / "client"
MAX_BODY_BYTES = 64 * 1024
router = APIRouter()

# A deliberately small, read-only database browser for the demo laptop.  These are application
# tables only: SQLite's internal and sqlite-vec implementation tables are never exposed.
_DATABASE_TABLES = (
    "Meeting", "Device", "Participant", "Utterance", "ConnectionEvent", "AuditEvent",
    "ModelExecution", "TranscriptChunk", "TranscriptIndexMeta", "QAQuery", "Summary",
    "ActionItem", "ActionItemNote", "Export",
    "PolicyDocument", "PolicyVersion", "PolicyChunk",
)
class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


async def read_json_body(request: Request) -> dict:
    """The request's JSON object: empty body is ``{}``; 415 for another content type; 413 over 64 KiB."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise ApiError(413, "payload_too_large", f"body is larger than {MAX_BODY_BYTES} bytes")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            raise ApiError(413, "payload_too_large", f"body is larger than {MAX_BODY_BYTES} bytes")
        chunks.append(chunk)
    raw = b"".join(chunks)
    if not raw.strip():
        return {}
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise ApiError(415, "unsupported_media_type", "send the body as application/json")
    try:
        body = json.loads(raw)
    except ValueError as exc:
        raise ApiError(400, "invalid_request", "body is not valid JSON") from exc
    if not isinstance(body, dict):
        raise ApiError(400, "invalid_request", "body must be a JSON object")
    return body


def _meeting_id(value: str) -> str:
    if not is_uuid4(value):
        raise ApiError(400, "invalid_request", "meeting id must be a UUID v4")
    return value


def _runtime(request: Request):
    return request.app.state.runtime


# -- REST -----------------------------------------------------------------------------------


@router.post("/api/meetings")
async def create_meeting(request: Request):
    body = await read_json_body(request)
    return JSONResponse(await service.create_meeting(_runtime(request), body), status_code=201)


@router.get("/api/meetings/{meeting_id}")
async def get_meeting(meeting_id: str, request: Request):
    return await service.get_meeting(_runtime(request), _meeting_id(meeting_id))


@router.patch("/api/meetings/{meeting_id}")
async def rename_meeting(meeting_id: str, request: Request):
    body = await read_json_body(request)
    return await service.rename_meeting(_runtime(request), _meeting_id(meeting_id), body)


@router.delete("/api/meetings/{meeting_id}")
async def delete_meeting(meeting_id: str, request: Request):
    """Permanent: the meeting and everything it owns are erased (docs/api.md)."""
    return await service.delete_meeting(_runtime(request), _meeting_id(meeting_id))


def _bound(value: str | None, name: str, *, end_of_day: bool) -> str | None:
    """An inclusive ``created_at`` bound: an ISO 8601 date (the whole day) or date-time, as canonical UTC."""
    if value is None or value == "":
        return None
    try:
        if len(value) == 10:
            day = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            moment = day + timedelta(days=1, milliseconds=-1) if end_of_day else day
        else:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if moment.tzinfo is None:
                raise ValueError("no time zone")
    except ValueError as exc:
        raise ApiError(400, "invalid_request", f"{name} must be an ISO 8601 date or UTC date-time") from exc
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@router.get("/api/meetings")
async def list_meetings(request: Request):
    """History list (CON-14). An empty result is an empty list, never an error."""
    params = request.query_params
    status = params.get("status")
    if status not in (None, "created", "live", "ended"):
        raise ApiError(400, "invalid_request", "status must be created, live, or ended")
    try:
        limit, offset = int(params.get("limit", "50")), int(params.get("offset", "0"))
    except ValueError as exc:
        raise ApiError(400, "invalid_request", "limit and offset must be integers") from exc
    if not 1 <= limit <= 200 or offset < 0:
        raise ApiError(400, "invalid_request", "limit must be 1..200 and offset must be non-negative")
    q = (params.get("q") or "").strip() or None
    if q is not None and len(q) > 200:
        raise ApiError(400, "invalid_request", "q is longer than 200 characters")
    from_at = _bound(params.get("from"), "from", end_of_day=False)
    to_at = _bound(params.get("to"), "to", end_of_day=True)
    return await service.list_history(_runtime(request), status=status, q=q, from_at=from_at, to_at=to_at,
                                      limit=limit, offset=offset)


@router.post("/api/meetings/{meeting_id}/devices")
async def register_device(meeting_id: str, request: Request):
    body = await read_json_body(request)
    status, payload = await service.register_device(
        _runtime(request), _meeting_id(meeting_id), body, request.headers.get("user-agent"))
    return JSONResponse(payload, status_code=status)


@router.get("/api/meetings/{meeting_id}/colors")
async def meeting_colors(meeting_id: str, request: Request):
    return await service.colors(_runtime(request), _meeting_id(meeting_id))


@router.post("/api/meetings/{meeting_id}/end")
async def end_meeting(meeting_id: str, request: Request):
    await read_json_body(request)
    status, payload = await service.end_meeting(_runtime(request), _meeting_id(meeting_id))
    return JSONResponse(payload, status_code=status)


@router.get("/api/meetings/{meeting_id}/transcript")
async def get_transcript(meeting_id: str, request: Request):
    meeting_id = _meeting_id(meeting_id)
    raw_after = request.query_params.get("after_seq")
    if raw_after is None:
        after_seq = 0
    else:
        try:
            after_seq = int(raw_after)
        except ValueError as exc:
            raise ApiError(400, "invalid_request", "after_seq must be a non-negative integer") from exc
        if after_seq < 0 or str(after_seq) != raw_after:
            raise ApiError(400, "invalid_request", "after_seq must be a non-negative integer")

    def read(tx):
        meetings_repo.require(tx.conn, meeting_id)
        rows = utterances.list_for_meeting(tx.conn, meeting_id)
        threshold = _runtime(request).settings.attribution.low_confidence_threshold
        views = [utterance_view(tx.conn, row, threshold) for row in rows]
        if raw_after is not None:
            views = [view for view in views if view["seq"] > after_seq]
        return {"meeting_id": meeting_id, "utterances": views, "as_of_seq": audit_events.max_seq(tx.conn)}

    return await _runtime(request).db.run(read)


@router.post("/api/meetings/{meeting_id}/utterances/{utterance_id}/correct")
async def correct_utterance(meeting_id: str, utterance_id: str, request: Request):
    meeting_id = _meeting_id(meeting_id)
    if not is_uuid4(utterance_id):
        raise ApiError(400, "invalid_request", "utterance id must be a UUID v4")
    body = await read_json_body(request)
    result = await _runtime(request).attribution.correct(meeting_id, utterance_id, body)

    def view(tx):
        threshold = _runtime(request).settings.attribution.low_confidence_threshold
        return {"utterance": utterance_view(tx.conn, result.utterance, threshold),
                "participant": asdict(result.participant),
                "created_participant": result.created_participant, "changed": result.changed}

    return await _runtime(request).db.run(view)


@router.post("/api/meetings/{meeting_id}/qa")
async def ask_question(meeting_id: str, request: Request):
    """Live Q&A: the only route that runs retrieval (ADR-11). All three outcomes are 200 results."""
    meeting_id = _meeting_id(meeting_id)
    body = await read_json_body(request)
    return await _runtime(request).qa.ask(meeting_id, body)


@router.post("/api/qa")
async def ask_history(request: Request):
    """History Q&A (CON-14): scope is in the body, ended meetings only. Never used for live questions."""
    body = await read_json_body(request)
    return await _runtime(request).qa.ask_history(body)


def _tags(raw: str | None) -> list[str]:
    if raw is None or not raw.strip(): return []
    values = json.loads(raw)
    if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
        raise ApiError(400, "invalid_request", "tags must be a JSON array of strings")
    return list(dict.fromkeys(" ".join(v.split()).casefold() for v in values if " ".join(v.split())))


@router.post("/api/policies")
async def create_policy(request: Request, title: str = Form(...), tags: str | None = Form(None), file: UploadFile = File(...)):
    title = " ".join(title.split())
    if not 1 <= len(title) <= 200: raise ApiError(400, "invalid_request", "title must be 1 to 200 characters")
    data = await file.read()
    try: return JSONResponse(await _runtime(request).policies.upload(title, _tags(tags), file.filename or "upload", data), status_code=202)
    except OverflowError: raise ApiError(413, "payload_too_large", "policy file exceeds the configured upload limit")
    except ValueError as exc: raise ApiError(400, "invalid_request", str(exc)) from exc
    except KeyError: raise ApiError(404, "policy_not_found", "no such policy")
    except ExtractionError as exc: raise ApiError(400, exc.code, exc.message) from exc


@router.post("/api/policies/{policy_id}/versions")
async def add_policy_version(policy_id: str, request: Request, file: UploadFile = File(...)):
    if not is_uuid4(policy_id): raise ApiError(400, "invalid_request", "policy id must be a UUID v4")
    data = await file.read()
    try: return JSONResponse(await _runtime(request).policies.upload(None, [], file.filename or "upload", data, policy_id), status_code=202)
    except OverflowError: raise ApiError(413, "payload_too_large", "policy file exceeds the configured upload limit")
    except ValueError as exc: raise ApiError(400, "invalid_request", str(exc)) from exc
    except KeyError: raise ApiError(404, "policy_not_found", "no such policy")
    except ExtractionError as exc: raise ApiError(400, exc.code, exc.message) from exc


@router.post("/api/policies/{policy_id}/versions/{version_id}/retry")
async def retry_policy_version(policy_id: str, version_id: str, request: Request):
    if not is_uuid4(policy_id) or not is_uuid4(version_id): raise ApiError(400,"invalid_request","policy ids must be UUID v4")
    await read_json_body(request)
    try:
        version=await _runtime(request).policies.retry(version_id, policy_id)
    except KeyError: raise ApiError(404,"policy_not_found","no such policy version")
    except PolicyVersionNotFailed as exc:
        raise ApiError(409,"policy_version_not_failed",f"only a failed version can be retried; this one is {exc}") from exc
    except FileNotFoundError: raise ApiError(500,"internal_error","the retained original file is missing")
    return JSONResponse({"version":asdict(version)},status_code=202)


@router.get("/api/policies")
async def list_policies(request: Request):
    query = " ".join((request.query_params.get("q") or "").split()).casefold()
    tag = " ".join((request.query_params.get("tag") or "").split()).casefold()
    if len(query) > 200 or len(tag) > 80:
        raise ApiError(400, "invalid_request", "policy filters are too long")
    def read(tx):
        result=[]
        for document in policies.documents(tx.conn):
            tags = json.loads(document.tags)
            if query and query not in document.title.casefold():
                continue
            if tag and tag not in tags:
                continue
            versions=policies.versions(tx.conn,document.policy_id); ready=next((v for v in versions if v.status == "ready"),None)
            result.append({"policy":{**asdict(document), "tags": tags},"current_version":asdict(ready) if ready else None,"latest_version":asdict(versions[0]) if versions else None})
        return {"policies":result}
    return await _runtime(request).db.run(read)


@router.get("/api/policies/{policy_id}")
async def get_policy(policy_id: str, request: Request):
    def read(tx):
        document=policies.document(tx.conn,policy_id)
        if not document: raise ApiError(404,"policy_not_found","no such policy")
        return {"policy":asdict(document),"versions":[asdict(v) for v in policies.versions(tx.conn,policy_id)]}
    return await _runtime(request).db.run(read)


@router.get("/api/policies/{policy_id}/versions/{version_id}/download")
async def download_policy(policy_id: str, version_id: str, request: Request):
    def read(tx):
        version=policies.version(tx.conn,version_id)
        if not version or version.policy_id != policy_id: raise ApiError(404,"policy_not_found","no such policy version")
        return version
    version=await _runtime(request).db.run(read)
    base = _runtime(request).settings.policies_dir or _runtime(request).settings.root / "data/policies"
    return FileResponse(base / version.storage_path, media_type=version.media_type, filename=version.original_filename)


@router.post("/api/meetings/{meeting_id}/summarize")
async def summarize(meeting_id: str, request: Request):
    """Start a summary attempt (live or ended meeting); the outcome arrives as ``summary_ready``/``summary_failed``."""
    meeting_id = _meeting_id(meeting_id)
    await read_json_body(request)
    return JSONResponse(await _runtime(request).summary.summarize(meeting_id), status_code=202)


@router.get("/api/meetings/{meeting_id}/summary")
async def get_summary(meeting_id: str, request: Request):
    meeting_id = _meeting_id(meeting_id)
    return await _runtime(request).db.run(lambda tx: summary_payload(tx.conn, meeting_id))


@router.get("/api/meetings/{meeting_id}/export")
async def get_export(meeting_id: str, request: Request):
    meeting_id = _meeting_id(meeting_id)
    if request.query_params.get("format", "docx") != "docx":
        raise ApiError(400, "unsupported_format", "only DOCX export is supported")
    try:
        export = await _runtime(request).export.ensure(meeting_id)
    except ExportRenderError as exc:
        state = await _runtime(request).export.payload(meeting_id)
        if state["export"] is None and state["latest_attempt"] is None:
            raise ApiError(409, "summary_not_ready", "no ready summary exists for this meeting") from exc
        raise ApiError(500, "export_render_failed", str(exc)) from exc
    return FileResponse(_runtime(request).export._path(export), media_type=MIME, filename=f"{meeting_id}.docx")


@router.get("/api/meetings/{meeting_id}/export/status")
async def get_export_status(meeting_id: str, request: Request):
    return await _runtime(request).export.payload(_meeting_id(meeting_id))


def _action_item_id(value: str) -> str:
    if not is_uuid4(value):
        raise ApiError(400, "invalid_request", "action item id must be a UUID v4")
    return value


def _due_bound(value: str | None, name: str) -> str | None:
    if value is None or value == "":
        return None
    try:
        if len(value) != 10 or datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d") != value:
            raise ValueError
    except ValueError as exc:
        raise ApiError(400, "invalid_request", f"{name} must be a date YYYY-MM-DD") from exc
    return value


@router.get("/api/action-items")
async def list_action_items(request: Request):
    """Global action-item list (CON-16): open items only unless ``status`` says otherwise."""
    params = request.query_params
    raw_status = (params.get("status") or "open").strip()
    statuses = action_item_views.STATUSES if raw_status == "all" else tuple(dict.fromkeys(raw_status.split(",")))
    if any(status not in action_item_views.STATUSES for status in statuses):
        raise ApiError(400, "invalid_request", "status must be open, done, cancelled, a comma-separated set, or all")
    sort = params.get("sort", "due")
    if sort not in action_item_views.SORTS:
        raise ApiError(400, "invalid_request", "sort must be due or recent")
    try:
        limit, offset = int(params.get("limit", "50")), int(params.get("offset", "0"))
    except ValueError as exc:
        raise ApiError(400, "invalid_request", "limit and offset must be integers") from exc
    if not 1 <= limit <= 200 or offset < 0:
        raise ApiError(400, "invalid_request", "limit must be 1..200 and offset must be non-negative")
    owner = " ".join((params.get("owner") or "").split()) or None
    if owner is not None and len(owner) > 80:
        raise ApiError(400, "invalid_request", "owner is longer than 80 characters")
    meeting_id = params.get("meeting_id") or None
    if meeting_id is not None and not is_uuid4(meeting_id):
        raise ApiError(400, "invalid_request", "meeting_id must be a UUID v4")
    overdue = params.get("overdue", "false")
    if overdue not in ("true", "false"):
        raise ApiError(400, "invalid_request", "overdue must be true or false")
    filters = {"statuses": statuses, "owner": owner, "meeting_id": meeting_id, "overdue": overdue == "true",
               "due_after": _due_bound(params.get("due_after"), "due_after"),
               "due_before": _due_bound(params.get("due_before"), "due_before"),
               "sort": sort, "limit": limit, "offset": offset}
    return await _runtime(request).db.run(lambda tx: action_item_views.list_items(tx.conn, **filters))


@router.get("/api/action-items/{action_item_id}")
async def get_action_item(action_item_id: str, request: Request):
    action_item_id = _action_item_id(action_item_id)
    return await _runtime(request).db.run(lambda tx: action_item_views.detail(tx.conn, action_item_id))


@router.patch("/api/action-items/{action_item_id}")
async def edit_action_item(action_item_id: str, request: Request):
    """Owner, due date and status edits: a row update plus one audit event, never a model call."""
    action_item_id = _action_item_id(action_item_id)
    body = await read_json_body(request)

    def edit(tx):
        changed = action_items.update_action_item(tx, action_item_id, body)
        return {"action_item": action_item_views.item_view(tx.conn, action_item_id), "changed": changed}

    return await _runtime(request).db.run(edit)


@router.post("/api/action-items/{action_item_id}/notes")
async def add_action_item_note(action_item_id: str, request: Request):
    """Append a note from the meeting it was mentioned in; never creates an item."""
    action_item_id = _action_item_id(action_item_id)
    body = await read_json_body(request)
    unknown = sorted(set(body) - {"source_meeting_id", "text", "status"})
    if unknown:
        raise ApiError(400, "invalid_request", f"unknown field(s): {', '.join(unknown)}")

    def add(tx):
        note = action_items.add_action_item_note(tx, action_item_id, body.get("source_meeting_id"), body.get("text"),
                                                 body.get("status"))
        return {"note": action_item_views.note_view(tx.conn, note),
                "action_item": action_item_views.item_view(tx.conn, action_item_id)}

    return JSONResponse(await _runtime(request).db.run(add), status_code=201)


@router.get("/metrics")
async def metrics(request: Request):
    """Read-only per-device diagnostics (not part of the contract; nothing may depend on it)."""
    return _runtime(request).peers.diagnostics()


@router.get("/api/database")
async def database_inspector(request: Request):
    """A bounded, read-only view of persisted application records for the local demo."""
    selected = request.query_params.get("table")
    if selected is not None and selected not in _DATABASE_TABLES:
        raise ApiError(400, "invalid_request", "table is not available in the database viewer")

    def read(tx):
        counts = [{"name": name, "count": tx.conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]}
                  for name in _DATABASE_TABLES]
        if selected is None:
            return {"tables": counts}
        columns = [row["name"] for row in tx.conn.execute(f'PRAGMA table_info("{selected}")')]
        # All application tables are ordinary SQLite rowid tables. Ordering by insertion order keeps this inspector
        # generic: tables such as Summary and ActionItem intentionally do not have a created_at column.
        rows = [dict(row) for row in tx.conn.execute(f'SELECT * FROM "{selected}" ORDER BY rowid DESC LIMIT 100')]
        return {"table": selected, "columns": columns, "rows": rows,
                "total": next(item["count"] for item in counts if item["name"] == selected), "limit": 100,
                "tables": counts}

    return await _runtime(request).db.run(read)


# -- pages ----------------------------------------------------------------------------------


# Every /static script and stylesheet a page links gets ?v=<modification time>, so a changed file has a new URL and
# a phone cannot keep running an old cached copy under a fresh page (a chosen colour was once never sent that way).
_ASSET_URL = re.compile(r'((?:src|href)="/static/)([^"?#]+)"')


def _versioned(match: re.Match) -> str:
    asset = CLIENT_DIR / match.group(2)
    version = asset.stat().st_mtime_ns if asset.is_file() and CLIENT_DIR in asset.resolve().parents else 0
    return f'{match.group(1)}{match.group(2)}?v={version}"'


def _page(name: str) -> HTMLResponse:
    html = _ASSET_URL.sub(_versioned, (CLIENT_DIR / name).read_text(encoding="utf-8"))
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


async def _existing_meeting(request: Request, meeting_id: str):
    if not is_uuid4(meeting_id):
        return None
    return await _runtime(request).db.run(lambda tx: meetings_repo.get(tx.conn, meeting_id))


def _not_found_page() -> HTMLResponse:
    return HTMLResponse("<!doctype html><meta charset=utf-8><title>Not found</title><h1>Meeting not found</h1>"
                        "<p>Check the link or scan the QR code again.</p>", status_code=404)


@router.get("/")
async def home():
    return _page("home.html")


@router.get("/history")
async def history_page():
    """Past meetings and cross-meeting Q&A (CON-14)."""
    return _page("history.html")


@router.get("/action-items")
async def action_items_page():
    """Action items across meetings (CON-16)."""
    return _page("action_items.html")


@router.get("/policies")
async def policies_page():
    return _page("policies.html")


@router.get("/policies/{policy_id}")
async def policy_page(policy_id: str, request: Request):
    if not is_uuid4(policy_id):
        return _not_found_page()
    found=await _runtime(request).db.run(lambda tx: policies.document(tx.conn,policy_id))
    return _page("policy.html") if found else _not_found_page()


@router.get("/database")
async def database_page():
    """Judge-facing, read-only SQLite inspector. Kept separate from the meeting workflow."""
    return _page("database.html")


@router.get("/join/{meeting_id}")
async def join_page(meeting_id: str, request: Request):
    return _page("index.html") if await _existing_meeting(request, meeting_id) else _not_found_page()


@router.get("/dashboard/{meeting_id}")
async def dashboard_page(meeting_id: str, request: Request):
    meeting = await _existing_meeting(request, meeting_id)
    if meeting is None:
        return _not_found_page()
    if meeting.status == "ended":
        return RedirectResponse(f"/meetings/{meeting_id}", status_code=302)
    return _page("dashboard.html")


@router.get("/meetings/{meeting_id}")
async def post_meeting_page(meeting_id: str, request: Request):
    """Summary, action items and transcript. Served for a live meeting too, for "summarize now"."""
    return _page("post_meeting.html") if await _existing_meeting(request, meeting_id) else _not_found_page()


# -- WebSockets -----------------------------------------------------------------------------


@router.websocket("/ws/signal/{meeting_id}")
async def signal(websocket: WebSocket, meeting_id: str):
    await signaling_endpoint(websocket, meeting_id)


@router.websocket("/ws/dashboard/{meeting_id}")
async def dashboard_feed(websocket: WebSocket, meeting_id: str):
    runtime = websocket.app.state.runtime
    await websocket.accept()
    meeting = None
    if is_uuid4(meeting_id):
        meeting = await runtime.db.run(lambda tx: meetings_repo.get(tx.conn, meeting_id))
    if meeting is None:
        await websocket.send_json({"type": "error", "seq": None, "meeting_id": meeting_id,
                                   "at": utc_now(),
                                   "code": "meeting_not_found", "message": f"no meeting with id {meeting_id}"})
        await websocket.close(code=1000)
        return
    await runtime.hub.serve(websocket, meeting_id)


# -- error handling -------------------------------------------------------------------------

_STORAGE_ERRORS = [
    (MeetingNotFoundError, 404, "meeting_not_found"),
    (ActionItemNotFoundError, 404, "action_item_not_found"),
    (DeviceNotFoundError, 404, "device_not_found"),
    (UtteranceNotFoundError, 404, "utterance_not_found"),
    (ParticipantNotFoundError, 404, "participant_not_found"),
    (SummaryNotFoundError, 404, "summary_not_found"),
    (SummaryInProgressError, 409, "summary_in_progress"),
    (TranscriptEmptyError, 409, "transcript_empty"),
    (AmbiguousDisplayNameError, 409, "ambiguous_display_name"),
    (ColorTakenError, 409, "color_taken"),
    (MeetingEndedError, 409, "meeting_ended"),
    (MeetingNotEndedError, 409, "meeting_not_ended"),
    (MeetingActiveError, 409, "meeting_active"),
    (DeviceConflictError, 409, "device_conflict"),
    (ValidationError, 400, "invalid_request"),
    (DatabaseBusyError, 500, "internal_error"),
]


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError):
        return _error(exc.status, exc.code, exc.message)

    @app.exception_handler(StorageError)
    async def storage_error(request: Request, exc: StorageError):
        for cls, status, code in _STORAGE_ERRORS:
            if isinstance(exc, cls):
                if status == 500:
                    log.error("storage error: %s", exc)
                    return _error(500, code, "the database is busy; try again")
                return _error(status, code, str(exc))
        log.exception("unexpected storage error", exc_info=exc)
        return _error(500, "internal_error", "unexpected server error")

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return _error(exc.status_code, code, str(exc.detail))

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception):
        log.exception("unhandled error on %s", request.url.path, exc_info=exc)
        return _error(500, "internal_error", "unexpected server error")
