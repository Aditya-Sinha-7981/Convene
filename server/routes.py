"""HTTP routes: the REST API and the served pages (docs/api.md). Handlers stay thin; logic is in meetings.py."""
import json
import logging
import re
from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import meetings as service
from .attribution.views import utterance_view
from .errors import (AmbiguousDisplayNameError, ColorTakenError, DatabaseBusyError, DeviceConflictError, DeviceNotFoundError,
                     MeetingEndedError, MeetingNotFoundError, ParticipantNotFoundError, StorageError,
                     SummaryInProgressError, SummaryNotFoundError, TranscriptEmptyError, UtteranceNotFoundError,
                     ValidationError)
from .ids import is_uuid4
from .timeutil import utc_now
from .repositories import audit_events, meetings as meetings_repo, utterances
from .summary.views import summary_payload
from .transport.signaling import signaling_endpoint

log = logging.getLogger("convene.api")

CLIENT_DIR = Path(__file__).resolve().parents[1] / "client"
MAX_BODY_BYTES = 64 * 1024
router = APIRouter()


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


@router.get("/metrics")
async def metrics(request: Request):
    """Read-only per-device diagnostics (not part of the contract; nothing may depend on it)."""
    return _runtime(request).peers.diagnostics()


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
    (DeviceNotFoundError, 404, "device_not_found"),
    (UtteranceNotFoundError, 404, "utterance_not_found"),
    (ParticipantNotFoundError, 404, "participant_not_found"),
    (SummaryNotFoundError, 404, "summary_not_found"),
    (SummaryInProgressError, 409, "summary_in_progress"),
    (TranscriptEmptyError, 409, "transcript_empty"),
    (AmbiguousDisplayNameError, 409, "ambiguous_display_name"),
    (ColorTakenError, 409, "color_taken"),
    (MeetingEndedError, 409, "meeting_ended"),
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
