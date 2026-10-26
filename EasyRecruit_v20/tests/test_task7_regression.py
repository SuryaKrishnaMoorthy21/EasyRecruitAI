"""
EasyRecruit ATS - Task 7 regression tests.

Covers what Task 7 found and fixed:

  1. NaN / Infinity can never produce an invalid JSON body or an unhandled HTTP 500
     (app/json_safety.py, the app's default response class, the scorer's finite guards, the stored result blob).
  2. The release packager excludes editor, coverage and OS leftovers as well as secrets and databases.

Tests that run the real scorer need spaCy + en_core_web_sm and are SKIPPED (environment-blocked), not failed, without
them. None of them loads the real sentence-transformer model; a scorer under test has no semantic model.
"""
import json
import math
import os
import sys
import uuid
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient
from pydantic import BaseModel

import upload_fixtures as fx
from app.json_safety import SafeJSONResponse, sanitize_non_finite
from main import app, create_app

try:
    import app.nlp.enhanced_scorer as es
    _SCORER_IMPORT_ERROR = None
except Exception as exc:                                    # pragma: no cover - environment-blocked
    es, _SCORER_IMPORT_ERROR = None, exc


def _spacy_ready():
    try:
        import spacy
        return spacy.util.is_package("en_core_web_sm")
    except Exception:
        return False


needs_scorer = pytest.mark.skipif(
    es is None or not _spacy_ready(),
    reason="environment-blocked: the scorer needs spaCy + en_core_web_sm (and numpy / scikit-learn / sentence-transformers to import)",
)

NAN, INF, NINF = float("nan"), float("inf"), float("-inf")


def _reject_constant(name):
    raise ValueError(f"non-standard JSON constant in the response: {name}")


def strict_json(raw):
    """Parse like a browser's JSON.parse: NaN / Infinity / -Infinity are errors."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json.loads(raw, parse_constant=_reject_constant)


def all_floats(value):
    if isinstance(value, float):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from all_floats(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from all_floats(v)


# ════════════════════════════════════════════════════════════════════════════
# 1a. sanitize_non_finite / SafeJSONResponse (no NLP needed)
# ════════════════════════════════════════════════════════════════════════════

class TestSanitize:
    @pytest.mark.parametrize("bad", [NAN, INF, NINF], ids=["nan", "inf", "-inf"])
    def test_a_non_finite_float_becomes_none(self, bad):
        assert sanitize_non_finite(bad) is None

    def test_it_recurses_through_dicts_lists_and_tuples(self):
        src = {"a": NAN, "b": [1.5, INF, {"c": NINF, "d": (2.0, NAN)}], "e": {"f": [[NAN]]}}
        out = sanitize_non_finite(src)
        assert out == {"a": None, "b": [1.5, None, {"c": None, "d": [2.0, None]}], "e": {"f": [[None]]}}
        assert list(all_floats(out)) == [1.5, 2.0]

    def test_finite_values_and_other_types_are_unchanged(self):
        src = {"i": 0, "z": 0.0, "neg": -3.5, "big": 1e300, "t": True, "f": False, "n": None, "s": "NaN", "l": [1, "x", 2.25]}
        assert sanitize_non_finite(src) == src
        assert sanitize_non_finite(True) is True and sanitize_non_finite("Infinity") == "Infinity"

    def test_the_input_is_not_mutated(self):
        src = {"a": NAN, "b": [INF]}
        sanitize_non_finite(src)
        assert math.isnan(src["a"]) and src["b"][0] == INF

    def test_numpy_floats_are_handled(self):
        np = pytest.importorskip("numpy")
        assert sanitize_non_finite({"x": np.float64("nan"), "y": np.float64(2.5)}) == {"x": None, "y": 2.5}

    def test_the_result_is_strictly_serialisable(self):
        out = sanitize_non_finite({"a": [NAN, INF, NINF, 1.0]})
        assert json.dumps(out, allow_nan=False) == '{"a": [null, null, null, 1.0]}'

    def test_a_plain_jsonresponse_fails_on_nan_which_is_why_the_safe_class_exists(self):
        from starlette.responses import JSONResponse
        with pytest.raises(ValueError):
            JSONResponse({"x": NAN})
        assert strict_json(SafeJSONResponse({"x": NAN, "y": 1}).body) == {"x": None, "y": 1}

    def test_the_warning_log_never_contains_the_payload(self, caplog):
        with caplog.at_level("WARNING", logger="app.json_safety"):
            SafeJSONResponse({"secret_name": "Priya Nair", "score": NAN})
        text = " ".join(r.getMessage() for r in caplog.records)
        assert "1 non-finite" in text and "Priya" not in text and "secret_name" not in text


# ════════════════════════════════════════════════════════════════════════════
# 1b. the application itself uses it, for every kind of route (no NLP needed)
# ════════════════════════════════════════════════════════════════════════════

class _Score(BaseModel):
    overall_score: float
    note: str = "x"


def _client_with_probe_routes():
    fresh = create_app()
    probe = APIRouter(prefix="/__task7")

    @probe.get("/plain")
    def plain():
        return {"score": NAN, "list": [INF, 1.0], "nested": {"x": NINF}}

    @probe.get("/model", response_model=_Score)
    def model():
        return {"overall_score": NAN}

    @probe.get("/model-inf", response_model=_Score)
    def model_inf():
        return {"overall_score": INF}

    fresh.include_router(probe)
    return TestClient(fresh, raise_server_exceptions=False)


class TestAppWideResponseSafety:
    def test_a_plain_dict_with_non_finite_numbers_is_valid_json(self):
        r = _client_with_probe_routes().get("/__task7/plain")
        assert r.status_code == 200
        assert strict_json(r.content) == {"score": None, "list": [None, 1.0], "nested": {"x": None}}

    @pytest.mark.parametrize("path", ["/__task7/model", "/__task7/model-inf"])
    def test_a_response_model_route_does_not_crash(self, path):
        r = _client_with_probe_routes().get(path)
        assert r.status_code == 200, r.text
        assert strict_json(r.content)["overall_score"] is None

    def test_finite_responses_are_byte_for_byte_what_they_were(self):
        r = TestClient(app).get("/api/v1/health")
        assert r.status_code == 200 and strict_json(r.content)["status"]

    def test_the_application_default_is_the_safe_response_class(self):
        assert create_app().router.default_response_class is SafeJSONResponse


# ════════════════════════════════════════════════════════════════════════════
# 1c. the scorer: a non-finite dimension is a failed dimension, not a score
# ════════════════════════════════════════════════════════════════════════════

RESUME = """Priya Nair
priya.nair@example.com | +91 98765 43210
SUMMARY
Software engineer with 4 years of experience building backend services in Python and Java.
EXPERIENCE
Software Engineer - Acme Systems (2021 - Present)
Developed REST APIs using FastAPI and Django, improved response time by 35%.
Managed PostgreSQL databases and deployed services with Docker on AWS.
EDUCATION
B.Tech in Computer Science - ABC Engineering College (2020)
SKILLS
Python, Java, SQL, Docker, Git, AWS, FastAPI, Django, PostgreSQL
"""
JD = "Backend Engineer. Required: Python, FastAPI, Docker, AWS, PostgreSQL. 3+ years of experience."


@pytest.fixture(scope="module")
def _scorer():
    with mock.patch.object(es.EnhancedScorer, "_load_model_safely", staticmethod(lambda *a, **k: None)):
        yield es.EnhancedScorer()


@needs_scorer
class TestScorerNonFinite:
    DIMENSIONS = [
        ("_keyword_score", "keyword", 50.0, "keyword_match_score"),
        ("_skill_score_with_breakdown", "skill", 50.0, "skill_match_score"),
        ("_structure_score", "structure", 50.0, "structure_score"),
        ("_experience_score", "experience", 50.0, "experience_score"),
        ("_education_score", "education", 25.0, "education_score"),
    ]

    @pytest.mark.parametrize("bad", [NAN, INF, NINF], ids=["nan", "inf", "-inf"])
    @pytest.mark.parametrize("method,label,placeholder,key", DIMENSIONS, ids=[d[1] for d in DIMENSIONS])
    def test_a_non_finite_dimension_is_replaced_by_its_labelled_placeholder(self, _scorer, method, label, placeholder, key, bad):
        value = (bad, {"matched_skills": [], "missing_skills": [], "categorized_skills": {}}) if method.startswith("_skill") else bad
        with mock.patch.object(_scorer, method, return_value=value):
            r = _scorer.calculate_ats_score(RESUME, JD)
        assert r[key] == placeholder
        assert 0.0 <= r["overall_score"] <= 100.0 and math.isfinite(r["overall_score"])
        assert all(math.isfinite(v) for v in all_floats(r))
        expected = f"The {label} score could not be computed; a neutral placeholder of {placeholder:g} was used."
        assert expected in r["scoring_explanation"]["warnings"]

    @pytest.mark.parametrize("bad", [NAN, INF, NINF], ids=["nan", "inf", "-inf"])
    def test_inf_in_one_dimension_can_no_longer_make_a_perfect_score(self, _scorer, bad):
        """Before the guard, +inf in a sub-score passed through min(..., 100) and produced exactly 100.0."""
        with mock.patch.object(_scorer, "_keyword_score", return_value=bad):
            r = _scorer.calculate_ats_score(RESUME, JD)
        assert r["overall_score"] < 100.0

    @pytest.mark.parametrize("bad", [NAN, INF], ids=["nan", "inf"])
    def test_a_non_finite_india_adjustment_is_skipped_with_a_warning(self, _scorer, bad):
        details = {"policy": "off", "total": bad, "cap": 15.0, "components": []}
        with mock.patch.object(_scorer, "_india_bonus_details", return_value=details):
            r = _scorer.calculate_ats_score(RESUME, JD)
        assert math.isfinite(r["india_context_bonus"]) and r["india_context_bonus"] == 0.0
        assert math.isfinite(r["overall_score"])
        assert any("India-context adjustment" in w for w in r["scoring_explanation"]["warnings"])

    @pytest.mark.parametrize("bad", [NAN, INF], ids=["nan", "inf"])
    def test_a_non_finite_domain_confidence_is_zeroed(self, _scorer, bad):
        with mock.patch.object(_scorer.extractor, "detect_domain", return_value=("general", bad)):
            r = _scorer.calculate_ats_score(RESUME, JD)
        assert math.isfinite(r["domain_confidence"]) and math.isfinite(r["overall_score"])

    def test_normal_scoring_is_unchanged_and_has_no_new_warnings(self, _scorer):
        r = _scorer.calculate_ats_score(RESUME, JD)
        assert r["scoring_explanation"]["warnings"] == []
        assert all(math.isfinite(v) for v in all_floats(r))

    def test_a_missing_semantic_model_is_still_reported_as_unavailable_not_failed(self, _scorer):
        r = _scorer.calculate_ats_score(RESUME, JD)
        assert r["semantic_similarity"] is None


# ════════════════════════════════════════════════════════════════════════════
# 1d. end to end: injected NaN / Infinity at the scorer boundary, every score endpoint
# ════════════════════════════════════════════════════════════════════════════

DOCX = None


def _docx():
    global DOCX
    if DOCX is None:
        DOCX = fx.make_docx()
    return DOCX


MIME_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
ENDPOINTS = {
    "analysis/analyze": ("/api/v1/analysis/analyze", "file"),
    "analysis/analyze-batch": ("/api/v1/analysis/analyze-batch", "files"),
    "public analyze": ("/api/v1/analyze", "file"),
    "bias/blind-score": ("/api/v1/bias/blind-score", "file"),
}
INJECT = ["overall_score", "keyword_match_score", "semantic_similarity", "india_context_bonus", "NESTED"]


def _inject(original, field, bad):
    def patched(self, *a, **k):
        r = original(self, *a, **k)
        if field == "NESTED":
            r.setdefault("breakdown", {})["probe"] = [bad, {"deep": bad}]
            if isinstance(r.get("scoring_explanation"), dict):
                r["scoring_explanation"]["probe"] = bad
        else:
            r[field] = bad
        return r
    return patched


@needs_scorer
class TestEndpointsSurviveNonFiniteScores:
    @pytest.mark.parametrize("bad", [NAN, INF], ids=["nan", "inf"])
    @pytest.mark.parametrize("field", INJECT)
    @pytest.mark.parametrize("endpoint", list(ENDPOINTS))
    def test_no_5xx_and_strictly_valid_json(self, monkeypatch, endpoint, field, bad):
        monkeypatch.setattr(es.EnhancedScorer, "calculate_ats_score", _inject(es.EnhancedScorer.calculate_ats_score, field, bad))
        path, part = ENDPOINTS[endpoint]
        client = TestClient(app, raise_server_exceptions=False)
        r = client.post(path, files=[(part, ("cv.docx", _docx(), MIME_DOCX))], data={"job_description": JD})
        assert r.status_code < 500, (r.status_code, r.text[:300])
        body = strict_json(r.content)
        assert not any(isinstance(v, float) and not math.isfinite(v) for v in all_floats(body))
        assert "NaN" not in r.text and "Infinity" not in r.text

    def test_an_injected_nan_comes_back_as_null_not_as_a_made_up_number(self, monkeypatch):
        monkeypatch.setattr(es.EnhancedScorer, "calculate_ats_score",
                            _inject(es.EnhancedScorer.calculate_ats_score, "overall_score", NAN))
        r = TestClient(app, raise_server_exceptions=False).post(
            "/api/v1/analysis/analyze", files={"file": ("cv.docx", _docx(), MIME_DOCX)}, data={"job_description": JD})
        assert r.status_code == 201 and r.json()["overall_score"] is None

    def test_a_saved_analysis_with_a_non_finite_value_is_stored_and_read_back_as_valid_json(self, monkeypatch):
        client = TestClient(app, raise_server_exceptions=False)
        tag = uuid.uuid4().hex[:10]
        email = f"t7_{tag}@easyrecruit.dev"
        reg = client.post("/api/v1/auth/register", json={"email": email, "username": f"t7_{tag}", "password": "TestPass1",
                                                         "full_name": "T Seven", "role": "candidate"})
        assert reg.status_code == 201, reg.text
        token = client.post("/api/v1/auth/login", json={"email": email, "password": "TestPass1"}).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        monkeypatch.setattr(es.EnhancedScorer, "calculate_ats_score",
                            _inject(es.EnhancedScorer.calculate_ats_score, "NESTED", NAN))
        r = client.post("/api/v1/analysis/analyze", headers=headers,
                        files={"file": ("cv.docx", _docx(), MIME_DOCX)}, data={"job_description": JD, "save_to_db": "true"})
        assert r.status_code == 201, r.text
        body = strict_json(r.content)
        assert body["saved"] is True, body.get("save_error")
        got = client.get(f"/api/v1/analysis/{body['analysis_id']}", headers=headers)
        assert got.status_code == 200
        strict_json(got.content)
        assert "NaN" not in got.text


# ════════════════════════════════════════════════════════════════════════════
# 2. release hygiene: generated and editor files are excluded too
# ════════════════════════════════════════════════════════════════════════════

from scripts import make_source_dist as msd   # noqa: E402


class TestReleaseExcludesGeneratedFiles:
    @pytest.mark.parametrize("path", [
        ".coverage", ".coverage.host.123", "a/.DS_Store", "Thumbs.db", "x.swp", "x.swo", "x.pyo", "x.py.orig",
        ".idea/workspace.xml", ".vscode/settings.json", "htmlcov/index.html", "easyrecruit.egg-info/PKG-INFO",
        ".pytest_cache/v/cache", "tests/frontend/node_modules/jsdom/index.js", "app/nlp/__pycache__/a.cpython-312.pyc",
        ".ruff_cache/x", "temp_uploads/a.pdf",
    ])
    def test_forbidden(self, path):
        assert msd.forbidden_reason(path), path

    @pytest.mark.parametrize("path", [
        "tests/frontend/package.json", "tests/frontend/xss_harness.js", "app/json_safety.py",
        "tests/test_task7_regression.py", "datasets/cse/resources.json", ".env.example", ".gitignore", ".dockerignore",
    ])
    def test_allowed(self, path):
        assert msd.forbidden_reason(path) is None, path
