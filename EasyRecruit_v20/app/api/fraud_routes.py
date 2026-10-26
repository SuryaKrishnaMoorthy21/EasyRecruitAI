"""
EasyRecruit ATS 3.0 — Resume red-flag indicators (human review required)
Highlights patterns worth a second look in a resume: date inconsistencies, long skill lists,
keyword stuffing and unusual experience claims.

These are HEURISTIC INDICATORS, not findings. Each can have an innocent explanation (a typo, an
expected graduation date, an unusual career path, an unconventional layout), and none proves
dishonesty. A person must review every flag; do not reject a candidate on this output alone.
The response says so in `requires_human_review`, `disclaimer` and `score_basis`.

Endpoints:
  POST /api/v1/fraud/detect         — upload resume for the indicator check
  POST /api/v1/fraud/detect-text    — check raw text
"""
import logging
import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from app.api.auth_routes import get_optional_user
from app.uploads import ingest_resume

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/fraud", tags=["Red-Flag Indicators (human review)"])


# ── How results are presented (Task 6) ────────────────────────────────────────

REVIEW_DISCLAIMER = (
    "These are automated indicators for human review, not proof of dishonesty. Each one can have an innocent "
    "explanation (a typo, an expected graduation date, an unusual career path, an unconventional layout). Review "
    "them with the candidate, and do not reject anyone on this output alone."
)
SCORE_BASIS = (
    "The indicator score is a count-weighted sum of the flags (high = 25, medium = 12, low = 5, capped at 100). "
    "It is not a probability that a resume is dishonest and has not been validated against known cases."
)
RISK_LABELS = {
    "clean": "No indicators found (this does not verify the resume)",
    "low_risk": "Low review priority",
    "moderate_risk": "Moderate review priority",
    "high_risk": "High review priority",
    "very_high_risk": "Very high review priority",
}
# Wording that marks a year as a planned / expected date rather than a claim about the past.
_EXPECTED_DATE_MARKERS = (
    "expected", "pursuing", "pursuit", "currently", "anticipated", "graduating", "graduation", "batch",
    "class of", "ongoing", "in progress", "final year", "third year", "second year", "first year",
)

# ── Red flag patterns ─────────────────────────────────────────────────────────

# Skills that are commonly inflated / stuffed without real usage
OVERUSED_BUZZWORDS = [
    "blockchain", "metaverse", "nft", "web3", "quantum computing",
    "artificial intelligence", "machine learning", "deep learning", "big data",
    "iot", "cybersecurity", "devops", "agile", "scrum", "digital transformation",
]

# Commonly faked or dubious certifications
SUSPICIOUS_CERTS = [
    "self certified", "self-certified", "auto certified", "lifetime certification",
]

# Real top companies — if someone claims these, we verify consistency
TIER1_COMPANIES = [
    "google", "microsoft", "amazon", "apple", "meta", "facebook",
    "netflix", "uber", "airbnb", "twitter", "linkedin", "salesforce",
    "infosys", "wipro", "tcs", "hcl", "accenture", "ibm", "oracle",
]

# Implausible skill volumes — if someone lists too many, flag it
MAX_REASONABLE_SKILLS = 40

# Pattern: month/year for date extraction
DATE_PATTERN = re.compile(
    r'\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|'
    r'jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)'
    r'[\s,\-./]+(\d{4})\b',
    re.IGNORECASE
)

YEAR_RANGE_PATTERN = re.compile(
    r'\b(20\d{2}|19\d{2})\s*[-–—to]+\s*(20\d{2}|19\d{2}|present|current|now|till date)\b',
    re.IGNORECASE
)


# ── Detection functions ───────────────────────────────────────────────────────

def _check_date_consistency(text: str) -> List[Dict]:
    """Detect overlapping employment periods, future dates, or impossible timelines."""
    flags = []
    current_year = datetime.now().year

    # Extract all 4-digit years
    all_years = [int(y) for y in re.findall(r'\b(19\d{2}|20\d{2})\b', text)]

    # Future dates
    future_years = [y for y in all_years if y > current_year + 1]
    if future_years:
        # A year next to "expected" / "pursuing" / "batch" ... is an expected completion date, which is normal for a
        # current student; only unexplained future years keep the high severity.
        lowered = text.lower()
        expected_years, unexplained_years = [], []
        for y in sorted(set(future_years)):
            contexts = [lowered[max(0, m.start() - 80):m.start()] for m in re.finditer(r"\b%d\b" % y, lowered)]
            if contexts and all(any(mark in c for mark in _EXPECTED_DATE_MARKERS) for c in contexts):
                expected_years.append(y)
            else:
                unexplained_years.append(y)
        if unexplained_years:
            flags.append({
                "type": "future_date",
                "severity": "high",
                "message": f"Resume contains year(s) in the future: {unexplained_years}. This may be a typo or a planned date.",
                "detail": f"Years found: {unexplained_years}. Please confirm with the candidate.",
            })
        if expected_years:
            flags.append({
                "type": "future_date",
                "severity": "low",
                "message": f"Year(s) {expected_years} appear next to wording such as 'expected' or 'pursuing', "
                           f"which usually means an expected completion date.",
                "detail": "Normal for a current student; confirm if the role requires a completed degree.",
            })

    # Very old dates mixed with recent (possible DOB inflation trick)
    very_old = [y for y in all_years if y < 1970]
    if very_old and any(y > 2000 for y in all_years):
        flags.append({
            "type": "suspicious_old_date",
            "severity": "low",
            "message": "Resume contains very old dates (pre-1970) mixed with recent dates.",
            "detail": f"Old years: {very_old}",
        })

    # Extract year ranges and check for overlaps
    ranges = []
    for match in YEAR_RANGE_PATTERN.finditer(text):
        start = int(match.group(1))
        end_raw = match.group(2).strip().lower()
        end = current_year if end_raw in ("present", "current", "now", "till date") else int(end_raw)
        if end >= start:
            ranges.append((start, end, match.group(0)))

    # Check overlapping ranges
    for i in range(len(ranges)):
        for j in range(i + 1, len(ranges)):
            s1, e1, label1 = ranges[i]
            s2, e2, label2 = ranges[j]
            overlap_start = max(s1, s2)
            overlap_end = min(e1, e2)
            if overlap_start < overlap_end - 1:  # more than 1yr overlap
                flags.append({
                    "type": "overlapping_dates",
                    "severity": "medium",
                    "message": f"Overlapping employment periods detected: '{label1}' and '{label2}'.",
                    "detail": f"Overlap: {overlap_start}–{overlap_end}. Concurrent roles (part-time work, freelancing, a role change) are common.",
                })

    # Total experience plausibility
    if ranges:
        total_years = sum(e - s for s, e, _ in ranges)
        if total_years > 40:
            flags.append({
                "type": "implausible_experience",
                "severity": "high",
                "message": f"The listed work periods add up to an unusually high {total_years} years.",
                "detail": "The sum exceeds 40 years. Overlapping roles are counted twice, so this can be a formatting or overlap artefact.",
            })

    return flags


def _check_skill_inflation(text: str) -> List[Dict]:
    """Detect keyword stuffing and unrealistic skill volume."""
    flags = []
    text_lower = text.lower()

    # Count skill-like lines (comma-separated lists)
    skill_tokens = re.findall(r'\b[a-zA-Z][a-zA-Z0-9+#.]{1,20}\b', text_lower)
    unique_skills = set(skill_tokens)

    # Check buzzword stuffing
    buzzwords_found = [bw for bw in OVERUSED_BUZZWORDS if bw in text_lower]
    if len(buzzwords_found) >= 5:
        flags.append({
            "type": "buzzword_stuffing",
            "severity": "medium",
            "message": f"Resume lists {len(buzzwords_found)} popular buzzwords; check that it gives context for them.",
            "detail": f"Buzzwords: {', '.join(buzzwords_found[:8])}",
        })

    # Suspicious cert claims
    for sc in SUSPICIOUS_CERTS:
        if sc in text_lower:
            flags.append({
                "type": "suspicious_certification",
                "severity": "high",
                "message": f"Certification wording to verify: '{sc}' (a self-declared credential is not issued by a recognised body).",
                "detail": "Verify certification with the issuing authority.",
            })

    # Skills section volume check
    skills_section = re.search(
        r'(?:skills?|technical skills?|competencies)[:\s]*(.{0,800})',
        text_lower, re.DOTALL
    )
    if skills_section:
        skills_text = skills_section.group(1)
        comma_count = skills_text.count(",")
        if comma_count > MAX_REASONABLE_SKILLS:
            flags.append({
                "type": "skill_overload",
                "severity": "medium",
                "message": f"Skills section lists {comma_count + 1} items — unusually high.",
                "detail": "Can reflect keyword stuffing, or simply a long and honest skills list.",
            })

    return flags


def _check_contact_inconsistencies(text: str) -> List[Dict]:
    """Check for missing or multiple conflicting contact details."""
    flags = []

    emails = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', text)
    phones = re.findall(r'[\+]?[\d\s\-().]{10,15}', text)

    if len(emails) == 0:
        flags.append({
            "type": "missing_email",
            "severity": "medium",
            "message": "No email address found in resume.",
            "detail": "A professional resume should always have contact email.",
        })

    if len(emails) > 2:
        flags.append({
            "type": "multiple_emails",
            "severity": "low",
            "message": f"{len(emails)} email addresses found — verify which is current.",
            "detail": f"Emails: {', '.join(emails[:3])}",
        })

    if len(phones) == 0:
        flags.append({
            "type": "missing_phone",
            "severity": "low",
            "message": "No phone number found in resume.",
            "detail": "Consider asking the candidate for contact details.",
        })

    return flags


def _check_education_claims(text: str) -> List[Dict]:
    """Check for suspicious education claims."""
    flags = []
    text_lower = text.lower()

    # Multiple conflicting degree levels
    phd_terms   = ["ph.d", "phd", "doctorate", "doctor of"]
    mtech_terms = ["m.tech", "m.e.", "master of technology", "mtech", "msc", "m.sc"]
    btech_terms = ["b.tech", "b.e.", "bachelor", "bsc", "b.sc", "bca", "mca"]

    has_phd   = any(t in text_lower for t in phd_terms)
    has_mtech = any(t in text_lower for t in mtech_terms)
    has_btech = any(t in text_lower for t in btech_terms)

    # Check graduation year vs claimed experience
    grad_years = re.findall(r'(?:graduated?|batch|passout|pass\s*out)[^0-9]*(20\d{2}|19\d{2})', text_lower)
    current_year = datetime.now().year
    if grad_years:
        grad_year = int(grad_years[0])
        years_since_grad = current_year - grad_year
        # Look for claimed experience
        exp_matches = re.findall(r'(\d+)\+?\s*(?:years?|yrs?)\s*(?:of\s+)?(?:experience|exp)', text_lower)
        if exp_matches:
            claimed_exp = max(int(y) for y in exp_matches)
            if claimed_exp > years_since_grad + 1:
                flags.append({
                    "type": "experience_mismatch",
                    "severity": "high",
                    "message": (
                        f"Claimed experience ({claimed_exp} years) exceeds time since graduation "
                        f"({years_since_grad} years since {grad_year})."
                    ),
                    "detail": "Possible explanations: internships or work before graduation, a second degree, or an inflated claim. Ask the candidate to clarify.",
                })

    return flags


def _check_text_quality(text: str) -> List[Dict]:
    """Check for copy-paste artifacts, gibberish, or template-not-filled markers."""
    flags = []
    text_lower = text.lower()

    # Template placeholders not filled
    placeholders = ["[your name]", "[company name]", "insert here", "lorem ipsum",
                    "your address", "city, state", "enter your"]
    found = [p for p in placeholders if p in text_lower]
    if found:
        flags.append({
            "type": "unfilled_template",
            "severity": "high",
            "message": "Resume appears to be an unfilled template.",
            "detail": f"Placeholder text found: {', '.join(found)}",
        })

    # Extremely short resume
    word_count = len(text.split())
    if word_count < 80:
        flags.append({
            "type": "insufficient_content",
            "severity": "medium",
            "message": f"Resume is very short ({word_count} words). May be incomplete.",
            "detail": "A typical resume has 300–800 words.",
        })

    # Repeated lines (copy-paste artifact)
    lines = [l.strip() for l in text.splitlines() if len(l.strip()) > 20]
    seen = {}
    for line in lines:
        seen[line] = seen.get(line, 0) + 1
    repeated = {k: v for k, v in seen.items() if v > 2}
    if repeated:
        flags.append({
            "type": "repeated_content",
            "severity": "medium",
            "message": f"{len(repeated)} line(s) appear more than twice — possible copy-paste artifact.",
            "detail": f"Example: '{list(repeated.keys())[0][:80]}'",
        })

    return flags


def _calculate_fraud_score(flags: List[Dict]) -> Tuple[int, str]:
    """Count-weighted indicator score (0–100) and review-priority label. Not a probability of dishonesty."""
    severity_weights = {"high": 25, "medium": 12, "low": 5}
    raw = sum(severity_weights.get(f["severity"], 5) for f in flags)
    score = min(100, raw)

    if score == 0:
        label = "clean"
    elif score <= 15:
        label = "low_risk"
    elif score <= 35:
        label = "moderate_risk"
    elif score <= 60:
        label = "high_risk"
    else:
        label = "very_high_risk"

    return score, label


def _analyze_fraud(text: str) -> Dict:
    """Run all fraud checks and return consolidated report."""
    all_flags = []
    all_flags.extend(_check_date_consistency(text))
    all_flags.extend(_check_skill_inflation(text))
    all_flags.extend(_check_contact_inconsistencies(text))
    all_flags.extend(_check_education_claims(text))
    all_flags.extend(_check_text_quality(text))

    fraud_score, risk_label = _calculate_fraud_score(all_flags)

    high_flags   = [f for f in all_flags if f["severity"] == "high"]
    medium_flags = [f for f in all_flags if f["severity"] == "medium"]
    low_flags    = [f for f in all_flags if f["severity"] == "low"]

    recommendations = []
    if any(f["type"] == "overlapping_dates" for f in all_flags):
        recommendations.append("Ask candidate to clarify their employment timeline during interview.")
    if any(f["type"] == "experience_mismatch" for f in all_flags):
        recommendations.append("Ask the candidate to explain the timeline and verify through your standard reference checks.")
    if any(f["type"] == "buzzword_stuffing" for f in all_flags):
        recommendations.append("Ask specific technical questions to verify claimed skills.")
    if any(f["type"] == "unfilled_template" for f in all_flags):
        recommendations.append("Ask candidate to resubmit with a properly completed resume.")
    if not all_flags:
        recommendations.append("No indicators were raised by these automated checks. That does not verify the resume; "
                               "follow your normal verification process.")

    return {
        "fraud_score": fraud_score,
        "risk_level": risk_label,
        "total_flags": len(all_flags),
        "high_severity": len(high_flags),
        "medium_severity": len(medium_flags),
        "low_severity": len(low_flags),
        "flags": all_flags,
        "recommendations": recommendations,
        "summary": (
            f"Found {len(all_flags)} indicator(s) for human review — {RISK_LABELS[risk_label]}."
            if all_flags else
            "No indicators were raised by these automated checks. This does not verify the resume."
        ),
        # Additive (Task 6): how this output must be read.
        "requires_human_review": True,
        "risk_level_label": RISK_LABELS[risk_label],
        "disclaimer": REVIEW_DISCLAIMER,
        "score_basis": SCORE_BASIS,
    }


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/detect")
async def detect_fraud_from_file(
    file: UploadFile = File(...),
    current_user: Optional[dict] = Depends(get_optional_user),
):
    """Upload a resume and run fraud detection analysis."""
    try:
        # Bounded streaming copy, type/content validation and parse (see app/uploads.py).
        parsed = await ingest_resume(file, min_text_chars=30)
        resume_text = parsed.text

        result = _analyze_fraud(resume_text)
        result["filename"] = file.filename
        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Fraud detection error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Fraud detection failed.")


@router.post("/detect-text")
async def detect_fraud_from_text(
    resume_text: str = Form(...),
    current_user: Optional[dict] = Depends(get_optional_user),
):
    """Run fraud detection on raw resume text."""
    if len(resume_text.strip()) < 30:
        raise HTTPException(status_code=400, detail="Text too short.")
    return _analyze_fraud(resume_text)
