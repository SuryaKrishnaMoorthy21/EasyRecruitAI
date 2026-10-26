"""
EasyRecruit ATS 3.0 — Keyword Extractor v9
Improvements over v8:
  • Indian & South Indian company/skill awareness
  • India-specific domain signals (SAP, COBOL mainframe, fintech India)
  • IT services vs product company differentiation
  • Indian education institution recognition for score bonuses
  • Fresher/entry-level pattern detection (Indian style)
  • Robust error handling on every public method
"""
import re
import json
import logging
import os
from collections import Counter
from typing import Dict, List, Optional, Tuple

import spacy

from app.nlp.text_match import has_term

logger = logging.getLogger(__name__)

_KB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "datasets", "kaggle_skill_kb.json")
_INDIA_KB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "datasets", "india_hiring_kb.json")


def _load_json(path: str, label: str) -> dict:
    try:
        p = os.path.normpath(path)
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        logger.info(f"Loaded {label}: {p}")
        return data
    except FileNotFoundError:
        logger.warning(f"{label} not found at {path}")
        return {}
    except json.JSONDecodeError as e:
        logger.error(f"{label} JSON parse error: {e}")
        return {}
    except Exception as e:
        logger.error(f"{label} load failed: {e}")
        return {}


# ── Candidate-name vs. organisation disambiguation ─────────────────────────
# A line/entity containing any of these tokens is an organisation, not a
# person — this is what stops letterhead text (e.g. "PK Placements Ltd")
# from ever being returned as the candidate's name.
ORG_DENYLIST_TOKENS = {
    "ltd", "llp", "pvt", "private", "limited", "inc", "incorporated",
    "corp", "corporation", "company", "co", "placements", "placement",
    "consultancy", "consultants", "consulting", "recruiters", "recruiter",
    "recruitment", "staffing", "manpower", "services", "solutions",
    "technologies", "technology", "systems", "enterprises", "industries",
    "industry", "group", "global", "associates", "llc", "plc", "agency",
    "agencies", "resources", "outsourcing", "ventures", "holdings",
}
# Job-title / role words — a redacted or unusually-templated resume often
# has a title ("Senior Accountant", "Maintenance Mechanic") sitting exactly
# where a name would be, and no shape rule tells the two apart. Deliberately
# excludes occupational words that are also common surnames (Taylor, Baker,
# Cook, Mason, Carter, Potter, Smith, Miller, Fisher, Hunter, Parker,
# Turner, Walker...) since denylisting those would reject real names.
JOB_TITLE_DENYLIST_TOKENS = {
    "accountant", "manager", "coordinator", "specialist", "officer",
    "director", "mechanic", "worker", "generalist", "engineer",
    "administrator", "consultant", "technician", "supervisor",
    "representative", "associate", "executive", "assistant", "analyst",
    "advocate", "receptionist", "instructor", "trainer", "therapist",
    "nurse", "teacher", "professor", "president", "chef", "clerk",
    "cashier", "driver", "operator", "auditor", "examiner", "inspector",
    "custodian", "attendant", "senior", "sr", "junior", "jr", "lead",
    "chief", "head", "intern", "trainee", "member", "volunteer",
    "substitute", "hygienist", "transporter", "maintainer", "stocker",
    "physician", "designer", "receivable", "stylist", "groomer",
    "paralegal", "bartender", "server", "housekeeper", "custom",
    "replenishment", "dispatcher", "underwriter", "adjuster",
}
# A line containing any of these is a document label / section header, not
# a name, even when it's short enough to otherwise pass.
_NON_NAME_LINE_MARKERS = (
    "resume", "curriculum", "vitae", " cv ", "profile", "summary",
    "bio-data", "biodata", "objective", "personal details", "declaration",
    "address", "contact information", "core qualifications", "career focus",
    "career summary", "career objective", "career highlights", "career overview",
    "professional summary", "executive summary", "qualifications summary",
    "summary of qualifications", "areas of expertise", "key skills",
    "core competencies", "highlights", "professional profile",
    "work experience", "professional experience", "relevant experience",
    "accomplishments", "achievements", "skills summary", "skill highlights",
    "additional information", "technical skills", "interests", "references",
    # Merged from KeywordExtractor.RESUME_SECTIONS (kept as literals here
    # since this runs at module level, before the class exists) plus a few
    # more generic headings found during broad real-resume testing.
    "experience", "work history", "employment", "projects", "certifications",
    "publications", "awards", "activities", "languages", "volunteer",
    "internship", "skills", "academic projects", "software info",
    # Common Indian HR/institutional document titles -- a student uploading
    # the wrong file (an offer letter, a certificate) instead of a resume
    # would have one of these sitting exactly where a name would be.
    "offer letter", "appointment letter", "experience certificate",
    "relieving letter", "joining letter", "internship certificate",
    "recommendation letter", "confirmation letter", "salary certificate",
    "bonafide certificate", "termination letter", "promotion letter",
    "increment letter", "transfer letter", "letter of intent",
)
# Role-based mailbox prefixes — real addresses, but never a specific
# candidate's personal one, so they're ranked below any alternative found.
GENERIC_EMAIL_PREFIXES = {
    "info", "hr", "admin", "contact", "careers", "career", "jobs",
    "placement", "placements", "recruitment", "recruiter", "support",
    "sales", "noreply", "no-reply", "enquiry", "enquiries", "office",
    "helpdesk", "team", "hello", "accounts", "billing",
}

# Shared with EnhancedScorer's JD-keyword matching so "matches" don't get
# credited for incidental overlap on connector/filler words.
STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for", "of", "with",
    "is", "are", "was", "were", "be", "been", "being", "have", "has", "had", "do", "does",
    "did", "will", "would", "could", "should", "may", "might", "shall", "can", "need",
    "that", "this", "these", "those", "it", "its", "they", "their", "them", "we", "our",
    "you", "your", "he", "she", "his", "her", "who", "which", "when", "where", "how", "why",
    "what", "from", "by", "about", "than", "more", "also", "not", "no", "yes", "other",
    "each", "all", "any", "some", "most", "much", "many", "very", "just", "only", "even",
    "i", "my", "me", "myself", "am", "as", "if", "so", "up", "out", "into", "through",
})


# Phrases that essentially never appear in a genuine resume but are
# near-universal in formal HR correspondence and institutional certificates
# -- a much higher-precision signal than any structure/keyword score that
# a document isn't a resume at all, regardless of what it happens to share
# in vocabulary with one.
NON_RESUME_DOCUMENT_MARKERS = (
    "offer letter", "appointment letter", "experience certificate",
    "relieving letter", "joining letter", "internship certificate",
    "letter of recommendation", "recommendation letter",
    "confirmation letter", "salary certificate", "bonafide certificate",
    "termination letter", "promotion letter", "increment letter",
    "transfer letter", "letter of intent", "letter of offer",
    "to whom it may concern", "yours sincerely", "yours faithfully",
    "we are pleased to offer", "we are pleased to inform you",
    "terms and conditions of employment",
)


def detect_formal_letter_marker(text: str) -> Optional[str]:
    """Returns the matched phrase if this document reads as a formal
    letter/certificate rather than a resume, else None. Checked separately
    from (and in addition to) resume-plausibility scoring, since a
    well-written letter can otherwise satisfy several weak resume-ish
    signals (dates, some section-keyword overlap, readable vocabulary) at
    once without actually being a resume."""
    low = text.lower()
    for marker in NON_RESUME_DOCUMENT_MARKERS:
        if marker in low:
            return marker
    return None


def _is_section_marker_line(line: str) -> bool:
    """True if this line is a resume section header / document label. Used
    by the heuristic name-scan to know when to give up rather than wander
    into body text — an org/job-title line just gets skipped (the name may
    still be a line or two further down, e.g. below a letterhead), but a
    section header means we've reached body content and no name follows."""
    low = line.lower()
    return any(marker.strip() in low for marker in _NON_NAME_LINE_MARKERS)


def _looks_like_person_name(candidate: str) -> bool:
    """Shape check for 'is this plausibly a human name' — 2 to 4 word-groups,
    letters/periods/apostrophes/hyphens only, no digits, and no org/document
    marker tokens. Used to gate both NER output and the heuristic fallback
    so a short organisation line can't slip through either path."""
    if not candidate:
        return False
    if "\n" in candidate or "\t" in candidate:
        return False
    words = candidate.strip().split()
    if not (1 < len(words) <= 4):
        return False
    if any(ch.isdigit() for ch in candidate):
        return False
    low = candidate.lower()
    if any(marker.strip() in low for marker in _NON_NAME_LINE_MARKERS):
        return False
    word_tokens = {w.strip(".,").lower() for w in words}
    if word_tokens & ORG_DENYLIST_TOKENS:
        return False
    if word_tokens & JOB_TITLE_DENYLIST_TOKENS:
        return False

    def _valid_name_word(w: str) -> bool:
        if re.match(r"^[A-Za-z]{1,2}\.$", w):   # initial, e.g. "R." or "K."
            return True
        return bool(re.match(r"^[A-Za-z][A-Za-z'\-]*$", w))  # no embedded/trailing periods

    return all(_valid_name_word(w) for w in words)


class KeywordExtractor:
    """
    NLP keyword/skill extractor — v9.
    Powered by Kaggle dataset + Indian hiring KB + curated patterns.
    """

    SKILL_DATABASE: Dict[str, List[str]] = {
        "programming_languages": [
            "python","java","javascript","typescript","c++","c#","ruby","go","golang",
            "rust","swift","kotlin","php","scala","r","matlab","perl","bash","shell",
            "powershell","objective-c","dart","elixir","haskell","lua","cobol","vba",
            "groovy","julia","assembly","solidity","fortran","abap","sas","vb.net",
            "foxpro","pl/sql","t-sql","natural","rpg","cl400",
        ],
        "web_frameworks": [
            "react","reactjs","angular","vue","vuejs","django","flask","fastapi",
            "spring","spring boot","spring mvc","express","expressjs","nextjs","nuxtjs",
            "svelte","ruby on rails","laravel","asp.net",".net","node","nodejs",
            "html","css","sass","less","tailwind","bootstrap","jquery","gatsby",
            "remix","astro","htmx","struts","wicket","vaadin","jhipster",
            "mean stack","mern stack","lamp stack",
        ],
        "databases": [
            "sql","mysql","postgresql","mongodb","redis","elasticsearch","cassandra",
            "oracle","sql server","sqlite","dynamodb","firebase","mariadb","neo4j",
            "snowflake","bigquery","redshift","hive","cockroachdb","supabase","tally",
            "db2","sybase","informix","progress openedge","teradata","greenplum",
        ],
        "cloud_devops": [
            "aws","azure","gcp","google cloud","docker","kubernetes","k8s","jenkins",
            "circleci","github actions","gitlab ci","terraform","ansible","puppet","chef",
            "serverless","lambda","ec2","s3","rds","grafana","prometheus","helm","argocd",
            "heroku","vercel","netlify","datadog","splunk","vmware","linux","unix",
            "windows server","openshift","rancher","citrix","f5","nginx","apache",
            "oci oracle cloud","ibm cloud","alibaba cloud","dynatrace","appdynamics",
            "sonarqube","nexus","artifactory","hashicorp vault",
        ],
        "data_science_ml": [
            "machine learning","deep learning","tensorflow","pytorch","keras",
            "scikit-learn","sklearn","pandas","numpy","matplotlib","seaborn","plotly",
            "nlp","natural language processing","computer vision","neural networks",
            "reinforcement learning","xgboost","lightgbm","catboost","apache spark",
            "hadoop","airflow","dbt","statistics","regression","classification",
            "clustering","transformers","hugging face","langchain","llm","rag",
            "data analysis","data visualization","tableau","power bi","powerbi",
            "excel","jupyter","data mining","feature engineering","a/b testing",
            "generative ai","genai","vector database","pinecone","weaviate",
        ],
        "soft_skills": [
            "leadership","communication","teamwork","problem solving","critical thinking",
            "time management","project management","agile","scrum","kanban","collaboration",
            "mentoring","strategic planning","decision making","analytical","creative",
            "adaptability","interpersonal","organizational","presentation","negotiation",
            "conflict resolution","public speaking","coaching","customer service",
            "stakeholder management","change management","multitasking","detail oriented",
            "client facing","onsite coordination","delivery management","escalation handling",
        ],
        "tools_ides": [
            "git","github","gitlab","bitbucket","jira","confluence","vscode",
            "visual studio","intellij","pycharm","eclipse","postman","swagger","figma",
            "notion","slack","trello","asana","sharepoint","ms office","microsoft office",
            "google workspace","salesforce","hubspot","adp","workday","bamboohr",
            "servicenow","zendesk","freshdesk","freshservice","zoho crm","zoho books",
            "zoho one","manageengine","remedy bmc","hp alm","quality center",
            "rational clearcase","clearquest","tfs","azure devops boards",
        ],
        "testing_qa": [
            "unit testing","integration testing","e2e testing","selenium","pytest",
            "jest","mocha","junit","testng","cypress","playwright","tdd","bdd",
            "test automation","performance testing","quality assurance","quality control",
            "jmeter","loadrunner","hp loadrunner","gatling","appium","robotframework",
            "tosca","ranorex","testcomplete","katalon","soapui","rest assured",
            "postman testing","api testing","mobile testing","cross browser testing",
            "regression testing","smoke testing","sanity testing","uat",
            "defect management","bugzilla","mantis","hp qc","test rail","zephyr",
        ],
        "mobile_development": [
            "ios","android","react native","flutter","xcode","android studio",
            "swiftui","jetpack compose","expo","mobile development","pwa",
            "dart","kotlin android","swift ios","ionic","capacitor",
        ],
        "cybersecurity": [
            "security","penetration testing","vulnerability assessment","encryption",
            "authentication","oauth","ssl","tls","siem","gdpr","hipaa","soc2",
            "firewall","network security","zero trust","compliance","owasp",
            "burp suite","metasploit","nmap","wireshark","kali linux",
            "iso 27001","pci dss","devsecops","sast dast","sca",
            "ceh","cissp","cism","oscp","giac",
        ],
        "finance_accounting": [
            "accounting","auditing","taxation","gst","vat","bookkeeping",
            "financial analysis","financial reporting","financial modeling","financial planning",
            "balance sheet","profit and loss","cash flow","accounts payable","accounts receivable",
            "general ledger","journal entries","bank reconciliation","payroll","budgeting",
            "forecasting","cost accounting","management accounting","cpa","cfa","cma","ca icai",
            "gaap","ifrs","ind as","sox","quickbooks","sap fico","oracle financials","tally erp",
            "investment analysis","portfolio management","risk management","equity research",
            "derivatives","fixed income","bloomberg","kyc","aml","fatca",
            "core banking","finacle","flexcube","temenos","credit risk","market risk",
            "npa management","rbi compliance","sebi regulations","nbfc",
        ],
        "hr_people_ops": [
            "human resources","talent acquisition","recruitment","onboarding",
            "employee relations","performance management","performance appraisal",
            "compensation and benefits","organizational development","hris",
            "workday","bamboohr","adp","payroll","training and development",
            "learning and development","succession planning","diversity equity inclusion",
            "labour law","employment law","hr compliance","headhunting","applicant tracking",
            "hr analytics","workforce planning","employee engagement",
            "posh act","vishaka guidelines","pf esic","gratuity","labour court",
            "campus hiring","lateral hiring","bulk hiring","niche hiring",
        ],
        "healthcare_clinical": [
            "patient care","clinical","nursing","medical","pharmacology","pharmacy",
            "diagnosis","treatment planning","electronic health records","ehr","emr",
            "hipaa","cpr","first aid","bls","acls","phlebotomy","medical coding",
            "icd-10","cpt","telemedicine","physical therapy","occupational therapy",
            "radiology","vital signs","medication administration","triage",
            "public health","epidemiology","infection control",
        ],
        "design_creative": [
            "photoshop","illustrator","indesign","adobe xd","adobe creative suite",
            "sketch","figma","ux design","ui design","wireframing","prototyping",
            "user research","usability testing","motion graphics","video editing",
            "premiere pro","after effects","3d modeling","blender","autocad",
            "typography","color theory","brand identity","print design","web design",
            "graphic design","visual design","logo design","animation","storyboarding",
        ],
        "business_sales": [
            "business development","sales strategy","crm","salesforce","hubspot",
            "digital marketing","seo","sem","ppc","content marketing","social media marketing",
            "email marketing","branding","market research","competitor analysis",
            "business analysis","requirements gathering","public relations","account management",
            "lead generation","pipeline management","b2b sales","b2c sales","enterprise sales",
            "cold calling","proposal writing","contract negotiation","roi analysis",
        ],
        "sap_erp": [
            "sap","sap abap","sap basis","sap fico","sap mm","sap sd","sap pp",
            "sap qm","sap wm","sap hr","sap hcm","sap bw","sap bi","sap hana",
            "sap s/4hana","sap fiori","sap ui5","sap ariba","sap successfactors",
            "sap concur","sap hybris","sap crm","sap srm","sap grc","sap apo",
            "oracle ebs","oracle fusion","oracle hrms","oracle scm","oracle financials",
            "peoplesoft","jd edwards","microsoft dynamics","dynamics 365","dynamics ax",
            "dynamics nav","dynamics crm","sage","epicor","syspro","infor",
            "ramco erp","ramco payroll","ifs applications","unit4","workday financials",
        ],
        "embedded_systems": [
            "embedded c","embedded systems","rtos","freertos","vxworks","qnx",
            "microcontroller","arduino","raspberry pi","stm32","pic","avr",
            "arm cortex","fpga","vhdl","verilog","xilinx","altera",
            "can bus","lin bus","flexray","ethernet","uart","spi","i2c",
            "autosar","misra c","iso 26262","aspice","dspace","vector canoe",
            "matlab simulink","labview","oscilloscope","jtag debugger",
            "plc","scada","hmi","modbus","profibus","opc ua",
            "iot","mqtt","coap","zigbee","lora","bluetooth ble","wifi esp32",
        ],
        "mainframe_legacy": [
            "mainframe","cobol","jcl","cics","vsam","ims db","db2 mainframe",
            "ispf","endevor","pds","jcl scripting","natural adabas",
            "rexx","sas mainframe","ibm z series","mvs","os390","zos",
        ],
    }

    RESUME_SECTIONS = [
        "summary","objective","profile","experience","work history","employment",
        "education","skills","projects","certifications","publications","awards",
        "activities","interests","languages","volunteer","achievements","references",
        "professional experience","work experience","technical skills","core competencies",
        "career objective","key skills","academic projects","internship","declaration",
    ]

    DOMAIN_SIGNALS: Dict[str, List[str]] = {
        "cse": [
            "software","developer","engineer","programming","code","algorithm","database",
            "api","backend","frontend","devops","cloud","network","python","java",
            "javascript","react","node","linux","docker","kubernetes","git","microservices",
        ],
        "it_services": [
            "tcs","infosys","wipro","hcl","tech mahindra","cognizant","capgemini",
            "accenture","ibm","client delivery","onsite offshore","billability",
            "sla","service delivery","support","l1 l2 l3","application support",
        ],
        "sap_erp": [
            "sap","abap","fico","mm","sd","basis","s4hana","fiori","bw","hana",
            "oracle ebs","oracle fusion","peoplesoft","dynamics","erp consulting",
        ],
        "finance": [
            "accounting","finance","audit","tax","cpa","cfa","gaap","ifrs","financial",
            "budget","forecast","investment","equity","portfolio","risk","compliance",
            "banking","credit","bloomberg","quickbooks","payroll","accounts","ledger",
            "core banking","finacle","flexcube","rbi","sebi","npa","nbfc","gst",
        ],
        "hr": [
            "human resources","recruitment","talent","onboarding","hris","employee relations",
            "compensation","benefits","payroll","workforce","training","development",
            "performance","labour","diversity","inclusion","engagement","adp","workday",
            "campus hiring","lateral hiring","posh","esic","pf","gratuity",
        ],
        "healthcare": [
            "patient","clinical","medical","nursing","pharmacy","diagnosis","treatment",
            "ehr","emr","hipaa","cpr","healthcare","hospital","physician","therapy",
            "radiology","medication","triage","public health",
        ],
        "design": [
            "design","photoshop","illustrator","figma","ux","ui","branding","typography",
            "creative","visual","graphic","animation","indesign","sketch","prototype","wireframe",
        ],
        "business": [
            "business development","sales","marketing","crm","salesforce","account management",
            "lead generation","revenue","pipeline","b2b","b2c","proposal","negotiation","growth",
        ],
        "embedded_automotive": [
            "embedded","microcontroller","rtos","can bus","autosar","matlab simulink",
            "vhdl","fpga","arm cortex","automotive","iso 26262","misra","dspace",
            "plc","scada","iot","mqtt","sensor","firmware",
        ],
        "mainframe": [
            "cobol","jcl","cics","vsam","mainframe","ibm z","mvs","zos","endevor",
            "natural","adabas","rexx","db2 mainframe","ims",
        ],
        "construction": [
            "construction","civil","structural","autocad","revit","bim","osha",
            "blueprint","site supervision","estimation","procurement","contractor","hvac",
        ],
        "education": [
            "curriculum","teaching","instruction","classroom","lesson plan","student",
            "learning","assessment","lms","e-learning","differentiated","stem","pedagogy",
        ],
        "ece_eee": [
            # Core ECE/EEE identifiers — strong signals
            "electronics","vlsi","vhdl","verilog","fpga","pcb design","embedded c",
            "microcontroller","stm32","arm cortex","rtos","freertos",
            "signal processing","digital signal processing","communication systems",
            "rf design","antenna design","analog circuits","digital electronics",
            "op-amp","basic electronics","circuit analysis","power electronics",
            "control systems","pid controller","plc","scada","motor drives",
            "matlab simulink","labview","kicad","altium","ltspice","multisim",
            "modulation","fourier","laplace","transistor","diode","mosfet",
            "asic","cadence","synopsys","oscilloscope","breadboard",
            "ece","eee","electrical","electronics and communication",
            "electrical and electronics","power systems","transformers",
            "induction motor","synchronous machine","protection relay",
        ],
        "aviation": [
            "aviation","aircraft","pilot","flight","faa","atpl","cpl","airframe",
            "powerplant","maintenance","air traffic","airways",
        ],
        "mechanical": [
            "mechanical","solidworks","catia","ansys","creo","autocad mechanical",
            "thermodynamics","fluid mechanics","heat transfer","machine design",
            "manufacturing","cnc","metrology","kinematics","dynamics",
            "fea","finite element","gd&t","lean manufacturing","six sigma",
            "production planning","quality control","mech","qs engineering",
        ],
        "civil": [
            "civil","structural","staad pro","etabs","revit structural","sap 2000",
            "rcc design","steel design","soil mechanics","geotechnical",
            "surveying","hydrology","transportation","environmental engineering",
            "quantity surveying","estimation","bill of quantities","bim",
            "is codes","aci codes","construction management",
        ],
        "commerce": [
            "commerce","tally","bookkeeping","gst filing","income tax","taxation",
            "accounts payable","accounts receivable","bank reconciliation",
            "financial reporting","auditing","journal entries","general ledger",
            "bcom","b.com","accounts","payroll processing",
        ],
        "mba": [
            "mba","business administration","strategic planning","business analysis",
            "operations management","supply chain management","marketing management",
            "human resource management","financial management","entrepreneurship",
            "organizational behavior","business strategy","market analysis",
        ],
        "biomedical": [
            "biomedical","medical devices","biomechanics","bioinstrumentation",
            "biosensors","medical imaging","fda regulations","iso 13485",
            "clinical trials","anatomy","physiology","bme","biomedical engineering",
        ],
    }

    # Indian education institutions that get ATS score bonuses
    # Matched as WHOLE WORDS (app/nlp/text_match.py). Task 6 removed the tokens that are not institutions:
    # "karnataka" (a state), "sairam" / "sriram" (common first names) and "lieu" (as in "in lieu of"); the full
    # institution names that contain them are kept.
    PREMIUM_INDIAN_INSTITUTES = [
        "iit","iim","iisc","iiit","nit","bits pilani","iiser",
        "tata institute","aiims","nlaw","xlri","iift","mica",
    ]
    GOOD_INDIAN_INSTITUTES = [
        "vit vellore","manipal","srm","amrita","sastra","anna university",
        "psg college","ceg","kerala university","osmania",
        "jntu","andhra university","cusat","nit trichy","nit calicut",
        "nit warangal","nit surathkal","coimbatore institute",
        "kongu engineering","bannari amman","kumaraguru",
        # Additional Tamil Nadu / South India colleges
        "sri sairam","sairam engineering","sairam institute","sairam college","sriram engineering",
        "loyola","krishna engineering","jerusalem college","karunya",
        "vel tech","saveetha","jeppiaar","karpaga vinayaga",
        "thiagarajar","mepco schlenk","lieu engineering",
        "sona college","kct","kcet","gct coimbatore",
        "government college of engineering",
    ]

    # Indian company tiers for domain detection
    TIER1_IT_COMPANIES = [
        "tcs","infosys","wipro","hcl technologies","tech mahindra",
        "cognizant","capgemini","accenture","ibm india","oracle india",
    ]
    TOP_PRODUCT_COMPANIES_INDIA = [
        "zoho","freshworks","razorpay","phonepe","swiggy","zomato",
        "cred","zepto","meesho","chargebee","postman","browserstack",
        "juspay","cashfree","hasura","darwinbox","leadsquared",
    ]

    def __init__(self, model: Optional[str] = None):
        from app.config import settings
        model = model or settings.SPACY_MODEL
        try:
            self.nlp = spacy.load(model)
            logger.info(f"spaCy loaded: {model}")
        except OSError:
            logger.warning(f"spaCy model '{model}' not found — using blank model.")
            self.nlp = spacy.blank("en")
            if "sentencizer" not in self.nlp.pipe_names:
                self.nlp.add_pipe("sentencizer")

        self._kb = _load_json(_KB_PATH, "Kaggle skill KB")
        self._india_kb = _load_json(_INDIA_KB_PATH, "India hiring KB")
        self._kb_domains = self._kb.get("domains", {})
        self._kb_global = self._kb.get("global_skills", {})
        self._india_skills = self._india_kb.get("india_specific_skills", {})
        self._india_patterns = self._india_kb.get("south_india_hiring_patterns", {})
        self._skill_patterns = self._compile_patterns()
        self._skill_to_cat = self._build_skill_to_cat()
        logger.info(f"KeywordExtractor v9 ready | patterns={len(self._skill_patterns)} | india_kb={bool(self._india_kb)}")

    # ── Pattern compilation ───────────────────────────────────────────────

    def _compile_patterns(self) -> Dict[str, re.Pattern]:
        patterns = {}
        all_skills = []
        for skills in self.SKILL_DATABASE.values():
            all_skills.extend(skills)
        # Add Indian-specific skills
        for cat_skills in self._india_skills.values():
            if isinstance(cat_skills, list):
                all_skills.extend(cat_skills)

        for skill in all_skills:
            key = skill.lower()
            if key not in patterns:
                try:
                    patterns[key] = re.compile(
                        r"(?<![a-zA-Z0-9/])" + re.escape(key) + r"(?![a-zA-Z0-9/])",
                        re.IGNORECASE,
                    )
                except re.error:
                    pass
        return patterns

    def _build_skill_to_cat(self) -> Dict[str, str]:
        m = {}
        for cat, skills in self.SKILL_DATABASE.items():
            for s in skills:
                m[s.lower()] = cat
        # India-specific mappings
        for cat_name, cat_skills in self._india_skills.items():
            if isinstance(cat_skills, list):
                for s in cat_skills:
                    m[s.lower()] = cat_name
        return m

    # ── Domain detection ─────────────────────────────────────────────────

    def detect_domain(self, text: str) -> Tuple[str, float]:
        """
        Detect professional domain from resume/JD text.
        Uses weighted scoring: strong/exclusive signals get 3x weight,
        common signals get 1x. Prevents ECE being mislabelled as CSE.
        """
        try:
            text_lower = text.lower()

            # High-weight exclusive signals — these strongly identify a domain
            # and should override ambiguous matches
            EXCLUSIVE_SIGNALS: Dict[str, List[str]] = {
                "ece_eee":    ["vlsi", "vhdl", "verilog", "fpga", "pcb design",
                               "embedded c", "kicad", "altium", "ltspice",
                               "signal processing", "rf design", "antenna",
                               "ece", "eee", "electronics and communication",
                               "electrical and electronics", "arm cortex",
                               "digital signal processing", "power electronics",
                               "control systems", "induction motor", "oscilloscope",
                               "cadence", "synopsys", "freertos", "rtos"],
                "mechanical": ["solidworks", "catia", "ansys", "creo",
                               "thermodynamics", "fluid mechanics", "heat transfer",
                               "machine design", "cnc", "fea", "gd&t",
                               "six sigma", "lean manufacturing"],
                "civil":      ["staad pro", "etabs", "rcc design", "steel design",
                               "soil mechanics", "geotechnical", "is codes",
                               "quantity surveying", "bim"],
                "sap_erp":   ["sap abap", "sap fico", "sap mm", "sap sd",
                               "s/4hana", "sap fiori", "sap basis"],
                "mainframe": ["cobol", "jcl", "cics", "vsam", "ibm z", "mvs"],
                "finance":   ["tally erp", "gst filing", "ind as", "ifrs",
                              "ca icai", "cfa", "financial modeling"],
                "hr":        ["talent acquisition", "hris", "pf esic",
                              "labour law", "workday", "zoho people"],
                "commerce":  ["bookkeeping", "gst filing", "bcom",
                              "accounts payable", "accounts receivable",
                              "bank reconciliation", "general ledger"],
                "biomedical":["biomedical", "medical devices", "iso 13485",
                              "bme", "biomechanics"],
                "mba":       ["business administration", "mba", "strategic planning",
                              "supply chain management"],
            }

            scores: Dict[str, float] = {}

            # Score with standard signals (weight 1)
            for domain, signals in self.DOMAIN_SIGNALS.items():
                scores[domain] = float(sum(
                    1 for s in signals
                    if re.search(r"(?<![a-zA-Z0-9])" + re.escape(s) + r"(?![a-zA-Z0-9])",
                                 text_lower)
                ))

            # Boost with exclusive signals (weight 3 — these are strong identifiers)
            for domain, excl_signals in EXCLUSIVE_SIGNALS.items():
                excl_hits = sum(
                    1 for s in excl_signals
                    if re.search(r"(?<![a-zA-Z0-9])" + re.escape(s) + r"(?![a-zA-Z0-9])",
                                 text_lower)
                )
                if domain not in scores:
                    scores[domain] = 0.0
                scores[domain] += excl_hits * 3.0

            # ECE vs CSE disambiguation: if both embedded AND electronics signals,
            # and ECE exclusive signals are present, strongly prefer ECE
            ece_exclusive_count = sum(
                1 for s in EXCLUSIVE_SIGNALS.get("ece_eee", [])
                if s in text_lower
            )
            if ece_exclusive_count >= 2:
                scores["ece_eee"] = scores.get("ece_eee", 0) + ece_exclusive_count * 2.0
                # Suppress CSE score if it's only matching generic terms
                if scores.get("cse", 0) < scores.get("ece_eee", 0):
                    scores["cse"] = scores.get("cse", 0) * 0.5

            if not scores or max(scores.values()) == 0:
                return "general", 0.0

            best  = max(scores, key=lambda d: scores[d])
            total = sum(scores.values())
            conf  = round(scores[best] / max(total, 1), 3)
            return best, conf
        except Exception as e:
            logger.error(f"detect_domain error: {e}")
            return "general", 0.0

    def detect_indian_context(self, text: str) -> Dict:
        """Detect Indian job market context — city, company tier, fresher/experienced."""
        try:
            text_lower = text.lower()
            result = {
                "is_indian_resume": False,
                "detected_city": None,
                "detected_tier": None,
                "is_fresher": False,
                "has_premium_institute": False,
                "has_good_institute": False,
                "indian_companies_mentioned": [],
                "india_specific_skills": [],
            }

            # City detection
            city_map = {
                "bengaluru": "bengaluru", "bangalore": "bengaluru",
                "chennai": "chennai", "madras": "chennai",
                "hyderabad": "hyderabad", "secunderabad": "hyderabad",
                "coimbatore": "coimbatore", "kochi": "kochi_trivandrum",
                "cochin": "kochi_trivandrum", "trivandrum": "kochi_trivandrum",
                "thiruvananthapuram": "kochi_trivandrum", "pune": "pune",
                "mumbai": "mumbai", "delhi": "delhi", "noida": "delhi",
                "gurgaon": "delhi", "kolkata": "kolkata",
            }
            for kw, city in city_map.items():
                if kw in text_lower:
                    result["detected_city"] = city
                    result["is_indian_resume"] = True
                    break

            # Institute detection (whole words only: a bare substring test matched "nit " inside "unit tests",
            # "mica" inside "chemical", "iit" inside "HIIT"). These flags are informational: whether they add
            # score points is decided by SCORING_PRESTIGE_BONUSES in the scorer.
            for inst in self.PREMIUM_INDIAN_INSTITUTES:
                if has_term(text_lower, inst):
                    result["has_premium_institute"] = True
                    result["is_indian_resume"] = True
                    break
            for inst in self.GOOD_INDIAN_INSTITUTES:
                if has_term(text_lower, inst):
                    result["has_good_institute"] = True
                    result["is_indian_resume"] = True
                    break

            # Company tier (whole words: "cred" must not match "credit"). A mention is not employment: tool
            # names such as Postman or BrowserStack also appear here, which is one reason this is informational.
            for co in self.TIER1_IT_COMPANIES:
                if has_term(text_lower, co):
                    result["detected_tier"] = "it_services"
                    result["is_indian_resume"] = True
                    result["indian_companies_mentioned"].append(co)
            for co in self.TOP_PRODUCT_COMPANIES_INDIA:
                if has_term(text_lower, co):
                    result["detected_tier"] = "product"
                    result["is_indian_resume"] = True
                    result["indian_companies_mentioned"].append(co)

            # Fresher detection
            fresher_signals = [
                "fresher", "fresh graduate", "0 years", "0-1 year", "trainee",
                "campus", "just graduated", "recently graduated", "no experience",
                "get program", "graduate engineer trainee", "associate engineer",
            ]
            if any(s in text_lower for s in fresher_signals):
                result["is_fresher"] = True

            # Indian-specific skills scan -- reuse the same word-boundary
            # protected patterns used everywhere else, so a skill like
            # "Consul" can't match as a bare substring inside an unrelated
            # word such as "consultancy".
            for cat, skills in self._india_skills.items():
                if isinstance(skills, list):
                    for skill in skills:
                        skill_l = skill.lower()
                        pattern = self._skill_patterns.get(skill_l)
                        matched = pattern.search(text) if pattern else re.search(
                            r"(?<![a-zA-Z0-9/])" + re.escape(skill_l) + r"(?![a-zA-Z0-9/])",
                            text_lower,
                        )
                        if matched:
                            result["india_specific_skills"].append(skill)
                            result["is_indian_resume"] = True

            # Deduplicate
            result["indian_companies_mentioned"] = list(set(result["indian_companies_mentioned"]))
            result["india_specific_skills"] = list(set(result["india_specific_skills"]))[:20]
            return result
        except Exception as e:
            logger.error(f"detect_indian_context error: {e}")
            return {"is_indian_resume": False, "detected_city": None, "detected_tier": None,
                    "is_fresher": False, "has_premium_institute": False, "has_good_institute": False,
                    "indian_companies_mentioned": [], "india_specific_skills": []}

    # ── Skill extraction ─────────────────────────────────────────────────

    def extract_skills(self, text: str) -> Dict:
        """Extract and categorise all skills from text. Never throws."""
        try:
            text_lower = text.lower()
            categorized: Dict[str, List[Dict]] = {cat: [] for cat in self.SKILL_DATABASE}
            categorized["sap_erp"] = []
            categorized["embedded_systems"] = []
            categorized["mainframe_legacy"] = []
            categorized["india_specific"] = []

            seen: set = set()
            for skill, pattern in self._skill_patterns.items():
                try:
                    if pattern.search(text_lower):
                        cat = self._skill_to_cat.get(skill, "general")
                        if cat not in categorized:
                            categorized[cat] = []
                        if skill not in seen:
                            seen.add(skill)
                            freq = len(pattern.findall(text_lower))
                            categorized[cat].append({
                                "skill": skill, "category": cat,
                                "frequency": freq, "confidence": min(0.6 + freq * 0.1, 1.0),
                            })
                except Exception:
                    continue

            # Note: India-specific skills don't need a second scan here --
            # _compile_patterns() already merges them into self._skill_patterns
            # (with the same word-boundary protection as every other skill),
            # so the loop above already finds and categorises them correctly.
            # A separate raw-substring scan used to run here too; it was
            # redundant for genuine matches and, worse, unprotected -- it
            # matched skill tokens as bare substrings inside unrelated words
            # (e.g. "Consul" inside "consultancy").

            total = sum(len(v) for v in categorized.values())
            detected_domain, conf = self.detect_domain(text)
            indian_ctx = self.detect_indian_context(text)
            all_skills = [item for items in categorized.values() for item in items]

            return {
                "categorized_skills": categorized,
                "all_skills": all_skills,
                "total_skills_found": total,
                "detected_domain": detected_domain,
                "domain_confidence": conf,
                "indian_context": indian_ctx,
            }
        except Exception as e:
            logger.error(f"extract_skills error: {e}")
            return {
                "categorized_skills": {}, "all_skills": [], "total_skills_found": 0,
                "detected_domain": "general", "domain_confidence": 0.0,
                "indian_context": {},
            }

    # ── Keyword extraction ───────────────────────────────────────────────

    def full_analysis(self, text: str) -> Dict:
        """
        Full NLP analysis — returns a normalised dict compatible with analysis_routes.
        Added in v9.1 to replace the AttributeError fallback path.
        """
        try:
            skills_info   = self.extract_skills(text)
            keywords_info = self.extract_keywords(text, top_k=20)
            contact_info  = self.extract_contact_info(text)
            structure_info = self.analyze_text_structure(text)

            # Build a flat list of skill names for safe frontend spreading
            flat_skills: List[str] = []
            for cat_skills in skills_info.get("categorized_skills", {}).values():
                if isinstance(cat_skills, list):
                    for item in cat_skills:
                        if isinstance(item, dict):
                            flat_skills.append(item.get("skill", ""))
                        elif isinstance(item, str):
                            flat_skills.append(item)
            flat_skills = [s for s in flat_skills if s]

            return {
                "contact_info":   contact_info,
                "skills":         flat_skills,         # flat list
                "skills_detail":  skills_info,         # full categorised dict
                "keywords":       [k["keyword"] for k in keywords_info if isinstance(k, dict)],
                "candidate_name": contact_info.get("name"),
                "structure":      structure_info,
                "indian_context": skills_info.get("indian_context", {}),
            }
        except Exception as e:
            logger.error(f"full_analysis error: {e}")
            return {
                "contact_info": {}, "candidate_name": None, "structure": {},
                "skills": [], "keywords": [], "indian_context": {},
            }

    def extract_keywords(self, text: str, top_k: int = 30) -> List[Dict]:
        """Extract top keywords from text using spaCy + frequency. Never throws."""
        try:
            STOP = STOPWORDS
            doc = self.nlp(text[:5000])
            freq: Counter = Counter()
            for token in doc:
                w = token.text.lower().strip()
                if len(w) >= 3 and w not in STOP and w.isalpha():
                    freq[w] += 1
            result = []
            counts = freq.most_common(top_k * 2)
            max_freq = counts[0][1] if counts else 1
            for word, count in counts:
                cat = self._skill_to_cat.get(word, "general")
                result.append({
                    "keyword": word, "frequency": count, "category": cat,
                    "score": round(count / max_freq, 3),
                })
                if len(result) >= top_k:
                    break
            return result
        except Exception as e:
            logger.error(f"extract_keywords error: {e}")
            return []

    # ── Text structure analysis ──────────────────────────────────────────

    def analyze_text_structure(self, text: str) -> Dict:
        """Analyze resume structural quality. Never throws."""
        try:
            lines = text.split("\n")
            non_empty = [l for l in lines if l.strip()]
            words = text.split()

            # Section detection -- require the keyword to actually function
            # as a header (start of a line, optionally after a bullet/number
            # marker), not just appear anywhere in running prose. A cover
            # letter saying "...this internship may be extended..." should
            # not count the same as an actual "INTERNSHIP" section header.
            text_lower = text.lower()
            found_sections = [
                s for s in self.RESUME_SECTIONS
                if re.search(r"(?im)^[\s\u2022\-*#>]*" + re.escape(s) + r"\b", text)
            ]

            # Bullet point detection (Indian resumes often use • or - or *)
            bullets = sum(1 for l in lines if re.match(r"^\s*[•\-\*►▪▸→]", l))
            # Date detection (various Indian formats)
            dates = re.findall(
                r"\b(\d{4}|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"
                r"|january|february|march|april|june|july|august|september|october|november|december"
                r"|present|current|till date|to date)\b",
                text_lower
            )
            # Contact info detection
            has_email = bool(re.search(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", text_lower))
            has_phone = bool(re.search(
                r"(\+91|0)?[6-9]\d{9}"                              # Indian mobile
                r"|\+?[1-9]\d{9,14}"                                 # generic contiguous
                r"|\+?\d{1,3}[\s.\-]?\(?\d{2,4}\)?(?:[\s.\-]?\d{2,4}){2,4}",  # e.g. +1 (555) 123-4567
                text
            ))
            # LinkedIn
            has_linkedin = "linkedin" in text_lower
            # GitHub / portfolio
            has_portfolio = any(x in text_lower for x in ["github", "gitlab", "portfolio", "bitbucket"])

            return {
                "total_words": len(words),
                "non_empty_lines": len(non_empty),
                "found_sections": found_sections,
                "section_count": len(found_sections),
                "bullet_count": bullets,
                "date_mentions": len(dates),
                "has_email": has_email,
                "has_phone": has_phone,
                "has_contact_info": has_email or has_phone,
                "has_linkedin": has_linkedin,
                "has_portfolio": has_portfolio,
                "structure_completeness": round(len(found_sections) / max(len(self.RESUME_SECTIONS), 1), 3),
            }
        except Exception as e:
            logger.error(f"analyze_text_structure error: {e}")
            return {
                "total_words": 0, "non_empty_lines": 0, "found_sections": [],
                "section_count": 0, "bullet_count": 0, "date_mentions": 0,
                "has_email": False, "has_phone": False, "has_contact_info": False,
                "has_linkedin": False, "has_portfolio": False, "structure_completeness": 0.0,
            }

    # ── Contact extraction ───────────────────────────────────────────────

    def _ner_person_candidates(self, text: str) -> List[str]:
        """PERSON-entity spans from the top of the document (where a resume's
        own name always lives), filtered to plausible human names that sit
        essentially alone on their line, and ordered earliest-first. A name
        *mentioned* inside a longer sentence (an award, a former employer,
        a reference) is rejected even if the entity span itself looks like
        a name — a real name heading is the whole line, not a fragment of
        one. Returns [] if the loaded spaCy pipeline has no NER component
        (e.g. the blank-model fallback used when en_core_web_sm isn't
        installed)."""
        if "ner" not in getattr(self.nlp, "pipe_names", []):
            return []
        head = text[:800]
        try:
            doc = self.nlp(head)
        except Exception:
            return []
        hits = []
        for ent in doc.ents:
            if ent.label_ != "PERSON":
                continue
            candidate = ent.text.strip()
            if "\n" in ent.text or "\n" in candidate:
                continue
            if not _looks_like_person_name(candidate):
                continue
            line_start = head.rfind("\n", 0, ent.start_char) + 1
            line_end_idx = head.find("\n", ent.end_char)
            line_end = line_end_idx if line_end_idx != -1 else len(head)
            line = head[line_start:line_end].strip()
            if "@" in line or re.search(r"\d{4,}", line):
                continue
            leftover = re.sub(r"[|,\-\u2013\u2014\u00b7•()\s]+", "",
                               line.replace(candidate, "", 1))
            if len(leftover) > 20:
                continue
            hits.append((ent.start_char, candidate))
        hits.sort(key=lambda t: t[0])
        return [c for _, c in hits]

    def extract_contact_info(self, text: str) -> Dict:
        """Extract candidate contact info from resume text. Never throws.

        Name: a spaCy PERSON entity near the top of the document is tried
        first, cross-checked against an organisation-keyword denylist so a
        letterhead/agency line (e.g. "PK Placements Ltd") is never returned
        as the candidate's name. Only if NER finds nothing plausible do we
        fall back to the old first-short-line heuristic — now with the same
        denylist and name-shape check applied. If nothing passes either
        check, name is None rather than a guess.

        Email/phone: every match in the document is collected and ranked,
        not just the first one found, so a generic role-based address
        (info@, hr@, placements@...) doesn't win over the candidate's own
        address when a better one is present. Both the best guess and the
        full ranked list are returned.
        """
        try:
            # ── Emails: collect all, rank, keep best + full ranked list ──
            email_hits = []
            for m in re.finditer(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", text):
                addr = m.group(0)
                local = addr.split("@", 1)[0].lower()
                email_hits.append((local in GENERIC_EMAIL_PREFIXES, m.start(), addr))
            email_hits.sort()  # generic=False sorts first, then earliest position
            all_emails = list(dict.fromkeys(addr for _, _, addr in email_hits))
            best_email = all_emails[0] if all_emails else None

            # ── Phones: collect all, keep best (earliest) + full list ──
            all_phones = list(dict.fromkeys(
                m.group(0) for m in re.finditer(
                    r"(\+91[\s\-]?)?[6-9]\d{4}[\s\-]?\d{5}"
                    r"|\+?[1-9]\d{9,14}"
                    r"|\+?\d{1,3}[\s.\-]?\(?\d{2,4}\)?(?:[\s.\-]?\d{2,4}){2,4}",
                    text
                )
            ))
            best_phone = all_phones[0] if all_phones else None

            # ── Name: NER first, shape-checked heuristic fallback second ──
            name, name_source = None, None
            ner_hits = self._ner_person_candidates(text)
            if ner_hits:
                name, name_source = ner_hits[0], "ner"

            if not name:
                # An org/job-title line just gets skipped -- the real name
                # may be a line or two further down (e.g. below a
                # letterhead). Hitting an actual section header, though,
                # means we've reached body content and should stop rather
                # than keep matching shape rules against prose fragments.
                for line in text.strip().split("\n")[:8]:
                    line = line.strip()
                    if not line:
                        continue
                    if _is_section_marker_line(line):
                        break
                    if "@" in line or re.match(r"^\d", line):
                        continue
                    if len(line.split()) > 5:
                        continue
                    if _looks_like_person_name(line):
                        name, name_source = line, "heuristic"
                        break

            # ── Profile links: GitHub, LinkedIn, portfolio/personal site ──
            github_m = re.search(
                r"(?:https?://)?(?:www\.)?github\.com/[A-Za-z0-9\-_.]+", text, re.IGNORECASE
            )
            linkedin_m = re.search(
                r"(?:https?://)?(?:www\.)?linkedin\.com/in/[A-Za-z0-9\-_%]+", text, re.IGNORECASE
            )
            portfolio_m = re.search(
                r"(?:https?://)?(?:www\.)?(?!github\.com|linkedin\.com)"
                r"[A-Za-z0-9\-]+\.(?:dev|me|io|design|xyz|vercel\.app|netlify\.app|github\.io)"
                r"(?:/[A-Za-z0-9\-_./]*)?",
                text, re.IGNORECASE,
            )

            return {
                "name": name,
                "name_source": name_source,
                "email": best_email,
                "emails": all_emails,
                "phone": best_phone,
                "phones": all_phones,
                "github": github_m.group(0) if github_m else None,
                "linkedin": linkedin_m.group(0) if linkedin_m else None,
                "portfolio": portfolio_m.group(0) if portfolio_m else None,
            }
        except Exception as e:
            logger.error(f"extract_contact_info error: {e}")
            return {
                "name": None, "name_source": None,
                "email": None, "emails": [],
                "phone": None, "phones": [],
                "github": None, "linkedin": None, "portfolio": None,
            }

    def extract_candidate_name(self, text: str) -> Optional[str]:
        """Convenience wrapper for callers/tests that just want the name."""
        return self.extract_contact_info(text).get("name")
