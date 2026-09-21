"""HTTP routes: the REST API and the served pages (docs/api.md). Handlers stay thin; logic is in meetings.py."""
import json
import logging
from pathlib import Path

from fastapi import APIRouter, FastAPI, Request, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import meetings as service
from .errors import (DatabaseBusyError, DeviceConflictError, DeviceNotFoundError, MeetingEndedError,
                     MeetingNotFoundError, StorageError, ValidationError)
from .ids import is_uuid4
from .timeutil import utc_now
from .repositories import meetings as meetings_repo
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


@router.post("/api/meetings/{meeting_id}/end")
async def end_meeting(meeting_id: str, request: Request):
    await read_json_body(request)
    status, payload = await service.end_meeting(_runtime(request), _meeting_id(meeting_id))
    return JSONResponse(payload, status_code=status)


@router.get("/metrics")
async def metrics(request: Request):
    """Read-only per-device diagnostics (not part of the contract; nothing may depend on it)."""
    return _runtime(request).peers.diagnostics()


# -- pages ----------------------------------------------------------------------------------


def _page(name: str) -> FileResponse:
    return FileResponse(CLIENT_DIR / name, headers={"Cache-Control": "no-store"})


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
