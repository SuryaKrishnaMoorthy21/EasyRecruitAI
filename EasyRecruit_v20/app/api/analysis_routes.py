"""
EasyRecruit ATS 3.0 — Analysis Routes
Improvements:
  • File-size validation before reading into memory
  • Candidate contact info saved to DB
  • Richer stats (highest/lowest score, top skills)
  • Proper delete endpoint for analyses
  • Consistent Depends() for auth
  • Task 5: bounded streaming intake + content validation (app/uploads.py), bounded concurrency and
    timeouts (app/concurrency.py), save_to_db honoured, atomic persistence
"""
import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import List, Optional, Tuple

from fastapi import (
    APIRouter, Depends, File, Form, Header, HTTPException, Query,
    UploadFile, status,
)
from fastapi.concurrency import run_in_threadpool

from app.api.auth_routes import get_current_user, get_optional_user
from app.concurrency import get_gate, singleton_lock
from app.config import settings
from app.db.database import db
from app.json_safety import sanitize_non_finite
from app.models.schemas import (
    APIResponse, CustomDatasetCreate, CustomDatasetResponse,
    PaginatedResponse, ResumeAnalysisFullResponse,
)
from app.request_limits import BoundedFormRoute, too_many_files_message
from app.uploads import DEFAULT_QUEUE_TIMEOUT, ingest_resume, resolve_queue_timeout, safe_label

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/analysis", tags=["Resume Analysis"], route_class=BoundedFormRoute)


# ────────────────────────────────────────────────────────────────────────────
# Lazy-loaded NLP singletons
# ────────────────────────────────────────────────────────────────────────────

_pdf_parser       = None
_docx_parser      = None
_keyword_extractor = None
_enhanced_scorer  = None


# The getters below can be reached from several worker threads at once (every file of a batch). Without
# the lock, N simultaneous first requests each built their own copy of spaCy / the transformer: N times
# the memory and N times the load time. Double-checked, so the lock costs nothing once built.

def _get_pdf_parser():
    global _pdf_parser
    if _pdf_parser is None:
        with singleton_lock:
            if _pdf_parser is None:
                from app.parsers.pdf_parser import PDFParser
                _pdf_parser = PDFParser()
    return _pdf_parser


def _get_docx_parser():
    global _docx_parser
    if _docx_parser is None:
        with singleton_lock:
            if _docx_parser is None:
                from app.parsers.docx_parser import DOCXParser
                _docx_parser = DOCXParser()
    return _docx_parser


def _get_extractor():
    global _keyword_extractor
    if _keyword_extractor is None:
        with singleton_lock:
            if _keyword_extractor is None:
                from app.nlp.keyword_extractor import KeywordExtractor
                _keyword_extractor = KeywordExtractor()
    return _keyword_extractor


def _get_scorer():
    global _enhanced_scorer
    if _enhanced_scorer is None:
        with singleton_lock:
            if _enhanced_scorer is None:
                from app.nlp.enhanced_scorer import EnhancedScorer
                _enhanced_scorer = EnhancedScorer()
    return _enhanced_scorer


def _extract_text_sync(temp_path: str, use_pdf: bool) -> str:
    """Blocking file parsing — always call via run_in_threadpool."""
    if use_pdf:
        return _get_pdf_parser().extract_text(temp_path)
    return _get_docx_parser().extract_text(temp_path)


def _run_nlp_pipeline_sync(text: str, job_description: Optional[str], skills_dict: Optional[dict]):
    """
    Runs extraction + scoring. This is where the (first-call-only) sentence-
    transformer model load can happen, which may block for a while on a slow
    or unreachable network. Always call via run_in_threadpool so a single
    slow request can't freeze the server for every other user.
    """
    extractor = _get_extractor()
    scorer    = _get_scorer()

    # v9 extractor uses modular methods; fall back gracefully if full_analysis absent
    try:
        nlp_analysis = extractor.full_analysis(text)
    except (AttributeError, Exception) as _attr_exc:
        # full_analysis not present — build equivalent structure from modular methods
        logger.warning(f"full_analysis unavailable ({type(_attr_exc).__name__}), using modular fallback")
        try:
            _skills_info   = extractor.extract_skills(text)
            _keywords_info = extractor.extract_keywords(text, top_k=20)
            _contact_info  = extractor.extract_contact_info(text)
            # Build a flat list of skill names for frontend consumption
            _flat_skills = []
            for _cat_skills in _skills_info.get("categorized_skills", {}).values():
                if isinstance(_cat_skills, list):
                    for _s in _cat_skills:
                        if isinstance(_s, dict):
                            _flat_skills.append(_s.get("skill", ""))
                        elif isinstance(_s, str):
                            _flat_skills.append(_s)
            _flat_skills = [s for s in _flat_skills if s]

            nlp_analysis = {
                "contact_info":   _contact_info,
                "skills":         _flat_skills,          # flat list — safe for frontend spread
                "skills_detail":  _skills_info,          # full dict for advanced use
                "keywords":       [k["keyword"] for k in _keywords_info if isinstance(k, dict)],
                "candidate_name": _contact_info.get("name"),
                "indian_context": _skills_info.get("indian_context", {}),
            }
        except Exception as _nlp_exc:
            logger.error(f"NLP modular fallback failed: {_nlp_exc}")
            nlp_analysis = {
                "contact_info": {}, "candidate_name": None,
                "skills": [], "keywords": [], "indian_context": {},
            }

    score_result = scorer.calculate_ats_score(text, job_description, skills_dict)
    return nlp_analysis, score_result


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────

def _cleanup(path: str):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception as exc:
        logger.debug(f"Temp-file cleanup: {exc}")


def _get_analysis(analysis_id: int) -> Optional[dict]:
    return db.fetch_one(
        "SELECT * FROM resume_analyses WHERE id = ?", (analysis_id,)
    )


def _paginate(user_id: int, page: int, per_page: int) -> dict:
    total_row = db.fetch_one(
        "SELECT COUNT(*) as total FROM resume_analyses WHERE user_id = ?", (user_id,)
    )
    total = total_row["total"] if total_row else 0
    offset = (page - 1) * per_page
    rows = db.fetch_all(
        """SELECT id, filename, overall_score, keyword_score, skill_score,
                  structure_score, semantic_score, candidate_name, candidate_email,
                  detected_domain, status, created_at, job_id
           FROM resume_analyses
           WHERE user_id = ?
           ORDER BY created_at DESC
           LIMIT ? OFFSET ?""",
        (user_id, per_page, offset),
    )
    total_pages = max(1, (total + per_page - 1) // per_page)
    return {
        "items":       rows,
        "total":       total,
        "page":        page,
        "page_size":   per_page,
        "total_pages": total_pages,
        "has_next":    page < total_pages,
        "has_prev":    page > 1,
    }


# ────────────────────────────────────────────────────────────────────────────
# SPECIFIC routes BEFORE dynamic
# ────────────────────────────────────────────────────────────────────────────

@router.get("/history", response_model=List[dict])
async def get_history(
    limit: int = Query(20, ge=1, le=100),
    current_user: Optional[dict] = Depends(get_optional_user),
):
    if not current_user:
        return []
    return db.fetch_all(
        """SELECT * FROM analysis_history
           WHERE user_id = ?
           ORDER BY created_at DESC
           LIMIT ?""",
        (current_user["id"], limit),
    )


@router.get("/stats/summary")
async def get_stats(current_user: Optional[dict] = Depends(get_optional_user)):
    empty = {
        "total_analyses": 0, "average_score": 0,
        "highest_score": 0, "lowest_score": 0,
        "score_distribution": {"0-20": 0, "20-40": 0, "40-60": 0, "60-80": 0, "80-100": 0},
        "recent_activity_7_days": 0,
        "total_jobs": 0, "total_datasets": 0, "top_skills_found": [],
    }
    if not current_user:
        return empty

    uid = current_user["id"]

    agg = db.fetch_one(
        """SELECT COUNT(*) as cnt,
                  AVG(overall_score)  as avg_score,
                  MAX(overall_score)  as max_score,
                  MIN(overall_score)  as min_score
           FROM resume_analyses WHERE user_id = ?""",
        (uid,),
    )

    total     = agg["cnt"]       if agg else 0
    avg_score = round(agg["avg_score"] or 0, 2) if agg else 0
    max_score = round(agg["max_score"] or 0, 2) if agg else 0
    min_score = round(agg["min_score"] or 0, 2) if agg else 0

    dist = {}
    for label, lo, hi in [
        ("0-20",   0,  20),
        ("20-40",  20, 40),
        ("40-60",  40, 60),
        ("60-80",  60, 80),
        ("80-100", 80, 101),
    ]:
        row = db.fetch_one(
            """SELECT COUNT(*) as cnt FROM resume_analyses
               WHERE user_id = ? AND overall_score >= ? AND overall_score < ?""",
            (uid, lo, hi),
        )
        dist[label] = row["cnt"] if row else 0

    recent = db.fetch_one(
        """SELECT COUNT(*) as cnt FROM analysis_history
           WHERE user_id = ? AND created_at > datetime('now','-7 days')""",
        (uid,),
    )

    total_jobs = db.fetch_one(
        "SELECT COUNT(*) as cnt FROM job_descriptions WHERE user_id = ? AND is_active = 1",
        (uid,),
    )
    total_ds = db.fetch_one(
        "SELECT COUNT(*) as cnt FROM custom_datasets WHERE user_id = ? AND is_active = 1",
        (uid,),
    )

    return {
        "total_analyses":         total,
        "average_score":          avg_score,
        "highest_score":          max_score,
        "lowest_score":           min_score,
        "score_distribution":     dist,
        "recent_activity_7_days": recent["cnt"] if recent else 0,
        "total_jobs":             total_jobs["cnt"] if total_jobs else 0,
        "total_datasets":         total_ds["cnt"]   if total_ds   else 0,
        "top_skills_found":       [],  # populated from NLP if needed
    }


@router.get("/datasets", response_model=List[dict])
async def list_datasets(current_user: Optional[dict] = Depends(get_optional_user)):
    if not current_user:
        return []
    return db.fetch_all(
        "SELECT * FROM custom_datasets WHERE user_id = ? AND is_active = 1",
        (current_user["id"],),
    )


@router.post("/datasets", response_model=CustomDatasetResponse, status_code=status.HTTP_201_CREATED)
async def create_dataset(
    dataset: CustomDatasetCreate,
    current_user: dict = Depends(get_current_user),
):
    ds_id = db.insert(
        """INSERT INTO custom_datasets (user_id, name, description, category, skills, keywords)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            current_user["id"],
            dataset.name,
            dataset.description,
            dataset.category.value,
            json.dumps(dataset.skills)    if dataset.skills    else None,
            json.dumps(dataset.keywords)  if dataset.keywords  else None,
        ),
    )
    logger.info(f"Dataset created: '{dataset.name}' by user {current_user['id']}")
    return db.fetch_one("SELECT * FROM custom_datasets WHERE id = ?", (ds_id,))


# ────────────────────────────────────────────────────────────────────────────
# List / delete individual analyses
# ────────────────────────────────────────────────────────────────────────────

@router.get("/", response_model=PaginatedResponse)
async def list_analyses(
    page:     int = Query(1, ge=1),
    per_page: int = Query(10, ge=1, le=100),
    current_user: Optional[dict] = Depends(get_optional_user),
):
    if not current_user:
        return {"items": [], "total": 0, "page": 1, "page_size": per_page,
                "total_pages": 0, "has_next": False, "has_prev": False}
    return _paginate(current_user["id"], page, per_page)


@router.get("/{analysis_id}", response_model=dict)
async def get_analysis(
    analysis_id: int,
    current_user: dict = Depends(get_current_user),
):
    row = _get_analysis(analysis_id)
    if not row:
        raise HTTPException(status_code=404, detail="Analysis not found.")
    if row.get("user_id") and row["user_id"] != current_user["id"]:
        raise HTTPException(status_code=403, detail="Not authorised.")
    return row


@router.delete("/{analysis_id}", response_model=APIResponse)
async def delete_analysis(
    analysis_id: int,
    current_user: dict = Depends(get_current_user),
):
    row = _get_analysis(analysis_id)
    if not row:
        raise HTTPException(status_code=404, detail="Analysis not found.")
    if row.get("user_id") != current_user["id"]:
        raise HTTPException(status_code=403, detail="Not authorised.")
    db.execute("DELETE FROM resume_analyses WHERE id = ?", (analysis_id,))
    return {"success": True, "message": "Analysis deleted.", "data": None}


# ────────────────────────────────────────────────────────────────────────────
# CORE: Analyse resume
# ────────────────────────────────────────────────────────────────────────────

@dataclass
class _Analysis:
    """Everything produced by analysing one file, before any persistence decision is made."""
    filename:        Optional[str]
    text:            str
    nlp_analysis:    dict
    score_result:    dict
    candidate_name:  Optional[str]
    candidate_email: Optional[str]
    candidate_phone: Optional[str]


async def _analyze_core(
    file: UploadFile,
    job_description: Optional[str],
    skills_dict: Optional[dict],
    job_id: Optional[int],
    current_user: Optional[dict],
    queue_timeout=DEFAULT_QUEUE_TIMEOUT,
) -> _Analysis:
    """
    validate -> bounded intake and parse -> (optional saved job description) -> NLP + scoring.
    Never writes to the database; persistence is decided separately by _save_analysis, so a file that is
    reported as failed can never have been stored.

    Both CPU-heavy steps (parsing, NLP + transformer) run on the AnalysisGate: at most
    ANALYSIS_CONCURRENCY at a time per process, each limited to ANALYSIS_TIMEOUT_SECONDS.
    Raises HTTPException with a user-safe message on any failure.
    """
    try:
        # Bounded streaming copy, extension / MIME / content validation, and the parse itself.
        parsed = await ingest_resume(file, min_text_chars=50, queue_timeout=queue_timeout)
        text = parsed.text

        # ── Optionally pull job description from DB ──
        if job_id and not job_description and current_user:
            row = db.fetch_one(
                "SELECT description FROM job_descriptions WHERE id = ? AND is_active = 1",
                (job_id,),
            )
            if row:
                job_description = row["description"]

        # ── NLP analysis + scoring ──
        # The sentence-transformer model is loaded here on its very first use, which can be slow on a
        # fresh install or a slow network; the gate keeps that off the event loop and bounded.
        nlp_analysis, score_result = await get_gate().run(
            _run_nlp_pipeline_sync, text, job_description, skills_dict,
            timeout=float(settings.ANALYSIS_TIMEOUT_SECONDS),
            queue_timeout=resolve_queue_timeout(queue_timeout),
        )

        # ── Extract candidate contact info ──
        contact = nlp_analysis.get("contact_info", {})
        _emails = contact.get("emails") or []
        _phones = contact.get("phones") or []
        candidate_email = contact.get("email") or (_emails[0] if _emails else None)
        candidate_name  = contact.get("name")  or nlp_analysis.get("candidate_name")
        candidate_phone = contact.get("phone") or (_phones[0] if _phones else None)

        return _Analysis(
            filename=file.filename, text=text, nlp_analysis=nlp_analysis, score_result=score_result,
            candidate_name=candidate_name, candidate_email=candidate_email, candidate_phone=candidate_phone,
        )

    except HTTPException:
        raise
    except MemoryError:
        logger.error("MemoryError during analysis")
        raise HTTPException(status_code=507, detail="Insufficient memory to process file. Try a smaller file.")
    except Exception as exc:
        # Details go to the log only: an internal exception message must never reach the client.
        logger.error(f"Unexpected analysis error: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail="Analysis failed unexpectedly. Please try again.")


# ── Persistence ──────────────────────────────────────────────────────────────

_ANALYSIS_INSERT_SQL = """INSERT INTO resume_analyses
                       (user_id, job_id, filename, candidate_name, candidate_email,
                        candidate_phone, extracted_text, overall_score, keyword_score,
                        skill_score, structure_score, semantic_score, experience_score,
                        education_score, matched_keywords, matched_skills,
                        missing_skills, recommendations, analysis_data, detected_domain, status)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""

_HISTORY_INSERT_SQL = """INSERT INTO analysis_history
                       (user_id, analysis_id, analysis_type, description, result_summary)
                       VALUES (?, ?, ?, ?, ?)"""


def _persist_analysis_sync(user_id: int, job_id: Optional[int], a: _Analysis) -> Tuple[int, Optional[str]]:
    """
    Write the analysis AND its history row in ONE transaction: both rows or neither (the old code used two
    separate transactions, so a failure in the second left an analysis with no history entry).
    Returns (analysis_id, created_at). Raises on any database error; the caller reports it.
    """
    score_result = a.score_result
    with db.connection() as conn:
        # A job_id that no longer exists would violate the foreign key and silently lose the whole result.
        # Keep the analysis and drop only the dangling reference.
        if job_id is not None and conn.execute(
            "SELECT 1 FROM job_descriptions WHERE id = ?", (job_id,)
        ).fetchone() is None:
            job_id = None

        analysis_id = conn.execute(
            _ANALYSIS_INSERT_SQL,
            (
                user_id,
                job_id,
                a.filename,
                a.candidate_name,
                a.candidate_email,
                a.candidate_phone,
                a.text[:6000],
                score_result["overall_score"],
                score_result["keyword_match_score"],
                score_result["skill_match_score"],
                score_result["structure_score"],
                score_result.get("semantic_similarity"),
                score_result.get("experience_score", 0),
                score_result.get("education_score", 0),
                json.dumps(
                    (score_result.get("breakdown", {}).get("keywords", {})
                     or score_result.get("breakdown", {}).get("keyword_analysis", {}))
                    .get("job_keywords_match", [])
                ),
                json.dumps(
                    score_result.get("breakdown", {})
                    .get("skill_analysis", {})
                    .get("matched_skills", [])
                ),
                json.dumps(
                    score_result.get("breakdown", {})
                    .get("skill_analysis", {})
                    .get("missing_skills", [])
                ),
                json.dumps(
                    score_result.get("breakdown", {})
                    .get("recommendations", [])
                ),
                json.dumps(sanitize_non_finite(score_result)),   # NaN/Infinity are not valid JSON (Task 7)
                score_result.get("detected_domain", "general"),
                "completed",
            ),
        ).lastrowid

        conn.execute(
            _HISTORY_INSERT_SQL,
            (
                user_id,
                analysis_id,
                "resume_analysis",
                f"Analysed: {a.filename}",
                f"Score: {score_result['overall_score']}%",
            ),
        )
        row = conn.execute("SELECT created_at FROM resume_analyses WHERE id = ?", (analysis_id,)).fetchone()
    return analysis_id, (row["created_at"] if row else None)


async def _save_analysis(
    a: _Analysis, job_id: Optional[int], current_user: Optional[dict], save_to_db: bool
) -> dict:
    """
    Persistence policy, in one place (the old code ignored `save_to_db` and stored every analysis of a
    signed-in user, even when they had unticked "Save result to history"):

      * save_to_db false (the default)  -> nothing is written; analysis_id stays 0.
      * save_to_db true, signed in      -> written atomically; analysis_id / created_at come from the row.
      * save_to_db true, anonymous      -> nothing can be written; saved=false with an explanation.
      * save_to_db true, write fails    -> the computed analysis is still returned, never discarded, with
                                           saved=false and save_error set, instead of looking like a success.
    """
    outcome = {"analysis_id": 0, "created_at": None, "saved": False, "save_error": None}
    if not save_to_db:
        return outcome
    if not current_user:
        outcome["save_error"] = "Sign in to save results to your history. This result was not saved."
        return outcome
    try:
        analysis_id, created_at = await run_in_threadpool(_persist_analysis_sync, current_user["id"], job_id, a)
        outcome.update(analysis_id=analysis_id, created_at=created_at, saved=True)
    except Exception as exc:
        logger.error(f"Error persisting analysis for '{safe_label(a.filename)}': {exc}", exc_info=True)
        outcome["save_error"] = "The analysis completed but could not be saved to your history. Please try again."
    return outcome


def _build_response(
    a: _Analysis, *, analysis_id: int, created_at, saved: bool, save_error: Optional[str]
) -> dict:
    s = a.score_result
    return {
        "analysis_id":          analysis_id or 0,
        "filename":             a.filename,
        "extracted_text_length": len(a.text),
        "candidate_name":       a.candidate_name,
        "candidate_email":      a.candidate_email,
        "overall_score":        s["overall_score"],
        "keyword_match_score":  s["keyword_match_score"],
        "skill_match_score":    s["skill_match_score"],
        "structure_score":      s["structure_score"],
        "semantic_similarity":  s.get("semantic_similarity"),
        "experience_score":     s.get("experience_score", 0),
        "education_score":      s.get("education_score", 0),
        "detected_domain":      s.get("detected_domain", "general"),
        "domain_confidence":    s.get("domain_confidence", 0.0),
        "india_context_bonus":  s.get("india_context_bonus", 0.0),
        "indian_context":       s.get("indian_context", {}),
        # `accuracy_estimate` is a deprecated alias of `extraction_confidence`: a parse-completeness heuristic, not a
        # measured accuracy. There is no invented fallback (it used to default to a hard-coded 88).
        "accuracy_estimate":    s.get("accuracy_estimate", s.get("extraction_confidence")),
        "extraction_confidence": s.get("extraction_confidence", s.get("accuracy_estimate")),
        "scoring_explanation":  s.get("scoring_explanation"),
        "nlp_analysis":         a.nlp_analysis,
        "breakdown":            s.get("breakdown", {}),
        "status":               "completed",
        "created_at":           created_at,
        # Additive: whether the result was stored, and why not if it was requested but could not be.
        "saved":                saved,
        "save_error":           save_error,
    }


async def _analyze_single_file(
    file: UploadFile,
    job_description: Optional[str],
    skills_dict: Optional[dict],
    job_id: Optional[int],
    current_user: Optional[dict],
    save_to_db: bool = False,
    queue_timeout=DEFAULT_QUEUE_TIMEOUT,
) -> dict:
    """
    Full analyze pipeline for ONE file: validate, extract, score, persist only if requested, and build
    the response dict. Raises HTTPException on failure.

    Used by /analyze. /analyze-batch shares _analyze_core and _build_response, so a resume is scored
    exactly the same way and with exactly the same validation no matter which endpoint processed it.
    """
    analysis = await _analyze_core(file, job_description, skills_dict, job_id, current_user, queue_timeout)
    outcome = await _save_analysis(analysis, job_id, current_user, save_to_db)
    return _build_response(analysis, **outcome)


@router.post("/analyze", response_model=ResumeAnalysisFullResponse, status_code=status.HTTP_201_CREATED)
async def analyze_resume(
    file:             UploadFile = File(...),
    job_description:  Optional[str] = Form(None),
    required_skills:  Optional[str] = Form(None),
    job_id:           Optional[int] = Form(None),
    save_to_db:       bool = Form(False),
    current_user: Optional[dict] = Depends(get_optional_user),
):
    """
    Analyse a resume file (PDF or DOCX).
    Optionally provide a job description and/or select a saved job by ID.
    The result is stored in the signed-in user's history only when save_to_db is true.
    """
    skills_dict = None
    if required_skills:
        try:
            skills_dict = json.loads(required_skills)
        except json.JSONDecodeError:
            pass

    return await _analyze_single_file(
        file, job_description, skills_dict, job_id, current_user, save_to_db=save_to_db
    )


BATCH_TIME_LIMIT_MESSAGE = (
    "Not processed: the batch time limit was reached. Please upload fewer files at once."
)


def _failed_entry(index: int, f: UploadFile, error: str) -> dict:
    return {"batch_index": index, "filename": f.filename, "status": "failed", "error": error}


@router.post("/analyze-batch")
async def analyze_resumes_batch(
    files:            List[UploadFile] = File(...),
    job_description:  Optional[str] = Form(None),
    required_skills:  Optional[str] = Form(None),
    job_id:           Optional[int] = Form(None),
    save_to_db:       bool = Form(False),
    current_user: Optional[dict] = Depends(get_optional_user),
):
    """
    Analyse several resumes in one request — for comparing candidates
    against the same role side by side.

    Each file gets its own completely independent extraction, NLP pass,
    and score (all run through the exact same pipeline as /analyze, via
    the shared helpers above), so results are never mixed between resumes.
    One bad file (wrong type, corrupt, oversized, scanned with no extractable
    text) only fails that file's entry — it never takes down the rest of the batch.

    Resource safety: files are queued on the process-wide analysis gate, so
    only ANALYSIS_CONCURRENCY of them (default 2) run at a time however many
    arrive; each step is limited to ANALYSIS_TIMEOUT_SECONDS, and the batch as
    a whole to BATCH_TIMEOUT_SECONDS — files not finished by then are reported
    as failed while the finished ones are kept. Results are saved only when
    save_to_db is true, and only for files that analysed successfully.
    """
    if not files:
        raise HTTPException(status_code=400, detail="No files provided.")

    max_files = settings.MAX_BATCH_FILES
    if len(files) > max_files:
        raise HTTPException(status_code=400, detail=too_many_files_message(max_files))

    skills_dict = None
    if required_skills:
        try:
            skills_dict = json.loads(required_skills)
        except json.JSONDecodeError:
            pass

    async def _one(index: int, f: UploadFile):
        """An _Analysis on success, or the failed-entry dict. Never raises (cancellation aside)."""
        try:
            # queue_timeout=None: batch items wait their turn; the batch deadline bounds the whole wait.
            return await _analyze_core(f, job_description, skills_dict, job_id, current_user, queue_timeout=None)
        except HTTPException as he:
            logger.warning(f"Batch item {index} ('{safe_label(f.filename)}') failed: {he.detail}")
            return _failed_entry(index, f, he.detail)
        except Exception as exc:
            logger.error(f"Batch item {index} ('{safe_label(f.filename)}') unexpected error: {exc}", exc_info=True)
            return _failed_entry(index, f, "Analysis failed unexpectedly. Please try again.")

    tasks = [asyncio.create_task(_one(i, f)) for i, f in enumerate(files)]
    try:
        done, pending = await asyncio.wait(tasks, timeout=float(settings.BATCH_TIMEOUT_SECONDS))
    except asyncio.CancelledError:                      # client went away: stop the queued work too
        for task in tasks:
            task.cancel()
        raise
    if pending:
        logger.warning(f"Batch deadline reached: {len(pending)} of {len(tasks)} file(s) not processed")
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    # Persistence happens only now, for files whose analysis finished: a file reported as failed or
    # "not processed" was never stored, and a stored file was never reported as failed.
    results = []
    for index, (f, task) in enumerate(zip(files, tasks)):
        if task not in done:
            results.append(_failed_entry(index, f, BATCH_TIME_LIMIT_MESSAGE))
            continue
        outcome = task.result()
        if isinstance(outcome, dict):                   # a failed entry
            results.append(outcome)
            continue
        saved = await _save_analysis(outcome, job_id, current_user, save_to_db)
        entry = _build_response(outcome, **saved)
        entry["batch_index"] = index
        entry["error"] = None
        results.append(entry)

    succeeded = sum(1 for r in results if not r.get("error"))
    return {
        "total":     len(results),
        "succeeded": succeeded,
        "failed":    len(results) - succeeded,
        "results":   results,
    }
