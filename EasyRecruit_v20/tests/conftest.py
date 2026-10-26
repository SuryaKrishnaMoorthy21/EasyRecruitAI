"""
Shared pytest setup.

TestClient(app) only runs FastAPI's lifespan startup handler when used as a
context manager (`with TestClient(app) as client:`) — on Starlette 0.35.1
(the version fastapi==0.109.0 pins), plain `client = TestClient(app)` does
not trigger it at all. Several test files construct the client at module
level without a `with` block, so `init_db()` (normally run during the app's
lifespan) never executes, and every DB-backed test fails immediately with
`sqlite3.OperationalError: no such table: users`. Verified directly against
Starlette 0.35.1 before writing this fix — it isn't a guess.

Running init_db() here, once, before the test session starts, fixes this
without needing to change how every existing test file constructs its
client.
"""
import atexit
import os
import secrets
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")

# ── Test isolation (must run BEFORE any app import) ──────────────────────────
# The test suite creates users, jobs and uploads. It must never write to the
# project's real database, so unless the caller explicitly chose a database via
# DATABASE_PATH we point the suite at a throw-away one that is deleted on exit.
if not os.environ.get("DATABASE_PATH"):
    _tmp_dir = tempfile.mkdtemp(prefix="easyrecruit-tests-")
    os.environ["DATABASE_PATH"] = os.path.join(_tmp_dir, "test.db")
    atexit.register(shutil.rmtree, _tmp_dir, ignore_errors=True)

# app.config refuses a missing/placeholder SECRET_KEY outside DEBUG. Tests get a
# random per-session key (setdefault: a key supplied by the caller still wins).
os.environ.setdefault("SECRET_KEY", secrets.token_hex(32))

from app.db.database import init_db

init_db()
