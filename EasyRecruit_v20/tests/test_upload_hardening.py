"""
Task 5 — resume-upload and batch-analysis hardening.

Groups
  TestConfig / TestHelpers        limit parsing and the small pure helpers
  TestGate                        AnalysisGate: bounded concurrency, honest timeout accounting, queueing
  TestSizeLimits                  bounded reads, request-size middleware, batch file/size caps
  TestValidation                  extension / MIME / content / parseability, on EVERY resume endpoint
  TestBatch                       mixed-success batches, concurrency, per-step and per-batch timeouts
  TestPersistence                 save_to_db semantics, atomicity, failure reporting
  TestResourceSafety              lazy-singleton race, event loop stays responsive, temp files cleaned up
  TestRegression                  the real NLP pipeline and the existing request/response shapes

The heavy NLP is replaced by a tiny deterministic stub wherever the test is about plumbing; TestRegression
runs the real pipeline and is skipped (not failed) where spaCy / the models are not installed.
All documents are generated at test time (see upload_fixtures.py); the tree holds no .pdf / .docx files.
"""
import asyncio
import glob
import os
import sqlite3
import sys
import tempfile
import threading
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile

import upload_fixtures as fx
import app.api.analysis_routes as ar
import app.api.bias_routes as bias
import app.api.interview_routes as interview
import app.api.routes as pr
import app.concurrency as concurrency
import app.uploads as uploads
from app.concurrency import AnalysisBusy, AnalysisGate, AnalysisTimeout, get_gate
from app.config import ResourceLimitConfigError, read_int_setting, settings
from app.db.database import db
from app.request_limits import REQUEST_OVERHEAD_BYTES, form_limits_for, request_limit_for
from main import app

client = TestClient(app)

ANALYZE = "/api/v1/analysis/analyze"
BATCH = "/api/v1/analysis/analyze-batch"
PUBLIC_ANALYZE = "/api/v1/analyze"
PUBLIC_BATCH = "/api/v1/analyze/batch"
EXTRACT = "/api/v1/extract-text"
UPLOAD_PREFIX = "easyrecruit-upload-"

# Every endpoint that accepts a single resume file (notifications/apply is covered separately: it needs a login).
SINGLE_FILE_ENDPOINTS = [
    pytest.param(ANALYZE, id="analysis/analyze"),
    pytest.param(PUBLIC_ANALYZE, id="public/analyze"),
    pytest.param(EXTRACT, id="public/extract-text"),
    pytest.param("/api/v1/bias/anonymize", id="bias/anonymize"),
    pytest.param("/api/v1/bias/blind-score", id="bias/blind-score"),
    pytest.param("/api/v1/bias/compare", id="bias/compare"),
    pytest.param("/api/v1/fraud/detect", id="fraud/detect"),
    pytest.param("/api/v1/interview/generate", id="interview/generate"),
]


# ── helpers ──────────────────────────────────────────────────────────────────

def _mime_for(name):
    lower = name.lower()
    if lower.endswith(".pdf"):
        return fx.PDF_MIME
    if lower.endswith(".docx"):
        return fx.DOCX_MIME
    return "application/octet-stream"


def _file(name, data, ctype=None):
    return {"file": (name, data, ctype or _mime_for(name))}


def _batch(*items):
    """items: (name, data[, ctype]) -> the multipart `files` list for a batch endpoint."""
    return [("files", (n, d, (c[0] if c else _mime_for(n)))) for n, d, *c in items]


def _leftover_uploads():
    return set(glob.glob(os.path.join(tempfile.gettempdir(), UPLOAD_PREFIX + "*")))


def _fake_pipeline(hook=None, score=62.5):
    """A deterministic stand-in for _run_nlp_pipeline_sync (same return shape, no spaCy / transformer)."""
    def fake(text, job_description, skills_dict):
        if hook is not None:
            hook(text)
        return (
            {"contact_info": {"name": "Jane Smith", "email": "jane.smith@example.com"},
             "candidate_name": "Jane Smith", "skills": [], "keywords": [], "indian_context": {}},
            {"overall_score": score, "keyword_match_score": 50.0, "skill_match_score": 40.0,
             "structure_score": 70.0, "semantic_similarity": None, "breakdown": {}, "detected_domain": "cse"},
        )
    return fake


class _Probe:
    """Counts how many jobs run at the same moment (thread-safe)."""
    def __init__(self):
        self._lock = threading.Lock()
        self.current = 0
        self.peak = 0
        self.calls = 0

    def __enter__(self):
        with self._lock:
            self.current += 1
            self.calls += 1
            self.peak = max(self.peak, self.current)

    def __exit__(self, *exc):
        with self._lock:
            self.current -= 1


@pytest.fixture
def stub_nlp(monkeypatch):
    monkeypatch.setattr(ar, "_run_nlp_pipeline_sync", _fake_pipeline())


def _make_user(role="candidate"):
    tag = uuid.uuid4().hex[:10]
    email = f"u{tag}@upload-tests.dev"
    client.post("/api/v1/auth/register",
                json={"email": email, "username": f"u{tag}", "password": "UploadTest1", "role": role})
    login = client.post("/api/v1/auth/login", json={"email": email, "password": "UploadTest1"})
    assert login.status_code == 200, login.text
    row = db.fetch_one("SELECT id FROM users WHERE email = ?", (email,))
    return {"Authorization": f"Bearer {login.json()['access_token']}"}, row["id"]


def _rows(user_id):
    analyses = db.fetch_one("SELECT COUNT(*) AS c FROM resume_analyses WHERE user_id = ?", (user_id,))["c"]
    history = db.fetch_one("SELECT COUNT(*) AS c FROM analysis_history WHERE user_id = ?", (user_id,))["c"]
    return analyses, history


def _all_rows():
    return (db.fetch_one("SELECT COUNT(*) AS c FROM resume_analyses")["c"],
            db.fetch_one("SELECT COUNT(*) AS c FROM analysis_history")["c"])


def _spy_upload_reads(monkeypatch):
    """Record (requested size, bytes returned) for every UploadFile.read call."""
    calls = []
    original = UploadFile.read

    async def spy(self, size=-1):
        data = await original(self, size)
        calls.append((size, len(data)))
        return data

    monkeypatch.setattr(UploadFile, "read", spy)
    return calls


def _count_upload_parts(monkeypatch, hide_size=False):
    created = []
    original = UploadFile.__init__

    def spy(self, *a, **k):
        original(self, *a, **k)
        created.append(1)
        if hide_size:
            self.size = None          # simulate a source whose length is not known up front

    monkeypatch.setattr(UploadFile, "__init__", spy)
    return created


def _async(coro):
    return asyncio.run(coro)


def _aclient():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


# ════════════════════════════════════════════════════════════════════════════
# Config and helpers
# ════════════════════════════════════════════════════════════════════════════

class TestConfig:
    def test_defaults_preserve_the_documented_contract(self):
        assert settings.MAX_FILE_SIZE == 10 * 1024 * 1024
        assert settings.MAX_BATCH_FILES == 15
        assert settings.MAX_BATCH_REQUEST_SIZE >= settings.MAX_FILE_SIZE
        assert settings.MAX_BATCH_REQUEST_SIZE == settings.MAX_FILE_SIZE * settings.MAX_BATCH_FILES + REQUEST_OVERHEAD_BYTES
        assert settings.ALLOWED_EXTENSIONS == {".pdf", ".docx"}          # legacy .doc is not supported

    @pytest.mark.parametrize("value", ["abc", "10MB", "1.5", "0", "-5", "99999999"])
    def test_invalid_limits_fail_closed_with_a_clear_message(self, value):
        with pytest.raises(ResourceLimitConfigError) as info:
            read_int_setting("MAX_BATCH_FILES", 15, minimum=1, maximum=100, environ={"MAX_BATCH_FILES": value})
        assert "MAX_BATCH_FILES" in str(info.value)

    def test_unset_blank_and_padded_values(self):
        assert read_int_setting("X", 7, minimum=1, maximum=9, environ={}) == 7
        assert read_int_setting("X", 7, minimum=1, maximum=9, environ={"X": "   "}) == 7
        assert read_int_setting("X", 7, minimum=1, maximum=9, environ={"X": " 3 "}) == 3


class TestHelpers:
    @pytest.mark.parametrize("name, ctype, expected", [
        ("cv.pdf", "application/pdf", "pdf"),
        ("CV.PDF", "application/octet-stream", "pdf"),                  # generic MIME is tolerated
        ("cv.pdf", "application/x-pdf; charset=binary", "pdf"),         # alias + parameters
        ("cv.pdf", None, "pdf"),
        ("cv.docx", fx.DOCX_MIME, "docx"),
        ("cv.docx", "application/x-zip-compressed", "docx"),            # what Windows browsers send
        ("C:\\Users\\me\\cv.docx", "", "docx"),                         # client sent a full path
    ])
    def test_classify_accepts(self, name, ctype, expected):
        assert uploads.classify_upload(name, ctype) == expected

    @pytest.mark.parametrize("name, ctype, status, fragment", [
        ("cv.doc", "application/msword", 415, "Legacy Word (.doc)"),
        ("cv.txt", "text/plain", 415, "Unsupported file type"),
        ("cv", "application/pdf", 415, "Unsupported file type"),
        ("cv.exe", "application/octet-stream", 415, "Unsupported file type"),
        ("cv.pdf", "image/png", 415, "does not match"),
        ("cv.docx", "application/pdf", 415, "does not match"),
        ("", "application/pdf", 400, "No filename"),
        (None, "application/pdf", 400, "No filename"),
    ])
    def test_classify_rejects(self, name, ctype, status, fragment):
        with pytest.raises(uploads.UploadRejected) as info:
            uploads.classify_upload(name, ctype)
        assert info.value.status_code == status and fragment in info.value.detail

    def test_safe_label_strips_paths_and_control_characters(self):
        assert uploads.safe_label("../../etc/passwd") == "passwd"
        assert uploads.safe_label("C:\\x\\y\\cv.pdf") == "cv.pdf"
        assert "\n" not in uploads.safe_label("a\nb\x00c.pdf")
        assert len(uploads.safe_label("x" * 500)) <= 83

    def test_format_size(self):
        assert uploads.format_size(10 * 1024 * 1024) == "10 MB"
        assert uploads.format_size(1536 * 1024) == "1.5 MB"
        assert uploads.format_size(2048) == "2 KB"
        assert uploads.format_size(300) == "300 bytes"

    def test_request_limits_per_path(self):
        assert request_limit_for("/api/v1/analysis/analyze") == settings.MAX_FILE_SIZE + REQUEST_OVERHEAD_BYTES
        assert request_limit_for("/api/v1/analysis/analyze-batch") == settings.MAX_BATCH_REQUEST_SIZE
        assert request_limit_for("/api/v1/analysis/analyze-batch/") == settings.MAX_BATCH_REQUEST_SIZE
        assert request_limit_for("/api/v1/analyze/batch") == settings.MAX_BATCH_REQUEST_SIZE
        assert request_limit_for("/api/v1/profile/photo") < request_limit_for("/api/v1/analysis/analyze")
        assert form_limits_for("/api/v1/analysis/analyze-batch") == (settings.MAX_BATCH_FILES, 20)
        assert form_limits_for("/api/v1/analyze/batch") == (10, 20)
        assert form_limits_for("/api/v1/analysis/analyze") is None


# ════════════════════════════════════════════════════════════════════════════
# AnalysisGate
# ════════════════════════════════════════════════════════════════════════════

class TestGate:
    def test_never_runs_more_than_max_concurrent_jobs(self):
        async def main():
            gate, probe = AnalysisGate(2), _Probe()

            def job():
                with probe:
                    time.sleep(0.15)
            try:
                await asyncio.gather(*(gate.run(job, timeout=10, queue_timeout=5) for _ in range(6)))
            finally:
                gate.shutdown()
            return probe
        probe = _async(main())
        assert probe.calls == 6
        assert probe.peak == 2

    def test_timeout_frees_the_caller_but_not_the_slot(self):
        async def main():
            gate, ev = AnalysisGate(1), {}

            def slow():
                time.sleep(0.5)
                ev["slow_end"] = time.monotonic()

            def quick():
                ev["quick_start"] = time.monotonic()
                return "ok"
            started = time.monotonic()
            with pytest.raises(AnalysisTimeout) as info:
                await gate.run(slow, timeout=0.1)
            released_after = time.monotonic() - started
            result = await gate.run(quick, timeout=10, queue_timeout=5)   # has to wait for the slow thread to really finish
            gate.shutdown()
            return info.value, released_after, result, ev
        exc, released_after, result, ev = _async(main())
        assert exc.status_code == 504
        assert released_after < 0.4                              # answered at the timeout, not after the 0.5 s job
        assert result == "ok"
        assert ev["quick_start"] >= ev["slow_end"]               # the slot stayed taken until the thread ended

    def test_a_timed_out_job_still_counts_as_running_for_admission(self):
        async def main():
            gate = AnalysisGate(1)
            with pytest.raises(AnalysisTimeout):
                await gate.run(lambda: time.sleep(0.6), timeout=0.1)
            # The caller is gone but the thread is not: a newcomer must see a full gate, not a free slot.
            with pytest.raises(AnalysisBusy):
                await gate.run(lambda: "x", timeout=10, queue_timeout=0.1)
            await asyncio.sleep(0.6)                                     # the slow thread finishes ...
            after = await gate.run(lambda: "ok", timeout=10, queue_timeout=2)   # ... and the slot comes back
            gate.shutdown()
            return after
        assert _async(main()) == "ok"

    def test_queue_timeout_raises_busy_and_runs_the_cleanup_callback(self):
        async def main():
            gate, started, cleaned = AnalysisGate(1), threading.Event(), []

            def hold():
                started.set()
                time.sleep(0.6)
            holder = asyncio.ensure_future(gate.run(hold, timeout=10))
            while not started.is_set():
                await asyncio.sleep(0.01)
            with pytest.raises(AnalysisBusy) as info:
                await gate.run(lambda: None, timeout=10, queue_timeout=0.1, on_not_started=lambda: cleaned.append(1))
            await holder
            gate.shutdown()
            return info.value, cleaned
        exc, cleaned = _async(main())
        assert exc.status_code == 503 and int(exc.headers["Retry-After"]) > 0
        assert cleaned == [1]

    def test_cancelled_while_queued_cleans_up_and_leaks_no_slot(self):
        async def main():
            gate, started, cleaned = AnalysisGate(1), threading.Event(), []

            def hold():
                started.set()
                time.sleep(0.4)
            holder = asyncio.ensure_future(gate.run(hold, timeout=10))
            while not started.is_set():
                await asyncio.sleep(0.01)
            waiter = asyncio.ensure_future(
                gate.run(lambda: "never", timeout=10, on_not_started=lambda: cleaned.append(1)))
            await asyncio.sleep(0.05)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            await holder
            after = await gate.run(lambda: "still works", timeout=10, queue_timeout=3)   # a leaked slot fails fast, never hangs
            gate.shutdown()
            return cleaned, after
        cleaned, after = _async(main())
        assert cleaned == [1]
        assert after == "still works"

    def test_job_exceptions_propagate_and_free_the_slot(self):
        async def main():
            gate = AnalysisGate(1)
            with pytest.raises(ZeroDivisionError):
                await gate.run(lambda: 1 / 0, timeout=10)
            after = await gate.run(lambda: "fine", timeout=10, queue_timeout=3)
            gate.shutdown()
            return after
        assert _async(main()) == "fine"

    def test_get_gate_follows_the_configured_concurrency(self, monkeypatch):
        monkeypatch.setattr(settings, "ANALYSIS_CONCURRENCY", 3)
        assert get_gate().max_concurrent == 3
        monkeypatch.setattr(settings, "ANALYSIS_CONCURRENCY", 1)
        assert get_gate().max_concurrent == 1
        assert get_gate() is get_gate()


# ════════════════════════════════════════════════════════════════════════════
# Size limits
# ════════════════════════════════════════════════════════════════════════════

class TestSizeLimits:
    def test_oversize_file_is_refused_without_reading_it_into_memory(self, monkeypatch, stub_nlp):
        monkeypatch.setattr(settings, "MAX_FILE_SIZE", 64 * 1024)
        reads = _spy_upload_reads(monkeypatch)
        parts = _count_upload_parts(monkeypatch, hide_size=True)        # force the chunked path (size unknown)
        data = b"%PDF-1.4\n" + os.urandom(1024) * 1024                  # ~1 MB, limit is 64 KB
        r = client.post(ANALYZE, files=_file("big.pdf", data))
        assert r.status_code == 413
        assert r.json()["detail"] == "File is too large. The maximum allowed size is 64 KB per file."
        assert parts and reads, "the upload was parsed and read"
        assert all(size > 0 for size, _ in reads), "an unbounded read() (size -1 / None) was issued"
        assert max(n for _, n in reads) <= uploads.CHUNK_SIZE
        assert sum(n for _, n in reads) <= 64 * 1024 + uploads.CHUNK_SIZE    # stopped right after the limit
        assert sum(n for _, n in reads) < len(data)

    def test_known_size_is_checked_before_a_single_byte_is_read(self, monkeypatch, stub_nlp):
        monkeypatch.setattr(settings, "MAX_FILE_SIZE", 64 * 1024)
        reads = _spy_upload_reads(monkeypatch)
        r = client.post(ANALYZE, files=_file("big.pdf", b"%PDF-1.4\n" + os.urandom(2048) * 100))
        assert r.status_code == 413
        assert reads == []

    def test_exact_limit_is_accepted_and_one_byte_over_is_not(self, monkeypatch, stub_nlp):
        data = fx.make_pdf()
        monkeypatch.setattr(settings, "MAX_FILE_SIZE", len(data))
        assert client.post(ANALYZE, files=_file("r.pdf", data)).status_code == 201
        monkeypatch.setattr(settings, "MAX_FILE_SIZE", len(data) - 1)
        assert client.post(ANALYZE, files=_file("r.pdf", data)).status_code == 413

    def test_request_over_the_limit_is_refused_before_anything_is_parsed(self, monkeypatch, stub_nlp):
        monkeypatch.setattr(settings, "MAX_FILE_SIZE", 1024)
        parts = _count_upload_parts(monkeypatch)
        body = b"%PDF-1.4\n" + os.urandom(1024) * 1500                  # ~1.5 MB > 1 KB + 1 MB overhead
        r = client.post(ANALYZE, files=_file("big.pdf", body))
        assert r.status_code == 413
        assert r.json()["detail"] == "Request is too large. The maximum allowed upload size is 1 KB per file."
        assert r.headers["connection"] == "close"
        assert parts == [], "the multipart body was parsed although the request was over the limit"

    def test_413_still_passes_through_the_outer_middleware(self, monkeypatch):
        monkeypatch.setattr(settings, "MAX_FILE_SIZE", 1024)
        r = client.post(ANALYZE, files=_file("big.pdf", b"%PDF-1.4\n" + os.urandom(1024) * 1500))
        assert r.status_code == 413
        assert r.headers.get("x-content-type-options") == "nosniff"      # security-header layer still decorates it

    def test_streaming_body_without_content_length_is_cut_off(self, monkeypatch, stub_nlp):
        monkeypatch.setattr(settings, "MAX_FILE_SIZE", 1024)
        parts = _count_upload_parts(monkeypatch)
        body = fx.multipart_body("XBOUNDARYX", [("file", "big.pdf", fx.PDF_MIME, b"%PDF-1.4\n" + os.urandom(1024) * 1500)])

        def chunks():                                                    # an iterator body => chunked transfer, no length
            for i in range(0, len(body), 64 * 1024):
                yield body[i:i + 64 * 1024]
        r = client.post(ANALYZE, content=chunks(), headers={"Content-Type": "multipart/form-data; boundary=XBOUNDARYX"})
        assert r.status_code == 413
        assert "Request is too large" in r.json()["detail"]

    def test_batch_total_size_cap(self, monkeypatch, stub_nlp):
        monkeypatch.setattr(settings, "MAX_BATCH_REQUEST_SIZE", 100 * 1024)
        pad = lambda: b"%PDF-1.4\n" + os.urandom(1024) * 60                # noqa: E731  (~60 KB each, 3 > 100 KB)
        r = client.post(BATCH, files=_batch(("a.pdf", pad()), ("b.pdf", pad()), ("c.pdf", pad())))
        assert r.status_code == 413
        assert "A batch may contain at most 15 files and 100 KB in total" in r.json()["detail"]

    def test_batch_file_count_is_enforced_while_parsing(self, monkeypatch, stub_nlp):
        parts = _count_upload_parts(monkeypatch)
        pdf = fx.make_pdf()
        r = client.post(BATCH, files=_batch(*[(f"r{i}.pdf", pdf) for i in range(25)]))
        assert r.status_code == 400
        assert r.json()["detail"] == "Too many files at once (max 15). Please split into smaller batches."
        assert len(parts) <= 15, f"the parser accepted {len(parts)} parts before the limit applied"

    def test_batch_accepts_exactly_the_maximum(self, stub_nlp):
        pdf = fx.make_pdf()
        r = client.post(BATCH, files=_batch(*[(f"r{i}.pdf", pdf) for i in range(15)]))
        assert r.status_code == 200
        assert r.json()["total"] == 15 and r.json()["succeeded"] == 15

    def test_batch_limit_follows_configuration(self, monkeypatch, stub_nlp):
        monkeypatch.setattr(settings, "MAX_BATCH_FILES", 3)
        pdf = fx.make_pdf()
        assert client.post(BATCH, files=_batch(*[(f"r{i}.pdf", pdf) for i in range(3)])).status_code == 200
        r = client.post(BATCH, files=_batch(*[(f"r{i}.pdf", pdf) for i in range(4)]))
        assert r.status_code == 400 and "max 3" in r.json()["detail"]

    def test_public_batch_keeps_its_limit_and_wording(self):
        pdf = fx.make_pdf()
        r = client.post(PUBLIC_BATCH, files=_batch(*[(f"r{i}.pdf", pdf) for i in range(11)]))
        assert r.status_code == 400
        assert r.json()["detail"] == "Maximum 10 files per request."

    @pytest.mark.parametrize("endpoint", SINGLE_FILE_ENDPOINTS)
    def test_every_resume_endpoint_refuses_oversize_without_an_unbounded_read(self, endpoint, monkeypatch):
        monkeypatch.setattr(settings, "MAX_FILE_SIZE", 64 * 1024)
        reads = _spy_upload_reads(monkeypatch)
        r = client.post(endpoint, files=_file("big.pdf", b"%PDF-1.4\n" + os.urandom(1024) * 300))
        assert r.status_code == 413, r.text
        assert "too large" in r.json()["detail"].lower()
        assert all(size > 0 for size, _ in reads)

    def test_notification_apply_is_bounded_and_stores_nothing_when_refused(self, monkeypatch):
        monkeypatch.setattr(settings, "MAX_FILE_SIZE", 64 * 1024)
        headers, user_id = _make_user()
        nid, position, company = _first_position(headers)
        before = _applications_for(user_id)
        r = client.post("/api/v1/notifications/apply", headers=headers,
                        data={"notification_id": str(nid), "position_title": position, "company": company},
                        files=_file("big.pdf", b"%PDF-1.4\n" + os.urandom(1024) * 300))
        assert r.status_code == 413
        assert _applications_for(user_id) == before


def _first_position(headers):
    feed = client.get("/api/v1/notifications/feed", headers=headers).json()
    notification = feed["notifications"][0]
    return notification["id"], notification["positions"][0]["title"], notification.get("company", "Test Co")


def _applications_for(user_id):
    return db.fetch_one("SELECT COUNT(*) AS c FROM notification_applications WHERE candidate_id = ?", (user_id,))["c"]


# ════════════════════════════════════════════════════════════════════════════
# Validation
# ════════════════════════════════════════════════════════════════════════════

# name, content-type, bytes, expected status, fragment of the message
BAD_UPLOADS = {
    "legacy .doc by extension": ("resume.doc", "application/msword", fx.make_ole2_blob(), 415, "Legacy Word (.doc)"),
    "legacy .doc content named .docx": ("resume.docx", fx.DOCX_MIME, fx.make_ole2_blob(), 415, "Legacy Word (.doc)"),
    "password-protected .docx": ("locked.docx", fx.DOCX_MIME, fx.make_ole2_blob(encrypted_package=True), 422, "password-protected"),
    "text file": ("notes.txt", "text/plain", b"hello world " * 30, 415, "Unsupported file type"),
    "no extension": ("resume", "application/octet-stream", fx.make_pdf(), 415, "Unsupported file type"),
    "html named .pdf": ("evil.pdf", "application/pdf", b"<html><script>alert(1)</script></html>" * 10, 415, "not a PDF"),
    "docx named .pdf": ("cv.pdf", "application/pdf", fx.make_docx(), 415, "not a PDF"),
    "pdf named .docx": ("cv.docx", fx.DOCX_MIME, fx.make_pdf(), 415, "is a PDF, not a DOCX"),
    "contradictory MIME": ("cv.pdf", "image/png", fx.make_pdf(), 415, "does not match"),
    "empty .pdf": ("empty.pdf", "application/pdf", b"", 400, "empty"),
    "empty .docx": ("empty.docx", fx.DOCX_MIME, b"", 400, "empty"),
    "password-protected PDF": ("locked.pdf", "application/pdf", fx.make_pdf(encrypted=True), 422, "password-protected"),
    "corrupt PDF": ("bad.pdf", "application/pdf", b"%PDF-1.4\n" + bytes(range(256)) * 4, 422, "not a valid PDF"),
    "truncated .docx": ("cut.docx", fx.DOCX_MIME, fx.make_docx()[:1500], 422, "not a valid DOCX"),
    "zip that is not a .docx": ("x.docx", fx.DOCX_MIME, fx.make_zip({"a.txt": b"hi"}), 422, "not a Word"),
}
BAD_IDS = list(BAD_UPLOADS)


class TestValidation:
    @pytest.mark.parametrize("case", BAD_IDS)
    @pytest.mark.parametrize("endpoint", SINGLE_FILE_ENDPOINTS)
    def test_every_resume_endpoint_rejects_bad_uploads_clearly(self, endpoint, case):
        name, ctype, data, status, fragment = BAD_UPLOADS[case]
        r = client.post(endpoint, files=_file(name, data, ctype))
        assert r.status_code == status, f"{case}: {r.status_code} {r.text}"
        detail = r.json()["detail"]
        assert fragment in detail, detail
        for leak in ("/tmp", "\\Temp", "Package not found", "Traceback", UPLOAD_PREFIX, "pdfminer", "docx.opc"):
            assert leak not in detail, f"internal detail leaked: {detail}"

    def test_scanned_pdf_is_refused_instead_of_scoring_a_placeholder(self, monkeypatch):
        fed = []
        monkeypatch.setattr(ar, "_run_nlp_pipeline_sync", _fake_pipeline(fed.append))
        r = client.post(ANALYZE, files=_file("scan.pdf", fx.make_blank_pdf()))
        assert r.status_code == 400
        assert "No readable text was found in this PDF" in r.json()["detail"]
        assert "scanned image" in r.json()["detail"]
        assert fed == [], f"the NLP pipeline was fed a scanned PDF: {fed!r}"

    def test_too_little_text_is_refused(self, stub_nlp):
        r = client.post(ANALYZE, files=_file("tiny.docx", fx.make_docx(["Just a few words."])))
        assert r.status_code == 400 and "Could not extract enough text" in r.json()["detail"]

    def test_pdf_page_limit(self, monkeypatch, stub_nlp):
        monkeypatch.setattr(settings, "MAX_PDF_PAGES", 5)
        ok = client.post(ANALYZE, files=_file("five.pdf", fx.make_pdf([fx.RESUME_LINES] * 5)))
        assert ok.status_code == 201
        r = client.post(ANALYZE, files=_file("six.pdf", fx.make_pdf([fx.RESUME_LINES] * 6)))
        assert r.status_code == 422
        assert "more than 5 pages" in r.json()["detail"]

    def test_decompression_bomb_is_refused_before_python_docx_loads_it(self, monkeypatch, stub_nlp):
        monkeypatch.setattr(settings, "MAX_DOCX_UNCOMPRESSED_SIZE", 2 * 1024 * 1024)
        import app.parsers.docx_parser as docx_parser

        def must_not_run(*_a, **_k):
            raise AssertionError("python-docx was asked to load a decompression bomb")
        monkeypatch.setattr(docx_parser, "Document", must_not_run)
        bomb = fx.make_docx_bomb(uncompressed_mb=24)
        assert len(bomb) < 200 * 1024                                     # tiny on the wire, huge when opened
        r = client.post(ANALYZE, files=_file("bomb.docx", bomb))
        assert r.status_code == 422
        assert "expands to more than 2 MB" in r.json()["detail"]

    def test_docx_with_an_absurd_number_of_parts_is_refused(self, stub_nlp):
        r = client.post(ANALYZE, files=_file("many.docx", fx.make_docx_many_entries(1500)))
        assert r.status_code == 422 and "unusual number of internal parts" in r.json()["detail"]

    def test_extracted_text_cap(self, monkeypatch, stub_nlp):
        monkeypatch.setattr(settings, "MAX_EXTRACTED_TEXT_CHARS", 200)
        r = client.post(ANALYZE, files=_file("long.docx", fx.make_docx()))
        assert r.status_code == 422 and "far more text than a resume should" in r.json()["detail"]

    def test_valid_files_still_pass_with_every_accepted_mime_label(self, stub_nlp):
        for name, ctype, data in [
            ("a.pdf", "application/pdf", fx.make_pdf()),
            ("b.pdf", "application/octet-stream", fx.make_pdf()),
            ("c.pdf", "application/x-pdf", fx.make_pdf()),
            ("d.docx", fx.DOCX_MIME, fx.make_docx()),
            ("e.docx", "application/octet-stream", fx.make_docx()),
            ("f.docx", "application/zip", fx.make_docx()),
        ]:
            r = client.post(ANALYZE, files=_file(name, data, ctype))
            assert r.status_code == 201, f"{name} / {ctype}: {r.text}"

    def test_notification_apply_validates_but_keeps_scanned_pdfs(self):
        headers, user_id = _make_user()
        nid, position, company = _first_position(headers)
        form = {"notification_id": str(nid), "position_title": position, "company": company}

        # refused: legacy .doc and a corrupt PDF -> nothing is stored
        for name, ctype, data, status in [
            ("resume.doc", "application/msword", fx.make_ole2_blob(), 415),
            ("bad.pdf", "application/pdf", b"%PDF-1.4\n" + bytes(range(256)) * 4, 422),
        ]:
            r = client.post("/api/v1/notifications/apply", headers=headers, data=form, files=_file(name, data, ctype))
            assert r.status_code == status, r.text
        assert _applications_for(user_id) == 0

        # accepted: a scanned (text-less) PDF is a legitimate application; the bytes are stored as uploaded
        scanned = fx.make_blank_pdf()
        r = client.post("/api/v1/notifications/apply", headers=headers, data=form, files=_file("scan.pdf", scanned))
        assert r.status_code == 200, r.text
        row = db.fetch_one("SELECT resume_bytes, resume_mime, resume_text FROM notification_applications WHERE id = ?",
                           (r.json()["application_id"],))
        assert bytes(row["resume_bytes"]) == scanned
        assert row["resume_mime"] == fx.PDF_MIME
        assert row["resume_text"].startswith("[PDF contains no extractable text")

    def test_notification_apply_stores_a_valid_resume_exactly_as_uploaded(self):
        headers, user_id = _make_user()
        nid, position, company = _first_position(headers)
        pdf = fx.make_pdf()
        r = client.post(
            "/api/v1/notifications/apply", headers=headers,
            data={"notification_id": str(nid), "position_title": position, "company": company, "cover_note": "Hello"},
            files=_file("cv.pdf", pdf))
        assert r.status_code == 200, r.text
        row = db.fetch_one("SELECT resume_bytes, resume_text FROM notification_applications WHERE id = ?",
                           (r.json()["application_id"],))
        assert bytes(row["resume_bytes"]) == pdf
        assert "Jane Smith" in row["resume_text"]


# ════════════════════════════════════════════════════════════════════════════
# Batch behaviour
# ════════════════════════════════════════════════════════════════════════════

class TestBatch:
    def test_mixed_batch_keeps_every_per_file_result(self, monkeypatch, stub_nlp):
        monkeypatch.setattr(settings, "MAX_FILE_SIZE", 100 * 1024)
        r = client.post(BATCH, files=_batch(
            ("good.docx", fx.make_docx()),
            ("good.pdf", fx.make_pdf()),
            ("corrupt.pdf", b"%PDF-1.4\n" + bytes(range(256)) * 4),
            ("huge.pdf", b"%PDF-1.4\n" + os.urandom(1024) * 150),
            ("notes.txt", b"plain text " * 20, "text/plain"),
            ("empty.docx", b""),
            ("locked.pdf", fx.make_pdf(encrypted=True)),
            ("scan.pdf", fx.make_blank_pdf()),
        ))
        assert r.status_code == 200, r.text
        body = r.json()
        assert (body["total"], body["succeeded"], body["failed"]) == (8, 2, 6)
        results = body["results"]
        assert [x["batch_index"] for x in results] == list(range(8))
        assert [x["filename"] for x in results] == ["good.docx", "good.pdf", "corrupt.pdf", "huge.pdf",
                                                    "notes.txt", "empty.docx", "locked.pdf", "scan.pdf"]
        good = results[:2]
        assert all(x["error"] is None and x["overall_score"] == 62.5 and x["status"] == "completed" for x in good)
        assert all(x["analysis_id"] == 0 and x["saved"] is False for x in good)
        expected = ["not a valid PDF", "too large", "Unsupported file type", "empty", "password-protected",
                    "No readable text was found in this PDF"]
        for entry, fragment in zip(results[2:], expected):
            assert entry["status"] == "failed" and fragment in entry["error"], entry

    def test_one_unexpected_crash_does_not_take_down_the_batch(self, monkeypatch):
        def flaky(text, jd, skills):
            if "CRASHME" in text:
                raise RuntimeError("secret internal detail /srv/app/venv/lib")
            return _fake_pipeline()(text, jd, skills)
        monkeypatch.setattr(ar, "_run_nlp_pipeline_sync", flaky)
        r = client.post(BATCH, files=_batch(
            ("a.docx", fx.make_docx()),
            ("b.docx", fx.make_docx(fx.RESUME_LINES + ["CRASHME"])),
            ("c.docx", fx.make_docx()),
        ))
        body = r.json()
        assert r.status_code == 200 and (body["succeeded"], body["failed"]) == (2, 1)
        crashed = body["results"][1]
        assert crashed["error"] == "Analysis failed unexpectedly. Please try again."
        assert "secret" not in crashed["error"]

    def test_batch_concurrency_is_bounded_across_both_stages(self, monkeypatch):
        monkeypatch.setattr(settings, "ANALYSIS_CONCURRENCY", 2)
        probe = _Probe()

        def hook(_text):
            with probe:
                time.sleep(0.1)
        monkeypatch.setattr(ar, "_run_nlp_pipeline_sync", _fake_pipeline(hook))
        original_parse = uploads._parse_job

        def counted_parse(*a, **k):
            with probe:
                return original_parse(*a, **k)
        monkeypatch.setattr(uploads, "_parse_job", counted_parse)

        r = client.post(BATCH, files=_batch(*[(f"r{i}.docx", fx.make_docx()) for i in range(8)]))
        assert r.status_code == 200 and r.json()["succeeded"] == 8
        assert probe.calls == 16                          # 8 parses + 8 scorings, all through the gate
        assert 1 <= probe.peak <= 2

    def test_simultaneous_batch_requests_share_one_bound(self, monkeypatch):
        monkeypatch.setattr(settings, "ANALYSIS_CONCURRENCY", 2)
        probe = _Probe()

        def hook(_text):
            with probe:
                time.sleep(0.08)
        monkeypatch.setattr(ar, "_run_nlp_pipeline_sync", _fake_pipeline(hook))
        statuses = []

        def one_batch():
            r = client.post(BATCH, files=_batch(*[(f"r{i}.pdf", fx.make_pdf()) for i in range(5)]))
            statuses.append((r.status_code, r.json()["succeeded"]))
        threads = [threading.Thread(target=one_batch) for _ in range(3)]
        [t.start() for t in threads]
        [t.join(timeout=60) for t in threads]
        assert statuses == [(200, 5)] * 3
        assert probe.calls == 15
        assert probe.peak <= 2, f"{probe.peak} analyses ran at once across simultaneous batches"

    def test_a_hung_file_times_out_alone_and_keeps_its_slot(self, monkeypatch):
        monkeypatch.setattr(settings, "ANALYSIS_CONCURRENCY", 1)
        monkeypatch.setattr(settings, "ANALYSIS_TIMEOUT_SECONDS", 0.4)
        probe, stamps = _Probe(), {}

        def hook(text):
            with probe:
                if "SLOWRESUME" in text:
                    stamps["slow_start"] = time.monotonic()
                    time.sleep(1.2)
                    stamps["slow_end"] = time.monotonic()
                else:
                    stamps.setdefault("next_start", time.monotonic())
        monkeypatch.setattr(ar, "_run_nlp_pipeline_sync", _fake_pipeline(hook))
        r = client.post(BATCH, files=_batch(
            ("slow.docx", fx.make_docx(fx.RESUME_LINES + ["SLOWRESUME"])),
            ("b.docx", fx.make_docx()),
            ("c.docx", fx.make_docx()),
        ))
        body = r.json()
        assert r.status_code == 200 and (body["succeeded"], body["failed"]) == (2, 1)
        assert "took too long" in body["results"][0]["error"]
        assert body["results"][1]["error"] is None and body["results"][2]["error"] is None
        assert probe.peak == 1, "a timed-out job freed its slot while its thread was still running"
        assert stamps["next_start"] >= stamps["slow_end"] - 0.05

    def test_batch_deadline_reports_unfinished_files_and_never_stores_them(self, monkeypatch):
        monkeypatch.setattr(settings, "ANALYSIS_CONCURRENCY", 1)
        monkeypatch.setattr(settings, "BATCH_TIMEOUT_SECONDS", 0.8)
        monkeypatch.setattr(ar, "_run_nlp_pipeline_sync", _fake_pipeline(lambda _t: time.sleep(0.4)))
        headers, user_id = _make_user()
        r = client.post(BATCH, headers=headers, data={"save_to_db": "true"},
                        files=_batch(*[(f"r{i}.docx", fx.make_docx()) for i in range(6)]))
        body = r.json()
        assert r.status_code == 200 and body["total"] == 6
        assert body["succeeded"] >= 1 and body["failed"] >= 1
        assert [x["batch_index"] for x in body["results"]] == list(range(6))
        for entry in body["results"]:
            if entry["error"]:
                assert entry["error"] == ar.BATCH_TIME_LIMIT_MESSAGE
        assert _rows(user_id) == (body["succeeded"], body["succeeded"]), "stored rows must equal reported successes"
        time.sleep(0.6)                                    # let the still-running worker thread finish before the next test

    def test_busy_server_answers_503_with_retry_after_for_a_single_file(self, monkeypatch):
        monkeypatch.setattr(settings, "ANALYSIS_CONCURRENCY", 1)
        monkeypatch.setattr(settings, "ANALYSIS_QUEUE_TIMEOUT_SECONDS", 0.3)
        monkeypatch.setattr(ar, "_run_nlp_pipeline_sync", _fake_pipeline(lambda _t: time.sleep(1.0)))

        async def main():
            async with _aclient() as c:
                async def post():
                    return await c.post(ANALYZE, files=_file("r.docx", fx.make_docx()))
                first = asyncio.ensure_future(post())
                await asyncio.sleep(0.4)                   # the first request is now inside the scoring step
                second = await post()
                return await first, second
        first, second = _async(main())
        assert first.status_code == 201
        assert second.status_code == 503
        assert int(second.headers["retry-after"]) > 0
        assert "busy" in second.json()["detail"].lower()


# ════════════════════════════════════════════════════════════════════════════
# Persistence
# ════════════════════════════════════════════════════════════════════════════

class TestPersistence:
    def test_save_to_db_true_stores_exactly_one_analysis_and_one_history_row(self, stub_nlp):
        headers, user_id = _make_user()
        r = client.post(ANALYZE, headers=headers, data={"save_to_db": "true"}, files=_file("a.docx", fx.make_docx()))
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["saved"] is True and body["save_error"] is None
        assert body["analysis_id"] > 0 and body["created_at"]
        assert _rows(user_id) == (1, 1)
        row = db.fetch_one("SELECT user_id, filename, overall_score FROM resume_analyses WHERE id = ?", (body["analysis_id"],))
        assert row["user_id"] == user_id and row["filename"] == "a.docx" and row["overall_score"] == 62.5
        history = db.fetch_one("SELECT analysis_id FROM analysis_history WHERE user_id = ?", (user_id,))
        assert history["analysis_id"] == body["analysis_id"]

    @pytest.mark.parametrize("data", [{}, {"save_to_db": "false"}], ids=["flag omitted", "flag false"])
    def test_nothing_is_stored_unless_save_to_db_is_true(self, data, stub_nlp):
        headers, user_id = _make_user()
        r = client.post(ANALYZE, headers=headers, data=data, files=_file("a.docx", fx.make_docx()))
        assert r.status_code == 201
        body = r.json()
        assert body["analysis_id"] == 0 and body["saved"] is False and body["save_error"] is None
        assert _rows(user_id) == (0, 0), "candidate data was stored although the user did not ask to save it"

    def test_anonymous_save_request_is_reported_not_silently_dropped(self, stub_nlp):
        before = _all_rows()
        r = client.post(ANALYZE, data={"save_to_db": "true"}, files=_file("a.docx", fx.make_docx()))
        body = r.json()
        assert r.status_code == 201
        assert body["saved"] is False and body["analysis_id"] == 0
        assert "Sign in" in body["save_error"]
        assert _all_rows() == before

    def test_unknown_job_id_keeps_the_result_and_drops_only_the_dangling_reference(self, stub_nlp):
        headers, user_id = _make_user()
        r = client.post(ANALYZE, headers=headers, data={"save_to_db": "true", "job_id": "99999999"},
                        files=_file("a.docx", fx.make_docx()))
        body = r.json()
        assert r.status_code == 201 and body["saved"] is True and body["analysis_id"] > 0
        assert db.fetch_one("SELECT job_id FROM resume_analyses WHERE id = ?", (body["analysis_id"],))["job_id"] is None

    def test_a_database_failure_is_reported_and_the_computed_result_is_not_thrown_away(self, monkeypatch, stub_nlp):
        def boom(*_a, **_k):
            raise sqlite3.OperationalError("database is locked")
        monkeypatch.setattr(ar, "_persist_analysis_sync", boom)
        headers, user_id = _make_user()
        r = client.post(ANALYZE, headers=headers, data={"save_to_db": "true"}, files=_file("a.docx", fx.make_docx()))
        body = r.json()
        assert r.status_code == 201
        assert body["overall_score"] == 62.5                      # the analysis itself is still delivered
        assert body["saved"] is False and body["analysis_id"] == 0
        assert "could not be saved" in body["save_error"] and "locked" not in body["save_error"]

    def test_the_two_rows_are_written_atomically(self, monkeypatch, stub_nlp):
        monkeypatch.setattr(ar, "_HISTORY_INSERT_SQL", "INSERT INTO a_table_that_does_not_exist VALUES (1)")
        headers, user_id = _make_user()
        r = client.post(ANALYZE, headers=headers, data={"save_to_db": "true"}, files=_file("a.docx", fx.make_docx()))
        assert r.status_code == 201 and r.json()["saved"] is False
        assert _rows(user_id) == (0, 0), "the analysis row survived although its history row failed"

    def test_batch_stores_only_the_files_that_succeeded(self, stub_nlp):
        headers, user_id = _make_user()
        r = client.post(BATCH, headers=headers, data={"save_to_db": "true"}, files=_batch(
            ("a.docx", fx.make_docx()), ("corrupt.pdf", b"%PDF-1.4\n" + bytes(range(256)) * 4), ("c.pdf", fx.make_pdf())))
        body = r.json()
        assert (body["succeeded"], body["failed"]) == (2, 1)
        ids = [x["analysis_id"] for x in body["results"] if not x["error"]]
        assert len(set(ids)) == 2 and all(i > 0 for i in ids)
        assert [x.get("saved") for x in body["results"]] == [True, None, True]
        assert _rows(user_id) == (2, 2)
        stored = {row["filename"] for row in db.fetch_all("SELECT filename FROM resume_analyses WHERE user_id = ?", (user_id,))}
        assert stored == {"a.docx", "c.pdf"}

    def test_batch_stores_nothing_by_default(self, stub_nlp):
        headers, user_id = _make_user()
        r = client.post(BATCH, headers=headers, files=_batch(("a.docx", fx.make_docx()), ("c.pdf", fx.make_pdf())))
        assert r.json()["succeeded"] == 2
        assert _rows(user_id) == (0, 0)

    def test_batch_reports_a_failed_save_per_file_without_failing_the_file(self, monkeypatch, stub_nlp):
        calls = []

        def flaky(user_id, job_id, analysis):
            calls.append(analysis.filename)
            if analysis.filename == "b.pdf":
                raise sqlite3.OperationalError("disk I/O error")
            return 1000 + len(calls), "2026-01-01 00:00:00"
        monkeypatch.setattr(ar, "_persist_analysis_sync", flaky)
        headers, _ = _make_user()
        body = client.post(BATCH, headers=headers, data={"save_to_db": "true"}, files=_batch(
            ("a.pdf", fx.make_pdf()), ("b.pdf", fx.make_pdf()), ("c.pdf", fx.make_pdf()))).json()
        assert body["succeeded"] == 3 and body["failed"] == 0
        assert [x["saved"] for x in body["results"]] == [True, False, True]
        assert body["results"][1]["save_error"] and body["results"][1]["overall_score"] == 62.5

    def test_public_endpoints_never_write_to_the_database(self, monkeypatch):
        class FastScorer:
            def calculate_ats_score(self, *a, **k):
                return {"overall_score": 50, "keyword_match_score": 50, "skill_match_score": 50,
                        "structure_score": 50, "breakdown": {}}

        class FastExtractor:
            def full_analysis(self, _t):
                return {"contact_info": {}, "keywords": [], "skills": [], "indian_context": {}}
        monkeypatch.setattr(pr, "_get_scorer", lambda: FastScorer())
        monkeypatch.setattr(pr, "_get_extractor", lambda: FastExtractor())
        before = _all_rows()
        assert client.post(PUBLIC_ANALYZE, files=_file("a.docx", fx.make_docx())).status_code == 200
        assert client.post(PUBLIC_BATCH, files=_batch(("a.pdf", fx.make_pdf()), ("b.pdf", fx.make_pdf()))).json()["successful"] == 2
        assert client.post(EXTRACT, files=_file("a.pdf", fx.make_pdf())).status_code == 200
        assert _all_rows() == before


# ════════════════════════════════════════════════════════════════════════════
# Resource safety
# ════════════════════════════════════════════════════════════════════════════

def _counting_class(delay=0.15):
    class Counted:
        built = 0
        _lock = threading.Lock()

        def __init__(self, *a, **k):
            with Counted._lock:
                Counted.built += 1
            time.sleep(delay)              # a slow constructor, like loading spaCy or a transformer
    return Counted


def _hammer(getters, threads=12):
    barrier = threading.Barrier(threads)

    def go():
        barrier.wait()
        for getter in getters:
            getter()
    pool = [threading.Thread(target=go) for _ in range(threads)]
    [t.start() for t in pool]
    [t.join(timeout=30) for t in pool]


class TestResourceSafety:
    def test_simultaneous_first_requests_build_the_models_once(self, monkeypatch):
        import app.nlp.enhanced_scorer as scorer_module
        import app.nlp.keyword_extractor as extractor_module
        Extractor, Scorer = _counting_class(), _counting_class()
        monkeypatch.setattr(extractor_module, "KeywordExtractor", Extractor)
        monkeypatch.setattr(scorer_module, "EnhancedScorer", Scorer)
        monkeypatch.setattr(ar, "_keyword_extractor", None)
        monkeypatch.setattr(ar, "_enhanced_scorer", None)
        _hammer([ar._get_extractor, ar._get_scorer])
        assert (Extractor.built, Scorer.built) == (1, 1)

    @pytest.mark.parametrize("module, extractor_attr, scorer_attr", [
        (pr, "_extractor", "_scorer"),
        (bias, "_extractor", "_scorer"),
    ], ids=["public routes", "bias routes"])
    def test_the_other_route_modules_are_thread_safe_too(self, monkeypatch, module, extractor_attr, scorer_attr):
        import app.nlp.enhanced_scorer as scorer_module
        import app.nlp.keyword_extractor as extractor_module
        import app.nlp.resume_scorer as resume_scorer_module
        Extractor, Scorer = _counting_class(), _counting_class()
        monkeypatch.setattr(extractor_module, "KeywordExtractor", Extractor)
        monkeypatch.setattr(scorer_module, "EnhancedScorer", Scorer)
        monkeypatch.setattr(resume_scorer_module, "ResumeScorer", Scorer)
        monkeypatch.setattr(module, extractor_attr, None)
        monkeypatch.setattr(module, scorer_attr, None)
        _hammer([module._get_extractor, module._get_scorer])
        assert (Extractor.built, Scorer.built) == (1, 1)

    def test_interview_extractor_is_thread_safe_too(self, monkeypatch):
        import app.nlp.keyword_extractor as extractor_module
        Extractor = _counting_class()
        monkeypatch.setattr(extractor_module, "KeywordExtractor", Extractor)
        monkeypatch.setattr(interview, "_extractor", None)
        _hammer([interview._get_extractor])
        assert Extractor.built == 1

    def test_public_endpoint_no_longer_freezes_the_event_loop(self, monkeypatch):
        class SlowScorer:
            def calculate_ats_score(self, *a, **k):
                time.sleep(0.8)
                return {"overall_score": 50, "keyword_match_score": 50, "skill_match_score": 50,
                        "structure_score": 50, "breakdown": {}}

        class FastExtractor:
            def full_analysis(self, _t):
                return {"contact_info": {}, "keywords": [], "skills": [], "indian_context": {}}
        monkeypatch.setattr(pr, "_get_scorer", lambda: SlowScorer())
        monkeypatch.setattr(pr, "_get_extractor", lambda: FastExtractor())

        async def main():
            lags = []

            async def heartbeat():
                while True:
                    started = time.perf_counter()
                    await asyncio.sleep(0.02)
                    lags.append(time.perf_counter() - started - 0.02)
            async with _aclient() as c:
                beat = asyncio.ensure_future(heartbeat())
                await asyncio.sleep(0.1)
                r = await c.post(PUBLIC_ANALYZE, files=_file("a.docx", fx.make_docx()))
                beat.cancel()
            return r.status_code, max(lags)
        status, worst_stall = _async(main())
        assert status == 200
        assert worst_stall < 0.3, f"the event loop stalled for {worst_stall:.2f}s during one scoring run"

    def test_temp_files_are_removed_on_every_path(self, monkeypatch, stub_nlp):
        monkeypatch.setattr(settings, "MAX_FILE_SIZE", 100 * 1024)
        before = _leftover_uploads()
        client.post(ANALYZE, files=_file("ok.docx", fx.make_docx()))
        client.post(ANALYZE, files=_file("ok.pdf", fx.make_pdf()))
        for name, ctype, data, *_ in BAD_UPLOADS.values():
            client.post(ANALYZE, files=_file(name, data, ctype))
        client.post(ANALYZE, files=_file("huge.pdf", b"%PDF-1.4\n" + os.urandom(1024) * 150))
        client.post(BATCH, files=_batch(("a.docx", fx.make_docx()), ("b.pdf", fx.make_blank_pdf())))
        assert _leftover_uploads() - before == set()

    def test_temp_file_of_a_timed_out_job_is_removed_when_its_thread_finishes(self, monkeypatch):
        monkeypatch.setattr(settings, "ANALYSIS_TIMEOUT_SECONDS", 0.2)
        original = uploads._parse_job

        def slow_parse(*a, **k):
            time.sleep(0.6)
            return original(*a, **k)
        monkeypatch.setattr(uploads, "_parse_job", slow_parse)
        before = _leftover_uploads()
        r = client.post(ANALYZE, files=_file("a.docx", fx.make_docx()))
        assert r.status_code == 504
        deadline = time.monotonic() + 5
        while _leftover_uploads() - before and time.monotonic() < deadline:
            time.sleep(0.05)
        assert _leftover_uploads() - before == set()

    def test_client_cancel_while_queued_removes_the_temp_file(self, monkeypatch):
        monkeypatch.setattr(settings, "ANALYSIS_CONCURRENCY", 1)
        monkeypatch.setattr(ar, "_run_nlp_pipeline_sync", _fake_pipeline(lambda _t: time.sleep(0.8)))
        before = _leftover_uploads()

        async def main():
            async with _aclient() as c:
                hold = asyncio.ensure_future(c.post(ANALYZE, files=_file("a.docx", fx.make_docx())))
                await asyncio.sleep(0.3)                                    # now inside the scoring step, slot taken
                queued = asyncio.ensure_future(c.post(ANALYZE, files=_file("b.docx", fx.make_docx())))
                await asyncio.sleep(0.2)                                    # waiting for a slot, temp file on disk
                queued.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await queued
                return await hold
        assert _async(main()).status_code == 201
        deadline = time.monotonic() + 5
        while _leftover_uploads() - before and time.monotonic() < deadline:
            time.sleep(0.05)
        assert _leftover_uploads() - before == set()


# ════════════════════════════════════════════════════════════════════════════
# Regression: the real pipeline and the existing request/response shapes
# ════════════════════════════════════════════════════════════════════════════

def _real_nlp_available():
    try:
        import spacy
        import sentence_transformers  # noqa: F401
        return spacy.util.is_package("en_core_web_sm")
    except Exception:
        return False


needs_nlp = pytest.mark.skipif(
    not _real_nlp_available(),
    reason="environment-blocked: spaCy + en_core_web_sm + sentence-transformers are required for the real NLP pipeline",
)

SINGLE_RESPONSE_KEYS = {
    "analysis_id", "filename", "extracted_text_length", "candidate_name", "candidate_email", "overall_score",
    "keyword_match_score", "skill_match_score", "structure_score", "semantic_similarity", "experience_score",
    "education_score", "accuracy_estimate", "nlp_analysis", "breakdown", "status", "created_at",
}


class TestRegression:
    """Plumbing that needs no NLP: the same documents must still flow through the lightweight endpoints."""

    def test_fraud_and_anonymize_still_accept_valid_documents(self):
        for endpoint, key in (("/api/v1/fraud/detect", None), ("/api/v1/bias/anonymize", "anonymized_text")):
            for name, data in (("cv.pdf", fx.make_pdf()), ("cv.docx", fx.make_docx())):
                r = client.post(endpoint, files=_file(name, data))
                assert r.status_code == 200, f"{endpoint} {name}: {r.text}"
                assert r.json()["filename"] == name
                if key:
                    assert r.json()[key]

    def test_extract_text_returns_exactly_what_the_legacy_parsers_returned(self, tmp_path):
        from app.parsers.docx_parser import DOCXParser
        from app.parsers.pdf_parser import PDFParser
        for name, data, parser in (("cv.pdf", fx.make_pdf(), PDFParser()), ("cv.docx", fx.make_docx(), DOCXParser())):
            path = tmp_path / name
            path.write_bytes(data)
            legacy = parser.extract_text(str(path))
            r = client.post(EXTRACT, files=_file(name, data))
            assert r.status_code == 200
            assert r.json() == {"filename": name, "extracted_text": legacy, "text_length": len(legacy)}

    def test_single_and_batch_response_shapes_are_unchanged(self, stub_nlp):
        single = client.post(ANALYZE, files=_file("a.docx", fx.make_docx()))
        assert single.status_code == 201
        assert SINGLE_RESPONSE_KEYS <= set(single.json())
        batch = client.post(BATCH, files=_batch(("a.docx", fx.make_docx()), ("b.pdf", fx.make_pdf()))).json()
        assert set(batch) == {"total", "succeeded", "failed", "results"}
        for entry in batch["results"]:
            assert {"batch_index", "filename", "error", "overall_score", "analysis_id", "status"} <= set(entry)

    def test_public_batch_shape_is_unchanged(self, monkeypatch):
        class FastScorer:
            def calculate_ats_score(self, *a, **k):
                return {"overall_score": 50, "breakdown": {}}
        monkeypatch.setattr(pr, "_get_scorer", lambda: FastScorer())
        body = client.post(PUBLIC_BATCH, files=_batch(("a.pdf", fx.make_pdf()), ("x.txt", b"nope", "text/plain"))).json()
        assert set(body) == {"total", "successful", "failed", "results", "errors"}
        assert (body["total"], body["successful"], body["failed"]) == (2, 1, 1)
        assert body["results"][0]["filename"] == "a.pdf" and body["results"][0]["overall_score"] == 50
        assert body["errors"][0]["filename"] == "x.txt" and "Unsupported" in body["errors"][0]["error"]

    @needs_nlp
    @pytest.mark.parametrize("name, build", [("cv.docx", fx.make_docx), ("cv.pdf", fx.make_pdf)])
    def test_real_pipeline_single_file_scores_exactly_as_before(self, name, build, tmp_path):
        data = build()
        path = tmp_path / name
        path.write_bytes(data)
        from app.parsers.docx_parser import DOCXParser
        from app.parsers.pdf_parser import PDFParser
        legacy_text = (PDFParser() if name.endswith(".pdf") else DOCXParser()).extract_text(str(path))
        _nlp, expected = ar._run_nlp_pipeline_sync(legacy_text, None, None)

        r = client.post(ANALYZE, files=_file(name, data))
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["extracted_text_length"] == len(legacy_text)
        for key in ("overall_score", "keyword_match_score", "skill_match_score", "structure_score"):
            assert body[key] == pytest.approx(expected[key]), key
        assert 0 <= body["overall_score"] <= 100 and body["status"] == "completed"

    @needs_nlp
    def test_real_pipeline_batch_matches_single_file_results(self):
        docx, pdf = fx.make_docx(), fx.make_pdf()
        single = {n: client.post(ANALYZE, files=_file(n, d)).json()["overall_score"]
                  for n, d in (("a.docx", docx), ("b.pdf", pdf))}
        batch = client.post(BATCH, files=_batch(("a.docx", docx), ("b.pdf", pdf))).json()
        assert (batch["total"], batch["succeeded"], batch["failed"]) == (2, 2, 0)
        for entry in batch["results"]:
            assert entry["error"] is None
            assert entry["overall_score"] == pytest.approx(single[entry["filename"]])

    @needs_nlp
    def test_real_pipeline_public_endpoint(self, tmp_path):
        data = fx.make_docx()
        path = tmp_path / "cv.docx"
        path.write_bytes(data)
        from app.parsers.docx_parser import DOCXParser
        legacy_text = DOCXParser().extract_text(str(path))
        expected = pr._get_scorer().calculate_ats_score(legacy_text, None, None)
        r = client.post(PUBLIC_ANALYZE, files=_file("cv.docx", data))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["analysis_id"] is None and body["file_type"] == "docx" and body["status"] == "completed"
        assert body["extracted_text_length"] == len(legacy_text)
        assert body["overall_score"] == pytest.approx(expected["overall_score"])
        batch = client.post(PUBLIC_BATCH, files=_batch(("cv.docx", data), ("cv.pdf", fx.make_pdf()))).json()
        assert (batch["total"], batch["successful"], batch["failed"]) == (2, 2, 0)
