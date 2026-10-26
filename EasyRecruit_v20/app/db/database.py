"""
EasyRecruit ATS 3.0 — Database Manager
SQLite with WAL, foreign keys, connection pooling helper, and migration support.
"""
import sqlite3
import logging
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent.parent
# Respect DATABASE_PATH env var so cloud platforms (Render, Railway) can
# point the DB at a persistent disk mount — falls back to local path.
_env_db = os.getenv("DATABASE_PATH", "")
DEFAULT_DB_PATH = Path(_env_db) if _env_db else (BASE_DIR / "app" / "data" / "easyrecruit.db")


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────

def _row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


# ────────────────────────────────────────────────────────────────────────────
# Manager
# ────────────────────────────────────────────────────────────────────────────

class DatabaseManager:
    """SQLite database manager for EasyRecruit ATS 3.0."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or str(DEFAULT_DB_PATH)
        self._ensure_directory()

    # ── Internal ──────────────────────────────────────────────────────────

    def _ensure_directory(self):
        data_dir = os.path.dirname(self.db_path)
        if data_dir:
            os.makedirs(data_dir, exist_ok=True)

    def _get_raw_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA cache_size = -8000")   # 8 MB page cache
        conn.execute("PRAGMA temp_store = MEMORY")
        return conn

    @contextmanager
    def connection(self):
        """Context manager that yields a connection and commits/rolls back."""
        conn = self._get_raw_connection()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ── Public query helpers ──────────────────────────────────────────────

    def fetch_all(self, query: str, params: tuple = ()) -> List[dict]:
        with self.connection() as conn:
            cur = conn.execute(query, params)
            return [_row_to_dict(r) for r in cur.fetchall()]

    def fetch_one(self, query: str, params: tuple = ()) -> Optional[dict]:
        results = self.fetch_all(query, params)
        return results[0] if results else None

    def execute(self, query: str, params: tuple = ()) -> int:
        """Execute INSERT/UPDATE/DELETE; returns rowcount."""
        with self.connection() as conn:
            cur = conn.execute(query, params)
            return cur.rowcount

    def insert(self, query: str, params: tuple = ()) -> int:
        """Execute INSERT; returns lastrowid."""
        with self.connection() as conn:
            cur = conn.execute(query, params)
            return cur.lastrowid

    def execute_many(self, query: str, params_list: List[tuple]) -> int:
        with self.connection() as conn:
            cur = conn.executemany(query, params_list)
            return cur.rowcount

    # Legacy aliases (keeps existing call-sites working)
    def execute_query(self, query: str, params: tuple = ()) -> List[dict]:
        return self.fetch_all(query, params)

    def execute_update(self, query: str, params: tuple = ()) -> int:
        return self.execute(query, params)

    def execute_insert(self, query: str, params: tuple = ()) -> int:
        return self.insert(query, params)

    # ── Schema ───────────────────────────────────────────────────────────

    def init_database(self):
        """Create all tables and indexes if they do not exist."""
        ddl_statements = [
            # Users
            """
            CREATE TABLE IF NOT EXISTS users (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                email            TEXT    UNIQUE NOT NULL,
                username         TEXT    UNIQUE NOT NULL,
                password_hash    TEXT    NOT NULL,
                full_name        TEXT,
                company_name     TEXT,
                role             TEXT    DEFAULT 'recruiter',
                approval_status  TEXT    DEFAULT 'approved',
                approved_by      INTEGER,
                approved_at      TIMESTAMP,
                is_active        INTEGER DEFAULT 1,
                last_login_at    TIMESTAMP,
                login_count      INTEGER DEFAULT 0,
                created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """,
            # Companies — one row per employer on the platform, each with its
            # own recruiter e-mail domain (e.g. zohorecruiter.com for Zoho).
            # A recruiter may only register under a company if their e-mail
            # matches that domain, and still needs that company's admin to
            # approve them before they can sign in.
            """
            CREATE TABLE IF NOT EXISTS companies (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                name             TEXT    UNIQUE NOT NULL,
                recruiter_domain TEXT    UNIQUE NOT NULL,
                created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """,
            # Job descriptions
            """
            CREATE TABLE IF NOT EXISTS job_descriptions (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id          INTEGER NOT NULL,
                title            TEXT    NOT NULL,
                company          TEXT,
                department       TEXT,
                location         TEXT,
                job_type         TEXT    DEFAULT 'full_time',
                description      TEXT    NOT NULL,
                required_skills  TEXT,
                preferred_skills TEXT,
                min_experience   INTEGER,
                max_experience   INTEGER,
                education_level  TEXT,
                salary_min       REAL,
                salary_max       REAL,
                is_active        INTEGER DEFAULT 1,
                created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """,
            # Resume analyses
            """
            CREATE TABLE IF NOT EXISTS resume_analyses (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id          INTEGER,
                job_id           INTEGER,
                filename         TEXT    NOT NULL,
                candidate_name   TEXT,
                candidate_email  TEXT,
                candidate_phone  TEXT,
                extracted_text   TEXT,
                overall_score    REAL,
                keyword_score    REAL,
                skill_score      REAL,
                structure_score  REAL,
                semantic_score   REAL,
                experience_score REAL,
                education_score  REAL,
                matched_keywords TEXT,
                matched_skills   TEXT,
                missing_skills   TEXT,
                recommendations  TEXT,
                analysis_data    TEXT,
                detected_domain  TEXT,
                status           TEXT    DEFAULT 'completed',
                created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL,
                FOREIGN KEY (job_id)  REFERENCES job_descriptions(id) ON DELETE SET NULL
            )
            """,
            # Custom datasets
            """
            CREATE TABLE IF NOT EXISTS custom_datasets (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                name        TEXT    NOT NULL,
                description TEXT,
                category    TEXT    NOT NULL,
                skills      TEXT,
                keywords    TEXT,
                is_active   INTEGER DEFAULT 1,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """,
            # Analysis history (activity log)
            """
            CREATE TABLE IF NOT EXISTS analysis_history (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id        INTEGER NOT NULL,
                analysis_id    INTEGER,
                analysis_type  TEXT    NOT NULL,
                description    TEXT,
                result_summary TEXT,
                ip_address     TEXT,
                created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """,
            # Token blacklist (for logout / revocation)
            """
            CREATE TABLE IF NOT EXISTS token_blacklist (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                jti        TEXT    UNIQUE NOT NULL,
                user_id    INTEGER NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """,
        ]

        index_statements = [
            "CREATE INDEX IF NOT EXISTS idx_users_email          ON users(email)",
            "CREATE INDEX IF NOT EXISTS idx_users_username       ON users(username)",
            "CREATE INDEX IF NOT EXISTS idx_users_company        ON users(company_name)",
            "CREATE INDEX IF NOT EXISTS idx_companies_name       ON companies(name)",
            "CREATE INDEX IF NOT EXISTS idx_companies_domain     ON companies(recruiter_domain)",
            "CREATE INDEX IF NOT EXISTS idx_jobs_user            ON job_descriptions(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_jobs_active          ON job_descriptions(is_active)",
            "CREATE INDEX IF NOT EXISTS idx_analyses_user        ON resume_analyses(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_analyses_job         ON resume_analyses(job_id)",
            "CREATE INDEX IF NOT EXISTS idx_analyses_score       ON resume_analyses(overall_score)",
            "CREATE INDEX IF NOT EXISTS idx_datasets_user        ON custom_datasets(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_history_user         ON analysis_history(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_history_created      ON analysis_history(created_at)",
            "CREATE INDEX IF NOT EXISTS idx_blacklist_jti        ON token_blacklist(jti)",
            "CREATE INDEX IF NOT EXISTS idx_blacklist_expires    ON token_blacklist(expires_at)",
        ]

        with self.connection() as conn:
            for stmt in ddl_statements:
                conn.execute(stmt)
            for stmt in index_statements:
                conn.execute(stmt)

        # ── Safe upgrade path for databases created before this column existed ──
        # SQLite has no "ADD COLUMN IF NOT EXISTS", so we probe and swallow the
        # "duplicate column" error on databases that already have it.
        for stmt in [
            "ALTER TABLE users ADD COLUMN approval_status TEXT DEFAULT 'approved'",
            "ALTER TABLE users ADD COLUMN approved_by INTEGER",
            "ALTER TABLE users ADD COLUMN approved_at TIMESTAMP",
            "ALTER TABLE resume_analyses ADD COLUMN detected_domain TEXT",
            # Job-seeker profile fields — additive, nullable, so existing
            # accounts are unaffected until the holder fills them in.
            "ALTER TABLE users ADD COLUMN photo_path TEXT",
            "ALTER TABLE users ADD COLUMN date_of_birth TEXT",
            "ALTER TABLE users ADD COLUMN gender TEXT",
            "ALTER TABLE users ADD COLUMN phone TEXT",
            "ALTER TABLE users ADD COLUMN address TEXT",
            "ALTER TABLE users ADD COLUMN profile_updated_at TIMESTAMP",
        ]:
            try:
                with self.connection() as conn:
                    conn.execute(stmt)
            except Exception:
                pass  # column already exists

        # ── One-time backfill: older rows saved before detected_domain existed
        #    still have it inside their analysis_data JSON blob — pull it out
        #    so existing candidates show a real domain instead of "-".
        try:
            rows = self.fetch_all(
                "SELECT id, analysis_data FROM resume_analyses "
                "WHERE detected_domain IS NULL AND analysis_data IS NOT NULL"
            )
            for row in rows:
                try:
                    domain = json.loads(row["analysis_data"]).get("detected_domain")
                except Exception:
                    domain = None
                if domain:
                    self.execute(
                        "UPDATE resume_analyses SET detected_domain = ? WHERE id = ?",
                        (domain, row["id"]),
                    )
        except Exception as exc:
            logger.debug(f"detected_domain backfill skipped: {exc}")

        logger.info("Database schema ready (v3.0)")

    def cleanup_expired_tokens(self):
        """Remove expired tokens from blacklist (call periodically)."""
        deleted = self.execute(
            "DELETE FROM token_blacklist WHERE expires_at < datetime('now')"
        )
        if deleted:
            logger.debug(f"Cleaned up {deleted} expired tokens")


# ── Singleton ─────────────────────────────────────────────────────────────

db = DatabaseManager()


def init_db():
    db.init_database()

    # Company registry only (names + public recruiter e-mail domains). No user
    # accounts are ever seeded: privileged accounts are provisioned explicitly
    # (python -m app.admin_cli provision ...) or, for the very first admin on a
    # fresh deployment, via the opt-in INITIAL_ADMIN_* environment variables.
    try:
        from app.db.company_seed import seed_companies
        seed_companies(db)
    except Exception as exc:
        logger.warning(f"Company seeding skipped: {exc}")

    try:
        from app.db.admin_provisioning import bootstrap_initial_admin_from_env
        bootstrap_initial_admin_from_env(db)
    except Exception as exc:
        logger.warning(f"Initial admin bootstrap skipped: {exc}")
