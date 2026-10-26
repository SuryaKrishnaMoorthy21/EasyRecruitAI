"""
EasyRecruit ATS 3.0 — Interview Question Generator
Generates tailored interview questions from resume text + job description.

Endpoints:
  POST /api/v1/interview/generate               — generate questions from resume upload
  POST /api/v1/interview/generate-from-text      — generate from raw text (no file)
  GET  /api/v1/interview/templates               — list question templates by domain
  GET  /api/v1/interview/departments             — 100 programmes grouped by degree (cascading picker)
  POST /api/v1/interview/generate-by-department  — curated Q&A bank lookup for a chosen programme
"""
import json
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from app.api.auth_routes import get_optional_user
from app.concurrency import get_gate, singleton_lock
from app.config import settings
from app.uploads import DEFAULT_QUEUE_TIMEOUT, ingest_resume, resolve_queue_timeout

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/interview", tags=["Interview Generator"])

DATASETS_DIR = Path(__file__).resolve().parent.parent.parent / "datasets"

# ── Lazy parsers ──────────────────────────────────────────────────────────────
_extractor   = None
_dept_cache: dict = {}

def _get_extractor():
    global _extractor
    if _extractor is None:
        with singleton_lock:                      # Task 5: one build even when many threads arrive together
            if _extractor is None:
                from app.nlp.keyword_extractor import KeywordExtractor
                _extractor = KeywordExtractor()
    return _extractor


def _load_departments() -> dict:
    """Load + cache the 100-programme dataset (degree_order + departments list)."""
    if "departments" in _dept_cache:
        return _dept_cache["departments"]
    path = DATASETS_DIR / "departments_100.json"
    if not path.exists():
        raise HTTPException(status_code=500, detail="Department dataset not found on server.")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        _dept_cache["departments"] = data
        return data
    except Exception as exc:
        logger.error(f"Failed to load departments_100.json: {exc}")
        raise HTTPException(status_code=500, detail="Failed to load department dataset.")


def _load_qa_bank() -> dict:
    """Load + cache the curated interview Q&A bank (common + aptitude + 12 subject categories)."""
    if "qa_bank" in _dept_cache:
        return _dept_cache["qa_bank"]
    path = DATASETS_DIR / "department_qa_bank.json"
    if not path.exists():
        raise HTTPException(status_code=500, detail="Q&A bank dataset not found on server.")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        _dept_cache["qa_bank"] = data
        return data
    except Exception as exc:
        logger.error(f"Failed to load department_qa_bank.json: {exc}")
        raise HTTPException(status_code=500, detail="Failed to load Q&A bank dataset.")


def _find_department(department_id: str) -> dict:
    data = _load_departments()
    for d in data.get("departments", []):
        if d["id"] == department_id:
            return d
    raise HTTPException(status_code=404, detail="Unknown department_id. Fetch /departments first.")


# ── Question bank by category ─────────────────────────────────────────────────

BEHAVIORAL_QUESTIONS = [
    "Tell me about a challenging project you worked on and how you handled it.",
    "Describe a situation where you had to work with a difficult team member.",
    "Give an example of a time you missed a deadline. What happened and what did you learn?",
    "Tell me about a time you had to learn a new technology quickly.",
    "Describe a situation where you showed leadership without a formal title.",
    "How do you prioritize tasks when you have multiple deadlines?",
    "Tell me about a time you received critical feedback. How did you respond?",
    "Describe your most successful project and what made it successful.",
]

SKILL_QUESTIONS: Dict[str, List[str]] = {
    "python": [
        "Explain the difference between a list and a tuple in Python.",
        "How do you handle exceptions in Python? Give an example.",
        "What are Python decorators and where would you use them?",
        "Explain the GIL (Global Interpreter Lock) and its impact on multithreading.",
        "How does Python's garbage collection work?",
    ],
    "java": [
        "What is the difference between an abstract class and an interface in Java?",
        "Explain Java's memory model — heap vs stack.",
        "What are Java generics and why are they useful?",
        "How does the JVM handle garbage collection?",
        "Explain the SOLID principles with a Java example.",
    ],
    "javascript": [
        "What is the difference between '==' and '===' in JavaScript?",
        "Explain closures with an example.",
        "What is the event loop in JavaScript?",
        "What are Promises and how do they differ from callbacks?",
        "Explain 'this' keyword behavior in different contexts.",
    ],
    "react": [
        "What is the difference between state and props in React?",
        "Explain the React component lifecycle.",
        "What are React hooks? Name and explain at least 3.",
        "How does virtual DOM improve performance?",
        "What is the purpose of useEffect and when does it run?",
    ],
    "sql": [
        "What is the difference between INNER JOIN and LEFT JOIN?",
        "Explain indexing and when you would use it.",
        "What are the ACID properties of a database transaction?",
        "What is the difference between WHERE and HAVING clauses?",
        "Explain normalization and give an example of 1NF, 2NF, 3NF.",
    ],
    "machine learning": [
        "Explain the bias-variance tradeoff.",
        "What is overfitting and how do you prevent it?",
        "Explain the difference between supervised and unsupervised learning.",
        "What is gradient descent and how does it work?",
        "How would you handle an imbalanced dataset?",
    ],
    "deep learning": [
        "Explain backpropagation in simple terms.",
        "What is the vanishing gradient problem?",
        "What are CNNs and where are they best used?",
        "Explain the attention mechanism in transformers.",
        "What is dropout regularization?",
    ],
    "aws": [
        "What is the difference between EC2 and Lambda?",
        "Explain S3 storage classes and when to use each.",
        "What is a VPC and why is it important?",
        "How does auto-scaling work in AWS?",
        "What is IAM and what are best practices for it?",
    ],
    "docker": [
        "What is the difference between a Docker image and a container?",
        "Explain the purpose of a Dockerfile.",
        "What is Docker Compose and when would you use it?",
        "How do you persist data in Docker containers?",
        "What are multi-stage builds in Docker?",
    ],
    "kubernetes": [
        "What is the difference between a Pod and a Deployment?",
        "Explain Kubernetes services and their types.",
        "What is a ConfigMap and a Secret?",
        "How does Kubernetes handle self-healing?",
        "What is a Helm chart?",
    ],
    "data structures": [
        "Explain the time complexity of common operations on a HashMap.",
        "What is the difference between a stack and a queue?",
        "Explain binary search trees and their operations.",
        "What is dynamic programming? Give an example.",
        "How does a heap work and where is it used?",
    ],
    "communication": [
        "How do you explain a complex technical concept to a non-technical stakeholder?",
        "Describe how you document your work for team members.",
        "How do you handle disagreements in a technical discussion?",
    ],
    "agile": [
        "What is the difference between Scrum and Kanban?",
        "How do you estimate story points?",
        "What happens in a sprint retrospective?",
        "How do you handle scope creep in an agile project?",
    ],
    "testing": [
        "What is the difference between unit testing and integration testing?",
        "What is TDD (Test Driven Development)? Have you practiced it?",
        "Explain mocking in unit tests.",
        "What metrics do you use to measure test quality?",
    ],
    "finance": [
        "Explain the difference between accounts payable and accounts receivable.",
        "What is EBITDA and how is it calculated?",
        "Walk me through a DCF valuation.",
        "What is the difference between GAAP and IFRS?",
        "How do you detect fraud in financial statements?",
    ],
    "sap": [
        "Explain the difference between SAP ECC and SAP S/4HANA.",
        "What is an ABAP program and how is it structured?",
        "Explain the procure-to-pay process in SAP MM.",
        "What is a transport request in SAP?",
        "How do you debug an ABAP program?",
    ],
}

DOMAIN_QUESTIONS: Dict[str, List[str]] = {
    "cse": [
        "What is the CAP theorem in distributed systems?",
        "Explain RESTful API design principles.",
        "What is the difference between TCP and UDP?",
        "Explain the concept of microservices vs monolithic architecture.",
        "What is CI/CD and how have you implemented it?",
    ],
    "finance": [
        "How do you ensure accuracy in financial reporting?",
        "Describe your experience with ERP systems like SAP or Tally.",
        "How do you stay updated with changing tax regulations?",
        "What is your approach to audit preparation?",
    ],
    "hr": [
        "How do you source candidates for hard-to-fill positions?",
        "Describe your onboarding process for new hires.",
        "How do you measure the effectiveness of a recruitment campaign?",
        "What metrics do you track in HR analytics?",
    ],
    "general": [
        "Why are you interested in this role?",
        "Where do you see yourself in 5 years?",
        "What is your biggest professional achievement?",
        "How do you keep your skills up to date?",
        "What does a typical productive workday look like for you?",
    ],
}

EXPERIENCE_QUESTIONS = {
    "fresher": [
        "Walk me through a college project you are proud of.",
        "What relevant coursework or certifications have you completed?",
        "How do you plan to bridge the gap between academic and industry experience?",
        "Have you done any internships or open-source contributions?",
    ],
    "mid": [
        "What is the most complex system you have designed or contributed to?",
        "How have your responsibilities grown in your current/last role?",
        "Tell me about a production incident you handled.",
        "How do you mentor junior team members?",
    ],
    "senior": [
        "How do you make architectural decisions under uncertainty?",
        "Describe your experience managing cross-functional teams.",
        "How do you balance technical debt with feature delivery?",
        "What is your approach to hiring and growing engineering talent?",
    ],
}


# ── Core generation logic ─────────────────────────────────────────────────────

def _detect_experience_level(resume_text: str) -> str:
    """Estimate candidate's experience level from resume text."""
    text_lower = resume_text.lower()
    years_matches = re.findall(
        r'(\d+)\+?\s*(?:years?|yrs?)\s*(?:of\s+)?(?:experience|exp)',
        text_lower
    )
    years = max([int(y) for y in years_matches], default=0)
    if years == 0:
        fresher_signals = ["fresher", "graduate", "b.tech", "b.e.", "intern", "trainee", "entry level"]
        if any(s in text_lower for s in fresher_signals):
            return "fresher"
        return "fresher"
    elif years <= 4:
        return "mid"
    else:
        return "senior"


def _generate_questions(
    resume_text: str,
    job_description: Optional[str],
    max_questions: int = 20,
) -> Dict:
    """
    Core logic — generates interview questions based on:
    1. Detected skills from resume
    2. Domain from resume/JD
    3. Experience level
    4. Behavioral questions (always included)
    """
    extractor = _get_extractor()

    # Extract skills and domain
    try:
        analysis = extractor.full_analysis(resume_text)
        skills_data = analysis.get("skills", {})
        domain, _ = extractor.detect_domain(resume_text)
    except Exception as e:
        logger.warning(f"Extractor error: {e}")
        skills_data = {}
        domain = "general"

    # Flatten all detected skills
    detected_skills: List[str] = []
    if isinstance(skills_data, dict):
        for cat_skills in skills_data.values():
            if isinstance(cat_skills, list):
                detected_skills.extend([s.lower() for s in cat_skills])
    elif isinstance(skills_data, list):
        detected_skills = [s.lower() for s in skills_data]

    # If JD provided, extract JD skills too
    if job_description:
        try:
            jd_analysis = extractor.full_analysis(job_description)
            jd_skills = jd_analysis.get("skills", {})
            if isinstance(jd_skills, dict):
                for cat_skills in jd_skills.values():
                    if isinstance(cat_skills, list):
                        detected_skills.extend([s.lower() for s in cat_skills])
        except Exception:
            pass

    # Deduplicate
    detected_skills = list(dict.fromkeys(detected_skills))

    experience_level = _detect_experience_level(resume_text)

    questions_by_category: Dict[str, List[str]] = {}

    # 1. Behavioral (always include 3)
    questions_by_category["Behavioral"] = BEHAVIORAL_QUESTIONS[:3]

    # 2. Experience-level specific
    exp_qs = EXPERIENCE_QUESTIONS.get(experience_level, EXPERIENCE_QUESTIONS["mid"])
    questions_by_category[f"Experience ({experience_level.title()})"] = exp_qs[:2]

    # 3. Skill-specific questions (top matched skills)
    skill_q_count = 0
    for skill in detected_skills:
        if skill_q_count >= 3:
            break
        for skill_key, qs in SKILL_QUESTIONS.items():
            if skill_key in skill or skill in skill_key:
                label = skill_key.replace("_", " ").title()
                if label not in questions_by_category:
                    questions_by_category[label] = qs[:3]
                    skill_q_count += 1
                break

    # 4. Domain questions
    domain_qs = DOMAIN_QUESTIONS.get(domain, DOMAIN_QUESTIONS["general"])
    questions_by_category["Domain Knowledge"] = domain_qs[:3]

    # 5. General closing questions
    questions_by_category["General"] = DOMAIN_QUESTIONS["general"][-3:]

    # Build flat list with category labels
    all_questions = []
    for category, qs in questions_by_category.items():
        for q in qs:
            all_questions.append({"category": category, "question": q})

    # Trim to max
    all_questions = all_questions[:max_questions]

    return {
        "total_questions": len(all_questions),
        "experience_level": experience_level,
        "detected_domain": domain,
        "detected_skills": detected_skills[:15],
        "questions": all_questions,
        "categories": questions_by_category,          # {category_label: [question, ...]} — used by the UI
        "category_list": list(questions_by_category.keys()),
    }


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/generate")
async def generate_from_resume(
    file: UploadFile = File(...),
    job_description: Optional[str] = Form(None),
    max_questions: int = Form(20),
    current_user: Optional[dict] = Depends(get_optional_user),
):
    """Upload a resume (PDF/DOCX) and get tailored interview questions."""
    try:
        # Bounded streaming copy, type/content validation and parse (see app/uploads.py).
        parsed = await ingest_resume(file, min_text_chars=50)
        resume_text = parsed.text

        max_questions = max(5, min(50, max_questions))
        # spaCy analysis is CPU-heavy: bounded by the shared AnalysisGate and kept off the event loop.
        result = await get_gate().run(
            _generate_questions, resume_text, job_description, max_questions,
            timeout=float(settings.ANALYSIS_TIMEOUT_SECONDS),
            queue_timeout=resolve_queue_timeout(DEFAULT_QUEUE_TIMEOUT),
        )
        result["filename"] = file.filename
        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Interview generation error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to generate questions.")


@router.post("/generate-from-text")
async def generate_from_text(
    resume_text: str = Form(...),
    job_description: Optional[str] = Form(None),
    max_questions: int = Form(20),
    current_user: Optional[dict] = Depends(get_optional_user),
):
    """Generate interview questions from raw resume text (no file upload needed)."""
    if len(resume_text.strip()) < 50:
        raise HTTPException(status_code=400, detail="Resume text too short.")

    max_questions = max(5, min(50, max_questions))
    return await get_gate().run(
        _generate_questions, resume_text, job_description, max_questions,
        timeout=float(settings.ANALYSIS_TIMEOUT_SECONDS),
        queue_timeout=resolve_queue_timeout(DEFAULT_QUEUE_TIMEOUT),
    )


@router.get("/departments")
async def list_departments():
    """
    Return all 100 programmes grouped by degree type, for a two-level
    cascading picker: choose a degree (B.E., B.Sc., M.Tech. ...) first,
    then the picker's programme list narrows to just that degree.
    """
    data = _load_departments()
    grouped: Dict[str, List[dict]] = {deg: [] for deg in data.get("degree_order", [])}
    for d in data.get("departments", []):
        grouped.setdefault(d["degree"], []).append({
            "id": d["id"],
            "name": d["programme"],
            "full_name": d["full_name"],
            "sno": d["sno"],
        })
    for deg in grouped:
        grouped[deg].sort(key=lambda x: x["sno"])
    return {
        "degree_order": data.get("degree_order", []),
        "grouped": grouped,
        "total_departments": len(data.get("departments", [])),
    }


@router.post("/generate-by-department")
async def generate_by_department(
    department_id: str = Form(...),
    questions_per_category: int = Form(5),
    current_user: Optional[dict] = Depends(get_optional_user),
):
    """
    Return curated Q&A (question + model answer) for a chosen programme,
    pulled from the local Q&A bank — no external calls, works offline.

    Every candidate gets: General/HR + Aptitude & Reasoning (common to all
    100 programmes) plus the technical category (or two) mapped to their
    specific programme.
    """
    dept = _find_department(department_id)
    bank = _load_qa_bank()

    n = max(1, min(20, questions_per_category))

    def _slice(items: List[dict], count: int) -> List[dict]:
        return [{"question": it["q"], "answer": it["a"]} for it in items[:count]]

    categories_out: Dict[str, List[dict]] = {}
    categories_out["General / HR"] = _slice(bank.get("common", []), n)
    categories_out["Aptitude & Reasoning"] = _slice(bank.get("aptitude", []), n)

    for cat_key in dept.get("categories", []):
        cat = bank.get("categories", {}).get(cat_key)
        if not cat:
            continue
        label = f"{cat['label']} (Core)"
        categories_out[label] = _slice(cat.get("items", []), n)

    total = sum(len(v) for v in categories_out.values())

    return {
        "department_id": dept["id"],
        "department": dept["full_name"],
        "degree": dept["degree"],
        "programme": dept["programme"],
        "questions_per_category": n,
        "total_questions": total,
        "categories": categories_out,
        "source_note": bank.get("meta", {}).get("source", ""),
    }


@router.get("/templates")
async def get_templates():
    """Return all available question categories and sample questions."""
    return {
        "skill_categories": list(SKILL_QUESTIONS.keys()),
        "domain_categories": list(DOMAIN_QUESTIONS.keys()),
        "experience_levels": list(EXPERIENCE_QUESTIONS.keys()),
        "total_skill_questions": sum(len(v) for v in SKILL_QUESTIONS.values()),
        "sample": {
            "behavioral": BEHAVIORAL_QUESTIONS[:2],
            "technical": SKILL_QUESTIONS["python"][:2],
        },
    }
