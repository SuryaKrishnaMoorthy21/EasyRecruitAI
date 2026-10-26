"""
EasyRecruit ATS 3.0 — Resource Recommendations API
Serves learning resource recommendations from the datasets/ folder.
Supports skill-based filtering, domain selection, and free/premium toggle.

v3.1 improvements:
  • Richer skill scoring — abbreviation expansion + synonym mapping
  • Subject-level relevance scoring (title + subject text match)
  • Better tie-breaking: score → rating → subject relevance
  • Domain display-name fix for ece_eee folder → "ECE / EEE"
"""
import json
import logging
import re
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/recommendations", tags=["Recommendations"])

DATASETS_DIR = Path(__file__).resolve().parent.parent.parent / "datasets"

_cache: dict = {}

# ── Synonym / abbreviation expansion ─────────────────────────────────────────
# Maps common resume terms → dataset tag keywords
SKILL_SYNONYMS: dict[str, list[str]] = {
    # CS/Programming
    "python": ["python", "py"],
    "java": ["java"],
    "javascript": ["javascript", "js", "node", "react", "vue", "angular"],
    "js": ["javascript", "js"],
    "cpp": ["c++", "cpp"],
    "c++": ["c++", "cpp"],
    "sql": ["sql", "database", "dbms", "mysql", "postgresql"],
    "html": ["html", "css", "web"],
    "css": ["css", "html", "web"],
    "machine learning": ["machine learning", "ml", "artificial intelligence", "ai"],
    "ml": ["machine learning", "ml", "deep learning"],
    "deep learning": ["deep learning", "dl", "neural", "tensorflow", "pytorch"],
    "ai": ["artificial intelligence", "ai", "ml", "machine learning"],
    "data structures": ["dsa", "data structures", "algorithms"],
    "dsa": ["dsa", "data structures", "algorithms"],
    "algorithms": ["algorithms", "dsa", "data structures"],
    "nlp": ["nlp", "natural language processing"],
    "computer vision": ["computer vision", "cv", "image processing"],
    "cloud": ["cloud", "aws", "azure", "gcp", "devops"],
    "aws": ["aws", "cloud"],
    "linux": ["linux", "os", "operating system"],
    "git": ["git", "version control"],
    "api": ["api", "rest", "backend"],
    "react": ["react", "javascript", "frontend"],
    "operating system": ["os", "operating system", "linux"],
    "os": ["os", "operating system"],
    "computer networks": ["cn", "computer networks", "networking"],
    "cn": ["cn", "computer networks"],
    "dbms": ["dbms", "database", "sql"],
    "software engineering": ["se", "software engineering"],
    "computer organization": ["coa", "computer organization"],
    "cybersecurity": ["cybersecurity", "security", "network security"],
    "system design": ["system design", "scalability", "architecture"],
    # ECE/EEE
    "electronics": ["electronics", "basic electronics", "analog"],
    "vlsi": ["vlsi", "chip", "semiconductor", "cmos"],
    "embedded": ["embedded", "microcontroller", "microprocessor", "arduino"],
    "matlab": ["matlab", "simulation"],
    "fpga": ["fpga", "vhdl", "hdl"],
    "signals": ["signals", "dsp", "signal processing"],
    "control systems": ["control", "pid", "bode"],
    "power systems": ["power", "electrical machines"],
    "iot": ["iot", "embedded", "arduino", "raspberry"],
    # Commerce
    "accounting": ["accounting", "accounts", "bookkeeping", "tally"],
    "tally": ["tally", "erp", "accounting"],
    "taxation": ["taxation", "tax", "gst", "income tax"],
    "excel": ["excel", "ms office", "spreadsheet"],
    "ms office": ["ms office", "excel", "office"],
    "finance": ["finance", "financial", "banking"],
    "marketing": ["marketing", "sales", "digital marketing"],
    "hrm": ["hrm", "human resource", "hr"],
    "communication": ["communication", "soft skills"],
    "receptionist": ["receptionist", "front office", "crm"],
}


def _expand_skills(raw_skills: List[str]) -> List[str]:
    """Expand skill terms to include synonyms/aliases for better matching."""
    expanded = set()
    for s in raw_skills:
        sl = s.strip().lower()
        if not sl:
            continue
        expanded.add(sl)
        for syns in SKILL_SYNONYMS.get(sl, []):
            expanded.add(syns)
        # Also add individual words from multi-word skills
        for word in sl.split():
            if len(word) > 2:
                expanded.add(word)
    return list(expanded)


def _score_resource(resource: dict, expanded_skills: List[str], priority_missing: List[str]) -> tuple:
    """
    Return (skill_score, rating, subject_relevance) for sorting.
    - skill_score: number of skill matches against tags + title + subject
    - rating: resource rating (tiebreaker)
    - subject_relevance: partial match of skills against subject name
    """
    if not expanded_skills:
        return (0, resource.get("rating", 0), 0)

    tags = [t.lower() for t in resource.get("tags", [])]
    title_lower = resource.get("title", "").lower()
    subject_lower = resource.get("subject", "").lower()
    search_text = " ".join(tags) + " " + title_lower + " " + subject_lower

    # Count matches — missing skills get double weight
    score = 0
    for s in expanded_skills:
        if s in search_text:
            weight = 2 if s in priority_missing else 1
            score += weight

    subject_rel = sum(1 for s in expanded_skills if s in subject_lower)
    return (score, resource.get("rating", 0), subject_rel)


# ── Dataset loader ────────────────────────────────────────────────────────────

def _load_domain(domain: str) -> dict:
    """Load and cache a domain's resources.json."""
    domain = domain.lower()
    if domain in _cache:
        return _cache[domain]
    path = DATASETS_DIR / domain / "resources.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Domain '{domain}' not found.")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        _cache[domain] = data
        return data
    except Exception as exc:
        logger.error(f"Failed to load dataset for domain '{domain}': {exc}")
        raise HTTPException(status_code=500, detail="Failed to load resource dataset.")


_DOMAIN_DISPLAY: dict[str, str] = {
    "ece_eee": "ECE / EEE",
    "cse": "Computer Science & Engineering",
    "commerce": "Commerce, Accounting & Office Management",
}


def _available_domains() -> List[dict]:
    """Return list of available domain folders + planned ones."""
    if not DATASETS_DIR.exists():
        return []
    domains = []
    for entry in sorted(DATASETS_DIR.iterdir()):
        if entry.is_dir() and (entry / "resources.json").exists():
            try:
                with open(entry / "resources.json", encoding="utf-8") as f:
                    meta = json.load(f).get("meta", {})
                display_full = (
                    _DOMAIN_DISPLAY.get(entry.name.lower())
                    or meta.get("domain_full")
                    or entry.name.upper()
                )
                domains.append({
                    "domain": entry.name.upper(),
                    "domain_full": display_full,
                    "version": meta.get("version", "1.0"),
                    "available": True,
                })
            except Exception:
                pass
    planned = [
        {"domain": "MECH",  "domain_full": "Mechanical Engineering", "available": False},
        {"domain": "CIVIL", "domain_full": "Civil Engineering",       "available": False},
        {"domain": "MBA",   "domain_full": "MBA / Management",        "available": False},
    ]
    existing_names = {d["domain"] for d in domains}
    for p in planned:
        if p["domain"] not in existing_names:
            domains.append(p)
    return domains


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/domains")
async def list_domains():
    """List all available (and planned) learning domains."""
    return {"domains": _available_domains()}


@router.get("/{domain}")
async def get_recommendations(
    domain: str,
    skills: Optional[str] = Query(None, description="Comma-separated skill keywords"),
    missing_skills: Optional[str] = Query(None, description="Missing skills from resume analysis"),
    resource_type: str = Query("all", description="all | free | premium"),
    limit: int = Query(12, ge=1, le=50),
    subject: Optional[str] = Query(None, description="Filter by subject"),
):
    """
    Get skill-matched learning resources for a domain.
    Skills are expanded with synonyms for better matching relevance.
    """
    data = _load_domain(domain)

    skill_list    = [s.strip().lower() for s in (skills or "").split(",") if s.strip()]
    missing_list  = [s.strip().lower() for s in (missing_skills or "").split(",") if s.strip()]

    # Expand both skill lists for richer matching
    expanded_missing = _expand_skills(missing_list)
    expanded_skills  = _expand_skills(skill_list + missing_list)

    free_resources    = data.get("free_resources", [])
    premium_resources = data.get("premium_resources", [])

    # Filter by subject if provided
    if subject:
        subj_lower = subject.lower()
        free_resources    = [r for r in free_resources    if subj_lower in r.get("subject", "").lower()]
        premium_resources = [r for r in premium_resources if subj_lower in r.get("subject", "").lower()]

    def _sorted(resources):
        scored = [
            (r, _score_resource(r, expanded_skills, expanded_missing))
            for r in resources
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        return [r for r, _ in scored]

    result: dict = {}

    if resource_type in ("all", "free"):
        result["free"] = _sorted(free_resources)[:limit]

    if resource_type in ("all", "premium"):
        result["premium"] = _sorted(premium_resources)[:limit]

    result["meta"]            = data.get("meta", {})
    result["total_free"]      = len(data.get("free_resources", []))
    result["total_premium"]   = len(data.get("premium_resources", []))
    result["domain"]          = domain.upper()
    result["filtered_by_skills"] = bool(expanded_skills)

    return result


@router.get("/{domain}/subjects")
async def get_subjects(domain: str):
    """List all subjects available in a domain."""
    data = _load_domain(domain)
    free_subjects    = sorted({r.get("subject", "") for r in data.get("free_resources", []) if r.get("subject")})
    premium_subjects = sorted({r.get("subject", "") for r in data.get("premium_resources", []) if r.get("subject")})
    all_subjects     = sorted(set(free_subjects) | set(premium_subjects))
    return {
        "domain": domain.upper(),
        "subjects": all_subjects,
        "free_subjects": free_subjects,
        "premium_subjects": premium_subjects,
    }
