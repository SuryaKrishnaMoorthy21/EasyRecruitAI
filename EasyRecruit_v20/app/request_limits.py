"""
Request-size and multipart-part limits (Task 5).

Why this has to happen BEFORE the route handler: FastAPI/Starlette parse a multipart body completely,
spooling every part to memory (up to 1 MB each) or disk, before the endpoint function runs. So a size
or file-count check inside the endpoint only ever fires after the damage (memory, disk, CPU) is done.

  * RequestSizeLimitMiddleware refuses a request whose Content-Length is over the limit without reading
    a single body byte, and cuts off a body that grows past it while streaming (chunked uploads, or a
    lying Content-Length). It is pure ASGI, so nothing downstream ever sees the excess.
  * BoundedFormRoute makes Starlette stop accepting multipart parts after a limit, instead of its
    default of 1000 files / 1000 fields, on the batch routes.

Limits are read from `settings` on every request, so they follow the configuration (and tests).
"""
from typing import Optional, Tuple

from fastapi import HTTPException, Request
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

from app.config import REQUEST_OVERHEAD_BYTES, settings
from app.uploads import format_size

BATCH_PATH = "/api/v1/analysis/analyze-batch"
PUBLIC_BATCH_PATH = "/api/v1/analyze/batch"
PUBLIC_BATCH_MAX_FILES = 10                      # the public batch endpoint has always been documented as "up to 10"
PHOTO_PATH = "/api/v1/profile/photo"
MAX_FORM_FIELDS = 20                             # batch forms carry a handful of small text fields at most
_BODY_METHODS = {"POST", "PUT", "PATCH"}


def _normalise(path: str) -> str:
    return path.rstrip("/") or "/"


def batch_file_limit(path: str) -> int:
    return PUBLIC_BATCH_MAX_FILES if _normalise(path) == PUBLIC_BATCH_PATH else settings.MAX_BATCH_FILES


def request_limit_for(path: str) -> int:
    """Largest request body (bytes) accepted for this path."""
    path = _normalise(path)
    if path in (BATCH_PATH, PUBLIC_BATCH_PATH):
        return settings.MAX_BATCH_REQUEST_SIZE
    if path == PHOTO_PATH:                       # profile photos have their own, smaller, long-standing limit
        try:
            from app.api.profile_routes import MAX_UPLOAD_BYTES as photo_limit
        except Exception:                        # pragma: no cover - defensive
            photo_limit = 5 * 1024 * 1024
        return photo_limit + REQUEST_OVERHEAD_BYTES
    return settings.MAX_FILE_SIZE + REQUEST_OVERHEAD_BYTES


def request_too_large_message(path: str) -> str:
    per_file = format_size(settings.MAX_FILE_SIZE)
    if _normalise(path) in (BATCH_PATH, PUBLIC_BATCH_PATH):
        return (f"Request is too large. A batch may contain at most {batch_file_limit(path)} files and "
                f"{format_size(request_limit_for(path))} in total, each file at most {per_file}.")
    return f"Request is too large. The maximum allowed upload size is {per_file} per file."


def too_many_files_message(limit: int, path: str = "") -> str:
    if _normalise(path) == PUBLIC_BATCH_PATH:
        return f"Maximum {limit} files per request."          # the public batch endpoint's long-standing wording
    return f"Too many files at once (max {limit}). Please split into smaller batches."


class RequestSizeLimitMiddleware:
    """Pure-ASGI body-size guard. Responds 413 {"detail": ...} and asks the client to close the connection."""

    def __init__(self, app, limit_for=None):
        self.app = app
        self._limit_for = limit_for or request_limit_for

    @staticmethod
    async def _reject(scope, receive, send, status_code: int, detail: str) -> None:
        # "Connection: close": the body was not consumed, so this connection must not be reused.
        response = JSONResponse({"detail": detail}, status_code=status_code, headers={"Connection": "close"})
        await response(scope, receive, send)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in _BODY_METHODS:
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        limit = self._limit_for(path)
        if limit is None:
            await self.app(scope, receive, send)
            return

        for name, value in scope["headers"]:
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    await self._reject(scope, receive, send, 400, "Invalid Content-Length header.")
                    return
                if declared > limit:
                    await self._reject(scope, receive, send, 413, request_too_large_message(path))
                    return
                break

        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    # Raised into whatever is reading the body; FastAPI re-raises an HTTPException as-is
                    # and the exception middleware turns it into the same JSON 413.
                    raise HTTPException(
                        status_code=413, detail=request_too_large_message(path), headers={"Connection": "close"}
                    )
            return message

        await self.app(scope, limited_receive, send)


def form_limits_for(path: str) -> Optional[Tuple[int, int]]:
    """(max file parts, max field parts) Starlette may accept for this path, or None for its defaults."""
    path = _normalise(path)
    if path in (BATCH_PATH, PUBLIC_BATCH_PATH):
        return batch_file_limit(path), MAX_FORM_FIELDS
    return None


class BoundedFormRoute(APIRoute):
    """
    Pre-parses the multipart body of the batch routes with a real part limit. Starlette caches the parsed
    form on the request, so FastAPI's own (unlimited-by-default) parse that follows reuses this one.
    """

    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request: Request):
            limits = form_limits_for(request.url.path) if request.method == "POST" else None
            if limits is not None and request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
                max_files, max_fields = limits
                try:
                    await request.form(max_files=max_files, max_fields=max_fields)
                except StarletteHTTPException as exc:
                    # Starlette raises ITS OWN HTTPException (the base of fastapi's) for a part-limit breach,
                    # and our size middleware raises fastapi's: catching the base class covers both.
                    if exc.status_code == 400 and "Too many files" in str(exc.detail):
                        raise HTTPException(
                            status_code=400, detail=too_many_files_message(max_files, request.url.path)
                        ) from None
                    raise
                except Exception:
                    raise HTTPException(status_code=400, detail="There was an error parsing the body") from None
            return await original(request)

        return handler
