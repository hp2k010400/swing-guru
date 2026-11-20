"""Loopback-only HTTP API for the blind coach review.

Security boundary (see VALIDATION_IMPLEMENTATION.md):

* binds to 127.0.0.1 and rejects any other ``Host``;
* only four UI assets are reachable, plus the registered videos and the JSON API;
* every mutation needs the per-process token, a bounded body and a request whose
  ``Origin`` is *exactly* this server's scheme, host and port (no other local port);
* before a submission the API returns an opaque item id, its ordinal, the club, the
  review state and the video URL - never a score, path, filename or analysis field;
* the score is unlocked per reviewer and per swing, and only by submitting ratings;
* videos stream in bounded chunks with byte ranges, never whole files in memory.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from . import config as C
from . import store
from . import ui

CHUNK = 64 * 1024
MEDIA_TYPES = {".mp4": "video/mp4", ".mov": "video/quicktime", ".m4v": "video/x-m4v"}


class ApiProblem(Exception):
    def __init__(self, status: int, message: str) -> None:
        self.status, self.message = status, message
        super().__init__(message)


class RangeError(Exception):
    """A malformed, multiple or unsatisfiable byte range."""


def _problem(error: store.StoreError) -> ApiProblem:
    text = str(error)
    lowered = text.lower()
    if "unknown batch" in lowered or "unknown swing" in lowered:
        return ApiProblem(404, text)
    if "hidden" in lowered:
        return ApiProblem(403, text)
    if "already" in lowered:
        return ApiProblem(409, text)
    return ApiProblem(400, text)


def same_origin(origin: str | None, host_header: str) -> bool:
    """True when ``Origin`` is exactly this server's http scheme, host and port.

    A missing ``Origin`` is allowed (non-browser clients); any other local port or
    scheme is refused, so another localhost page cannot drive the review.
    """

    if origin is None:
        return True
    return origin.strip().rstrip("/").lower() == "http://" + host_header.strip().lower()


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """``None`` for no range; ``(start, end)`` inclusive; :class:`RangeError` otherwise.

    Multiple ranges are refused rather than silently truncated to the first one.
    """

    if not header:
        return None
    value = header.strip().lower()
    if not value.startswith("bytes="):
        raise RangeError("unsupported range unit")
    specs = [part.strip() for part in value[len("bytes="):].split(",")]
    if len(specs) != 1:
        raise RangeError("multiple ranges are not supported")
    first, separator, last = specs[0].partition("-")
    if not separator or (first and not first.isdecimal()) or (last and not last.isdecimal()):
        raise RangeError("malformed range")
    try:
        if first:
            start = int(first)
            end = int(last) if last else size - 1
        else:
            if not last:
                raise RangeError("empty range")
            length = int(last)
            if length <= 0:
                raise RangeError("empty suffix range")
            start = max(0, size - length)
            end = size - 1
    except ValueError:
        raise RangeError("malformed range") from None
    if start < 0 or end < start or size == 0 or start >= size:
        raise RangeError("unsatisfiable range")
    return start, min(end, size - 1)


def _chunks(path: Path, start: int, end: int) -> Iterator[bytes]:
    remaining = end - start + 1
    with path.open("rb") as handle:
        handle.seek(start)
        while remaining > 0:
            block = handle.read(min(CHUNK, remaining))
            if not block:
                break
            remaining -= len(block)
            yield block


def media_headers(path: Path, size: int) -> dict[str, str]:
    return {"Accept-Ranges": "bytes", "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Content-Length": str(size)}


def media_response(path: Path, range_header: str | None, *, head_only: bool = False) -> Response:
    """Byte-range video responses: seeking works, chunks stay bounded, no filename leaks."""

    size = path.stat().st_size
    media_type = MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")
    headers = media_headers(path, size)
    try:
        window = parse_range(range_header, size)
    except RangeError:
        headers["Content-Range"] = f"bytes */{size}"
        headers["Content-Length"] = "0"
        return Response(status_code=416, headers=headers)
    if window is None:
        if head_only:
            return Response(status_code=200, headers=headers, media_type=media_type)
        return StreamingResponse(_chunks(path, 0, size - 1), status_code=200, headers=headers, media_type=media_type)
    start, end = window
    length = end - start + 1
    headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    headers["Content-Length"] = str(length)
    if head_only:
        return Response(status_code=206, headers=headers, media_type=media_type)
    return StreamingResponse(_chunks(path, start, end), status_code=206, headers=headers, media_type=media_type)


def create_app(*, db_path: str | Path, batch: str, reviewer_id: str, reviewer_name: str, token: str,
               edit_mode: bool = False) -> FastAPI:
    app = FastAPI(title="Swing Guru coach review", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.batch = batch
    app.state.reviewer_id = reviewer_id
    app.state.reviewer_name = reviewer_name
    app.state.edit_mode = bool(edit_mode)
    app.state.db_path = str(db_path)

    @contextmanager
    def db() -> Iterator[sqlite3.Connection]:
        conn = store.connect(db_path)
        try:
            yield conn
        finally:
            conn.close()

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host_header = (request.headers.get("host") or "").strip()
        hostname = host_header.rsplit(":", 1)[0].strip("[]").lower()
        if hostname not in C.ALLOWED_HOSTS:
            return JSONResponse({"error": "This review server only answers on loopback."}, status_code=403)
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            if not same_origin(request.headers.get("origin"), host_header):
                return JSONResponse({"error": "Cross-origin request refused."}, status_code=403)
            if request.headers.get(C.TOKEN_HEADER) != token:
                return JSONResponse({"error": "Missing or wrong request token."}, status_code=403)
            length = request.headers.get("content-length")
            if length and length.isdigit() and int(length) > C.MAX_BODY_BYTES:
                return JSONResponse({"error": "Request body too large."}, status_code=413)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if response.headers.get("content-type", "").startswith("text/html"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; script-src 'self'; style-src 'self'; media-src 'self'; img-src 'self'; "
                "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        return response

    @app.exception_handler(ApiProblem)
    async def _api_problem(_: Request, error: ApiProblem):
        return JSONResponse({"error": error.message}, status_code=error.status)

    @app.exception_handler(store.StoreError)
    async def _store_error(_: Request, error: store.StoreError):
        problem = _problem(error)
        return JSONResponse({"error": problem.message}, status_code=problem.status)

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, error: Exception):  # pragma: no cover - safety net
        return JSONResponse({"error": "Something went wrong in the review server."}, status_code=500)

    async def body(request: Request) -> dict:
        """Read the JSON body with the cap enforced *while* the bytes arrive."""

        chunks: list[bytes] = []
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > C.MAX_BODY_BYTES:
                raise ApiProblem(413, "Request body too large.")
            chunks.append(chunk)
        raw = b"".join(chunks)
        if not raw:
            return {}
        try:
            payload = json.loads(raw)
        except ValueError:
            raise ApiProblem(400, "Request body must be JSON.") from None
        if not isinstance(payload, dict):
            raise ApiProblem(400, "Request body must be a JSON object.")
        return payload

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse(ui.page(batch=batch, reviewer=reviewer_name, token=token, edit_mode=edit_mode))

    @app.get("/app.css")
    def stylesheet() -> Response:
        return Response(ui.CSS, media_type="text/css")

    @app.get("/app.js")
    def script() -> Response:
        return Response(ui.JS, media_type="text/javascript")

    @app.get("/healthz")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/api/session")
    def session() -> dict:
        with db() as conn:
            return store.session_view(conn, batch, reviewer_id, reviewer_name, edit_mode=edit_mode)

    @app.get("/api/items/{public_id}")
    def item(public_id: str) -> dict:
        with db() as conn:
            return store.item_view(conn, batch, reviewer_id, public_id, edit_mode=edit_mode)

    @app.get("/api/items/{public_id}/score")
    def score(public_id: str) -> dict:
        with db() as conn:
            return store.score_view(conn, batch, reviewer_id, public_id)

    @app.post("/api/items/{public_id}/review")
    async def submit(public_id: str, request: Request) -> dict:
        payload = await body(request)
        allow_edit = bool(payload.pop("edit", False)) and edit_mode
        with db() as conn:
            result = store.submit_review(conn, batch, reviewer_id, public_id,
                                         ratings=payload.get("ratings"), confidence=payload.get("confidence"),
                                         notes=payload.get("notes", ""), allow_edit=allow_edit)
            view = store.item_view(conn, batch, reviewer_id, public_id, edit_mode=edit_mode)
        return {**result, "locked": not edit_mode, "review": view["review"]}

    @app.post("/api/items/{public_id}/skip")
    async def skip(public_id: str, request: Request) -> dict:
        await body(request)
        with db() as conn:
            return store.skip_item(conn, batch, reviewer_id, public_id)

    @app.post("/api/items/{public_id}/unskip")
    async def unskip(public_id: str, request: Request) -> dict:
        await body(request)
        with db() as conn:
            return store.unskip_item(conn, batch, reviewer_id, public_id)

    def find_media(public_id: str) -> Path:
        with db() as conn:
            path = store.video_path_for(conn, batch, public_id)
        if not path.is_file():
            raise ApiProblem(404, "This swing video is not available on disk.")
        return path

    @app.get("/media/{public_id}")
    def media(public_id: str, request: Request) -> Response:
        return media_response(find_media(public_id), request.headers.get("range"))

    @app.head("/media/{public_id}")
    def media_head(public_id: str, request: Request) -> Response:
        return media_response(find_media(public_id), request.headers.get("range"), head_only=True)

    return app


__all__ = ["ApiProblem", "RangeError", "create_app", "media_headers", "media_response", "parse_range", "same_origin"]
