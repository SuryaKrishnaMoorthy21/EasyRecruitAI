"""
EasyRecruit ATS 3.0 — Public API Routes
Quick-analysis endpoint (no auth required), batch analysis, health check.
"""
import asyncio
import json
import logging
import time
from typing import List, Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from app.concurrency import get_gate, singleton_lock
from app.config import settings
from app.parsers.pdf_parser import PDFParser
from app.parsers.docx_parser import DOCXParser
from app.request_limits import PUBLIC_BATCH_MAX_FILES, BoundedFormRoute
from app.uploads import DEFAULT_QUEUE_TIMEOUT, ingest_resume, resolve_queue_timeout, safe_label

logger = logging.getLogger(__name__)
BATCH_TIME_LIMIT_MESSAGE = (
    "Not processed: the batch time limit was reached. Please upload fewer files at once."
)
router = APIRouter(prefix="/api/v1", tags=["Public"], route_class=BoundedFormRoute)

# Eager-init parsers (lightweight)
_pdf_parser  = PDFParser()
_docx_parser = DOCXParser()

# Lazy NLP
_extractor = None
_scorer    = None


# The getters can be reached from several analysis worker threads at once; the lock stops N simultaneous
# first requests from each building their own copy of spaCy / the transformer (Task 5).
def _get_extractor():
    global _extractor
    if _extractor is None:
        with singleton_lock:
            if _extractor is None:
                from app.nlp.keyword_extractor import KeywordExtractor
                _extractor = KeywordExtractor()
    return _extractor


def _get_scorer():
    global _scorer
    if _scorer is None:
        with singleton_lock:
            if _scorer is None:
                from app.nlp.resume_scorer import ResumeScorer
                _scorer = ResumeScorer()
    return _scorer


def _run_public_pipeline_sync(text: str, job_description: Optional[str], skills_dict: Optional[dict]):
    """
    NLP extraction + ATS scoring for the public /analyze endpoint. Blocking and CPU-heavy, so it runs on the
    AnalysisGate (worker thread, bounded concurrency) and never on the event loop, where it used to freeze
    the whole server for the duration of every analysis.
    """
    extractor = _get_extractor()
    scorer    = _get_scorer()
    # v9 extractor: full_analysis may not exist; use modular fallback
    try:
        nlp_analysis = extractor.full_analysis(text)
    except AttributeError:
        try:
            _si = extractor.extract_skills(text)
            _ci = extractor.extract_contact_info(text)
            nlp_analysis = {
                "contact_info": _ci,
                "skills": _si,
                "keywords": extractor.extract_keywords(text, top_k=20),
                "candidate_name": _ci.get("name"),
                "indian_context": _si.get("indian_context", {}),
            }
        except Exception as _e:
            logger.error(f"NLP fallback failed: {_e}")
            nlp_analysis = {"contact_info": {}, "keywords": [], "skills": [], "indian_context": {}}
    except Exception as _e:
        logger.error(f"NLP analysis error: {_e}")
        nlp_analysis = {"contact_info": {}, "keywords": [], "skills": [], "indian_context": {}}

    score_result = scorer.calculate_ats_score(text, job_description, skills_dict)
    return nlp_analysis, score_result


def _score_only_sync(text: str, job_description: Optional[str]):
    """Scoring step of /analyze/batch (blocking; runs on the AnalysisGate)."""
    return _get_scorer().calculate_ats_score(text, job_description)


@router.post("/analyze", tags=["Public"])
async def analyze_resume(
    file:            UploadFile    = File(...),
    job_description: Optional[str] = Form(None),
    required_skills: Optional[str] = Form(None),
):
    """
    Quick resume analysis — no login required.
    Results are NOT saved to the database.
    """
    try:
        # Bounded streaming copy, extension / MIME / content validation and parse (see app/uploads.py).
        parsed = await ingest_resume(file, min_text_chars=50)
        text = parsed.text

        skills_dict = None
        if required_skills:
            try:
                skills_dict = json.loads(required_skills)
            except json.JSONDecodeError:
                pass

        nlp_analysis, score_result = await get_gate().run(
            _run_public_pipeline_sync, text, job_description, skills_dict,
            timeout=float(settings.ANALYSIS_TIMEOUT_SECONDS),
            queue_timeout=resolve_queue_timeout(DEFAULT_QUEUE_TIMEOUT),
        )

        return {
            "analysis_id":         None,
            "filename":            file.filename,
            "file_type":           parsed.kind,
            "extracted_text_length": len(text),
            "overall_score":       score_result.get("overall_score", 0),
            "keyword_match_score": score_result.get("keyword_match_score", 0),
            "skill_match_score":   score_result.get("skill_match_score", 0),
            "structure_score":     score_result.get("structure_score", 0),
            "semantic_similarity": score_result.get("semantic_similarity"),
            "keywords":            nlp_analysis.get("keywords", []),
            "skills":              nlp_analysis.get("skills", []),
            "recommendations":     score_result.get("breakdown", {}).get("recommendations", []),
            "breakdown":           score_result.get("breakdown", {}),
            "detected_domain":     score_result.get("detected_domain", "general"),
            "india_context_bonus": score_result.get("india_context_bonus", 0.0),
            "indian_context":      score_result.get("indian_context", {}),
            "extraction_confidence": score_result.get("extraction_confidence"),
            "scoring_explanation": score_result.get("scoring_explanation"),
            "nlp_analysis":        nlp_analysis,
            "status":              "completed",
        }

    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Public analyze error: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail="Analysis failed unexpectedly. Please try again.")


@router.post("/analyze/batch")
async def analyze_batch(
    files:           List[UploadFile] = File(...),
    job_description: Optional[str]   = Form(None),
):
    """
    Analyze up to 10 resumes at once. Files are processed one after another (each CPU-heavy step on the
    shared AnalysisGate); one bad file only fails its own entry. The batch as a whole is limited to
    BATCH_TIMEOUT_SECONDS: files not reached by then are reported as failed and finished ones are kept.
    """
    if len(files) > PUBLIC_BATCH_MAX_FILES:
        raise HTTPException(status_code=400, detail=f"Maximum {PUBLIC_BATCH_MAX_FILES} files per request.")

    async def _process(f: UploadFile) -> dict:
        parsed = await ingest_resume(f, min_text_chars=50, queue_timeout=None)
        score = await get_gate().run(
            _score_only_sync, parsed.text, job_description,
            timeout=float(settings.ANALYSIS_TIMEOUT_SECONDS), queue_timeout=None,
        )
        return {"filename": f.filename, **score}

    results, errors = [], []
    deadline = time.monotonic() + float(settings.BATCH_TIMEOUT_SECONDS)
    for f in files:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            errors.append({"filename": f.filename, "error": BATCH_TIME_LIMIT_MESSAGE})
            continue
        try:
            results.append(await asyncio.wait_for(_process(f), timeout=remaining))
        except asyncio.TimeoutError:
            errors.append({"filename": f.filename, "error": BATCH_TIME_LIMIT_MESSAGE})
        except HTTPException as exc:
            errors.append({"filename": f.filename, "error": exc.detail})
        except MemoryError:
            logger.error(f"MemoryError processing batch file: {safe_label(f.filename)}")
            errors.append({"filename": f.filename, "error": "File too large to process in memory"})
        except Exception as exc:
            logger.error(f"Batch analysis error for {safe_label(f.filename)}: {exc}")
            errors.append({"filename": f.filename, "error": "Unexpected error during analysis"})

    return {
        "total":      len(files),
        "successful": len(results),
        "failed":     len(errors),
        "results":    results,
        "errors":     errors,
    }


@router.post("/extract-text")
async def extract_text_only(file: UploadFile = File(...)):
    """Extract raw text from a resume without scoring."""
    try:
        parsed = await ingest_resume(file, min_text_chars=1)
        return {
            "filename":       file.filename,
            "extracted_text": parsed.text,
            "text_length":    len(parsed.text),
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Text extraction error: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail="Text extraction failed unexpectedly. Please try again.")


@router.get("/health")
async def health_check():
    """Service health check."""
    from datetime import datetime
    from app.config import settings as cfg
    from app.db.database import db as _db

    db_ok = "ok"
    try:
        _db.fetch_one("SELECT 1")
    except Exception:
        db_ok = "error"

    return {
        "status":   "healthy",
        "version":  cfg.API_VERSION,
        "database": db_ok,
        "services": {
            "pdf_parser":        "ready",
            "docx_parser":       "ready",
            "keyword_extractor": "ready" if _extractor else "lazy",
            "scorer":            "ready" if _scorer else "lazy",
        },
        "timestamp": datetime.utcnow().isoformat(),
    }


@router.get("/skills-database")
async def get_skills_database():
    """Return the full skill taxonomy used for analysis."""
    extractor = _get_extractor()
    db_map    = extractor.SKILL_DATABASE
    return {
        "categories":   list(db_map.keys()),
        "total_skills": sum(len(v) for v in db_map.values()),
        "skills":       db_map,
    }
