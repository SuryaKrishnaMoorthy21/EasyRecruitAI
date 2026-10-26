"""
Resume-upload intake (Task 5): the one place where an untrusted upload becomes text.

Every endpoint that accepts a resume file used to carry its own copy of "check the extension, read the
whole file into memory, write a temp file, parse it". Those copies disagreed (some never checked the
size at all) and shared the same flaws, so they now all call ingest_resume():

  1. metadata  extension must be .pdf / .docx (legacy .doc is rejected with a clear message) and the
               declared MIME type must not contradict it. The client's MIME type is advisory only.
  2. size      the upload is copied to a temp file in bounded chunks and refused the moment it passes
               MAX_FILE_SIZE; it is never read into memory as a whole.
  3. content   the file's own bytes must match its claimed type (a renamed .exe, an HTML page called
               .pdf or a PDF called .docx is refused), legacy / password-protected Word containers are
               recognised, and the document must really parse.
  4. bounds    parsing runs on the AnalysisGate (bounded concurrency, off the event loop) with a page
               cap, a decompression-bomb guard, a text cap and a deadline.

Failures are HTTPExceptions with a status code and a message that is safe to show: never a temp path,
a library traceback or the text of an internal exception (those go to the log).
"""
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Optional, Tuple

from fastapi import HTTPException, UploadFile

from app.concurrency import get_gate
from app.config import settings
from app.parsers.errors import (
    DocumentError, EncryptedDocumentError, NoExtractableTextError, UnsupportedDocumentError,
)

logger = logging.getLogger(__name__)

CHUNK_SIZE = 256 * 1024          # bytes copied per read; the upload is never held in memory as a whole
HEAD_BYTES = 2048                # leading bytes kept for the signature check

OLE2_MAGIC = bytes.fromhex("D0CF11E0A1B11AE1")      # legacy .doc AND password-protected .docx containers
_ENCRYPTED_PACKAGE_MARKER = "EncryptedPackage".encode("utf-16-le")   # stream name inside encrypted OOXML

LEGACY_DOC_MESSAGE = (
    "Legacy Word (.doc) files are not supported. Please open the file in Word, save it as .docx "
    "(or export it as PDF) and upload it again."
)
NOT_ENOUGH_TEXT_MESSAGE = (
    "Could not extract enough text from this file. It may be a scanned image or contain too little content."
)

_KIND_BY_EXTENSION = {".pdf": "pdf", ".docx": "docx"}
_PDF_MIME = {"application/pdf", "application/x-pdf", "application/acrobat", "applications/vnd.pdf",
             "text/pdf", "text/x-pdf"}
# application/msword is tolerated for .docx only because some clients mislabel it; the content check decides.
_DOCX_MIME = {"application/vnd.openxmlformats-officedocument.wordprocessingml.document", "application/zip",
              "application/x-zip-compressed", "application/msword"}
_GENERIC_MIME = {"", "application/octet-stream", "binary/octet-stream", "application/x-octet-stream",
                 "application/x-download", "application/download", "application/force-download"}

DEFAULT_QUEUE_TIMEOUT = object()      # "use ANALYSIS_QUEUE_TIMEOUT_SECONDS"; pass None to wait indefinitely (batch items)


def resolve_queue_timeout(value):
    return settings.ANALYSIS_QUEUE_TIMEOUT_SECONDS if value is DEFAULT_QUEUE_TIMEOUT else value


class UploadRejected(HTTPException):
    """An upload refused for a reason the user can act on. Being an HTTPException, the existing
    `except HTTPException: raise` clauses in the route handlers (and the batch wrapper) already do the right thing."""


@dataclass
class ParsedResume:
    text: str
    kind: str                           # "pdf" | "docx"
    size: int                           # bytes uploaded
    raw_bytes: Optional[bytes] = None   # only when ingest_resume(keep_bytes=True)


@dataclass
class _Stored:
    path: str
    size: int
    head: bytes


# ── small helpers ────────────────────────────────────────────────────────────

def format_size(num_bytes: int) -> str:
    if num_bytes >= 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):g} MB"
    if num_bytes >= 1024:
        return f"{num_bytes / 1024:g} KB"
    return f"{num_bytes} bytes"


def too_large_message(limit: int) -> str:
    return f"File is too large. The maximum allowed size is {format_size(limit)} per file."


def safe_label(value: Optional[str], limit: int = 80) -> str:
    """A client-supplied name made safe to echo in an error message or log line."""
    base = (value or "").replace("\\", "/").rsplit("/", 1)[-1]
    base = "".join(ch if ch.isprintable() else "?" for ch in base)
    return base if len(base) <= limit else base[:limit] + "..."


def _remove(path: Optional[str]) -> None:
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError as exc:
        logger.debug("Temp-file cleanup failed: %s", exc)


def _reject(exc: DocumentError, label: str) -> UploadRejected:
    if exc.detail_for_log:
        logger.warning("Upload '%s' rejected (%s): %s", label, type(exc).__name__, exc.detail_for_log[:300])
    else:
        logger.info("Upload '%s' rejected (%s)", label, type(exc).__name__)
    return UploadRejected(status_code=exc.status_code, detail=exc.public_message)


# ── step 1: metadata ─────────────────────────────────────────────────────────

def classify_upload(filename: Optional[str], content_type: Optional[str]) -> str:
    """
    Decide from the file NAME and the declared MIME type whether this could be a PDF or a DOCX.
    Returns "pdf" or "docx"; raises UploadRejected (400 / 415) otherwise. The MIME type can only veto
    (a contradictory type is refused); it never makes an unknown file acceptable. Content is verified later.
    """
    if not filename or not filename.strip():
        raise UploadRejected(status_code=400, detail="No filename provided. Please attach a file.")
    label = safe_label(filename)
    extension = os.path.splitext(filename.replace("\\", "/").rsplit("/", 1)[-1])[1].lower()
    if extension == ".doc":
        raise UploadRejected(status_code=415, detail=LEGACY_DOC_MESSAGE)
    if extension not in settings.ALLOWED_EXTENSIONS or extension not in _KIND_BY_EXTENSION:
        raise UploadRejected(
            status_code=415,
            detail=f"Unsupported file type for '{label}'. Please upload a PDF or DOCX file.",
        )
    kind = _KIND_BY_EXTENSION[extension]
    mime = (content_type or "").split(";")[0].strip().lower()
    if mime and mime not in _GENERIC_MIME and mime not in (_PDF_MIME if kind == "pdf" else _DOCX_MIME):
        raise UploadRejected(
            status_code=415,
            detail=(f"The declared content type '{safe_label(mime)}' does not match a {kind.upper()} file "
                    f"('{label}'). Please upload a PDF or DOCX file."),
        )
    return kind


# ── step 2: bounded copy to disk ─────────────────────────────────────────────

async def _stream_to_tempfile(upload: UploadFile, kind: str, max_bytes: int) -> _Stored:
    """Copy the upload to a temp file in CHUNK_SIZE pieces, refusing as soon as it passes max_bytes."""
    declared = getattr(upload, "size", None)
    if declared is not None and declared > max_bytes:
        raise UploadRejected(status_code=413, detail=too_large_message(max_bytes))

    fd, path = tempfile.mkstemp(prefix="easyrecruit-upload-", suffix=".pdf" if kind == "pdf" else ".docx")
    total, head = 0, b""
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = await upload.read(CHUNK_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise UploadRejected(status_code=413, detail=too_large_message(max_bytes))
                if len(head) < HEAD_BYTES:
                    head += chunk[: HEAD_BYTES - len(head)]
                out.write(chunk)
    except BaseException:
        _remove(path)
        raise
    if total == 0:
        _remove(path)
        raise UploadRejected(status_code=400, detail="The uploaded file is empty.")
    return _Stored(path=path, size=total, head=head)


# ── step 3: content must match the claimed type ──────────────────────────────

def _looks_like(head: bytes) -> str:
    # Prefix signatures first (exact at offset 0); the PDF header is a search within the first KB.
    if head.startswith(OLE2_MAGIC):
        return "ole2"
    if head[:2] == b"PK":
        return "zip"
    if b"%PDF-" in head[:1024]:
        return "pdf"
    return "unknown"


def quick_signature_check(head: bytes, kind: str) -> None:
    """Head-bytes-only check, cheap enough for the event loop. Raises UnsupportedDocumentError on a mismatch."""
    actual = _looks_like(head)
    if kind == "pdf":
        if actual == "pdf":
            return
        if actual in ("ole2", "zip"):
            raise UnsupportedDocumentError(
                "This file is a Word/ZIP document, not a PDF. Please check the file extension or upload a real PDF."
            )
        raise UnsupportedDocumentError("This file is not a PDF: its contents are not in PDF format.")
    if actual in ("zip", "ole2"):          # an OLE2 container is refined in the worker (legacy .doc vs encrypted .docx)
        return
    if actual == "pdf":
        raise UnsupportedDocumentError(
            "This file is a PDF, not a DOCX. Please check the file extension or upload a real DOCX."
        )
    raise UnsupportedDocumentError("This file is not a DOCX: its contents are not in Word (.docx) format.")


def _file_contains(path: str, marker: bytes) -> bool:
    """Scan a file for a byte string in bounded blocks (overlap handles a marker split across blocks)."""
    block, keep = 1024 * 1024, len(marker) - 1
    tail = b""
    with open(path, "rb") as handle:
        while True:
            data = handle.read(block)
            if not data:
                return False
            window = tail + data
            if marker in window:
                return True
            tail = window[-keep:] if keep else b""


def _ole2_error(path: str) -> DocumentError:
    """An OLE2 container named .docx is either a legacy .doc or a password-protected .docx."""
    if _file_contains(path, _ENCRYPTED_PACKAGE_MARKER):
        return EncryptedDocumentError(
            "This Word file is password-protected. Please remove the password and upload it again as .docx or PDF."
        )
    return UnsupportedDocumentError(LEGACY_DOC_MESSAGE)


# ── step 4: the bounded parse (runs on a gate worker thread) ─────────────────

_parser_lock = threading.Lock()
_pdf_parser = None
_docx_parser = None


def _parsers():
    global _pdf_parser, _docx_parser
    if _pdf_parser is None or _docx_parser is None:
        with _parser_lock:
            if _pdf_parser is None:
                from app.parsers.pdf_parser import PDFParser
                _pdf_parser = PDFParser()
            if _docx_parser is None:
                from app.parsers.docx_parser import DOCXParser
                _docx_parser = DOCXParser()
    return _pdf_parser, _docx_parser


def _parse_job(path: str, kind: str, head: bytes, keep_bytes: bool, require_text: bool,
               timeout_seconds: float) -> Tuple[str, Optional[bytes]]:
    """
    Worker-thread job. It OWNS the temp file: it is deleted here, when the job really ends, because a
    job that outlives its timeout is still running and must not have its input pulled out from under it.
    """
    try:
        deadline = time.monotonic() + timeout_seconds
        pdf_parser, docx_parser = _parsers()
        try:
            if kind == "pdf":
                text = pdf_parser.extract_text_strict(
                    path, max_pages=settings.MAX_PDF_PAGES, max_chars=settings.MAX_EXTRACTED_TEXT_CHARS,
                    deadline=deadline,
                )
            else:
                if head.startswith(OLE2_MAGIC):
                    raise _ole2_error(path)
                text = docx_parser.extract_text_strict(
                    path, max_uncompressed_bytes=settings.MAX_DOCX_UNCOMPRESSED_SIZE,
                    max_chars=settings.MAX_EXTRACTED_TEXT_CHARS, deadline=deadline,
                )
        except NoExtractableTextError:
            if require_text:
                raise
            text = ""
        raw = None
        if keep_bytes:
            with open(path, "rb") as handle:        # bounded: the file already passed MAX_FILE_SIZE
                raw = handle.read()
        return text, raw
    finally:
        _remove(path)


# ── public entry point ───────────────────────────────────────────────────────

async def ingest_resume(
    file: UploadFile,
    *,
    min_text_chars: int = 50,
    require_text: bool = True,
    keep_bytes: bool = False,
    queue_timeout=DEFAULT_QUEUE_TIMEOUT,
) -> ParsedResume:
    """
    Validate and parse one uploaded resume. Raises UploadRejected / AnalysisBusy / AnalysisTimeout
    (all HTTPExceptions) with a user-safe message on any failure.

    min_text_chars -- fewer extracted characters than this is "not enough text" (each endpoint keeps its
                      own historical threshold).
    require_text   -- False accepts a document with no extractable text (e.g. a scanned PDF attached to a
                      job application, where the file itself is what the recruiter reads).
    keep_bytes     -- also return the validated file's bytes (bounded by MAX_FILE_SIZE).
    queue_timeout  -- seconds to wait for a free analysis slot; None waits indefinitely (batch items).
    """
    label = safe_label(file.filename)
    kind = classify_upload(file.filename, file.content_type)
    stored = await _stream_to_tempfile(file, kind, settings.MAX_FILE_SIZE)
    try:
        quick_signature_check(stored.head, kind)
    except DocumentError as exc:
        _remove(stored.path)
        raise _reject(exc, label) from None
    except BaseException:
        _remove(stored.path)
        raise

    queue_timeout = resolve_queue_timeout(queue_timeout)
    try:
        text, raw = await get_gate().run(
            _parse_job, stored.path, kind, stored.head, keep_bytes, require_text,
            float(settings.ANALYSIS_TIMEOUT_SECONDS),
            timeout=float(settings.ANALYSIS_TIMEOUT_SECONDS),
            queue_timeout=queue_timeout,
            on_not_started=lambda: _remove(stored.path),
        )
    except DocumentError as exc:
        raise _reject(exc, label) from None
    except MemoryError:
        logger.error("MemoryError while parsing upload '%s'", label)
        raise UploadRejected(status_code=507, detail="Insufficient memory to process this file. Try a smaller file.") from None

    if require_text and len(text.strip()) < min_text_chars:
        raise UploadRejected(status_code=400, detail=NOT_ENOUGH_TEXT_MESSAGE)
    return ParsedResume(text=text, kind=kind, size=stored.size, raw_bytes=raw)
