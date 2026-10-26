"""
Task 4 regression tests: production CORS configuration.

Covers (1) strict parsing/validation of CORS_ORIGINS, (2) real HTTP behaviour of the app (configured origins get
the right headers, untrusted ones are NOT granted access), (3) that main.py uses the validated settings instead of a
hardcoded wildcard, (4) fail-closed startup for missing/malformed/unsafe production settings, and (5) that the
shipped deployment files no longer set a wildcard. No real secret is used; nothing here touches the project DB.
"""
import ast
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import main                                                   # noqa: E402
from app.config import (CorsConfigError, cors_allow_credentials, cors_origins_problems,   # noqa: E402
                        describe_cors, dev_cors_origins, normalize_cors_origin,
                        parse_cors_origins, resolve_cors_origins)

ORIGIN = "https://app.example.com"
OTHER = "https://admin.example.com"
SECRETISH = "SuperSecret123-do-not-leak"


# ── 1. parsing & validation ───────────────────────────────────────────────────────────────────
class TestParsing:
    @pytest.mark.parametrize("raw,expected", [
        ("https://App.Example.com", "https://app.example.com"),
        ("  https://app.example.com/  ", "https://app.example.com"),          # trailing slash, whitespace
        ("https://app.example.com:443", "https://app.example.com"),           # default port is never in Origin
        ("http://localhost:80", "http://localhost"),
        ("https://app.example.com:8443", "https://app.example.com:8443"),
        ("HTTP://LOCALHOST:3000", "http://localhost:3000"),
        ("http://[::1]:3000", "http://[::1]:3000"),
        ("http://127.0.0.1:5500", "http://127.0.0.1:5500"),
        ("*", "*"),
    ])
    def test_normalization(self, raw, expected):
        assert normalize_cors_origin(raw, 1) == expected

    def test_list_is_deduplicated_and_blank_items_ignored(self):
        assert parse_cors_origins("https://a.example, ,https://b.example,https://a.example/,") == \
            ["https://a.example", "https://b.example"]

    @pytest.mark.parametrize("raw", [None, "", "   ", " , ,"])
    def test_empty_means_no_origins(self, raw):
        assert parse_cors_origins(raw) == []

    @pytest.mark.parametrize("raw", [
        "app.example.com", "localhost:3000", "ftp://a.example", "https://", "https://a.example/path",
        "https://a.example?x=1", "https://a.example#frag", "https://user:pw@a.example", "https://*.example.com",
        "null", "NULL", "https://a.example:99999", "https://a.example:0", "https://a.example:abc",
        "https://exa mple.com", "https://a_b.example", "https://b\u00fccher.example", "https://a.example\\evil",
        '["https://a.example"]', "https://-bad.example", "https://a..example",
    ])
    def test_malformed_or_untrustworthy_entries_are_rejected(self, raw):
        with pytest.raises(CorsConfigError):
            parse_cors_origins(raw)
        with pytest.raises(CorsConfigError):                 # same in production and development
            resolve_cors_origins(raw, True)
        with pytest.raises(CorsConfigError):
            resolve_cors_origins(raw, False)

    @pytest.mark.parametrize("raw", [
        f"https://admin:{SECRETISH}@evil.example/x?token=abc", SECRETISH, f"ok.example,{SECRETISH}",
    ])
    def test_error_messages_never_echo_the_offending_value(self, raw):
        with pytest.raises(CorsConfigError) as exc:
            parse_cors_origins(raw)
        msg = str(exc.value)
        assert SECRETISH not in msg and "token=abc" not in msg and "entry #" in msg


# ── 2. production / development policy ────────────────────────────────────────────────────────
class TestPolicy:
    @pytest.mark.parametrize("raw", [None, "", "  "])
    def test_production_default_is_same_origin_only(self, raw):
        assert resolve_cors_origins(raw, False) == []

    def test_production_accepts_explicit_https_origins(self):
        assert resolve_cors_origins(f"{ORIGIN}, {OTHER}", False) == [ORIGIN, OTHER]

    @pytest.mark.parametrize("raw", ["*", f"{ORIGIN},*", "http://app.example.com", "http://localhost:3000"])
    def test_production_refuses_wildcard_and_plain_http(self, raw):
        with pytest.raises(CorsConfigError) as exc:
            resolve_cors_origins(raw, False)
        assert "Refusing to start" in str(exc.value) and "CORS_ORIGINS" in str(exc.value)

    def test_development_default_is_loopback_only_and_includes_the_app_port(self):
        origins = resolve_cors_origins(None, True, 9123)
        assert "http://localhost:9123" in origins and "http://127.0.0.1:9123" in origins
        assert "http://localhost:3000" in origins and "*" not in origins
        assert all(re.match(r"^http://(localhost|127\.0\.0\.1):\d+$", o) for o in origins)
        assert origins == dev_cors_origins(9123)

    def test_development_tolerates_wildcard_and_http(self):
        assert resolve_cors_origins("*", True) == ["*"]
        assert resolve_cors_origins("http://localhost:4000", True) == ["http://localhost:4000"]

    def test_problems_function(self):
        assert cors_origins_problems([ORIGIN], False) == []
        assert cors_origins_problems(["*"], True) == []
        assert len(cors_origins_problems(["*", "http://a.example"], False)) == 2

    @pytest.mark.parametrize("origins,expected", [
        ([], False), (["*"], False), ([ORIGIN], True), ([ORIGIN, "*"], False), ([ORIGIN, OTHER], True),
    ])
    def test_credentials_never_combined_with_wildcard(self, origins, expected):
        assert cors_allow_credentials(origins) is expected

    def test_describe_is_secret_free_and_informative(self):
        assert "same-origin" in describe_cors([])
        assert "wildcard" in describe_cors(["*"]) and "disabled" in describe_cors(["*"])
        assert describe_cors([ORIGIN, OTHER]) == f"2 trusted origin(s): {ORIGIN}, {OTHER}"


# ── 3. real HTTP behaviour ────────────────────────────────────────────────────────────────────
@pytest.fixture()
def make_client(monkeypatch):
    def _make(origins):
        monkeypatch.setattr(main.settings, "CORS_ORIGINS", list(origins))
        monkeypatch.setattr(main.settings, "CORS_ALLOW_CREDENTIALS", cors_allow_credentials(origins))
        return TestClient(main.create_app())
    return _make


def preflight(client, origin, path="/api/v1/auth/login"):
    return client.options(path, headers={"Origin": origin, "Access-Control-Request-Method": "POST",
                                         "Access-Control-Request-Headers": "authorization,content-type"})


UNTRUSTED = [
    "https://evil.example", "null", "https://app.example.com.evil.com", "https://evil-app.example.com",
    "https://sub.app.example.com", "http://app.example.com", "https://app.example.com:8443",
    "https://app.example.com.", "https://APP.EXAMPLE.COM.evil.test", "file://",
]


class TestHttpBehaviour:
    def test_trusted_origin_preflight_is_granted(self, make_client):
        r = preflight(make_client([ORIGIN]), ORIGIN)
        assert r.status_code == 200
        assert r.headers["access-control-allow-origin"] == ORIGIN
        assert r.headers["access-control-allow-credentials"] == "true"
        assert "origin" in r.headers["vary"].lower()
        assert "POST" in r.headers["access-control-allow-methods"]
        assert "authorization" in r.headers["access-control-allow-headers"].lower()

    def test_trusted_origin_actual_request_is_granted(self, make_client):
        r = make_client([ORIGIN]).get("/api/v1/health", headers={"Origin": ORIGIN})
        assert r.status_code == 200
        assert r.headers["access-control-allow-origin"] == ORIGIN
        assert r.headers["access-control-allow-credentials"] == "true"
        assert "X-Process-Time-Ms" in r.headers["access-control-expose-headers"]

    def test_each_trusted_origin_gets_only_itself_echoed(self, make_client):
        c = make_client([ORIGIN, OTHER])
        for o in (ORIGIN, OTHER):
            assert c.get("/api/v1/health", headers={"Origin": o}).headers["access-control-allow-origin"] == o
            assert preflight(c, o).headers["access-control-allow-origin"] == o

    @pytest.mark.parametrize("origin", UNTRUSTED)
    def test_untrusted_origin_is_not_granted(self, make_client, origin):
        c = make_client([ORIGIN])
        pf = preflight(c, origin)
        assert pf.status_code == 400
        assert "access-control-allow-origin" not in pf.headers
        got = c.get("/api/v1/health", headers={"Origin": origin})
        assert "access-control-allow-origin" not in got.headers          # browser will refuse to expose the response
        assert got.headers.get("access-control-allow-origin") != "*"

    def test_empty_allowlist_grants_nobody(self, make_client):
        c = make_client([])                                            # the production default
        for o in [ORIGIN, "https://evil.example", "null"]:
            assert preflight(c, o).status_code == 400
            assert "access-control-allow-origin" not in c.get("/api/v1/health", headers={"Origin": o}).headers

    def test_requests_without_an_origin_header_are_unaffected(self, make_client):
        for origins in ([], [ORIGIN], ["*"]):
            r = make_client(origins).get("/api/v1/health")
            assert r.status_code == 200 and "access-control-allow-origin" not in r.headers

    def test_wildcard_never_gets_credentials(self, make_client):
        c = make_client(["*"])                                         # only reachable with DEBUG=true
        r = c.get("/api/v1/health", headers={"Origin": "https://anywhere.example"})
        assert r.headers["access-control-allow-origin"] == "*"
        assert "access-control-allow-credentials" not in r.headers
        pf = preflight(c, "https://anywhere.example")
        assert pf.status_code == 200 and "access-control-allow-credentials" not in pf.headers

    @pytest.mark.parametrize("origins", [[], [ORIGIN], [ORIGIN, OTHER], ["*"], dev_cors_origins(8001)])
    def test_invariant_wildcard_and_credentials_never_coexist(self, make_client, origins):
        c = make_client(origins)
        for o in [ORIGIN, OTHER, "https://evil.example", "null", "http://localhost:8001"]:
            for r in (preflight(c, o), c.get("/api/v1/health", headers={"Origin": o})):
                acao = r.headers.get("access-control-allow-origin")
                acac = r.headers.get("access-control-allow-credentials")
                assert not (acao == "*" and acac == "true")
                if acao is not None and acao != "*":
                    assert acao == o and acao in origins              # only ever an exact, configured origin

    def test_error_responses_still_carry_cors_headers_for_trusted_origins(self, make_client):
        r = make_client([ORIGIN]).get("/api/v1/definitely-not-a-route", headers={"Origin": ORIGIN})
        assert r.status_code == 404 and r.headers["access-control-allow-origin"] == ORIGIN

    def test_login_and_authenticated_calls_work_with_a_trusted_origin(self, make_client):
        from app.db import admin_provisioning as ap
        from app.db.database import db as live_db
        email, pw = f"cors-{secrets.token_hex(4)}@example.org", "Cors-Fixture-Qm4-Zt"
        ap.provision_account(live_db, email=email, password=pw, role="admin")
        c = make_client([ORIGIN])
        login = c.post("/api/v1/auth/login", json={"email": email, "password": pw}, headers={"Origin": ORIGIN})
        assert login.status_code == 200 and login.headers["access-control-allow-origin"] == ORIGIN
        token = login.json()["access_token"]
        me = c.get("/api/v1/auth/me", headers={"Origin": ORIGIN, "Authorization": f"Bearer {token}"})
        assert me.status_code == 200 and me.json()["email"] == email
        assert me.headers["access-control-allow-origin"] == ORIGIN
        # a bad token is still rejected (CORS is not authentication)
        bad = c.get("/api/v1/auth/me", headers={"Origin": ORIGIN, "Authorization": "Bearer nope"})
        assert bad.status_code == 401

    def test_frontend_page_is_still_served(self, make_client, monkeypatch):
        monkeypatch.chdir(ROOT)
        r = make_client([]).get("/")
        assert r.status_code == 200 and "text/html" in r.headers["content-type"]


# ── 4. main.py wiring ─────────────────────────────────────────────────────────────────────────
class TestMainWiring:
    def _cors_call(self):
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "add_middleware"
                    and node.args and isinstance(node.args[0], ast.Name) and node.args[0].id == "CORSMiddleware"):
                return {kw.arg: kw.value for kw in node.keywords}
        pytest.fail("CORSMiddleware is not registered in main.py")

    @staticmethod
    def _is_settings_attr(node, attr):
        return isinstance(node, ast.Attribute) and node.attr == attr and isinstance(node.value, ast.Name) and node.value.id == "settings"

    def test_origins_and_credentials_come_from_validated_settings(self):
        kw = self._cors_call()
        assert self._is_settings_attr(kw["allow_origins"], "CORS_ORIGINS")
        assert self._is_settings_attr(kw["allow_credentials"], "CORS_ALLOW_CREDENTIALS")

    def test_no_hardcoded_wildcard_origin_or_credentials(self):
        kw = self._cors_call()
        assert not isinstance(kw["allow_origins"], ast.List)
        assert not isinstance(kw["allow_credentials"], ast.Constant)

    def test_real_app_uses_the_settings_allowlist(self):
        mw = [m for m in main.app.user_middleware if m.cls.__name__ == "CORSMiddleware"]
        assert len(mw) == 1
        kwargs = getattr(mw[0], "kwargs", None) or dict(getattr(mw[0], "options", {}))
        assert kwargs["allow_origins"] == main.settings.CORS_ORIGINS
        assert "*" not in kwargs["allow_origins"] or kwargs["allow_credentials"] is False


# ── 5. fail-closed startup (fresh interpreter, real config import) ────────────────────────────
STRONG_KEY = secrets.token_hex(32)


def import_config(**env):
    base = {k: v for k, v in os.environ.items() if k not in ("CORS_ORIGINS", "DEBUG", "PORT", "SECRET_KEY")}
    base.update({"SECRET_KEY": STRONG_KEY, "PORT": "8001", **env})
    return subprocess.run(
        [sys.executable, "-c", "from app.config import settings; print(settings.CORS_ORIGINS, settings.CORS_ALLOW_CREDENTIALS)"],
        cwd=ROOT, env=base, capture_output=True, text=True, timeout=60)


class TestFailClosedStartup:
    def test_production_unset_or_empty_starts_same_origin_only(self):
        for val in ("",):
            r = import_config(DEBUG="false", CORS_ORIGINS=val)
            assert r.returncode == 0 and r.stdout.strip() == "[] False", r.stderr[-400:]

    def test_production_explicit_https_origin_starts_with_credentials(self):
        r = import_config(DEBUG="false", CORS_ORIGINS=f"{ORIGIN},{OTHER}")
        assert r.returncode == 0 and r.stdout.strip() == f"['{ORIGIN}', '{OTHER}'] True"

    @pytest.mark.parametrize("val", ["*", f"{ORIGIN},*", "http://app.example.com", "null", "app.example.com",
                                     "https://a.example/path", f"https://u:{SECRETISH}@evil.example"])
    def test_production_refuses_to_start_on_unsafe_or_malformed_values(self, val):
        r = import_config(DEBUG="false", CORS_ORIGINS=val)
        out = r.stdout + r.stderr
        assert r.returncode != 0 and "CorsConfigError" in out and "CORS_ORIGINS" in out
        assert SECRETISH not in out and STRONG_KEY not in out         # no secret ever reaches the logs

    def test_development_unset_allows_loopback_only(self):
        r = import_config(DEBUG="true", CORS_ORIGINS="")
        assert r.returncode == 0 and "http://localhost:8001" in r.stdout and "'*'" not in r.stdout
        assert r.stdout.strip().endswith("True")

    def test_development_wildcard_works_but_without_credentials(self):
        r = import_config(DEBUG="true", CORS_ORIGINS="*")
        assert r.returncode == 0 and r.stdout.strip() == "['*'] False"

    def test_malformed_value_fails_even_in_development(self):
        r = import_config(DEBUG="true", CORS_ORIGINS="localhost:3000")
        assert r.returncode != 0 and "CorsConfigError" in (r.stdout + r.stderr)


# ── 6. shipped deployment files ───────────────────────────────────────────────────────────────
def _env_value(path, key="CORS_ORIGINS"):
    for line in (ROOT / path).read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    pytest.fail(f"{key} missing from {path}")


class TestShippedConfiguration:
    def test_production_template_is_valid_and_not_a_wildcard(self):
        value = _env_value(".env.production.example")
        assert value != "*" and resolve_cors_origins(value, False) == []

    def test_development_template_is_valid_with_debug_on(self):
        assert resolve_cors_origins(_env_value(".env.example"), True)

    def test_compose_has_no_wildcard_and_defaults_to_same_origin(self):
        text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        assert not re.search(r"CORS_ORIGINS=\*", text)
        assert "CORS_ORIGINS=${CORS_ORIGINS:-}" in text and "DEBUG=false" in text

    def test_render_blueprint_has_no_wildcard(self):
        text = (ROOT / "render.yaml").read_text(encoding="utf-8")
        m = re.search(r"- key: CORS_ORIGINS\s*\n\s*value:\s*(\"[^\"]*\"|\S+)", text)
        assert m, "CORS_ORIGINS entry missing"
        value = m.group(1).strip('"')
        assert value != "*" and resolve_cors_origins(value, False) == []

    def test_docs_no_longer_advertise_a_wildcard_default(self):
        text = (ROOT / "DEPLOY.md").read_text(encoding="utf-8")
        row = next(l for l in text.splitlines() if l.startswith("| `CORS_ORIGINS`"))
        assert "| `*` |" not in row and "same-origin" in row.lower()
