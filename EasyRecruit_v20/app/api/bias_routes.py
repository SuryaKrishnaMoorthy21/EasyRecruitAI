"""
EasyRecruit ATS 3.0 — Blind Screening Module (a bias-REDUCTION aid)
Redacts common personal details (emails, phone numbers, honorifics, gender words, age/date of
birth, religion/caste, marital status and labelled personal-detail lines) before scoring, and
returns that "blind" score alongside the original for comparison.

This does NOT eliminate bias and must not be described as bias-free: the candidate's name,
institution, employer and location names stay in the text and can still act as proxies for
identity, and the scoring heuristics can themselves be biased. A score difference between the two
versions is an INDICATOR for human review, not proof of bias (removing contact lines also changes
the structure part of the score). See BIAS_DISCLAIMER / ANONYMIZATION_LIMITATIONS.

Endpoints:
  POST /api/v1/bias/anonymize         — anonymize resume text
  POST /api/v1/bias/blind-score       — score resume without PII
  POST /api/v1/bias/compare           — side-by-side: biased vs blind score
"""
import logging
import re
from typing import Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from app.api.auth_routes import get_optional_user
from app.concurrency import get_gate, singleton_lock
from app.config import settings
from app.uploads import DEFAULT_QUEUE_TIMEOUT, ingest_resume, resolve_queue_timeout

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/bias", tags=["Blind Screening (bias-reduction aid)"])

_extractor   = None
_scorer      = None

def _get_extractor():
    global _extractor
    if _extractor is None:
        with singleton_lock:                      # Task 5: one build even when many threads arrive together
            if _extractor is None:
                from app.nlp.keyword_extractor import KeywordExtractor
                _extractor = KeywordExtractor()
    return _extractor

def _get_scorer():
    global _scorer
    if _scorer is None:
        with singleton_lock:
            if _scorer is None:
                from app.nlp.enhanced_scorer import EnhancedScorer
                _scorer = EnhancedScorer()
    return _scorer

def _score_text_sync(text: str, job_description: Optional[str]) -> dict:
    """Model load (first use) + ATS scoring: blocking and heavy, so it runs on the shared AnalysisGate."""
    return _get_scorer().calculate_ats_score(text, job_description)


# ── How results are presented (Task 6) ────────────────────────────────────────

BIAS_DISCLAIMER = (
    "Blind screening is a bias-reduction aid, not a guarantee of fairness. It hides some personal details but does "
    "not remove every proxy for identity, and the scoring heuristics can themselves be biased. A person must review "
    "the results; do not treat either score as a final decision."
)
ANONYMIZATION_LIMITATIONS = [
    "The candidate's name is NOT removed from the text; it is only detected for the report.",
    "Institution, employer and most location names are not removed and can still act as proxies for identity.",
    "Only common patterns are redacted (emails, phone numbers, profile links, honorifics, gender words, age/date of "
    "birth, religion/caste, marital status and labelled personal-detail lines); anything outside them stays.",
    "A few redactions can touch ordinary words (for example the standalone words 'man', 'sc' or 'st').",
    "Removing contact lines also changes the structure part of the score, so a score difference is not by itself "
    "evidence of bias.",
]

# ── Anonymization patterns ────────────────────────────────────────────────────

# Indian & global name patterns (honorifics + common first names)
HONORIFICS = re.compile(
    r'\b(Mr\.?|Mrs\.?|Ms\.?|Dr\.?|Prof\.?|Er\.?|Shri\.?|Smt\.?|Kumari\.?|Sri\.?)\s+',
    re.IGNORECASE
)

# Email — replace with placeholder
EMAIL_PATTERN = re.compile(
    r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
)

# Phone numbers
PHONE_PATTERN = re.compile(
    r'[\+]?[\d\s\-().]{10,15}'
)

# LinkedIn / GitHub / personal URLs
URL_PATTERN = re.compile(
    r'(?:https?://)?(?:www\.)?(?:linkedin\.com|github\.com|gitlab\.com|twitter\.com|instagram\.com)'
    r'[^\s\n,<>]*',
    re.IGNORECASE
)

# Gender markers
GENDER_PATTERN = re.compile(
    r'\b(male|female|man|woman|he/him|she/her|they/them|gender\s*:\s*\w+|m/f|f/m)\b',
    re.IGNORECASE
)

# Age / DOB patterns
AGE_PATTERN = re.compile(
    r'\b(?:age|dob|date\s+of\s+birth|born|d\.o\.b)[:\s]*[\d/.\-,\s]{4,20}',
    re.IGNORECASE
)
DOB_PATTERN = re.compile(
    r'\b\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}\b'  # DD/MM/YYYY style
)

# Religion / caste (Indian context)
RELIGION_PATTERN = re.compile(
    r'\b(hindu|muslim|christian|sikh|jain|buddhist|parsi|brahmin|kshatriya|'
    r'sc|st|obc|general category|caste\s*:\s*\w+|religion\s*:\s*\w+)\b',
    re.IGNORECASE
)

# Marital status
MARITAL_PATTERN = re.compile(
    r'\b(?:marital\s+status|married|unmarried|divorced|widowed|'
    # "single" is also ordinary technical vocabulary: single sign-on, single-page app, single point of failure ...
    r'single(?![ \t-]+(?:sign|page|point|source|thread|responsib|node|instance|tenant|window|machine|file|view|table|'
    r'unit|pass|stage|digit|line|cell|core|database|tier|purpose|region|cluster|server|master|entry|dashboard|'
    r'pane|screen|app|tab)|-))\b',
    re.IGNORECASE
)

# Photo / passport / address indicators
PERSONAL_INFO_PATTERN = re.compile(
    r'\b(?:photo|photograph|passport size|father\'?s?\s+name|mother\'?s?\s+name|'
    r'nationality|permanent\s+address|residential\s+address|'
    r'home\s+address|current\s+address|place\s+of\s+birth)\b[^\n]*',
    re.IGNORECASE
)

# Name extraction heuristic: first line or "Name: ..." pattern
NAME_LABEL_PATTERN = re.compile(
    r'(?:name\s*[:\-]\s*)([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})',
    re.IGNORECASE
)


def _looks_like_phone(candidate: str) -> bool:
    """A phone number has at least 10 digits and is not a run of 4-digit years such as '2018 - 2022 - 2024'."""
    if len(re.sub(r"\D", "", candidate)) < 10:
        return False
    groups = re.findall(r"\d+", candidate)
    return not all(len(g) == 4 and g[:2] in ("19", "20") for g in groups)


def _redact_keeping_whitespace(text: str, match_text: str, placeholder: str) -> str:
    """Replace the first occurrence of match_text, keeping the whitespace/line breaks the pattern captured."""
    lead = match_text[: len(match_text) - len(match_text.lstrip())]
    trail = match_text[len(match_text.rstrip()):]
    return text.replace(match_text, lead + placeholder + trail, 1)


def _anonymize_text(text: str) -> Tuple[str, Dict]:
    """
    Remove / replace all PII from resume text.
    Returns (anonymized_text, report_of_what_was_removed).
    """
    removed = {
        "emails": [],
        "phones": [],
        "urls": [],
        "gender_markers": [],
        "age_dob": [],
        "religion_caste": [],
        "marital_status": [],
        "personal_info_lines": [],
        "honorifics": 0,
        "name_detected": None,
    }

    anonymized = text

    # 1. Extract name for report (don't replace — needed for ID)
    name_match = NAME_LABEL_PATTERN.search(anonymized)
    if name_match:
        removed["name_detected"] = name_match.group(1)

    # Try first non-empty line as candidate name
    first_lines = [l.strip() for l in text.splitlines() if l.strip()]
    if first_lines and re.match(r'^[A-Z][a-z]+(?: [A-Z][a-z]+)+$', first_lines[0]):
        removed["name_detected"] = first_lines[0]

    # 2. Emails
    emails = EMAIL_PATTERN.findall(anonymized)
    removed["emails"] = emails
    anonymized = EMAIL_PATTERN.sub("[EMAIL REDACTED]", anonymized)

    # 3. Phone numbers (be conservative: year ranges such as "2018 - 2022" are not phone numbers, and the
    #    line breaks the pattern captures are kept so the resume's structure survives the redaction)
    phones = PHONE_PATTERN.findall(anonymized)
    real_phones = [p for p in phones if _looks_like_phone(p)]
    removed["phones"] = [p.strip() for p in real_phones]
    for ph in real_phones:
        anonymized = _redact_keeping_whitespace(anonymized, ph, "[PHONE REDACTED]")

    # 4. Social URLs
    urls = URL_PATTERN.findall(anonymized)
    removed["urls"] = urls
    anonymized = URL_PATTERN.sub("[PROFILE REDACTED]", anonymized)

    # 5. Honorifics (Mr, Mrs, Dr, Shri, etc.)
    removed["honorifics"] = len(HONORIFICS.findall(anonymized))
    anonymized = HONORIFICS.sub("", anonymized)

    # 6. Gender markers
    gender = GENDER_PATTERN.findall(anonymized)
    removed["gender_markers"] = list(set(gender))
    anonymized = GENDER_PATTERN.sub("[GENDER REDACTED]", anonymized)

    # 7. Age / DOB
    age_matches = AGE_PATTERN.findall(anonymized)
    dob_matches = DOB_PATTERN.findall(anonymized)
    removed["age_dob"] = age_matches + dob_matches
    # keep the whitespace the pattern captured (it can include the line break after "DOB: 12/05/1998")
    anonymized = AGE_PATTERN.sub(lambda m: "[AGE REDACTED]" + m.group(0)[len(m.group(0).rstrip()):], anonymized)
    anonymized = DOB_PATTERN.sub("[DOB REDACTED]", anonymized)

    # 8. Religion / caste
    religion = RELIGION_PATTERN.findall(anonymized)
    removed["religion_caste"] = list(set(religion))
    anonymized = RELIGION_PATTERN.sub("[RELIGION/CASTE REDACTED]", anonymized)

    # 9. Marital status
    marital = MARITAL_PATTERN.findall(anonymized)
    removed["marital_status"] = list(set(marital))
    anonymized = MARITAL_PATTERN.sub("[MARITAL STATUS REDACTED]", anonymized)

    # 10. Personal info lines (photo, address, father's name, etc.)
    personal = PERSONAL_INFO_PATTERN.findall(anonymized)
    removed["personal_info_lines"] = [p.strip()[:60] for p in personal]
    anonymized = PERSONAL_INFO_PATTERN.sub("[PERSONAL INFO REDACTED]", anonymized)

    total_removed = (
        len(removed["emails"]) + len(removed["phones"]) +
        len(removed["gender_markers"]) + len(removed["age_dob"]) +
        len(removed["religion_caste"]) + len(removed["marital_status"]) +
        len(removed["personal_info_lines"]) + removed["honorifics"]
    )

    return anonymized, {**removed, "total_items_removed": total_removed}


def _bias_score_comparison(
    original_text: str,
    anonymized_text: str,
    job_description: Optional[str],
) -> Dict:
    """
    Score the original and the anonymized text and report the difference as an INDICATOR for human review.

    A difference is not proof of bias: redacting contact lines and dates also changes the structure part of the
    score, and the candidate's name, institution and location are still in the text. `bias_detected` therefore
    only means "the score moved by more than 3 points"; it is kept for API compatibility next to the clearer
    `score_shift_flag`. If either scoring fails the comparison is reported as unavailable, never as a score of 0.
    """
    scorer = _get_scorer()
    meta = {
        "requires_human_review": True,
        "indicator_only": True,
        "score_shift_threshold": 3,
        "bias_detected_note": "bias_detected is true only when the score moved by more than 3 points after removing "
                              "personal details. It is an indicator for review, not a finding of bias.",
        "disclaimer": BIAS_DISCLAIMER,
        "limitations": list(ANONYMIZATION_LIMITATIONS),
    }

    original_score = blind_score = None
    try:
        original_score = scorer.calculate_ats_score(original_text, job_description)
    except Exception as e:
        logger.warning(f"Original scoring failed: {e}")
    try:
        blind_score = scorer.calculate_ats_score(anonymized_text, job_description)
    except Exception as e:
        logger.warning(f"Blind scoring failed: {e}")

    if original_score is None or blind_score is None:
        return {
            "original_score": original_score.get("overall_score") if original_score else None,
            "blind_score": blind_score.get("overall_score") if blind_score else None,
            "score_difference": None,
            "bias_detected": False,
            "score_shift_flag": False,
            "comparison_available": False,
            "bias_interpretation": "Comparison unavailable: scoring failed for one of the two versions, so no "
                                   "conclusion can be drawn.",
            "recommendation": "Review this resume manually.",
            **meta,
        }

    orig = original_score.get("overall_score", 0)
    blind = blind_score.get("overall_score", 0)
    diff = round(orig - blind, 2)
    shifted = abs(diff) > 3

    if diff > 3:
        interpretation = (
            f"The score is {abs(diff)} points lower after removing personal details. That can mean personal details "
            "influenced the automated score, but it can equally reflect the removed contact lines or dates. Treat it "
            "as a prompt for human review, not proof of bias."
        )
    elif diff < -3:
        interpretation = (
            f"The score is {abs(diff)} points higher after removing personal details. That can mean personal details "
            "were working against this candidate, but it can equally be a side effect of the removal. Treat it as a "
            "prompt for human review, not proof of bias."
        )
    else:
        interpretation = (
            "The score changed by 3 points or less after removing personal details. That does not show the process is "
            "free of bias: the name, institution and other proxies remain in the text."
        )

    return {
        "original_score": orig,
        "blind_score": blind,
        "score_difference": diff,
        "bias_detected": shifted,        # deprecated name: see bias_detected_note
        "score_shift_flag": shifted,
        "comparison_available": True,
        "bias_interpretation": interpretation,
        "recommendation": (
            "Review both versions side by side and decide manually; do not rely on either score alone."
            if shifted else
            "The two scores are similar. Keep evaluating candidates on evidence relevant to the role; "
            "this check does not rule out bias."
        ),
        **meta,
    }


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/anonymize")
async def anonymize_resume(
    file: UploadFile = File(...),
    current_user: Optional[dict] = Depends(get_optional_user),
):
    """Upload a resume and get back an anonymized version with a report of what was removed."""
    try:
        # Bounded streaming copy, type/content validation and parse (see app/uploads.py).
        parsed = await ingest_resume(file, min_text_chars=50)
        resume_text = parsed.text

        anonymized_text, removal_report = _anonymize_text(resume_text)

        return {
            "filename": file.filename,
            "original_length": len(resume_text),
            "anonymized_length": len(anonymized_text),
            "anonymized_text": anonymized_text,
            "removal_report": removal_report,
            "candidate_id": removal_report.get("name_detected", "Candidate"),
            # Additive (Task 6): what this does and does not do.
            "requires_human_review": True,
            "disclaimer": BIAS_DISCLAIMER,
            "limitations": list(ANONYMIZATION_LIMITATIONS),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Anonymize error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Anonymization failed.")


async def _blind_score_core(resume_text: str, job_description: Optional[str]) -> dict:
    if not resume_text or len(resume_text.strip()) < 50:
        raise HTTPException(status_code=400, detail="Could not extract enough text.")

    anonymized_text, removal_report = _anonymize_text(resume_text)

    # Model load + transformer scoring are CPU/memory heavy: bounded by the shared AnalysisGate (Task 5).
    score_result = await get_gate().run(
        _score_text_sync, anonymized_text, job_description,
        timeout=float(settings.ANALYSIS_TIMEOUT_SECONDS),
        queue_timeout=resolve_queue_timeout(DEFAULT_QUEUE_TIMEOUT),
    )

    return {
        "mode": "blind_scoring",
        "pii_removed": removal_report["total_items_removed"],
        "overall_score": score_result.get("overall_score", 0),
        "keyword_match_score": score_result.get("keyword_match_score", 0),
        "skill_match_score": score_result.get("skill_match_score", 0),
        "structure_score": score_result.get("structure_score", 0),
        "breakdown": score_result.get("breakdown", {}),
        "scoring_explanation": score_result.get("scoring_explanation"),
        "removal_report": removal_report,
        "requires_human_review": True,
        "disclaimer": BIAS_DISCLAIMER,
        "limitations": list(ANONYMIZATION_LIMITATIONS),
    }


@router.post("/blind-score")
async def blind_score(
    file: UploadFile = File(...),
    job_description: Optional[str] = Form(None),
    current_user: Optional[dict] = Depends(get_optional_user),
):
    """Score a resume with common personal details removed. A bias-reduction aid, not a guarantee: see `limitations`."""
    try:
        # Bounded streaming copy, type/content validation and parse (see app/uploads.py).
        parsed = await ingest_resume(file, min_text_chars=50)
        resume_text = parsed.text

        result = await _blind_score_core(resume_text, job_description)
        result["filename"] = file.filename
        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Blind score error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Blind scoring failed.")


@router.post("/blind-score-text")
async def blind_score_text(
    resume_text: str = Form(...),
    job_description: Optional[str] = Form(None),
    current_user: Optional[dict] = Depends(get_optional_user),
):
    """Score pasted resume text with all PII removed — text-input twin of /blind-score."""
    try:
        result = await _blind_score_core(resume_text, job_description)
        result["filename"] = None
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Blind score (text) error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Blind scoring failed.")


async def _compare_core(resume_text: str, job_description: Optional[str]) -> dict:
    if not resume_text or len(resume_text.strip()) < 50:
        raise HTTPException(status_code=400, detail="Could not extract enough text.")

    anonymized_text, removal_report = _anonymize_text(resume_text)
    comparison = await get_gate().run(
        _bias_score_comparison, resume_text, anonymized_text, job_description,
        timeout=float(settings.ANALYSIS_TIMEOUT_SECONDS),
        queue_timeout=resolve_queue_timeout(DEFAULT_QUEUE_TIMEOUT),
    )

    return {
        "pii_removed": removal_report["total_items_removed"],
        "removal_report": removal_report,
        **comparison,
    }


@router.post("/compare")
async def compare_biased_vs_blind(
    file: UploadFile = File(...),
    job_description: Optional[str] = Form(None),
    current_user: Optional[dict] = Depends(get_optional_user),
):
    """Compare the original score with the blind score. A score shift is an indicator for human review, not proof of bias."""
    try:
        # Bounded streaming copy, type/content validation and parse (see app/uploads.py).
        parsed = await ingest_resume(file, min_text_chars=50)
        resume_text = parsed.text

        result = await _compare_core(resume_text, job_description)
        result["filename"] = file.filename
        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Bias compare error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Bias comparison failed.")


@router.post("/compare-text")
async def compare_biased_vs_blind_text(
    resume_text: str = Form(...),
    job_description: Optional[str] = Form(None),
    current_user: Optional[dict] = Depends(get_optional_user),
):
    """
    Compare original vs blind score for pasted resume text — text-input twin
    of /compare. This is what the frontend's "Paste Text" tab on the
    Blind Score page actually calls; without it, every submission from
    that tab failed with a missing-required-field error, since /compare
    only ever accepted a file upload.
    """
    try:
        result = await _compare_core(resume_text, job_description)
        result["filename"] = None
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Bias compare (text) error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Bias comparison failed.")
