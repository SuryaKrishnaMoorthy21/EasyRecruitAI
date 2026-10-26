"""
EasyRecruit ATS 3.0 v12 — Job Notification Routes
- GET  /api/v1/notifications/feed            → job seeker sees all live notifications
- GET  /api/v1/notifications/stats           → stat counts for dashboard widgets
- POST /api/v1/notifications/apply           → candidate submits resume to a notification job
- GET  /api/v1/notifications/my-applications → candidate sees their own submissions
- GET  /api/v1/notifications/applications    → recruiter sees applications (company-filtered)
- GET  /api/v1/notifications/applications/{id} → recruiter views one application detail
"""

import json
import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status

from app.api.auth_routes import get_current_user
from app.db.database import db
from app.uploads import ingest_resume

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/notifications", tags=["Job Notifications"])

# ── Load notification dataset ─────────────────────────────────────────────────
_DATASET_PATH = (
    Path(__file__).resolve().parent.parent.parent / "datasets" / "job_notifications.json"
)

def _load_notifications() -> list:
    try:
        with open(_DATASET_PATH, "r", encoding="utf-8") as f:
            return json.load(f).get("notifications", [])
    except Exception as exc:
        logger.error(f"Could not load job_notifications.json: {exc}")
        return []

_NOTIFICATIONS: list = _load_notifications()


# ── Ensure DB table exists ────────────────────────────────────────────────────
def _ensure_table():
    db.execute("""
        CREATE TABLE IF NOT EXISTS notification_applications (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            notification_id INTEGER NOT NULL,
            company         TEXT    NOT NULL,
            position_title  TEXT    NOT NULL,
            candidate_id    INTEGER NOT NULL,
            candidate_name  TEXT,
            candidate_email TEXT,
            resume_filename TEXT,
            resume_mime     TEXT,
            resume_bytes    BLOB,
            resume_text     TEXT,
            cover_note      TEXT,
            status          TEXT    DEFAULT 'submitted',
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (candidate_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)
    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_na_notif
        ON notification_applications(notification_id)
    """)
    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_na_candidate
        ON notification_applications(candidate_id)
    """)
    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_na_company
        ON notification_applications(company)
    """)

try:
    _ensure_table()
except Exception as _e:
    logger.warning(f"notification_applications table init warning: {_e}")
    for _col in ["ALTER TABLE notification_applications ADD COLUMN resume_bytes BLOB",
                 "ALTER TABLE notification_applications ADD COLUMN resume_mime TEXT"]:
        try: db.execute(_col)
        except Exception: pass


# ── Helpers ───────────────────────────────────────────────────────────────────
# What recruiters have always seen for a PDF with no extractable text (a scanned résumé is still a
# legitimate application: the attached file itself is what they read).
SCANNED_PDF_PLACEHOLDER = "[PDF contains no extractable text - may be a scanned image or protected PDF]"
_MIME_BY_KIND = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/feed")
async def notification_feed(current_user: dict = Depends(get_current_user)):
    """All active job notifications. Returns applied-position list per candidate."""
    applied_map: dict = {}
    uid = current_user["id"]
    role = current_user.get("role", "")
    if role in ("candidate", "job_seeker"):
        rows = db.fetch_all(
            "SELECT notification_id, position_title FROM notification_applications WHERE candidate_id=?",
            (uid,),
        )
        for r in rows:
            applied_map.setdefault(r["notification_id"], []).append(r["position_title"])

    result = []
    for n in _NOTIFICATIONS:
        item = dict(n)
        item["already_applied"] = applied_map.get(n["id"], [])
        result.append(item)

    return {"total": len(result), "notifications": result}


@router.get("/stats")
async def notification_stats(current_user: dict = Depends(get_current_user)):
    """Counts for dashboard widgets."""
    total_companies = len(_NOTIFICATIONS)
    total_positions = sum(len(n.get("positions", [])) for n in _NOTIFICATIONS)
    total_openings  = sum(
        p.get("openings", 0)
        for n in _NOTIFICATIONS
        for p in n.get("positions", [])
    )
    apps = db.fetch_one("SELECT COUNT(*) AS c FROM notification_applications", ())
    return {
        "total_companies": total_companies,
        "total_positions": total_positions,
        "total_openings":  total_openings,
        "total_applications": apps["c"] if apps else 0,
    }


@router.post("/apply")
async def apply_notification(
    notification_id: int        = Form(...),
    position_title:  str        = Form(...),
    company:         str        = Form(...),
    cover_note:      str        = Form(""),
    file:            UploadFile = File(...),
    current_user:    dict       = Depends(get_current_user),
):
    """Candidate submits resume for a notification-based opening."""
    role = current_user.get("role", "")
    if role not in ("candidate", "job_seeker"):
        raise HTTPException(status_code=403, detail="Only job seekers can apply here.")

    # Validate notification & position exist
    notif = next((n for n in _NOTIFICATIONS if n["id"] == notification_id), None)
    if not notif:
        raise HTTPException(status_code=404, detail="Job notification not found.")
    if not any(p["title"] == position_title for p in notif.get("positions", [])):
        raise HTTPException(status_code=400, detail="Position not found in this notification.")

    # Duplicate guard
    dup = db.fetch_one(
        "SELECT id FROM notification_applications "
        "WHERE candidate_id=? AND notification_id=? AND position_title=?",
        (current_user["id"], notification_id, position_title),
    )
    if dup:
        raise HTTPException(
            status_code=409,
            detail=f"You already applied for '{position_title}' at {company}.",
        )

    try:
        # Bounded streaming copy (<= MAX_FILE_SIZE, so the stored BLOB is bounded too), extension / MIME /
        # content validation and a real parse (see app/uploads.py). A scanned PDF with no text is accepted.
        parsed = await ingest_resume(file, require_text=False, keep_bytes=True)
        resume_text = parsed.text or (SCANNED_PDF_PLACEHOLDER if parsed.kind == "pdf" else "")
        raw_bytes, mime = parsed.raw_bytes, _MIME_BY_KIND[parsed.kind]
        app_id = db.insert(
            """INSERT INTO notification_applications
               (notification_id, company, position_title, candidate_id,
                candidate_name, candidate_email, resume_filename, resume_mime,
                resume_bytes, resume_text, cover_note)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                notification_id, company, position_title,
                current_user["id"],
                current_user.get("full_name") or current_user.get("username"),
                current_user.get("email"),
                file.filename, mime, raw_bytes,
                resume_text[:8000],
                cover_note[:1000],
            ),
        )
        logger.info(f"NotifApp #{app_id}: user {current_user['id']} -> {company} / {position_title}")
        return {
            "success": True,
            "application_id": app_id,
            "message": f"Application submitted for '{position_title}' at {company}.",
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Notification apply error: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail="Submission failed. Please try again.")


@router.get("/my-applications")
async def my_applications(current_user: dict = Depends(get_current_user)):
    """Candidate views their own submitted applications."""
    rows = db.fetch_all(
        """SELECT id, notification_id, company, position_title,
                  resume_filename, status, created_at
           FROM notification_applications
           WHERE candidate_id=?
           ORDER BY created_at DESC""",
        (current_user["id"],),
    )
    return {"total": len(rows), "applications": rows}


@router.get("/applications")
async def recruiter_applications(
    company: Optional[str] = None,
    current_user: dict = Depends(get_current_user),
):
    """
    Recruiter sees ALL applications submitted by job seekers via the Job Market.
    All recruiters see all applications (no strict company wall — they can filter by company).
    Candidate email is included so recruiter can reach out after shortlisting.
    Admin sees everything.
    """
    role = current_user.get("role", "")
    if role not in ("recruiter", "admin"):
        raise HTTPException(status_code=403, detail="Recruiters only.")

    # Always fetch ALL applications — recruiter can filter by company in the UI
    rows = db.fetch_all(
        """SELECT id, notification_id, company, position_title,
                  candidate_name, candidate_email, resume_filename,
                  cover_note, resume_text, status, created_at
           FROM notification_applications
           ORDER BY created_at DESC
           LIMIT 500""",
        (),
    )

    # Optional company filter from query param
    if company:
        rows = [r for r in (rows or []) if company.lower() in (r.get("company") or "").lower()]

    # Truncate resume_text for list view (full text available via /applications/{id})
    result = []
    for r in (rows or []):
        r_copy = dict(r)
        rt = r_copy.get("resume_text") or ""
        r_copy["resume_snippet"] = rt[:400] + "…" if len(rt) > 400 else rt
        r_copy.pop("resume_text", None)  # remove full text from list view
        result.append(r_copy)

    rec_company = (current_user.get("company_name") or "").strip()
    return {
        "total": len(result),
        "applications": result,
        "recruiter_company": rec_company or None,
        "company_filter": company or None,
    }



@router.get("/resume/{app_id}")
async def get_resume_file(app_id: int, current_user: dict = Depends(get_current_user)):
    """Stream the original PDF/DOCX file to the recruiter's browser — properly formatted."""
    if current_user.get("role") not in ("recruiter", "admin"):
        raise HTTPException(status_code=403, detail="Recruiters only.")
    row = db.fetch_one(
        "SELECT resume_bytes, resume_mime, resume_filename FROM notification_applications WHERE id=?",
        (app_id,)
    )
    if not row:
        raise HTTPException(status_code=404, detail="Application not found.")
    raw = row.get("resume_bytes")
    if not raw:
        raise HTTPException(status_code=404,
            detail="File not stored — this application was submitted before file storage was added.")
    from fastapi.responses import Response
    return Response(
        content=bytes(raw),
        media_type=row.get("resume_mime") or "application/pdf",
        headers={"Content-Disposition": f'inline; filename="{row.get("resume_filename") or "resume.pdf"}"'},
    )

@router.get("/applications/{app_id}")
async def application_detail(app_id: int, current_user: dict = Depends(get_current_user)):
    """Full detail of one notification application (resume snippet included)."""
    if current_user.get("role") not in ("recruiter", "admin"):
        raise HTTPException(status_code=403, detail="Recruiters only.")
    row = db.fetch_one("SELECT * FROM notification_applications WHERE id=?", (app_id,))
    if not row:
        raise HTTPException(status_code=404, detail="Application not found.")
    return row
