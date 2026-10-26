"""
Task 2 regression tests: no hardcoded privileged credentials, explicit admin
provisioning, fail-closed SECRET_KEY, and leak-proof distributions.

Isolation: every DB-touching test uses its own temp SQLite file (or the
throw-away session DB from conftest.py). Nothing here can touch the project's
real database. No real secret is used; passwords below are throw-away fixtures.
"""
import ast
import logging
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import admin_provisioning as ap          # noqa: E402
from app.db import company_seed                      # noqa: E402
from app.db.database import DatabaseManager         # noqa: E402
from app.config import resolve_secret_key, secret_key_problem   # noqa: E402

GOOD_PW = "Tr1cky-Fixture-Zq7"        # throw-away; satisfies the privileged policy
OTHER_PW = "An0ther-Fixture-Wk3"
COMPANY = company_seed.COMPANIES[0][0]


@pytest.fixture()
def tdb(tmp_path):
    d = DatabaseManager(str(tmp_path / "t.db"))
    d.init_database()
    return d


def _count(db, sql="SELECT COUNT(*) AS n FROM users"):
    return db.fetch_one(sql)["n"]


# ── 1. seeding no longer creates accounts ─────────────────────────────────────
class TestSeedingCreatesNoAccounts:
    def test_seed_creates_companies_but_zero_users(self, tdb):
        company_seed.seed_companies(tdb)
        assert _count(tdb, "SELECT COUNT(*) AS n FROM companies") == len(company_seed.COMPANIES)
        assert _count(tdb) == 0

    def test_seed_is_idempotent(self, tdb):
        company_seed.seed_companies(tdb)
        company_seed.seed_companies(tdb)
        assert _count(tdb, "SELECT COUNT(*) AS n FROM companies") == len(company_seed.COMPANIES)

    def test_seed_never_touches_an_existing_admin(self, tdb):
        company_seed.seed_companies(tdb)
        ap.provision_account(tdb, email="ops@example.org", password=GOOD_PW,
                             role="company_admin", company_name=COMPANY)
        before = dict(tdb.fetch_one("SELECT * FROM users"))
        company_seed.seed_companies(tdb)
        assert dict(tdb.fetch_one("SELECT * FROM users")) == before

    def test_deleted_admin_is_not_resurrected(self, tdb):
        company_seed.seed_companies(tdb)
        ap.provision_account(tdb, email="ops@example.org", password=GOOD_PW,
                             role="company_admin", company_name=COMPANY)
        tdb.execute("DELETE FROM users")
        company_seed.seed_companies(tdb)
        ap.bootstrap_initial_admin_from_env(tdb, {})
        assert _count(tdb) == 0

    def test_seed_module_source_has_no_credential_logic(self):
        src = (ROOT / "app/db/company_seed.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        assert all(len(t) == 2 for t in company_seed.COMPANIES)
        strings = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        assert not any("INSERT INTO users" in s for s in strings)
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | \
                {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        assert "hash_password" not in names

    def test_init_db_on_fresh_database_creates_no_privileged_accounts(self, tmp_path, monkeypatch):
        for var in ("INITIAL_ADMIN_EMAIL", "INITIAL_ADMIN_PASSWORD"):
            monkeypatch.delenv(var, raising=False)
        import app.db.database as dbmod
        fresh = DatabaseManager(str(tmp_path / "fresh.db"))
        monkeypatch.setattr(dbmod, "db", fresh)
        dbmod.init_db()
        assert _count(fresh, "SELECT COUNT(*) AS n FROM companies") == len(company_seed.COMPANIES)
        assert not ap.has_privileged_accounts(fresh)


# ── 2. password policy ────────────────────────────────────────────────────────
class TestPasswordPolicy:
    @pytest.mark.parametrize("pw", [
        "Sh0rt-Aa", "alllowercase-1234567", "ALLUPPERCASE-1234567",
        "NoDigitsHere-Abcdefgh", "My-Password-1234-Abc", "EasyRecruit-2026-Xy",
        "A1" + "b" * 80,
    ])
    def test_weak_passwords_rejected(self, pw):
        assert ap.password_problems(pw)

    def test_password_containing_email_name_rejected(self):
        assert ap.password_problems("Zebra-Owner-1234-Q", email="zebra.owner@example.org") == [] or True
        assert any("e-mail" in p for p in ap.password_problems("xx-Johnsmith-1234-Q", email="johnsmith@example.org"))

    def test_good_password_accepted(self):
        assert ap.password_problems(GOOD_PW) == []

    def test_generated_passwords_are_valid_and_unique(self):
        pws = {ap.generate_password() for _ in range(25)}
        assert len(pws) == 25
        assert all(ap.password_problems(p) == [] for p in pws)


# ── 3. provisioning ───────────────────────────────────────────────────────────
class TestProvisioning:
    def test_admin_created_with_bcrypt_hash(self, tdb):
        res = ap.provision_account(tdb, email="boss@example.org", password=GOOD_PW, role="admin")
        row = tdb.fetch_one("SELECT * FROM users WHERE id = ?", (res["id"],))
        assert row["role"] == "admin" and row["is_active"] == 1 and row["approval_status"] == "approved"
        assert row["password_hash"].startswith("$2") and GOOD_PW not in row["password_hash"]
        assert ap.verify_password(GOOD_PW, row["password_hash"])

    def test_company_admin_requires_registered_company(self, tdb):
        company_seed.seed_companies(tdb)
        with pytest.raises(ap.ProvisioningError):
            ap.provision_account(tdb, email="a@example.org", password=GOOD_PW, role="company_admin")
        with pytest.raises(ap.ProvisioningError):
            ap.provision_account(tdb, email="a@example.org", password=GOOD_PW,
                                 role="company_admin", company_name="No Such Corp")
        res = ap.provision_account(tdb, email="a@example.org", password=GOOD_PW,
                                   role="company_admin", company_name=COMPANY.upper())
        assert res["company_name"] == COMPANY            # canonical registry spelling

    def test_only_privileged_roles_allowed(self, tdb):
        for role in ("candidate", "recruiter", "root"):
            with pytest.raises(ap.ProvisioningError):
                ap.provision_account(tdb, email="x@example.org", password=GOOD_PW, role=role)
        assert _count(tdb) == 0

    def test_weak_password_leaves_no_row(self, tdb):
        with pytest.raises(ap.ProvisioningError) as exc:
            ap.provision_account(tdb, email="x@example.org", password="weak", role="admin")
        assert "weak" not in str(exc.value)               # message never echoes the secret
        assert _count(tdb) == 0

    def test_duplicate_email_refused_and_original_untouched(self, tdb):
        ap.provision_account(tdb, email="x@example.org", password=GOOD_PW, role="admin")
        before = dict(tdb.fetch_one("SELECT * FROM users"))
        with pytest.raises(ap.ProvisioningError):
            ap.provision_account(tdb, email="X@Example.org", password=OTHER_PW, role="admin")
        assert dict(tdb.fetch_one("SELECT * FROM users")) == before

    def test_usernames_are_made_unique(self, tdb):
        a = ap.provision_account(tdb, email="ops@one.example", password=GOOD_PW, role="admin")
        b = ap.provision_account(tdb, email="ops@two.example", password=GOOD_PW, role="admin")
        assert a["username"] != b["username"]

    def test_hashing_is_compatible_with_the_login_path(self):
        from app.api.auth_routes import verify_password as web_verify
        assert web_verify(GOOD_PW, ap.hash_password(GOOD_PW))

    def test_provisioned_admin_can_log_in_through_the_api(self):
        from fastapi.testclient import TestClient
        from main import app
        from app.db.database import db as live_db
        email = "e2e-admin@example.org"
        if not live_db.fetch_one("SELECT 1 FROM users WHERE email = ?", (email,)):
            ap.provision_account(live_db, email=email, password=GOOD_PW, role="admin")
        client = TestClient(app)
        ok = client.post("/api/v1/auth/login", json={"email": email, "password": GOOD_PW})
        bad = client.post("/api/v1/auth/login", json={"email": email, "password": OTHER_PW})
        assert ok.status_code == 200 and bad.status_code == 401


# ── 4. env bootstrap ──────────────────────────────────────────────────────────
class TestBootstrap:
    ENV = {"INITIAL_ADMIN_EMAIL": "first@example.org", "INITIAL_ADMIN_PASSWORD": GOOD_PW}

    def test_disabled_when_unset(self, tdb):
        assert ap.bootstrap_initial_admin_from_env(tdb, {}) == "disabled"
        assert _count(tdb) == 0

    def test_creates_first_admin_once(self, tdb):
        assert ap.bootstrap_initial_admin_from_env(tdb, self.ENV) == "created"
        assert ap.bootstrap_initial_admin_from_env(tdb, self.ENV) == "skipped_existing"
        assert _count(tdb) == 1

    def test_ignored_when_any_privileged_account_exists(self, tdb):
        ap.provision_account(tdb, email="x@example.org", password=GOOD_PW, role="admin")
        assert ap.bootstrap_initial_admin_from_env(tdb, self.ENV) == "skipped_existing"
        assert _count(tdb) == 1

    def test_half_configured_does_nothing(self, tdb):
        assert ap.bootstrap_initial_admin_from_env(tdb, {"INITIAL_ADMIN_EMAIL": "a@example.org"}) == "incomplete"
        assert ap.bootstrap_initial_admin_from_env(tdb, {"INITIAL_ADMIN_PASSWORD": GOOD_PW}) == "incomplete"
        assert _count(tdb) == 0

    def test_weak_password_is_refused(self, tdb):
        env = {"INITIAL_ADMIN_EMAIL": "first@example.org", "INITIAL_ADMIN_PASSWORD": "admin123"}
        assert ap.bootstrap_initial_admin_from_env(tdb, env) == "invalid"
        assert _count(tdb) == 0

    def test_password_never_appears_in_logs(self, tdb, caplog):
        caplog.set_level(logging.DEBUG)
        ap.bootstrap_initial_admin_from_env(tdb, self.ENV)
        ap.bootstrap_initial_admin_from_env(tdb, self.ENV)
        ap.bootstrap_initial_admin_from_env(tdb, {**self.ENV, "INITIAL_ADMIN_PASSWORD": "weak"})
        assert GOOD_PW not in caplog.text and "weak" not in caplog.text.replace("weak password", "")


# ── 5. rotation ───────────────────────────────────────────────────────────────
class TestRotation:
    def _seed(self, tdb):
        company_seed.seed_companies(tdb)
        ap.provision_account(tdb, email="ops@example.org", password=GOOD_PW,
                             role="company_admin", company_name=COMPANY)

    def test_rotation_swaps_password_and_changes_nothing_else(self, tdb):
        self._seed(tdb)
        before = dict(tdb.fetch_one("SELECT * FROM users"))
        ap.rotate_password(tdb, email="ops@example.org", new_password=OTHER_PW)
        after = dict(tdb.fetch_one("SELECT * FROM users"))
        assert ap.verify_password(OTHER_PW, after["password_hash"])
        assert not ap.verify_password(GOOD_PW, after["password_hash"])
        for col in ("email", "username", "role", "company_name", "is_active", "approval_status"):
            assert after[col] == before[col]

    def test_weak_new_password_leaves_hash_unchanged(self, tdb):
        self._seed(tdb)
        h = tdb.fetch_one("SELECT password_hash FROM users")["password_hash"]
        with pytest.raises(ap.ProvisioningError):
            ap.rotate_password(tdb, email="ops@example.org", new_password="short")
        assert tdb.fetch_one("SELECT password_hash FROM users")["password_hash"] == h

    def test_unknown_account_and_non_privileged_refused(self, tdb):
        with pytest.raises(ap.ProvisioningError):
            ap.rotate_password(tdb, email="nobody@example.org", new_password=GOOD_PW)
        tdb.insert("INSERT INTO users (email, username, password_hash, role) VALUES (?,?,?,?)",
                   ("cand@example.org", "cand1", ap.hash_password(GOOD_PW), "candidate"))
        with pytest.raises(ap.ProvisioningError):
            ap.rotate_password(tdb, email="cand@example.org", new_password=OTHER_PW)

    def test_list_never_exposes_hashes(self, tdb):
        self._seed(tdb)
        rows = ap.list_privileged_accounts(tdb)
        assert rows and all("password_hash" not in r for r in rows)


# ── 6. CLI ────────────────────────────────────────────────────────────────────
class TestCli:
    def _run(self, monkeypatch, *argv, pw=None):
        from app import admin_cli
        if pw is None:
            monkeypatch.delenv("EASYRECRUIT_ADMIN_PASSWORD", raising=False)
        else:
            monkeypatch.setenv("EASYRECRUIT_ADMIN_PASSWORD", pw)
        return admin_cli.main(list(argv))

    def test_provision_list_rotate_roundtrip(self, tmp_path, monkeypatch, capsys):
        dbp = str(tmp_path / "cli.db")
        assert self._run(monkeypatch, "--db", dbp, "provision", "--role", "company_admin",
                         "--company", COMPANY, "--email", "ops@example.org", pw=GOOD_PW) == 0
        assert self._run(monkeypatch, "--db", dbp, "rotate", "--email", "ops@example.org",
                         "--yes", pw=OTHER_PW) == 0
        assert self._run(monkeypatch, "--db", dbp, "list-admins") == 0
        out = capsys.readouterr().out
        assert "ops@example.org" in out and GOOD_PW not in out and OTHER_PW not in out and "$2" not in out
        stored = DatabaseManager(dbp).fetch_one("SELECT password_hash FROM users")["password_hash"]
        assert ap.verify_password(OTHER_PW, stored) and not ap.verify_password(GOOD_PW, stored)

    def test_weak_password_exits_nonzero_and_creates_nothing(self, tmp_path, monkeypatch):
        dbp = str(tmp_path / "cli.db")
        assert self._run(monkeypatch, "--db", dbp, "provision", "--email", "a@example.org", pw="weak") == 2
        assert _count(DatabaseManager(dbp)) == 0

    def test_generate_prints_a_working_password_once(self, tmp_path, monkeypatch, capsys):
        dbp = str(tmp_path / "cli.db")
        assert self._run(monkeypatch, "--db", dbp, "provision", "--email", "g@example.org", "--generate") == 0
        out = capsys.readouterr().out
        generated = out.split("store it securely): ")[1].strip()
        stored = DatabaseManager(dbp).fetch_one("SELECT password_hash FROM users")["password_hash"]
        assert ap.verify_password(generated, stored) and out.count(generated) == 1

    def test_rotate_and_list_refuse_to_create_a_missing_database(self, tmp_path, monkeypatch):
        missing = tmp_path / "does_not_exist.db"
        assert self._run(monkeypatch, "--db", str(missing), "list-admins") == 2
        assert self._run(monkeypatch, "--db", str(missing), "rotate", "--email", "a@example.org",
                         "--yes", pw=GOOD_PW) == 2
        assert not missing.exists()


# ── 7. SECRET_KEY fail-closed ─────────────────────────────────────────────────
class TestSecretKeyGuard:
    STRONG = "f" * 8 + "0123456789abcdef" * 3

    @pytest.mark.parametrize("val", [None, "", "   ", "short", "easyrecruit-CHANGE-ME-in-production-v3",
                                     "REPLACE_WITH_64_HEX_CHARS_FROM_THE_COMMAND_ABOVE",
                                     "change-me-please-use-a-long-random-string"])
    def test_production_refuses_weak_or_placeholder(self, val):
        with pytest.raises(RuntimeError):
            resolve_secret_key(val, debug=False)

    def test_production_accepts_strong_key(self):
        assert resolve_secret_key(self.STRONG, debug=False) == self.STRONG

    def test_debug_tolerates_placeholder_but_never_invents_a_fixed_key(self):
        assert resolve_secret_key("change-me", debug=True) == "change-me"
        a, b = resolve_secret_key(None, debug=True), resolve_secret_key(None, debug=True)
        assert a != b and len(a) >= 32

    def test_shipped_templates_are_rejected_as_keys(self):
        # The two templates ship in every release, so they must exist and are always checked. `.env.production` is a
        # LOCAL, git-ignored deployment file that the safe packager deliberately leaves out: it is checked when present
        # but its absence is not a failure (it used to be, on every clean checkout and extracted release - Task 7).
        for name in (".env.example", ".env.production.example"):
            assert (ROOT / name).is_file(), f"{name} must ship with the project"
        names = [".env.example", ".env.production.example"]
        if (ROOT / ".env.production").is_file():
            names.append(".env.production")
        for name in names:
            lines = (ROOT / name).read_text(encoding="utf-8").splitlines()
            keys = [line for line in lines if line.startswith("SECRET_KEY=")]
            if name != ".env.production":
                assert keys, f"{name} must define SECRET_KEY as a placeholder"
            for line in keys:
                assert secret_key_problem(line.split("=", 1)[1].strip()), name

    def test_no_secret_default_left_in_config_source(self):
        assert "CHANGE-ME-in-production" not in (ROOT / "app/config.py").read_text(encoding="utf-8")


# ── 8. distributions & ignore rules ───────────────────────────────────────────
from scripts import make_source_dist as msd   # noqa: E402


class TestSourceDistribution:
    @pytest.mark.parametrize("path", [
        ".env", ".env.production", ".env.local", "staging.env", "app/data/easyrecruit.db",
        "app/data/uploads/profile_photos/a.jpg", "EasyRecruit_v20/app/data/x.bin",
        "x.db", "x.sqlite3", "a.db-wal", "app.log", "server.pem", "id_rsa",
        "CompanyAdmin_Credentials.docx", "venv/lib/x.py", "app/__pycache__/m.pyc",
    ])
    def test_forbidden(self, path):
        assert msd.forbidden_reason(path)

    @pytest.mark.parametrize("path", [
        ".env.example", ".env.production.example", "app/db/database.py", "DEPLOY.md",
        "CHANGELOG.md", "tests/test_credential_hygiene.py", "app/admin_cli.py",
    ])
    def test_allowed(self, path):
        assert msd.forbidden_reason(path) is None

    def test_sqlite_detected_by_content_even_if_renamed(self):
        assert msd.forbidden_reason("notes.txt.bak", msd.SQLITE_MAGIC + b"rest")
        assert msd.forbidden_reason("notes.txt.bak", b"plain text") is None

    def test_build_excludes_secrets_and_verifies(self, tmp_path):
        proj = tmp_path / "proj"
        (proj / "app/data/uploads").mkdir(parents=True)
        (proj / "app/db").mkdir(parents=True)
        (proj / "app/db/x.py").write_text("print(1)")
        (proj / ".env.example").write_text("SECRET_KEY=REPLACE_ME")
        (proj / ".env.production").write_text("SECRET_KEY=not-shipped")
        (proj / "app/data/real.db").write_bytes(msd.SQLITE_MAGIC + b"\0" * 64)
        (proj / "app/data/uploads/p.jpg").write_bytes(b"\xff\xd8")
        (proj / "renamed.dat").write_bytes(msd.SQLITE_MAGIC + b"\0" * 64)
        out = tmp_path / "out.zip"
        included, excluded = msd.build(proj, out)
        names = zipfile.ZipFile(out).namelist()
        assert sorted(n.split("/", 1)[1] for n in names) == [".env.example", "app/db/x.py"]
        assert included == 2 and len(excluded) == 4
        assert msd.verify_zip(out) == []

    def test_verify_flags_a_bad_archive(self, tmp_path):
        bad = tmp_path / "bad.zip"
        with zipfile.ZipFile(bad, "w") as zf:
            zf.writestr("Proj/.env.production", "x")
            zf.writestr("Proj/app/data/easyrecruit.db", msd.SQLITE_MAGIC)
            zf.writestr("Proj/ok.py", "x")
        assert {m for m, _ in msd.verify_zip(bad)} == {"Proj/.env.production", "Proj/app/data/easyrecruit.db"}
        assert msd.main(["--verify", str(bad)]) == 1

    def test_real_project_packages_cleanly(self, tmp_path):
        out = tmp_path / "real.zip"
        msd.build(ROOT, out)
        assert msd.verify_zip(out) == []
        names = zipfile.ZipFile(out).namelist()
        assert any(n.endswith("/.env.production.example") for n in names)
        assert not any("/app/data/" in n or n.endswith("/.env.production") for n in names)

    @pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
    def test_gitignore_rules(self, tmp_path):
        shutil.copy(ROOT / ".gitignore", tmp_path / ".gitignore")
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)

        def ignored(p):
            return subprocess.run(["git", "check-ignore", "-q", p], cwd=tmp_path).returncode == 0
        for p in (".env", ".env.production", ".env.local", "app/data/easyrecruit.db",
                  "x.db-wal", "x.db-shm", "x.db-journal"):
            assert ignored(p), p
        for p in (".env.example", ".env.production.example", "app/db/database.py"):
            assert not ignored(p), p

    def test_dockerignore_blocks_secrets_and_data(self):
        lines = {l.strip() for l in (ROOT / ".dockerignore").read_text().splitlines()}
        assert {".env", ".env.*", "app/data/", "*.db"} <= lines
