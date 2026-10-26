"""
EasyRecruit ATS 3.0 — Enhanced Scorer v10
Major improvements over v9:
  • ACCURATE domain-matched missing skills — only shows skills actually absent from resume
  • Per-domain skill sets covering: CSE, ECE/EEE, Mechanical, Civil, Commerce, MBA,
    Biomedical, Agriculture, Marine, IT Services, SAP/ERP, Embedded/Automotive,
    Finance, Banking, HR, Healthcare, Design, Business, Mainframe, Education
  • ECE/EEE-specific skills: VLSI, VHDL, Verilog, Signal Processing, PCB, Analog circuits
  • Mechanical-specific: SolidWorks, AutoCAD, FEA, Thermodynamics, Manufacturing
  • Smart recommendations — reads what's ACTUALLY missing vs present in THIS resume
  • Severity-based priorities: high/medium/low based on how many skills are missing
  • Fresher-aware: different advice for 0 exp vs experienced
  • Domain confidence threshold: prevents mislabelling (falls back to general if unclear)
  • Resume text cross-check: missing skills truly absent, not just not in standard list
"""
import math
import os
import re
import socket
import logging
from typing import Dict, List, Optional, Tuple

import numpy as np
from sentence_transformers import SentenceTransformer
from sklearn.metrics.pairwise import cosine_similarity

from app.config import settings
from app.nlp.text_match import has_term, term_pattern

logger = logging.getLogger(__name__)

_MAX_CHUNK = 2048

# ── Score weights (Task 6: named, validated at import, and reported in every result) ──────────────────────
# overall = sum(weight x score) + India-context adjustment, capped at 100. With a job description AND a working
# semantic model the first scheme applies; otherwise the second (it has no semantic term, so a missing model
# never costs a candidate points). Each scheme must total 100%.
SCORE_WEIGHTS_WITH_SEMANTIC = {
    "keyword": 0.20, "skill": 0.25, "structure": 0.15, "semantic": 0.20, "experience": 0.10, "education": 0.10,
}
SCORE_WEIGHTS_NO_SEMANTIC = {
    "keyword": 0.25, "skill": 0.30, "structure": 0.20, "experience": 0.15, "education": 0.10,
}


def _is_finite_number(value) -> bool:
    """True for a real, finite int/float (not bool, not NaN, not +-Infinity)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _validate_weights() -> None:
    for name, weights in (("SCORE_WEIGHTS_WITH_SEMANTIC", SCORE_WEIGHTS_WITH_SEMANTIC),
                          ("SCORE_WEIGHTS_NO_SEMANTIC", SCORE_WEIGHTS_NO_SEMANTIC)):
        total = sum(weights.values())
        if abs(total - 1.0) > 1e-9 or any(w < 0 for w in weights.values()):
            raise ValueError(f"{name} must be non-negative and total 100% (got {total:.6f})")


_validate_weights()

# Connector/filler words that satisfy the old "len >= 4" JD-keyword-match
# check by accident (e.g. "with", "team", "work", "role", "join") but carry
# no signal about whether the candidate actually has a required skill.
# Excluding these stops resumes from scoring "keyword matches" purely from
# shared business-English phrasing.
_FILLER_WORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for", "of", "with",
    "is", "are", "was", "were", "be", "been", "being", "have", "has", "had", "do", "does",
    "did", "will", "would", "could", "should", "may", "might", "shall", "can", "need",
    "that", "this", "these", "those", "it", "its", "they", "their", "them", "we", "our",
    "you", "your", "he", "she", "his", "her", "who", "which", "when", "where", "how", "why",
    "what", "from", "by", "about", "than", "more", "also", "not", "no", "yes", "other",
    "each", "all", "any", "some", "most", "much", "many", "very", "just", "only", "even",
    "team", "work", "works", "working", "role", "join", "years", "year", "looking",
    "required", "requirement", "requirements", "preferred", "must", "strong", "good",
    "excellent", "ability", "including", "such", "well", "using", "used", "use",
})


class EnhancedScorer:
    """
    Weighted ATS scoring engine v10 — India-aware, domain-accurate.

    Weight schemes:
      With JD  → keyword 20%, skill 25%, structure 15%, semantic 20%, exp 10%, edu 10%
      Without  → keyword 25%, skill 30%, structure 20%, exp 15%, edu 10%

    India-context adjustment (additive, capped at SCORING_INDIA_BONUS_CAP, default 15; Task 6):
      Default policy "off" (evidence-only):
        Certifications          +2.5 each, max +8    only when relevant to the job description (always without one)
        India-specific skills   +0.5 each, max +5    only when named in the job description (always without one)
        Institution / employer prestige  +0          detected and shown, but not scored
      Policy "legacy" (SCORING_PRESTIGE_BONUSES=legacy) restores the pre-Task-6 prestige points: premium institute
      (IIT/IIM/IISc) +10, good institute (NIT/IIIT/VIT ...) +5, product company +6, IT-services company +3,
      education-tier bumps +15/+10/+5, and certifications / skills counted whether or not they are relevant.

    The overall score is a screening aid built from heuristics. It has NOT been validated against labelled hiring
    outcomes, and `extraction_confidence` (formerly `accuracy_estimate`) is a parse-completeness heuristic, not a
    measured accuracy. Every result carries `scoring_explanation` saying so.
    """

    # ── Domain-specific required skills (COMPREHENSIVE & ACCURATE per domain) ──

    DOMAIN_REQUIRED_SKILLS: Dict[str, Dict[str, List[str]]] = {

        # ── CSE / Software Engineering ──────────────────────────────────────
        "cse": {
            "programming_languages": ["python", "java", "javascript", "typescript", "c++"],
            "web_frameworks":        ["react", "django", "flask", "node", "spring boot"],
            "databases":             ["sql", "postgresql", "mongodb", "mysql"],
            "cloud_devops":          ["docker", "aws", "git", "linux", "ci/cd"],
            "data_science_ml":       ["machine learning", "pandas", "numpy", "scikit-learn"],
            "tools_ides":            ["git", "github", "jira", "vscode"],
            "testing_qa":            ["unit testing", "pytest", "selenium"],
            "soft_skills":           ["problem solving", "communication", "agile", "teamwork"],
        },

        # ── IT Services (TCS/Infosys/Wipro style) ───────────────────────────
        "it_services": {
            "programming_languages": ["java", "python", "javascript", "sql"],
            "tools_ides":            ["git", "jira", "confluence", "servicenow"],
            "testing_qa":            ["manual testing", "selenium", "api testing", "istqb"],
            "cloud_devops":          ["aws", "azure", "docker", "linux"],
            "soft_skills":           ["communication", "client handling", "agile",
                                     "delivery management", "escalation handling"],
        },

        # ── ECE / EEE — Electronics & Communication / Electrical ─────────────
        "ece_eee": {
            "core_electronics":      ["analog circuits", "digital electronics", "basic electronics",
                                     "circuit analysis", "op-amp", "transistor", "diode",
                                     "rc circuits", "filters"],
            "signals_communication": ["signals and systems", "digital signal processing",
                                     "communication systems", "modulation", "antennas",
                                     "rf design", "fourier transform", "laplace transform"],
            "embedded_hardware":     ["embedded c", "microcontroller", "arduino", "raspberry pi",
                                     "stm32", "arm cortex", "rtos", "freertos", "uart",
                                     "spi", "i2c", "can bus", "iot"],
            "vlsi_fpga":             ["vlsi design", "vhdl", "verilog", "fpga", "xilinx",
                                     "cadence", "synopsys", "cmos", "asic", "layout design"],
            "pcb_tools":             ["pcb design", "kicad", "altium", "eagle", "ltspice",
                                     "multisim", "proteus", "autocad electrical"],
            "power_control":         ["power electronics", "control systems", "pid controller",
                                     "plc", "scada", "motor drives", "transformers",
                                     "power systems", "matlab simulink"],
            "programming_tools":     ["matlab", "python", "c", "labview", "simulink"],
            "soft_skills":           ["problem solving", "analytical", "teamwork",
                                     "technical documentation", "circuit debugging"],
        },

        # ── Mechanical Engineering ───────────────────────────────────────────
        "mechanical": {
            "cad_cam":               ["solidworks", "autocad", "catia", "ansys", "creo",
                                     "inventor", "hypermesh", "unigraphics", "3ds max"],
            "core_mechanical":       ["thermodynamics", "fluid mechanics", "heat transfer",
                                     "machine design", "manufacturing processes", "metrology",
                                     "strength of materials", "kinematics"],
            "manufacturing":         ["cnc programming", "fea", "finite element analysis",
                                     "gd&t", "lean manufacturing", "six sigma", "kaizen",
                                     "production planning", "quality control"],
            "tools_simulation":      ["matlab", "labview", "ansys fluent", "abaqus"],
            "soft_skills":           ["project management", "problem solving", "teamwork",
                                     "technical report writing", "analytical"],
        },

        # ── Civil Engineering ────────────────────────────────────────────────
        "civil": {
            "design_tools":          ["autocad", "staad pro", "etabs", "revit", "primavera",
                                     "ms project", "sap 2000"],
            "core_civil":            ["structural analysis", "rcc design", "steel design",
                                     "soil mechanics", "geotechnical engineering",
                                     "surveying", "hydrology", "transportation engineering",
                                     "environmental engineering"],
            "construction_mgmt":     ["quantity surveying", "estimation", "cost planning",
                                     "bill of quantities", "project scheduling",
                                     "site supervision", "quality assurance"],
            "standards":             ["is codes", "aci codes", "bim", "osha safety"],
            "soft_skills":           ["project management", "leadership", "communication",
                                     "problem solving", "teamwork"],
        },

        # ── SAP / ERP Consulting ─────────────────────────────────────────────
        "sap_erp": {
            "sap_modules":           ["sap", "sap abap", "sap fico", "sap mm", "sap sd",
                                     "sap basis", "sap hana", "sap s/4hana", "sap fiori",
                                     "sap successfactors", "sap ariba"],
            "tools_ides":            ["sap gui", "jira", "ms office", "eclipse"],
            "soft_skills":           ["stakeholder management", "project management",
                                     "communication", "business analysis", "client handling"],
        },

        # ── Mainframe / Legacy ───────────────────────────────────────────────
        "mainframe": {
            "mainframe_legacy":      ["cobol", "jcl", "cics", "db2 mainframe", "vsam",
                                     "rexx", "ispf", "ibm z series", "natural adabas"],
            "soft_skills":           ["problem solving", "analytical", "communication"],
        },

        # ── Embedded / Automotive ────────────────────────────────────────────
        "embedded_automotive": {
            "embedded_systems":      ["embedded c", "rtos", "microcontroller", "arm cortex",
                                     "can bus", "autosar", "misra c", "iso 26262",
                                     "lin bus", "uart", "spi", "i2c"],
            "programming_languages": ["c++", "python", "matlab"],
            "tools_ides":            ["keil", "iar", "gdb", "vector canoe", "dspace",
                                     "matlab simulink", "oscilloscope", "jtag debugger"],
            "soft_skills":           ["problem solving", "analytical", "teamwork",
                                     "technical documentation"],
        },

        # ── Finance / Accounting ─────────────────────────────────────────────
        "finance": {
            "finance_accounting":    ["accounting", "financial analysis", "auditing",
                                     "financial reporting", "budgeting", "forecasting",
                                     "gaap", "ifrs", "ind as", "payroll", "gst", "tds",
                                     "tally erp", "sap fico"],
            "data_tools":            ["excel", "power bi", "tableau", "ms office"],
            "soft_skills":           ["analytical", "communication", "attention to detail",
                                     "problem solving"],
        },

        # ── Banking / Fintech ────────────────────────────────────────────────
        "banking_fintech": {
            "banking_systems":       ["core banking", "finacle", "flexcube", "rbi compliance",
                                     "kyc", "aml", "swift", "neft", "rtgs", "upi",
                                     "payment gateway", "credit risk"],
            "programming_languages": ["java", "python", "sql"],
            "cloud_devops":          ["aws", "docker", "kafka"],
            "soft_skills":           ["compliance", "risk management", "communication"],
        },

        # ── HR / People Ops ──────────────────────────────────────────────────
        "hr": {
            "hr_people_ops":         ["talent acquisition", "recruitment", "onboarding",
                                     "employee relations", "performance management",
                                     "compensation and benefits", "hris", "payroll",
                                     "pf esic", "labour law", "workday", "zoho people"],
            "soft_skills":           ["communication", "leadership", "interpersonal",
                                     "organisational", "conflict resolution"],
        },

        # ── Healthcare / Clinical ────────────────────────────────────────────
        "healthcare": {
            "healthcare_clinical":   ["patient care", "clinical", "ehr", "emr", "hipaa",
                                     "cpr", "medication administration", "diagnosis",
                                     "treatment planning", "vital signs", "triage"],
            "soft_skills":           ["communication", "teamwork", "critical thinking",
                                     "empathy", "adaptability"],
        },

        # ── Design / UI-UX ───────────────────────────────────────────────────
        "design": {
            "design_tools":          ["figma", "photoshop", "illustrator", "adobe xd",
                                     "sketch", "invision", "zeplin", "after effects"],
            "design_skills":         ["ux design", "ui design", "wireframing", "prototyping",
                                     "user research", "usability testing", "typography",
                                     "color theory", "brand identity", "web design"],
            "soft_skills":           ["creative", "communication", "collaboration",
                                     "attention to detail"],
        },

        # ── Business / Sales / Marketing ─────────────────────────────────────
        "business": {
            "business_skills":       ["business development", "sales strategy", "crm",
                                     "market research", "account management",
                                     "lead generation", "negotiation", "b2b sales",
                                     "pipeline management"],
            "tools_ides":            ["salesforce", "hubspot", "ms office", "google analytics"],
            "soft_skills":           ["leadership", "communication", "strategic planning",
                                     "negotiation", "presentation"],
        },

        # ── Commerce / Accounts ──────────────────────────────────────────────
        "commerce": {
            "accounting_finance":    ["accounting", "tally erp", "gst", "taxation",
                                     "bookkeeping", "financial reporting", "auditing",
                                     "ms excel", "payroll", "bank reconciliation",
                                     "accounts payable", "accounts receivable"],
            "soft_skills":           ["analytical", "communication", "attention to detail",
                                     "organisational"],
        },

        # ── MBA / Management ─────────────────────────────────────────────────
        "mba": {
            "management_skills":     ["strategic planning", "business analysis", "project management",
                                     "financial modeling", "market research", "operations management",
                                     "supply chain", "crm", "hrm", "entrepreneurship"],
            "tools":                 ["ms office", "power bi", "tableau", "excel", "salesforce"],
            "soft_skills":           ["leadership", "communication", "decision making",
                                     "negotiation", "teamwork", "presentation"],
        },

        # ── Biomedical Engineering ───────────────────────────────────────────
        "biomedical": {
            "biomedical_core":       ["medical devices", "biomechanics", "bioinstrumentation",
                                     "biosensors", "medical imaging", "fda regulations",
                                     "iso 13485", "clinical trials", "anatomy"],
            "programming_tools":     ["matlab", "python", "labview", "spss"],
            "soft_skills":           ["research", "analytical", "communication", "teamwork"],
        },

        # ── Education / Teaching ─────────────────────────────────────────────
        "education": {
            "teaching_skills":       ["curriculum development", "lesson planning",
                                     "classroom management", "assessment", "e-learning",
                                     "lms", "student evaluation", "pedagogy"],
            "tools_ides":            ["ms office", "google workspace", "zoom", "moodle"],
            "soft_skills":           ["communication", "leadership", "patience",
                                     "creative", "organisational"],
        },

        # ── Construction / Architecture ──────────────────────────────────────
        "construction": {
            "design_tools":          ["autocad", "revit", "sketchup", "3ds max",
                                     "primavera", "ms project"],
            "core_skills":           ["structural design", "site management", "estimation",
                                     "quantity surveying", "project management",
                                     "building codes", "bim", "osha"],
            "soft_skills":           ["project management", "leadership", "communication",
                                     "organisational", "problem solving"],
        },

        # ── General (fallback) ───────────────────────────────────────────────
        "general": {
            "soft_skills":           ["communication", "teamwork", "problem solving", "leadership"],
            "tools_ides":            ["ms office", "google workspace"],
        },
    }

    # ── Skill category weights ────────────────────────────────────────────────
    SKILL_WEIGHTS: Dict[str, float] = {
        "programming_languages":  2.5,
        "web_frameworks":         2.2,
        "databases":              1.8,
        "cloud_devops":           2.0,
        "data_science_ml":        2.5,
        "tools_ides":             1.2,
        "testing_qa":             1.5,
        "soft_skills":            1.0,
        "core_electronics":       2.8,
        "signals_communication":  2.5,
        "embedded_hardware":      2.5,
        "vlsi_fpga":              2.8,
        "pcb_tools":              2.0,
        "power_control":          2.2,
        "programming_tools":      1.8,
        "cad_cam":                2.5,
        "core_mechanical":        2.5,
        "manufacturing":          2.2,
        "tools_simulation":       2.0,
        "design_tools":           2.0,
        "design_skills":          2.5,
        "core_civil":             2.5,
        "construction_mgmt":      2.2,
        "design_tools":           2.0,
        "finance_accounting":     2.3,
        "banking_systems":        2.3,
        "hr_people_ops":          2.0,
        "healthcare_clinical":    2.3,
        "business_skills":        1.8,
        "sap_modules":            2.4,
        "embedded_systems":       2.2,
        "mainframe_legacy":       2.0,
        "management_skills":      2.0,
        "accounting_finance":     2.3,
        "biomedical_core":        2.5,
        "teaching_skills":        2.0,
        "core_skills":            2.0,
        "india_specific":         1.5,
    }

    _ACTION_VERBS = [
        "achieved", "built", "created", "designed", "developed", "enhanced",
        "implemented", "improved", "launched", "led", "managed", "optimised",
        "optimized", "reduced", "scaled", "shipped", "solved", "streamlined",
        "transformed", "delivered", "automated", "increased", "generated",
        "negotiated", "coordinated", "supervised", "mentored", "established",
        "deployed", "migrated", "integrated", "configured", "analysed", "executed",
        "spearheaded", "championed", "conducted", "performed", "resolved",
        "prototyped", "verified", "validated", "tested", "debugged",
    ]

    INDIAN_CERT_PATTERNS = [
        r"aws certified", r"azure certified", r"gcp certified",
        r"sap certified", r"oracle certified", r"salesforce certified",
        r"pmp", r"prince2", r"itil", r"istqb", r"cissp", r"cism", r"ceh",
        r"ca icai", r"cfa", r"cpa", r"frm", r"cma",
        r"ckad", r"cka", r"rhce", r"mcsa", r"mcse",
        r"cisco ccna", r"cisco ccnp", r"cisco ccie",
        r"gate qualified", r"nptel", r"coursera certified",
    ]

    # Which job-description words make a certification relevant to the job (Task 6). Credentials not listed here
    # (MOOC / exam results) are judged by the resume line they appear on: relevant when that line shares a content
    # word with the job description.
    CERT_RELEVANCE_TERMS: Dict[str, List[str]] = {
        r"aws certified": ["aws", "amazon web services"],
        r"azure certified": ["azure"],
        r"gcp certified": ["gcp", "google cloud"],
        r"sap certified": ["sap"],
        r"oracle certified": ["oracle"],
        r"salesforce certified": ["salesforce"],
        r"pmp": ["pmp", "project management", "project manager", "program manager"],
        r"prince2": ["prince2", "project management", "project manager"],
        r"itil": ["itil", "it service management", "service management", "service desk"],
        r"istqb": ["istqb", "testing", "qa", "quality assurance", "test engineer"],
        r"cissp": ["cissp", "security", "infosec", "cybersecurity", "cyber security"],
        r"cism": ["cism", "information security", "security", "governance", "risk"],
        r"ceh": ["ceh", "ethical hacking", "penetration", "security"],
        r"ca icai": ["chartered accountant", "ca", "audit", "accounting", "taxation", "finance"],
        r"cfa": ["cfa", "finance", "investment", "equity research", "portfolio"],
        r"cpa": ["cpa", "accounting", "audit", "taxation", "finance"],
        r"frm": ["frm", "risk", "finance", "treasury"],
        r"cma": ["cma", "management accounting", "cost accounting", "accounting", "finance"],
        r"ckad": ["ckad", "kubernetes", "k8s"],
        r"cka": ["cka", "kubernetes", "k8s"],
        r"rhce": ["rhce", "red hat", "linux", "system administrator", "sysadmin"],
        r"mcsa": ["mcsa", "microsoft", "windows server", "active directory"],
        r"mcse": ["mcse", "microsoft", "windows server", "active directory"],
        r"cisco ccna": ["ccna", "cisco", "network"],
        r"cisco ccnp": ["ccnp", "cisco", "network"],
        r"cisco ccie": ["ccie", "cisco", "network"],
    }
    _CERT_GENERIC_WORDS = frozenset({
        "certified", "certificate", "certification", "course", "courses", "completed", "online", "nptel", "coursera",
        "gate", "qualified", "score", "rank", "exam", "elite", "silver", "gold", "swayam", "through", "from",
    })

    # Degree levels, first match wins (order matters). "be" / "ba" are two-letter forms that collide with ordinary
    # words ("will be", "Samba"), so they only count as the capitalised degree spelling ("BE ", "BA ").
    _DEGREE_LEVELS = [
        ("phd", 95), ("doctorate", 95), ("doctor of", 95),
        ("master", 80), ("msc", 78), ("mba", 78), ("meng", 75), ("mca", 72),
        ("bachelor", 65), ("bsc", 63), ("btech", 63), ("b.tech", 63), ("be", 62),
        ("bca", 58), ("bcom", 55), ("ba", 52), ("degree", 50),
        ("university", 45), ("college", 40), ("institute", 42),
        ("certificate", 38), ("diploma", 30), ("polytechnic", 28),
        ("hsc", 20), ("sslc", 15), ("10th", 15), ("12th", 20),
    ]

    _DIMENSION_LABELS = {
        "keyword": "Keyword match", "skill": "Skill match", "structure": "Resume structure",
        "semantic": "Semantic similarity", "experience": "Experience", "education": "Education",
    }
    _DIMENSION_BASIS = {
        "keyword": "Share of the job description's key terms found in the resume (keyword frequency when there is no job description).",
        "skill": "Weighted share of the required skills found in the resume (derived from the job description, or the domain defaults).",
        "structure": "Standard sections, bullet points, dates, contact details and a sensible length.",
        "semantic": "Embedding similarity between resume and job description, calibrated so raw similarity 0.30 maps to 0 and 0.85 to 100. Only used with a job description and a loaded model.",
        "experience": "Seniority words, stated years of experience, quantified achievements and action verbs.",
        "education": "Highest degree level found in the text. The institution's name does not change this score unless SCORING_PRESTIGE_BONUSES=legacy.",
    }
    _LIMITATIONS = (
        "This score comes from keyword, pattern and (optionally) embedding heuristics. It has not been validated against "
        "labelled hiring outcomes, so it is not a measure of candidate quality or of the score's own accuracy.",
        "Use it to help prioritise a human review, never as the sole basis for a hiring decision.",
    )
    _CONFIDENCE_NOTE = (
        "A heuristic indicator of how completely the resume text could be parsed (name, email, resume-like structure, "
        "domain match). It is NOT a measured accuracy of the score and has not been validated against labelled data."
    )

    # ── Domain tip library (expanded, domain-specific) ────────────────────────
    DOMAIN_TIPS: Dict[str, str] = {
        "cse": (
            "Mention specific technologies, frameworks, and cloud platforms with versions. "
            "Add a GitHub profile with live projects. Highlight LeetCode/competitive programming if fresher."
        ),
        "it_services": (
            "Highlight delivery metrics, SLA adherence, client communication, and specific tools. "
            "Mention billability rate, onsite experience, and ITIL/Agile certifications."
        ),
        "ece_eee": (
            "List embedded systems tools (Keil/IAR), microcontrollers, VLSI tools (Cadence/Xilinx), "
            "and hardware protocols (UART/SPI/I2C/CAN). Add PCB design tools and MATLAB/Simulink experience. "
            "Mention specific ICs, boards (STM32/Arduino), and project hardware used."
        ),
        "mechanical": (
            "List specific CAD software (SolidWorks/CATIA/ANSYS) with project context. "
            "Mention manufacturing processes, certifications (Six Sigma/Lean), and industry internships."
        ),
        "civil": (
            "Include specific design software (AutoCAD/STAAD PRO/ETABS), IS code references, "
            "and project details with estimated values. Add site supervision experience."
        ),
        "sap_erp": (
            "Specify SAP module(s) clearly (FICO, MM, SD, etc.), mention S/4HANA and Fiori experience. "
            "List SAP certifications and end-to-end implementation projects."
        ),
        "mainframe": (
            "Clearly state COBOL versions, JCL expertise, CICS/VSAM experience, "
            "and IBM z-series environment exposure."
        ),
        "embedded_automotive": (
            "List microcontrollers, RTOS platforms, protocols (CAN/LIN/SPI/I2C), "
            "and tools (Keil, IAR, MATLAB/Simulink). Add AUTOSAR and ISO 26262 if applicable."
        ),
        "finance": (
            "Include certifications (CA/ICAI, CFA, CPA, CMA). Mention GST filing, "
            "Tally ERP, and Ind AS compliance. Add specific financial modeling tools."
        ),
        "banking_fintech": (
            "Highlight core banking systems (Finacle/Flexcube), RBI compliance, "
            "and UPI/payment gateway experience for Indian fintech roles."
        ),
        "hr": (
            "Mention HRIS tools (Workday, Zoho People, ADP), PF/ESIC compliance, "
            "and Indian labour law knowledge. Quantify hiring metrics."
        ),
        "healthcare": (
            "Include clinical certifications, EMR/EHR software, and compliance (HIPAA). "
            "For Indian hospitals, mention MCI registration and relevant specialisations."
        ),
        "design": (
            "List your design tools (Figma, Photoshop, Illustrator) in a dedicated skills section. "
            "Add a Behance/Dribbble/portfolio link. Mention specific brand projects."
        ),
        "business": (
            "Quantify sales results (revenue, growth %, deals closed). "
            "Include CRM tools and pipeline management metrics."
        ),
        "commerce": (
            "Mention Tally ERP proficiency level, GST filing experience, and specific accounting software. "
            "Include relevant certifications (CA Foundation/IPCC, CMA) if applicable."
        ),
        "mba": (
            "Quantify business impact — revenue generated, cost saved, team size managed. "
            "Mention specialisation clearly (Finance/Marketing/HR/Operations) and industry exposure."
        ),
        "biomedical": (
            "List specific medical device projects, regulatory standards (FDA/ISO 13485), "
            "and simulation tools (MATLAB/LabVIEW). Mention clinical trial experience if any."
        ),
        "education": (
            "Mention curriculum frameworks, LMS platforms, and grade levels or subjects taught. "
            "Include digital teaching tools and any training certifications."
        ),
        "construction": (
            "Include certifications (IS codes), project values handled, and software "
            "like AutoCAD/Revit/Primavera. Mention OSHA/safety training."
        ),
        "general": (
            "Tailor your resume to the specific job description. "
            "Add a strong professional summary and quantify your achievements."
        ),
    }

    # ── City tips ─────────────────────────────────────────────────────────────
    CITY_TIPS: Dict[str, str] = {
        "bengaluru": (
            "Bengaluru recruiters value GitHub/open-source contributions, "
            "system design skills, and startup exposure."
        ),
        "chennai": (
            "Chennai IT market values SAP/Oracle expertise, Java/J2EE skills, "
            "and automotive IT/embedded domain knowledge."
        ),
        "hyderabad": (
            "Hyderabad employers value Azure/.NET stack, Power Platform, "
            "and pharma/clinical domain knowledge."
        ),
        "coimbatore": (
            "Coimbatore market values embedded systems, ERP (Zoho/Tally), "
            "and manufacturing/textile domain knowledge."
        ),
        "kochi_trivandrum": (
            "Kerala IT hubs value product engineering skills, "
            "Flutter/React Native, and fintech/blockchain experience."
        ),
    }

    def __init__(self, model_name: Optional[str] = None):
        model_name = model_name or settings.SENTENCE_TRANSFORMER_MODEL
        logger.info(f"Loading sentence-transformer: {model_name}")
        self.model = self._load_model_safely(model_name)
        self._extractor = None

    @staticmethod
    def _load_model_safely(model_name: str, reachability_timeout: float = 3.0):
        """
        Load the sentence-transformer model without letting a flaky or
        blocked network connection turn into a multi-minute hang.

        huggingface_hub retries failed requests several times with growing
        backoff (1s, 2s, 4s, 8s, 8s...) for *every* file the model needs
        (config, tokenizer, weights...). On a machine that can't reach
        huggingface.co at all, that adds up to several minutes of a
        seemingly-frozen app before it finally gives up. To avoid that:

          1. Try loading straight from the local cache (HF_HUB_OFFLINE=1).
             This is instant and needs no network at all. It succeeds on
             every run after the first successful download.
          2. If nothing is cached yet, do a quick, cheap reachability probe
             to huggingface.co with a short timeout. If it's unreachable,
             skip straight to disabling semantic scoring instead of letting
             huggingface_hub's slow retry loop run to completion.
          3. If the host looks reachable, attempt the real (network-enabled)
             download.

        Keyword and structure scoring both work without this model, so a
        failure here only disables the semantic-similarity sub-score.
        """
        # 1) Fastest path: already downloaded during a previous run.
        prev_offline = os.environ.get("HF_HUB_OFFLINE")
        try:
            os.environ["HF_HUB_OFFLINE"] = "1"
            model = SentenceTransformer(model_name)
            logger.info("EnhancedScorer v10: sentence-transformer ready (local cache)")
            return model
        except Exception:
            pass  # not cached yet — fall through to the network path below
        finally:
            if prev_offline is None:
                os.environ.pop("HF_HUB_OFFLINE", None)
            else:
                os.environ["HF_HUB_OFFLINE"] = prev_offline

        # 2) Not cached — check the network is actually usable before handing
        #    control to huggingface_hub's slow built-in retry loop.
        try:
            socket.create_connection(("huggingface.co", 443), timeout=reachability_timeout).close()
            reachable = True
        except OSError:
            reachable = False

        if not reachable:
            logger.warning(
                "huggingface.co is unreachable and no local model cache was found. "
                "Semantic-similarity scoring will be disabled for this session; "
                "keyword and structure scoring are unaffected. It will retry "
                "automatically the next time the app is started with a working "
                "internet connection."
            )
            return None

        # 3) Network looks reachable — attempt the real (first-time) download.
        try:
            model = SentenceTransformer(model_name)
            logger.info("EnhancedScorer v10: sentence-transformer ready (downloaded)")
            return model
        except Exception as e:
            logger.error(f"Sentence transformer load failed: {e}. Semantic scoring disabled.")
            return None

    @property
    def extractor(self):
        if self._extractor is None:
            from app.nlp.keyword_extractor import KeywordExtractor
            self._extractor = KeywordExtractor()
        return self._extractor

    # ── Public entry point ────────────────────────────────────────────────────

    def calculate_ats_score(
        self,
        resume_text: str,
        job_description: Optional[str] = None,
        required_skills: Optional[Dict] = None,
    ) -> Dict:
        """Main scoring entry point. Returns full ATS score breakdown. Never raises."""
        if not resume_text or not resume_text.strip():
            return self._empty_result("Empty resume text provided.")

        # Missing-information handling (Task 6): a whitespace-only job description is no job description (it used to
        # run JD-mode keyword scoring against nothing, scoring 0 on 20-25% of the formula), and an empty or
        # non-mapping required_skills falls back to the JD-derived or domain defaults.
        if job_description is not None and not str(job_description).strip():
            job_description = None
        if required_skills is not None and (not isinstance(required_skills, dict) or not required_skills):
            required_skills = None
        warnings: List[str] = []

        resume_lower = resume_text.lower()

        # Domain detection with confidence gating
        try:
            detected_domain, domain_conf = self.extractor.detect_domain(resume_text)
            # If confidence is too low, fall back to general
            if domain_conf < 0.05:
                detected_domain = "general"
        except Exception:
            detected_domain, domain_conf = "general", 0.0

        # Indian context
        try:
            indian_ctx = self.extractor.detect_indian_context(resume_text)
        except Exception:
            indian_ctx = {}

        domain_key = detected_domain

        # Required skills resolution
        if required_skills is None:
            if job_description:
                try:
                    required_skills = self._extract_required_from_jd(job_description, domain_key)
                except Exception:
                    required_skills = self.DOMAIN_REQUIRED_SKILLS.get(
                        domain_key, self.DOMAIN_REQUIRED_SKILLS["general"]
                    )
            else:
                required_skills = self.DOMAIN_REQUIRED_SKILLS.get(
                    domain_key, self.DOMAIN_REQUIRED_SKILLS["general"]
                )

        # Sub-scores (all error-isolated)
        try:
            kw_score = self._keyword_score(resume_text, job_description)
        except Exception as e:
            logger.error(f"keyword_score failed: {e}"); kw_score = 50.0
            warnings.append("The keyword score could not be computed; a neutral placeholder of 50 was used.")

        try:
            sk_score, skill_analysis = self._skill_score_with_breakdown(resume_text, required_skills)
        except Exception as e:
            logger.error(f"skill_score failed: {e}")
            sk_score = 50.0
            skill_analysis = {"matched_skills": [], "missing_skills": [], "categorized_skills": {}}
            warnings.append("The skill score could not be computed; a neutral placeholder of 50 was used.")

        try:
            st_score = self._structure_score(resume_text)
        except Exception as e:
            logger.error(f"structure_score failed: {e}"); st_score = 50.0
            warnings.append("The structure score could not be computed; a neutral placeholder of 50 was used.")

        try:
            exp_score = self._experience_score(resume_text)
        except Exception as e:
            logger.error(f"experience_score failed: {e}"); exp_score = 50.0
            warnings.append("The experience score could not be computed; a neutral placeholder of 50 was used.")

        try:
            edu_score = self._education_score(resume_text)
        except Exception as e:
            logger.error(f"education_score failed: {e}"); edu_score = 25.0
            warnings.append("The education score could not be computed; a neutral placeholder of 25 was used.")

        # A dimension that returns NaN / Infinity (no exception, so the handlers above never saw it) is treated exactly
        # like one that failed: labelled placeholder + warning. Otherwise it would reach the weighted sum, where
        # min()/max() argument order decides whether it silently becomes 0 or a perfect 100 (Task 7).
        for _label, _value, _placeholder in (("keyword", kw_score, 50.0), ("skill", sk_score, 50.0),
                                             ("structure", st_score, 50.0), ("experience", exp_score, 50.0),
                                             ("education", edu_score, 25.0)):
            if not _is_finite_number(_value):
                logger.error(f"{_label}_score was not a finite number; a placeholder was used")
                warnings.append(f"The {_label} score could not be computed; a neutral placeholder of "
                                f"{_placeholder:g} was used.")
                if _label == "keyword":     kw_score = _placeholder
                elif _label == "skill":     sk_score = _placeholder
                elif _label == "structure": st_score = _placeholder
                elif _label == "experience": exp_score = _placeholder
                else:                       edu_score = _placeholder
        if not _is_finite_number(domain_conf):
            domain_conf = 0.0

        # Semantic similarity needs a job description AND a working model. If either is missing, or the model fails
        # at runtime, it is left OUT (sem_score None -> the no-semantic weights apply) rather than scored as 0.
        sem_score: Optional[float] = None
        semantic_status = "not_requested"
        if job_description:
            if not self.model:
                semantic_status = "unavailable"
            else:
                try:
                    sem_score = self._semantic_similarity(resume_text, job_description)
                    semantic_status = "used"
                except Exception as e:
                    logger.error(f"semantic_similarity failed: {e}")
                    semantic_status = "failed"
                    warnings.append("Semantic similarity failed at runtime and was left out of the score.")

        try:
            india_details = self._india_bonus_details(resume_text, indian_ctx, job_description)
            india_bonus = india_details["total"]
            if not _is_finite_number(india_bonus):
                raise ValueError("the India-context adjustment was not a finite number")
        except Exception as e:
            logger.error(f"india bonus failed: {e}")
            india_details = {"policy": self._prestige_policy_name(), "total": 0.0,
                             "cap": float(settings.SCORING_INDIA_BONUS_CAP), "components": []}
            india_bonus = 0.0
            warnings.append("The India-context adjustment could not be computed and was skipped (0 points).")
        # Tell the UI, per result, whether prestige actually changed the score.
        indian_ctx = dict(indian_ctx) if isinstance(indian_ctx, dict) else {}
        _components = {c["name"]: c for c in india_details.get("components", [])}
        indian_ctx["bonus_policy"] = india_details.get("policy")
        indian_ctx["institution_bonus_applied"] = bool(_components.get("institution", {}).get("applied"))
        indian_ctx["employer_bonus_applied"] = bool(_components.get("employer_tier", {}).get("applied"))

        overall = self._final_score(kw_score, sk_score, st_score, sem_score,
                                    exp_score, edu_score, india_bonus)

        # Breakdown builders
        try:
            kw_bd = self._kw_breakdown(resume_text, job_description)
        except Exception:
            kw_bd = {}

        try:
            st_bd = self._structure_breakdown(resume_text)
        except Exception:
            st_bd = {}

        try:
            exp_bd = self._exp_breakdown(resume_text)
        except Exception:
            exp_bd = {}

        try:
            edu_bd = self._edu_breakdown(resume_text)
        except Exception:
            edu_bd = {}

        # Fresher detection
        is_fresher = indian_ctx.get("is_fresher", False) or self._detect_fresher(resume_text)

        recs = self._recommendations(
            kw_score, sk_score, st_score, sem_score,
            exp_score, edu_score, detected_domain,
            indian_ctx, skill_analysis, resume_text, is_fresher
        )

        # ── Plausibility gate: is this even a parseable resume? ────────────
        # A document with no detectable candidate name, no recognised
        # skills, and no resume-shaped sections isn't a *weak* resume — it's
        # not a resume. The floors removed above were what let a document
        # like that still receive a confident score and generic coaching
        # tips; this makes that case explicit instead of silent.
        try:
            _contact_probe = self.extractor.extract_contact_info(resume_text)
        except Exception:
            _contact_probe = {}
        # skill_analysis here is _skill_score_with_breakdown's dict, which
        # exposes a flat matched_skills list of genuine domain-relevant
        # matches -- a single incidental match isn't strong enough evidence
        # on its own to call something a resume.
        _core_skill_count = len(skill_analysis.get("matched_skills", []))
        is_likely_resume = bool(
            _contact_probe.get("name")
            or _core_skill_count >= 3
            or len(st_bd.get("found_sections", [])) > 0
        )

        # A well-written letter can satisfy several of the checks above at
        # once (real dates, some section-keyword overlap, coherent
        # vocabulary) without being a resume at all -- checked separately,
        # as a much higher-precision override, since phrases like "offer
        # letter" or "yours sincerely" essentially never appear in a genuine
        # resume regardless of what else the document happens to contain.
        _formal_letter_marker = None
        try:
            from app.nlp.keyword_extractor import detect_formal_letter_marker
            _formal_letter_marker = detect_formal_letter_marker(resume_text)
        except Exception:
            pass

        letter_cap = None
        if _formal_letter_marker:
            is_likely_resume = False
            if overall > 12.0:
                letter_cap = {
                    "name": "formal_letter_cap", "limit": 12.0, "applied": True,
                    "detail": f'The text contains the phrase "{_formal_letter_marker}", which marks a formal letter or '
                              f"certificate rather than a resume.",
                }
            overall = min(overall, 12.0)
            recs = [{
                "category": "Document Check",
                "suggestion": (
                    f'This reads like a formal letter or certificate (it contains '
                    f'the phrase "{_formal_letter_marker}"), not a resume. '
                    f"Scoring and skill-matching don't meaningfully apply here — "
                    f"please upload the candidate's actual resume/CV instead."
                ),
                "priority": "high",
            }]
        elif not is_likely_resume:
            recs = [{
                "category": "Document Check",
                "suggestion": (
                    "This file doesn't look like a resume — no candidate name, "
                    "recognisable skills, or resume sections (Experience, "
                    "Education, Skills, etc.) were found in the extracted text. "
                    "Double-check the correct file was uploaded before trusting "
                    "this score."
                ),
                "priority": "high",
            }] + recs

        # Extraction confidence (formerly `accuracy_estimate`). This is a heuristic about how completely the text was
        # parsed: a name, an email, resume-like structure and a domain match each add points. It is NOT a measured
        # accuracy and nothing here is compared with ground truth. `accuracy_estimate` is kept as a deprecated alias
        # so existing API consumers keep working.
        _conf_basis = [{"indicator": "text_extracted", "points": 40.0, "detail": "Text was extracted from the file."}]
        _conf_basis.append({"indicator": "candidate_name", "points": 20.0 if _contact_probe.get("name") else 0.0,
                            "detail": "A candidate name was detected." if _contact_probe.get("name") else "No candidate name was detected."})
        _conf_basis.append({"indicator": "email", "points": 15.0 if _contact_probe.get("email") else 0.0,
                            "detail": "An email address was detected." if _contact_probe.get("email") else "No email address was detected."})
        _conf_basis.append({"indicator": "resume_structure", "points": 15.0 if is_likely_resume else 0.0,
                            "detail": "The text has resume-like content." if is_likely_resume else "The text does not look like a resume."})
        _conf_basis.append({"indicator": "domain_match", "points": round(min(domain_conf * 10, 10.0), 1),
                            "detail": "Confidence of the detected job domain (up to 10 points)."})
        extraction_confidence = round(min(100.0, max(15.0, sum(b["points"] for b in _conf_basis))), 1)
        confidence = {
            "value": extraction_confidence,
            "is_measured_accuracy": False,
            "basis": _conf_basis,
            "note": self._CONFIDENCE_NOTE,
        }

        explanation = self._build_explanation(
            job_description=job_description, sem_score=sem_score, semantic_status=semantic_status,
            dim_scores={"keyword": kw_score, "skill": sk_score, "structure": st_score, "semantic": sem_score,
                        "experience": exp_score, "education": edu_score},
            india_details=india_details, india_bonus=india_bonus, overall=round(overall, 1),
            letter_cap=letter_cap, warnings=warnings, confidence=confidence,
        )

        return {
            "overall_score":       round(overall, 1),
            "keyword_match_score": round(kw_score, 1),
            "skill_match_score":   round(sk_score, 1),
            "structure_score":     round(st_score, 1),
            "semantic_similarity": round(sem_score, 1) if sem_score is not None else None,
            "experience_score":    round(exp_score, 1),
            "education_score":     round(edu_score, 1),
            "india_context_bonus": round(india_bonus, 1),
            "detected_domain":     detected_domain,
            "domain_confidence":   domain_conf,
            "indian_context":      indian_ctx,
            "is_likely_resume":    is_likely_resume,
            "extraction_confidence": extraction_confidence,
            "accuracy_estimate":   extraction_confidence,     # deprecated alias of extraction_confidence
            "scoring_explanation": explanation,
            "breakdown": {
                "keywords":        kw_bd,
                "skill_analysis":  skill_analysis,
                "structure":       st_bd,
                "experience":      exp_bd,
                "education":       edu_bd,
                "recommendations": recs,
            },
        }

    # ── Explanation ───────────────────────────────────────────────────────────

    def _build_explanation(
        self, *, job_description, sem_score, semantic_status, dim_scores, india_details, india_bonus,
        overall, letter_cap, warnings, confidence,
    ) -> Dict:
        """Human-readable account of exactly how this overall score was produced."""
        with_semantic = sem_score is not None
        weights = dict(SCORE_WEIGHTS_WITH_SEMANTIC if with_semantic else SCORE_WEIGHTS_NO_SEMANTIC)
        if with_semantic:
            scheme = "jd_with_semantic"
        elif job_description:
            scheme = "jd_without_semantic"
        else:
            scheme = "no_job_description"

        components, weighted_total = [], 0.0
        for name in ("keyword", "skill", "structure", "semantic", "experience", "education"):
            weight = weights.get(name)
            if weight is None:
                continue
            score = float(dim_scores[name])
            points = score * weight
            weighted_total += points
            components.append({
                "dimension": name, "label": self._DIMENSION_LABELS[name], "score": round(score, 1),
                "weight": weight, "points": round(points, 2), "basis": self._DIMENSION_BASIS[name],
            })

        notes: List[str] = []
        if scheme == "no_job_description":
            notes.append("No job description was supplied, so the weights without a semantic term were used "
                         "(keyword 25%, skill 30%, structure 20%, experience 15%, education 10%).")
        elif scheme == "jd_without_semantic":
            if semantic_status == "failed":
                notes.append("Semantic similarity failed at runtime and was left out of the score (it is NOT counted as 0); "
                             "the weights without a semantic term were used instead.")
            else:
                notes.append("Semantic similarity was unavailable (the embedding model is not loaded), so the weights without a "
                             "semantic term were used instead of scoring it as 0.")
        policy = india_details.get("policy")
        if policy == "legacy_prestige":
            notes.append("Prestige bonuses are ENABLED by configuration (SCORING_PRESTIGE_BONUSES=legacy): institution and "
                         "employer prestige can add points that are not evidence about this job.")
        else:
            _c = {c["name"]: c for c in india_details.get("components", [])}
            if _c.get("institution", {}).get("detected") or _c.get("employer_tier", {}).get("detected"):
                notes.append("An institution or employer name was detected. Under the evidence-only policy that adds no "
                             "points: the score reflects skills and experience, not where they were gained.")

        caps = []
        if weighted_total + india_bonus > 100.0:
            caps.append({"name": "maximum_score", "limit": 100.0, "applied": True,
                         "detail": "The weighted total plus the India-context adjustment exceeded 100 and was capped."})
        if letter_cap:
            caps.append(letter_cap)

        return {
            "scheme": scheme,
            "weights": weights,
            "weights_total": round(sum(weights.values()), 6),
            "components": components,
            "weighted_total": round(weighted_total, 2),
            "adjustments": [{
                "name": "india_context", "points": round(india_bonus, 2), "policy": policy,
                "cap": india_details.get("cap"), "components": india_details.get("components", []),
            }],
            "caps": caps,
            "final_score": overall,
            "formula": "overall = min(100, sum(weight x score) + India-context adjustment); "
                       "a document that reads as a formal letter is capped at 12.",
            "notes": notes,
            "warnings": list(warnings),
            "extraction_confidence": confidence,
            "limitations": list(self._LIMITATIONS),
        }

    # ── India bonus ───────────────────────────────────────────────────────────

    @staticmethod
    def _prestige_policy_name() -> str:
        return "legacy_prestige" if settings.SCORING_PRESTIGE_BONUSES == "legacy" else "evidence_only"

    def _cert_relevant(self, pattern: str, resume: str, jd_lower: str) -> bool:
        """Is this certification relevant to the job description? (see CERT_RELEVANCE_TERMS)"""
        terms = self.CERT_RELEVANCE_TERMS.get(pattern)
        if terms is not None:
            return any(has_term(jd_lower, t) for t in terms)
        match = term_pattern(pattern).search(resume)
        if not match:
            return False
        line_start = resume.rfind("\n", 0, match.start()) + 1
        line_end = resume.find("\n", match.end())
        line_end = len(resume) if line_end == -1 else line_end
        ignore = _FILLER_WORDS | self._CERT_GENERIC_WORDS
        line_words = {w for w in re.findall(r"[a-z][a-z+#]{2,}", resume[line_start:line_end].lower()) if w not in ignore}
        return bool(line_words & set(re.findall(r"[a-z][a-z+#]{2,}", jd_lower)))

    def _india_bonus_details(self, resume: str, indian_ctx: Dict, job_description: Optional[str] = None) -> Dict:
        """
        Explainable India-aware adjustment (Task 6). Returns {"policy", "total", "cap", "components"}.

        Evidence-only (default): certifications and India-specific skills earn points, and only when relevant to
        the job description (always when there is none); institution and employer PRESTIGE earn nothing, because
        where someone studied or worked is not evidence about this job. They are still reported in `components`.
        Legacy (SCORING_PRESTIGE_BONUSES=legacy): the pre-Task-6 prestige points, with exact detection.
        """
        legacy = settings.SCORING_PRESTIGE_BONUSES == "legacy"
        cap = float(settings.SCORING_INDIA_BONUS_CAP)
        lower = resume.lower()
        jd_lower = job_description.lower() if job_description else None
        components: List[Dict] = []

        # 1) institution prestige
        premium = bool(indian_ctx.get("has_premium_institute"))
        good = bool(indian_ctx.get("has_good_institute"))
        label = "premium" if premium else "good" if good else None
        points = (10.0 if premium else 5.0 if good else 0.0) if legacy else 0.0
        if label is None:
            reason = "No recognised Indian institution name was found."
        elif legacy:
            reason = "Institution prestige credit (legacy policy)."
        else:
            reason = "Institution name found. Prestige alone adds no points; education is scored from the degree level."
        components.append({"name": "institution", "detected": label, "points": points, "applied": points > 0, "reason": reason})

        # 2) employer tier
        tier = indian_ctx.get("detected_tier")
        points = ({"product": 6.0, "it_services": 3.0}.get(tier, 0.0)) if legacy else 0.0
        if tier is None:
            reason = "No recognised employer name was found."
        elif legacy:
            reason = "Employer-tier credit (legacy policy)."
        else:
            reason = ("Employer name found. A name in the text is not evidence of the skills this job needs "
                      "(tool names such as Postman also match), so it adds no points.")
        components.append({"name": "employer_tier", "detected": tier, "points": points, "applied": points > 0, "reason": reason})

        # 3) certifications (+2.5 each, max +8)
        detected = [p for p in self.INDIAN_CERT_PATTERNS if has_term(lower, p)]
        counted, not_relevant = [], []
        for pattern in detected:
            if legacy or jd_lower is None or self._cert_relevant(pattern, resume, jd_lower):
                counted.append(pattern)
            else:
                not_relevant.append(pattern)
        points = min(2.5 * len(counted), 8.0)
        if not detected:
            reason = "No certification was found."
        elif not_relevant and not counted:
            reason = "Certifications were found but none is relevant to the job description, so they add no points."
        elif not_relevant:
            reason = "Only certifications relevant to the job description were counted."
        else:
            reason = "Certifications count as evidence of skills."
        components.append({"name": "certifications", "detected": detected, "counted": counted,
                           "not_relevant_to_job": not_relevant, "points": points, "applied": points > 0, "reason": reason})

        # 4) India-specific skills (+0.5 each, max +5)
        skills = list(indian_ctx.get("india_specific_skills", []) or [])
        counted_skills = skills if (legacy or jd_lower is None) else [k for k in skills if has_term(jd_lower, k)]
        points = min(len(counted_skills) * 0.5, 5.0)
        if not skills:
            reason = "No India-specific skill was found."
        elif len(counted_skills) < len(skills):
            reason = "Only skills named in the job description were counted."
        else:
            reason = "India-specific skills count as evidence."
        components.append({"name": "india_specific_skills", "detected": skills, "counted": counted_skills,
                           "points": points, "applied": points > 0, "reason": reason})

        total = min(sum(c["points"] for c in components), cap)
        return {"policy": "legacy_prestige" if legacy else "evidence_only", "total": total, "cap": cap,
                "components": components}

    def _india_score_bonus(self, resume: str, indian_ctx: Dict, job_description: Optional[str] = None) -> float:
        """Total India-context adjustment (see _india_bonus_details for the per-component explanation)."""
        return self._india_bonus_details(resume, indian_ctx, job_description)["total"]

    # ── Sub-scorers ───────────────────────────────────────────────────────────

    def _keyword_score(self, resume: str, jd: Optional[str]) -> float:
        if jd:
            matches = self._match_jd_keywords(resume, jd)
            if not matches:
                # No extractable JD terms — no basis to credit the resume.
                return 0.0
            matched = sum(1 for m in matches if m["matched"])
            score = (matched / max(len(matches), 1)) * 100
            resume_lower = resume.lower()
            jd_words = self._tokenise(jd)
            jd_bigrams = [" ".join(jd_words[i:i+2]) for i in range(len(jd_words)-1)]
            jd_trigrams = [" ".join(jd_words[i:i+3]) for i in range(len(jd_words)-2)]
            phrase_hits = sum(1 for p in jd_bigrams + jd_trigrams
                              if p in resume_lower and len(p) > 8)
            return min(score + phrase_hits * 1.5, 100.0)
        kws = self.extractor.extract_keywords(resume, top_k=30)
        if not kws:
            return 0.0
        unique_high = sum(1 for k in kws if k["frequency"] >= 2)
        return min(8 + unique_high * 3.0, 80.0)

    def _skill_score_with_breakdown(
        self, resume: str, required: Dict
    ) -> Tuple[float, Dict]:
        """
        Returns (score, skill_analysis_dict).
        skill_analysis contains per-category matched/missing, and a
        cross-checked missing_skills list (truly absent from resume text).
        """
        extracted = self.extractor.extract_skills(resume)
        resume_lower = resume.lower()

        # Build a rich found-set from extracted skills + raw text scan
        found_set: set = set()
        for cat, skills_list in extracted.get("categorized_skills", {}).items():
            for item in skills_list:
                if isinstance(item, dict):
                    found_set.add(item["skill"].lower())
                elif isinstance(item, str):
                    found_set.add(item.lower())
        for skill in extracted.get("indian_context", {}).get("india_specific_skills", []):
            found_set.add(skill.lower())

        # Also do a direct text scan for multi-word skills
        # (the pattern extractor may miss some domain-specific ones)
        def in_resume(skill_name: str) -> bool:
            s = skill_name.lower()
            if s in found_set:
                return True
            # direct substring check for multi-word phrases
            if re.search(r"(?<![a-z])" + re.escape(s) + r"(?![a-z])", resume_lower):
                return True
            # Check abbreviations / aliases
            aliases = SKILL_ALIASES.get(s, [])
            for alias in aliases:
                if re.search(r"(?<![a-z])" + re.escape(alias) + r"(?![a-z])", resume_lower):
                    return True
            return False

        # Build categorized breakdown
        categorized: Dict = {}
        all_matched: List[str] = []
        all_missing: List[str] = []

        total_weight = 0.0
        matched_weight = 0.0

        for cat, skills in required.items():
            w = self.SKILL_WEIGHTS.get(cat, 1.0)
            matched_in_cat = []
            missing_in_cat = []
            for skill in skills:
                total_weight += w
                if in_resume(skill):
                    matched_in_cat.append(skill)
                    matched_weight += w
                else:
                    missing_in_cat.append(skill)

            categorized[cat] = {
                "matched": matched_in_cat,
                "missing": missing_in_cat,
                "found_in_resume": matched_in_cat,
            }
            all_matched.extend(matched_in_cat)
            all_missing.extend(missing_in_cat)

        score = 0.0 if total_weight == 0 else round(
            min((matched_weight / total_weight) * 100, 100.0), 1
        )

        # Deduplicate preserving order
        all_matched = list(dict.fromkeys(all_matched))
        all_missing = list(dict.fromkeys(all_missing))

        skill_analysis = {
            "total_skills_found":    extracted.get("total_skills_found", 0),
            "categorized_skills":    categorized,
            "matched_skills":        all_matched,
            "missing_skills":        all_missing,
            "detected_domain":       extracted.get("detected_domain", "general"),
            "india_specific_skills": extracted.get("indian_context", {}).get(
                                         "india_specific_skills", []
                                     ),
        }
        return score, skill_analysis

    def _structure_score(self, resume: str) -> float:
        try:
            structure = self.extractor.analyze_text_structure(resume)
        except Exception:
            structure = {}

        score = 10.0
        sections = structure.get("found_sections", [])
        score += min(len(sections) * 6, 30)
        bullets = structure.get("bullet_count", 0)
        score = min(score + bullets * 1.2, score + 15)
        dates = structure.get("date_mentions", 0)
        score = min(score + dates * 1.5, score + 10)
        words = structure.get("total_words", 0)
        if 300 <= words <= 700:
            score = min(score + 10, 100)
        elif words > 700:
            score = min(score + 7, 100)
        if structure.get("has_email"):
            score = min(score + 3, 100)
        if structure.get("has_phone"):
            score = min(score + 3, 100)
        if structure.get("has_linkedin"):
            score = min(score + 3, 100)
        if structure.get("has_portfolio"):
            score = min(score + 4, 100)
        return min(score, 100.0)

    def _experience_score(self, resume: str) -> float:
        lower = resume.lower()
        score = 5.0
        seniority_map = [
            (["vp", "vice president", "director", "cto", "cio", "head of", "general manager"], 35),
            (["principal", "staff engineer", "distinguished", "fellow"], 30),
            (["senior", "lead", "manager", "architect", "consultant"], 22),
            (["mid-level", "module lead", "tech lead", "solution architect"], 18),
            (["junior", "associate", "analyst", "developer", "engineer"], 12),
            (["trainee", "intern", "fresher", "graduate engineer", "get program"], 5),
        ]
        for index, (roles, boost) in enumerate(seniority_map):
            # The executive band (vp / director / cto / cio / head of ...) matches whole words: as bare substrings
            # "vp" matched "VPN", "cto" matched "sector", "director" matched "Active Directory" and "head of"
            # matched "ahead of schedule", each worth +30 points of this dimension.
            hit = any(has_term(lower, r, plural=True) for r in roles) if index == 0 else any(r in lower for r in roles)
            if hit:
                score = min(score + boost, 85.0)
                break
        if any(s in lower for s in ("experience", "work history", "employment",
                                     "professional experience")):
            score = min(score + 10, 85.0)
        years = re.findall(
            r"(\d+)\+?\s*(?:years?|yrs?)\s*(?:of\s+)?(?:experience|exp)", lower
        )
        if years:
            max_yrs = max(int(y) for y in years)
            score = min(score + min(max_yrs * 3, 20), 100.0)
        if re.search(
            r"\b\d+\s*%|\$[\d,]+|₹[\d,]+|\d+\+?\s*(?:projects?|clients?|teams?|employees?|users?|crores?|lakhs?)",
            resume, re.I
        ):
            score = min(score + 12, 100.0)
        av = sum(1 for v in self._ACTION_VERBS if v in lower)
        score = min(score + av * 1.5, 100.0)
        return score

    @staticmethod
    def _degree_present(resume: str, lower: str, keyword: str) -> bool:
        if keyword in ("be", "ba"):
            # only the capitalised degree spelling counts; the lowercase verb "be" used to credit a bachelor's degree
            return re.search(r"(?<![A-Za-z0-9])" + keyword.upper() + r"(?=\s)", resume) is not None
        # whole words: "master" used to match "mastered" / "webmaster", "be " matched "Adobe "
        return has_term(lower, keyword, plural=True)

    @staticmethod
    def _institution_education_bump(lower: str) -> int:
        """Legacy-policy only: the pre-Task-6 prestige bump to the education score (exact whole-word detection)."""
        if any(has_term(lower, t) for t in ("iit", "iim", "iisc", "iiser")):
            return 15
        if any(has_term(lower, t) for t in ("nit", "iiit", "bits pilani", "bits goa", "bits hyderabad")):
            return 10
        if any(has_term(lower, t) for t in ("vit", "manipal", "amrita", "srm", "sastra",
                                            "sri sairam", "sairam engineering")):
            return 5
        return 0

    def _education_score(self, resume: str) -> float:
        """
        Highest degree level found, 0-100 (0 when no education is mentioned). The institution's name does not change
        the score unless SCORING_PRESTIGE_BONUSES=legacy.
        """
        lower = resume.lower()
        for keyword, val in self._DEGREE_LEVELS:
            if self._degree_present(resume, lower, keyword):
                base = float(val)
                if settings.SCORING_PRESTIGE_BONUSES == "legacy":
                    base = min(base + self._institution_education_bump(lower), 100.0)
                return base
        return 0.0

    # Generic sentence-transformer embeddings of two *unrelated* pieces of
    # professional English text routinely land around 0.25-0.35 cosine
    # similarity purely from shared register/vocabulary. Left unscaled,
    # that noise floor reads as a 30-50% "semantic match" even when the
    # content has nothing to do with each other. These two constants map
    # the range where real signal actually lives onto 0-100.
    _SIM_FLOOR = 0.30
    _SIM_CEIL = 0.85

    def _semantic_similarity(self, resume: str, jd: str) -> float:
        """
        Calibrated embedding similarity, 0-100. RAISES when the model cannot produce a usable value (encoder error,
        non-finite similarity, nothing to compare): calculate_ats_score treats that as "semantic unavailable" and
        re-balances the weights. It used to swallow the error and return 0.0, which silently cost a candidate up to
        20 points and was reported as a genuine zero match.
        """
        r_chunks = [resume[i:i+_MAX_CHUNK]
                    for i in range(0, min(len(resume), _MAX_CHUNK*3), _MAX_CHUNK)]
        j_chunks = [jd[i:i+_MAX_CHUNK]
                    for i in range(0, min(len(jd), _MAX_CHUNK*2), _MAX_CHUNK)]
        r_embs = self.model.encode(r_chunks)
        j_embs = self.model.encode(j_chunks)
        sims = [
            float(cosine_similarity([r_e], [j_e])[0][0])
            for r_e in r_embs for j_e in j_embs
        ]
        if not sims:
            raise ValueError("nothing to compare")
        raw = 0.7 * max(sims) + 0.3 * (sum(sims) / len(sims))
        if not math.isfinite(raw):
            raise ValueError("the similarity was not a finite number")
        calibrated = max(0.0, min(1.0, (raw - self._SIM_FLOOR) / (self._SIM_CEIL - self._SIM_FLOOR)))
        return round(calibrated * 100, 2)

    def _final_score(self, kw, sk, st, sem, exp, edu, india_bonus=0.0) -> float:
        if sem is not None:
            w = SCORE_WEIGHTS_WITH_SEMANTIC
            raw = kw*w["keyword"] + sk*w["skill"] + st*w["structure"] + sem*w["semantic"] + exp*w["experience"] + edu*w["education"]
        else:
            w = SCORE_WEIGHTS_NO_SEMANTIC
            raw = kw*w["keyword"] + sk*w["skill"] + st*w["structure"] + exp*w["experience"] + edu*w["education"]
        return round(max(0.0, min(raw + india_bonus, 100.0)), 1)

    # ── Smart Recommendations (v10 — ACCURATE & DYNAMIC) ─────────────────────

    def _recommendations(
        self,
        kw:        float,
        sk:        float,
        st:        float,
        sem:       Optional[float],
        exp:       float,
        edu:       float,
        domain:    str,
        indian_ctx: Dict,
        skill_analysis: Dict,
        resume_text: str,
        is_fresher: bool,
    ) -> List[Dict]:
        """
        Generate accurate, resume-specific recommendations.
        Each recommendation is based on WHAT IS ACTUALLY MISSING from this resume.
        """
        recs = []
        resume_lower = resume_text.lower()

        def add(cat: str, msg: str, pri: str):
            recs.append({"category": cat, "suggestion": msg, "priority": pri})

        missing_skills = skill_analysis.get("missing_skills", [])
        matched_skills = skill_analysis.get("matched_skills", [])
        categorized    = skill_analysis.get("categorized_skills", {})

        # ── 1. SKILLS — show EXACTLY which skills are missing (domain-aware) ──
        if missing_skills:
            # Group missing by category for targeted advice
            high_priority_cats = self._high_priority_cats(domain)
            critical_missing = []
            other_missing    = []

            for cat, data in categorized.items():
                cat_missing = data.get("missing", [])
                if not cat_missing:
                    continue
                if cat in high_priority_cats:
                    critical_missing.extend(cat_missing[:4])  # top 4 per critical cat
                else:
                    other_missing.extend(cat_missing[:2])

            critical_missing = list(dict.fromkeys(critical_missing))[:8]
            other_missing    = list(dict.fromkeys(other_missing))[:5]

            if critical_missing:
                skills_str = ", ".join(
                    s.title() if len(s) <= 4 else s.capitalize()
                    for s in critical_missing
                )
                add(
                    "Skills",
                    f"Critical {domain.upper().replace('_', '/')} skills not found in your resume: "
                    f"{skills_str}. Add these to your Skills section with specific proficiency levels.",
                    "high",
                )

            if other_missing:
                skills_str = ", ".join(
                    s.title() if len(s) <= 4 else s.capitalize()
                    for s in other_missing
                )
                add(
                    "Skills",
                    f"Consider adding these supporting skills to strengthen your profile: {skills_str}.",
                    "medium",
                )

        elif sk >= 80:
            add("Skills", "Strong skills profile! Consider adding proficiency levels "
                "(e.g. Advanced/Intermediate) and version numbers to key tools.", "low")
        elif sk >= 60:
            add("Skills", "Good skill coverage. Add a dedicated 'Technical Skills' section "
                "with categorised tools and technologies for better ATS parsing.", "low")

        # ── 2. KEYWORDS ───────────────────────────────────────────────────────
        if kw < 40:
            add("Keywords",
                "Mirror key phrases directly from the job description — ATS scans for exact matches. "
                "Include acronyms (e.g. REST API, CI/CD, VLSI) as they appear in the job post.",
                "high")
        elif kw < 65:
            add("Keywords",
                "Include more industry-specific terminology from the job post. "
                "Check for domain acronyms and expand abbreviated terms.",
                "medium")

        # ── 3. STRUCTURE ──────────────────────────────────────────────────────
        has_sections = self._check_resume_sections(resume_lower)
        missing_sections = [s for s in ["summary", "experience", "education", "skills", "projects"]
                            if s not in has_sections]

        if missing_sections:
            sec_str = ", ".join(s.title() for s in missing_sections)
            add("Structure",
                f"Your resume is missing these standard sections: {sec_str}. "
                "Add them clearly — ATS systems scan for section headers.",
                "high" if len(missing_sections) >= 3 else "medium")

        if st < 65 and "structure" not in [r["category"].lower() for r in recs]:
            add("Structure",
                "Use bullet points for experience entries and quantify achievements "
                "(%, ₹, numbers, team size, project value).",
                "medium")

        if not re.search(r"\d{2}/\d{4}|\d{4}\s*[-–]\s*(\d{4}|present|current|till date)",
                         resume_text, re.I):
            add("Structure",
                "Add dates (MM/YYYY or YYYY–YYYY format) to all experience and education entries "
                "for ATS compatibility.",
                "low")

        # ── 4. EXPERIENCE ─────────────────────────────────────────────────────
        action_verbs_found = sum(1 for v in self._ACTION_VERBS if v in resume_lower)
        has_numbers = bool(re.search(
            r"\b\d+\s*%|₹[\d,]+|\$[\d,]+|\d+\+?\s*(?:projects?|clients?|teams?|employees?|users?)",
            resume_text, re.I
        ))

        if is_fresher:
            if not re.search(r"project|internship|hackathon|open.?source|github|kaggle|workshop",
                              resume_lower):
                add("Experience",
                    "As a fresher, emphasise: academic projects with tech stack details, "
                    "internships, hackathon participations, and online course certifications. "
                    "Add GitHub/LeetCode/Kaggle profile links.",
                    "high")
            elif action_verbs_found < 5:
                add("Experience",
                    "Start each project/internship bullet with strong action verbs: "
                    "Developed, Implemented, Designed, Built, Automated, Validated.",
                    "medium")
        else:
            if not has_numbers:
                add("Experience",
                    "Quantify your achievements: e.g. 'Reduced build time by 35%', "
                    "'Led a team of 6 engineers', 'Handled ₹2Cr project budget', "
                    "'Delivered 3 modules ahead of schedule'.",
                    "medium")
            elif action_verbs_found < 8:
                add("Experience",
                    "Use stronger action verbs: Architected, Spearheaded, Delivered, "
                    "Optimised, Automated, Migrated, Championed.",
                    "low")

        # ── 5. EDUCATION ──────────────────────────────────────────────────────
        if edu < 40:
            add("Education",
                "Include your full degree title, institution name, graduation year, "
                "and CGPA if above 7.5.",
                "low")

        if edu < 70:
            add("Education",
                "If you have relevant certifications (AWS, Azure, GATE, NPTEL, Coursera), "
                "add them prominently — they are concrete evidence of skills, and they count "
                "toward the India-context adjustment when they match the job description.",
                "low")

        # ── 6. SEMANTIC / RELEVANCE (only if JD provided) ────────────────────
        if sem is not None and sem < 50:
            add("Relevance",
                "Rewrite your Professional Summary to directly address the job requirements. "
                "Mirror the job description's core role, industry, and impact language.",
                "high")
        elif sem is not None and sem < 70:
            add("Relevance",
                "Tailor your experience bullet points to use specific language "
                "from the job posting. Align your achievements with the role's responsibilities.",
                "medium")

        # ── 7. DOMAIN-SPECIFIC TIP ────────────────────────────────────────────
        domain_tip = self.DOMAIN_TIPS.get(domain)
        if domain_tip:
            add("Domain Tip", domain_tip, "medium")

        # ── 8. CONTACT & ONLINE PRESENCE ─────────────────────────────────────
        missing_contact = []
        if "linkedin" not in resume_lower:
            missing_contact.append("LinkedIn URL")
        if not re.search(r"github|gitlab|bitbucket|portfolio", resume_lower):
            missing_contact.append("GitHub/Portfolio link")

        if missing_contact:
            add("Profile",
                f"Add your {' and '.join(missing_contact)} to your resume header — "
                "recruiters and ATS systems value verified online profiles.",
                "low")

        # ── 9. CITY-SPECIFIC TIP ─────────────────────────────────────────────
        city = indian_ctx.get("detected_city")
        if city and city in self.CITY_TIPS:
            add("Location Tip", self.CITY_TIPS[city], "low")

        # ── 10. FRESHER BONUS TIPS ────────────────────────────────────────────
        if is_fresher and not any(r["category"] == "Experience" for r in recs):
            add("Fresher Tip",
                "Highlight academic projects with tech stack details, "
                "NPTEL/Coursera certifications, and internship learnings. "
                "Add your GitHub and competitive programming profiles (LeetCode/CodeChef).",
                "medium")

        # ── Fallback: if everything looks good ───────────────────────────────
        if not recs:
            add("General",
                "Excellent resume! Keep it updated with your latest achievements. "
                "Ensure ATS-friendly formatting: no tables-for-layout, no images in text body, "
                "standard fonts, and clear section headers.",
                "low")

        return recs

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _detect_fresher(self, resume_text: str) -> bool:
        lower = resume_text.lower()
        fresher_signals = [
            "fresher", "fresh graduate", "0 years", "0-1 year", "trainee",
            "recently graduated", "no experience", "get program",
            "graduate engineer trainee", "associate engineer",
            "currently pursuing", "final year", "passout", "pass out",
        ]
        return any(s in lower for s in fresher_signals)

    def _check_resume_sections(self, resume_lower: str) -> List[str]:
        """Return list of standard section headers found in resume."""
        found = []
        for sec in ["summary", "objective", "experience", "education", "skills",
                    "projects", "certifications", "internship", "achievements"]:
            if sec in resume_lower:
                found.append(sec)
        return found

    def _high_priority_cats(self, domain: str) -> List[str]:
        """Return the most important skill categories for a given domain."""
        priority_map = {
            "cse":                ["programming_languages", "web_frameworks", "databases",
                                   "cloud_devops", "data_science_ml"],
            "ece_eee":            ["core_electronics", "embedded_hardware", "vlsi_fpga",
                                   "signals_communication", "programming_tools"],
            "it_services":        ["programming_languages", "testing_qa", "cloud_devops",
                                   "tools_ides"],
            "mechanical":         ["cad_cam", "core_mechanical", "manufacturing"],
            "civil":              ["design_tools", "core_civil", "construction_mgmt"],
            "embedded_automotive":["embedded_systems", "programming_languages", "tools_ides"],
            "sap_erp":            ["sap_modules"],
            "mainframe":          ["mainframe_legacy"],
            "finance":            ["finance_accounting", "data_tools"],
            "banking_fintech":    ["banking_systems", "programming_languages"],
            "hr":                 ["hr_people_ops"],
            "healthcare":         ["healthcare_clinical"],
            "design":             ["design_tools", "design_skills"],
            "business":           ["business_skills"],
            "commerce":           ["accounting_finance"],
            "mba":                ["management_skills"],
            "biomedical":         ["biomedical_core"],
            "education":          ["teaching_skills"],
        }
        return priority_map.get(domain, list(
            self.DOMAIN_REQUIRED_SKILLS.get(domain, self.DOMAIN_REQUIRED_SKILLS["general"]).keys()
        )[:3])

    # ── Breakdown builders ────────────────────────────────────────────────────

    def _kw_breakdown(self, resume: str, jd: Optional[str]) -> Dict:
        top_kw = self.extractor.extract_keywords(resume, top_k=20)
        matched_list = self._match_jd_keywords(resume, jd) if jd else []
        return {
            "top_keywords":       top_kw,
            "keyword_count":      len(top_kw),
            "job_keywords_match": matched_list,
        }

    def _match_jd_keywords(self, resume: str, jd: str) -> List[Dict]:
        try:
            jd_words     = self._tokenise(jd)
            resume_words = set(self._tokenise(resume))
            resume_lower = resume.lower()
            jd_bigrams   = [f"{jd_words[i]} {jd_words[i+1]}"
                            for i in range(len(jd_words)-1)]
            result, seen = [], set()
            for w in jd_words:
                if w in seen or len(w) < 4 or w in _FILLER_WORDS:
                    continue
                seen.add(w)
                matched = w in resume_words or any(
                    w in bg for bg in jd_bigrams if bg in resume_lower
                )
                result.append({"keyword": w, "matched": matched})
                if len(result) >= 30:
                    break
            return result
        except Exception as e:
            logger.error(f"_match_jd_keywords error: {e}")
            return []

    def _structure_breakdown(self, resume: str) -> Dict:
        try:
            structure = self.extractor.analyze_text_structure(resume)
            lower = resume.lower()
            section_detail = {}
            for sec in ["summary", "experience", "education", "skills",
                        "projects", "certifications"]:
                section_detail[sec] = {
                    "present":    sec in lower,
                    "word_count": self._section_word_count(resume, sec),
                }
            structure["section_detail"] = section_detail
            return structure
        except Exception as e:
            logger.error(f"_structure_breakdown error: {e}")
            return {}

    def _exp_breakdown(self, resume: str) -> Dict:
        try:
            lower = resume.lower()
            yoe = re.findall(
                r"(\d+)\+?\s*(?:years?|yrs?)\s*(?:of\s+)?(?:experience|exp)", lower
            )
            return {
                "has_experience_section": any(
                    s in lower for s in ("experience", "work history", "employment")
                ),
                "years_mentioned":     sorted(set(int(y) for y in yoe)),
                "has_senior_roles":    any(
                    w in lower for w in ("senior", "lead", "manager", "director",
                                         "head", "principal")
                ),
                "has_junior_roles":    any(
                    w in lower for w in ("junior", "intern", "entry", "trainee",
                                         "fresher", "get program")
                ),
                "action_verbs_found":  [v for v in self._ACTION_VERBS if v in lower],
                "has_quantified_achievements": bool(re.search(
                    r"\b\d+\s*%|₹[\d,]+|\$[\d,]+|\d+\+?\s*(?:projects?|clients?|employees?|users?)",
                    resume, re.I
                )),
            }
        except Exception as e:
            logger.error(f"_exp_breakdown error: {e}")
            return {}

    def _edu_breakdown(self, resume: str) -> Dict:
        try:
            lower = resume.lower()
            return {
                "has_education_section": "education" in lower,
                "mentions_phd":          any(has_term(lower, k) for k in ("phd", "doctorate", "doctor of")),
                "mentions_masters":      any(has_term(lower, k, plural=True) for k in ("master", "msc", "mba", "mca", "meng")),
                "mentions_bachelors":    (any(has_term(lower, k, plural=True) for k in ("bachelor", "bsc", "btech", "b.tech", "bca", "bcom"))
                                          or self._degree_present(resume, lower, "be")),
                "mentions_certificate":  has_term(lower, "certificate", plural=True),
                "mentions_university":   any(has_term(lower, k, plural=True) for k in ("university", "college", "institute")),
                # informational only: whether these change the score is decided by SCORING_PRESTIGE_BONUSES
                "iit_iim_mentioned":     any(has_term(lower, k) for k in ("iit", "iim", "iisc", "iiser")),
                "nit_iiit_mentioned":    any(has_term(lower, k) for k in ("nit", "iiit", "bits pilani", "bits goa", "bits hyderabad")),
                "tier2_institute":       any(has_term(lower, k) for k in ("vit", "manipal", "amrita", "srm", "sastra",
                                                                         "anna university", "sri sairam", "sairam engineering")),
            }
        except Exception as e:
            logger.error(f"_edu_breakdown error: {e}")
            return {}

    # ── JD parsing ────────────────────────────────────────────────────────────

    def _extract_required_from_jd(self, jd: str, detected_domain: str) -> Dict:
        try:
            base      = self.DOMAIN_REQUIRED_SKILLS.get(
                detected_domain, self.DOMAIN_REQUIRED_SKILLS["general"]
            )
            extracted = self.extractor.extract_skills(jd)
            jd_required: Dict[str, List[str]] = {}
            for cat, skills_list in extracted.get("categorized_skills", {}).items():
                high_conf = [s["skill"] for s in skills_list
                             if isinstance(s, dict) and s.get("confidence", 0) >= 0.6]
                if high_conf:
                    jd_required[cat] = high_conf[:8]
            merged = {}
            for cat in set(list(base.keys()) + list(jd_required.keys())):
                jd_skills   = jd_required.get(cat, [])
                base_skills = base.get(cat, [])
                combined = list(dict.fromkeys(
                    [s.lower() for s in (jd_skills or base_skills)]
                ))
                if combined:
                    merged[cat] = combined[:10]
            return merged if merged else base
        except Exception as e:
            logger.error(f"_extract_required_from_jd error: {e}")
            return self.DOMAIN_REQUIRED_SKILLS.get(
                detected_domain, self.DOMAIN_REQUIRED_SKILLS["general"]
            )

    # ── Utilities ─────────────────────────────────────────────────────────────

    def _empty_result(self, reason: str) -> Dict:
        logger.warning(f"Empty result: {reason}")
        return {
            "overall_score": 0, "keyword_match_score": 0, "skill_match_score": 0,
            "structure_score": 0, "semantic_similarity": None,
            "experience_score": 0, "education_score": 0,
            "india_context_bonus": 0.0,
            "detected_domain": "unknown", "domain_confidence": 0.0,
            "indian_context": {},
            "extraction_confidence": 0.0, "accuracy_estimate": 0.0,
            "scoring_explanation": {
                "scheme": "none", "weights": {}, "components": [], "adjustments": [], "caps": [],
                "notes": [reason], "warnings": [], "limitations": list(self._LIMITATIONS),
            },
            "breakdown": {
                "recommendations": [{"category": "Error", "suggestion": reason, "priority": "high"}],
                "skill_analysis":  {"matched_skills": [], "missing_skills": []},
            },
            "error": reason,
        }

    @staticmethod
    def _tokenise(text: str) -> List[str]:
        words = re.split(r"[\s,;.:()[\]\"']+", text.lower())
        return [w for w in words if len(w) > 3]

    @staticmethod
    def _section_word_count(text: str, section: str) -> int:
        try:
            m = re.search(
                rf"{section}[:\s]*(.+?)(?=\n\s*\n|\n[A-Z]|\Z)", text,
                re.DOTALL | re.IGNORECASE,
            )
            return len(m.group(1).split()) if m else 0
        except Exception:
            return 0


# ── Skill aliases dictionary (for cross-checking alternate names) ─────────────
SKILL_ALIASES: Dict[str, List[str]] = {
    # ECE/EEE aliases
    "embedded c":            ["embedded c programming", "c for embedded"],
    "vlsi design":           ["vlsi", "chip design", "ic design"],
    "fpga":                  ["fpga programming", "fpga development"],
    "vhdl":                  ["vhsic hardware description language"],
    "matlab simulink":       ["matlab", "simulink"],
    "pcb design":            ["pcb layout", "pcb routing", "pcb fabrication"],
    "signals and systems":   ["signal processing", "dsp", "signals & systems"],
    "control systems":       ["control theory", "pid", "feedback control"],
    "power electronics":     ["power systems", "power conversion"],
    "arm cortex":            ["arm processor", "arm architecture"],
    "microcontroller":       ["mcu", "pic", "avr", "stm32", "arduino"],
    "can bus":               ["can protocol", "controller area network"],
    # CSE aliases
    "machine learning":      ["ml", "deep learning", "ai", "artificial intelligence"],
    "javascript":            ["js", "ecmascript"],
    "postgresql":            ["postgres"],
    "docker":                ["containerization", "containers"],
    "ci/cd":                 ["cicd", "continuous integration", "continuous deployment"],
    "node":                  ["nodejs", "node.js"],
    "react":                 ["reactjs", "react.js"],
    # Mechanical aliases
    "solidworks":            ["solid works", "sw cad"],
    "finite element analysis":["fea", "fem", "ansys", "abaqus"],
    "cnc programming":       ["cnc machining", "g-code"],
    "six sigma":             ["6 sigma", "lean six sigma"],
    # Finance aliases
    "tally erp":             ["tally", "tally.erp9", "tally prime"],
    "financial analysis":    ["financial modeling", "financial modelling"],
    # HR aliases
    "talent acquisition":    ["recruitment", "hiring", "staffing"],
    # General
    "ms office":             ["microsoft office", "office 365", "ms word", "ms excel"],
    "google workspace":      ["google suite", "g suite", "google docs"],
}
