"""
EasyRecruit ATS 3.0 - Configuration
All settings sourced from environment variables with sane defaults.
"""
import ipaddress
import logging
import os
import re
import secrets
from pathlib import Path
from urllib.parse import urlsplit
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

logger = logging.getLogger(__name__)

MIN_SECRET_KEY_LENGTH = 32
_PLACEHOLDER_MARKERS = (
    "change-me", "changeme", "change_me", "replace", "your-", "your_",
    "placeholder", "example", "please-use", "secret-key-here",
)


def secret_key_problem(value):
    """Return why a SECRET_KEY is unacceptable, or None if it is fine."""
    if not value or not value.strip():
        return "SECRET_KEY is not set"
    if len(value) < MIN_SECRET_KEY_LENGTH:
        return f"SECRET_KEY is shorter than {MIN_SECRET_KEY_LENGTH} characters"
    low = value.lower()
    if any(marker in low for marker in _PLACEHOLDER_MARKERS):
        return "SECRET_KEY is a placeholder value"
    return None


def resolve_secret_key(raw, debug):
    """
    Fail closed: outside DEBUG a missing, short or placeholder key aborts startup,
    because anyone who knows the key can forge login tokens (including admin).
    In DEBUG a weak key is allowed with a warning, and a missing key becomes a
    random per-process key, so no usable secret ever lives in source code.
    """
    problem = secret_key_problem(raw)
    if problem is None:
        return raw
    if not debug:
        raise RuntimeError(
            f"{problem}. Refusing to start with DEBUG=false. Generate one with: "
            "python -c \"import secrets; print(secrets.token_hex(32))\" "
            "and set it as the SECRET_KEY environment variable."
        )
    logger.warning(f"{problem}; DEBUG=true so continuing. Never run production like this.")
    return raw if (raw and raw.strip()) else secrets.token_hex(32)


# ── CORS origin validation ───────────────────────────────────────────────────
class CorsConfigError(RuntimeError):
    """
    Raised at startup for a malformed or unsafe CORS_ORIGINS. Messages name the
    entry NUMBER and the problem but never echo a malformed value (it could be a
    mis-pasted secret or a URL with credentials in it).
    """


_DEV_CORS_EXTRA_PORTS = (3000, 5173, 5500)      # common local static/dev servers
_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def normalize_cors_origin(entry, position):
    """
    Return the canonical form of one CORS_ORIGINS entry: "*" or "scheme://host[:port]"
    (lower-case, default ports removed, trailing slash dropped). Raises CorsConfigError
    for anything that is not an exact http(s) origin.
    """
    item = (entry or "").strip()
    if item == "*":
        return "*"
    where = f"CORS_ORIGINS entry #{position}"
    hint = "expected an exact origin such as https://app.example.com (scheme://host[:port], no path)"
    if re.search(r"\s", item):
        raise CorsConfigError(f"{where} contains whitespace; {hint}")
    if item.lower() == "null":
        raise CorsConfigError(f"{where} is 'null'; the null origin (sandboxed iframes, file://) must never be trusted")
    if "*" in item:
        raise CorsConfigError(f"{where} contains a wildcard; only exact origins are supported ({hint})")
    if any(c in item for c in "?#\\"):
        raise CorsConfigError(f"{where} must not contain a query, fragment or backslash; {hint}")
    try:
        parts = urlsplit(item)
    except ValueError:
        raise CorsConfigError(f"{where} is not a valid URL; {hint}") from None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.netloc:
        raise CorsConfigError(f"{where} must start with http:// or https://; {hint}")
    if "@" in parts.netloc:
        raise CorsConfigError(f"{where} must not contain credentials (user:password@); {hint}")
    if parts.path not in ("", "/"):
        raise CorsConfigError(f"{where} must not include a path; {hint}")
    host = parts.hostname
    if not host:
        raise CorsConfigError(f"{where} has no host; {hint}")
    try:
        port = parts.port
    except ValueError:
        raise CorsConfigError(f"{where} has an invalid port; {hint}") from None
    if port is not None and not 1 <= port <= 65535:
        raise CorsConfigError(f"{where} has an invalid port; {hint}")
    if not host.isascii():
        raise CorsConfigError(f"{where} has non-ASCII characters in the host; use the punycode (xn--) form")
    try:
        ip = ipaddress.ip_address(host)
        host_out = f"[{host}]" if ip.version == 6 else host
    except ValueError:
        if not all(_HOST_LABEL.match(label) for label in host.split(".")):
            raise CorsConfigError(f"{where} has a host that is not a valid DNS name; {hint}") from None
        host_out = host
    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        port = None                                   # browsers omit default ports in the Origin header
    return f"{scheme}://{host_out}" + (f":{port}" if port else "")


def parse_cors_origins(raw):
    """Comma-separated CORS_ORIGINS -> de-duplicated canonical list. Blank items are ignored."""
    origins, seen = [], set()
    if not raw:
        return origins
    for position, piece in enumerate(str(raw).split(","), start=1):
        if not piece.strip():
            continue
        origin = normalize_cors_origin(piece, position)
        if origin not in seen:
            seen.add(origin)
            origins.append(origin)
    return origins


def cors_origins_problems(origins, debug):
    """Well-formed but unsafe settings. In production only explicit https origins are acceptable."""
    problems = []
    if not debug:
        if "*" in origins:
            problems.append("a wildcard origin ('*') is not allowed when DEBUG=false")
        problems += [f"{o} uses http:// but DEBUG=false requires https://" for o in origins if o.startswith("http://")]
    return problems


def dev_cors_origins(port):
    """Default allowlist for local development (DEBUG=true and CORS_ORIGINS unset): loopback origins only."""
    ports = [port] + [p for p in _DEV_CORS_EXTRA_PORTS if p != port]
    return [f"http://{host}:{p}" for p in ports for host in ("localhost", "127.0.0.1")]


def resolve_cors_origins(raw, debug, port=8001):
    """
    Fail closed. Production (DEBUG=false): CORS_ORIGINS must be empty (same-origin only: the app
    serves its own frontend) or an explicit list of https origins; '*', 'null', http:// and malformed
    entries abort startup. Development (DEBUG=true): unset -> loopback origins; '*' is tolerated with
    a warning (credentials are always disabled for it, see cors_allow_credentials).
    """
    origins = parse_cors_origins(raw)                 # malformed entries raise in every mode
    problems = cors_origins_problems(origins, debug)
    if problems:
        raise CorsConfigError(
            "Refusing to start: " + "; ".join(problems) + ". Set CORS_ORIGINS to a comma-separated list of "
            "exact https origins (e.g. https://app.example.com), or leave it empty when this app serves its "
            "own frontend (same origin). Wildcards are only accepted with DEBUG=true."
        )
    if not origins:
        if debug:
            origins = dev_cors_origins(port)
            logger.info("CORS_ORIGINS is not set and DEBUG=true: allowing local development origins only.")
        else:
            logger.info("CORS_ORIGINS is not set: cross-origin browser access is disabled (same-origin only).")
    elif "*" in origins:
        logger.warning("CORS_ORIGINS contains '*' (accepted only because DEBUG=true): any website can call "
                       "this API; credentials are disabled. Never use this in production.")
    return origins


def cors_allow_credentials(origins):
    """Credentialed cross-origin access only for an explicit allowlist, never together with '*'."""
    return bool(origins) and "*" not in origins


def describe_cors(origins):
    """One-line, secret-free summary of the effective policy for the startup log."""
    if not origins:
        return "same-origin only (cross-origin browser access disabled)"
    if "*" in origins:
        return "ANY origin (wildcard, DEBUG only; credentials disabled)"
    return f"{len(origins)} trusted origin(s): " + ", ".join(origins)


# ── Upload / analysis resource limits ────────────────────────────────────────
class ResourceLimitConfigError(RuntimeError):
    """
    Raised at startup for an unusable upload/analysis limit (non-numeric, or outside a sane range).
    Fails closed like the SECRET_KEY and CORS checks: a typo must never silently disable a limit.
    """


REQUEST_OVERHEAD_BYTES = 1024 * 1024   # multipart framing + the small text fields that ride along with a file


def read_int_setting(name, default, *, minimum, maximum, environ=None):
    """
    Integer setting from the environment. Unset/blank -> `default`. Anything non-numeric or outside
    [minimum, maximum] aborts startup with a message naming the variable.
    """
    raw = (os.environ if environ is None else environ).get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(str(raw).strip())
    except ValueError:
        raise ResourceLimitConfigError(
            f"{name} must be a whole number (got a non-numeric value). Unset it to use the default ({default})."
        ) from None
    if not minimum <= value <= maximum:
        raise ResourceLimitConfigError(
            f"{name}={value} is outside the allowed range {minimum}..{maximum}. Unset it to use the default ({default})."
        )
    return value



class ScoringConfigError(RuntimeError):
    """Raised at startup for an unusable scoring-policy setting (fails closed, like the other config checks)."""


def read_choice_setting(name, default, choices, *, environ=None):
    """Setting that must be one of `choices` (case-insensitive). Unset/blank -> `default`."""
    raw = (os.environ if environ is None else environ).get(name)
    if raw is None or not str(raw).strip():
        return default
    value = str(raw).strip().lower()
    if value not in choices:
        raise ScoringConfigError(
            f"{name} must be one of {sorted(choices)} (got {value!r}). Unset it to use the default ({default!r})."
        )
    return value


def describe_scoring_policy(s):
    """One-line summary of the scoring policy for the startup log."""
    if s.SCORING_PRESTIGE_BONUSES == "legacy":
        return (f"prestige bonuses ENABLED (legacy): institution/employer prestige adds points; "
                f"India bonus cap {s.SCORING_INDIA_BONUS_CAP}")
    return (f"evidence-only: institution/employer prestige adds no points; certifications count only when relevant "
            f"to the job; India bonus cap {s.SCORING_INDIA_BONUS_CAP}")


def describe_upload_limits(s):
    """One-line, secret-free summary of the effective upload/analysis limits for the startup log."""
    mb = lambda n: f"{n / (1024 * 1024):g} MB"                                  # noqa: E731
    return (
        f"<= {mb(s.MAX_FILE_SIZE)}/file, <= {s.MAX_BATCH_FILES} files and <= {mb(s.MAX_BATCH_REQUEST_SIZE)}/batch request; "
        f"PDF <= {s.MAX_PDF_PAGES} pages, DOCX <= {mb(s.MAX_DOCX_UNCOMPRESSED_SIZE)} expanded; "
        f"{s.ANALYSIS_CONCURRENCY} concurrent analyses/process, {s.ANALYSIS_TIMEOUT_SECONDS}s per step, "
        f"{s.BATCH_TIMEOUT_SECONDS}s per batch"
    )


class Settings:
    # ── API ──
    API_TITLE: str = "EasyRecruit ATS API 3.0"
    API_VERSION: str = "3.0.0"
    API_DESCRIPTION: str = (
        "Smart Applicant Tracking System — resume parsing, NLP scoring, "
        "and candidate management. Version 3.0"
    )

    # ── Server ──
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", "8001"))
    DEBUG: bool = os.getenv("DEBUG", "false").lower() == "true"

    # ── Security ──
    SECRET_KEY: str = resolve_secret_key(os.getenv("SECRET_KEY"), DEBUG)
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = int(
        os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "1440")  # 24 h
    )
    REFRESH_TOKEN_EXPIRE_DAYS: int = int(
        os.getenv("REFRESH_TOKEN_EXPIRE_DAYS", "7")
    )

    # ── Upload ── (every limit is validated at startup; see read_int_setting)
    MAX_FILE_SIZE: int = read_int_setting("MAX_FILE_SIZE", 10 * 1024 * 1024, minimum=1024, maximum=1024 ** 3)  # 10 MB
    ALLOWED_EXTENSIONS: set = {".pdf", ".docx"}      # legacy .doc is NOT supported (python-docx cannot read it)
    ALLOWED_MIME_TYPES: list = [
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ]
    # Batch: how many files per request, and the total request-body ceiling (default = the documented
    # "up to 15 files, 10 MB each" plus multipart overhead; lower it if your résumés are small).
    MAX_BATCH_FILES: int = read_int_setting("MAX_BATCH_FILES", 15, minimum=1, maximum=100)
    MAX_BATCH_REQUEST_SIZE: int = read_int_setting(
        "MAX_BATCH_REQUEST_SIZE", MAX_FILE_SIZE * MAX_BATCH_FILES + REQUEST_OVERHEAD_BYTES,
        minimum=1024, maximum=100 * 1024 ** 3,
    )
    # Document complexity limits (a small upload can still be a very expensive document).
    MAX_PDF_PAGES: int = read_int_setting("MAX_PDF_PAGES", 50, minimum=1, maximum=5000)
    MAX_DOCX_UNCOMPRESSED_SIZE: int = read_int_setting(          # decompression-bomb guard
        "MAX_DOCX_UNCOMPRESSED_SIZE", 20 * 1024 * 1024, minimum=64 * 1024, maximum=1024 ** 3
    )
    MAX_EXTRACTED_TEXT_CHARS: int = read_int_setting("MAX_EXTRACTED_TEXT_CHARS", 300_000, minimum=1000, maximum=10_000_000)

    # ── Analysis resource limits (per worker process) ──
    ANALYSIS_CONCURRENCY: int = read_int_setting("ANALYSIS_CONCURRENCY", 2, minimum=1, maximum=32)
    ANALYSIS_TIMEOUT_SECONDS: int = read_int_setting("ANALYSIS_TIMEOUT_SECONDS", 120, minimum=1, maximum=3600)
    ANALYSIS_QUEUE_TIMEOUT_SECONDS: int = read_int_setting("ANALYSIS_QUEUE_TIMEOUT_SECONDS", 60, minimum=1, maximum=3600)
    BATCH_TIMEOUT_SECONDS: int = read_int_setting("BATCH_TIMEOUT_SECONDS", 300, minimum=1, maximum=7200)

    # ── Scoring policy (Task 6) ──
    # "off" (default): institution and employer PRESTIGE add no points (they are still detected and shown for
    # information); certifications and India-specific skills count only when they are relevant to the job
    # description. "legacy": the pre-Task-6 prestige points (institution +10/+5, employer +6/+3, education-tier
    # bumps +15/+10/+5) and relevance-free certification credit; detection stays exact either way.
    SCORING_PRESTIGE_BONUSES: str = read_choice_setting("SCORING_PRESTIGE_BONUSES", "off", {"off", "legacy"})
    SCORING_INDIA_BONUS_CAP: int = read_int_setting("SCORING_INDIA_BONUS_CAP", 15, minimum=0, maximum=30)

    # ── NLP ──
    SPACY_MODEL: str = os.getenv("SPACY_MODEL", "en_core_web_sm")
    SENTENCE_TRANSFORMER_MODEL: str = os.getenv(
        "SENTENCE_TRANSFORMER_MODEL", "all-MiniLM-L6-v2"
    )

    # ── Scoring weights (legacy, UNUSED: the scorer's weights are the SCORE_WEIGHTS_* constants in enhanced_scorer.py) ──
    KEYWORD_WEIGHT: float = float(os.getenv("KEYWORD_WEIGHT", "0.25"))
    SKILL_WEIGHT: float = float(os.getenv("SKILL_WEIGHT", "0.30"))
    STRUCTURE_WEIGHT: float = float(os.getenv("STRUCTURE_WEIGHT", "0.20"))
    SEMANTIC_WEIGHT: float = float(os.getenv("SEMANTIC_WEIGHT", "0.25"))

    # ── Database ──
    DATABASE_PATH: str = os.getenv(
        "DATABASE_PATH", str(BASE_DIR / "app" / "data" / "easyrecruit.db")
    )

    # ── Logging ──
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()
    LOG_FILE: str = os.getenv("LOG_FILE", "easyrecruit.log")

    # ── CORS ── (validated and normalized; see resolve_cors_origins above)
    CORS_ORIGINS: list = resolve_cors_origins(os.getenv("CORS_ORIGINS"), DEBUG, PORT)
    CORS_ALLOW_CREDENTIALS: bool = cors_allow_credentials(CORS_ORIGINS)

    # ── Rate limiting (simple in-memory) ──
    RATE_LIMIT_REQUESTS: int = int(os.getenv("RATE_LIMIT_REQUESTS", "100"))
    RATE_LIMIT_WINDOW_SECONDS: int = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60"))

    # ── Pagination defaults ──
    DEFAULT_PAGE_SIZE: int = 10
    MAX_PAGE_SIZE: int = 100


settings = Settings()

if settings.MAX_BATCH_REQUEST_SIZE < settings.MAX_FILE_SIZE:
    raise ResourceLimitConfigError(
        f"MAX_BATCH_REQUEST_SIZE ({settings.MAX_BATCH_REQUEST_SIZE}) must be at least MAX_FILE_SIZE "
        f"({settings.MAX_FILE_SIZE}), otherwise a single maximum-size file could never be uploaded in a batch."
    )
