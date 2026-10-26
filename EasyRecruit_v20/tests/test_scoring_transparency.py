"""
Task 6 — scoring transparency and fairness.

Groups
  TestWeights                 the six JD-based weights, totals, bounds
  TestMissingInformation      blank / absent inputs and placeholder failures
  TestSemanticFailures        no model, encoder failure, non-finite similarity, calibration, the 20% weight
  TestInstitutionFairness     prestige adds no points by default, exact detection, the legacy opt-in
  TestCertifications          certification credit needs relevance to the job; no substring false positives
  TestExplanations            every result says how it was produced
  TestExtractionConfidence    accuracy_estimate is a labelled heuristic alias, never an invented number
  TestFraudPresentation       indicators for human review, not proof
  TestBiasPresentation        a bias-reduction aid, not "bias-free"; anonymizer damage fixed
  TestUiWording               the page no longer makes the overclaims
  TestConfig                  scoring-policy settings

Tests that run the scorer need spaCy + en_core_web_sm and are skipped (environment-blocked), not failed, without them.
The sentence-transformer is replaced by tiny deterministic fakes: the real model is never needed to test the plumbing,
and nothing here claims anything about the real model's accuracy.
"""
import json
import math
import os
import random
import sys
import uuid
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pytest
from fastapi.testclient import TestClient

import upload_fixtures as fx
import app.api.analysis_routes as ar
import app.api.bias_routes as bias
import app.api.fraud_routes as fraud
from app.db.database import db
from app.config import (
    ResourceLimitConfigError, ScoringConfigError, describe_scoring_policy, read_choice_setting, read_int_setting, settings,
)
from app.nlp.text_match import find_terms, has_term, term_pattern
from main import app

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

client = TestClient(app)

NEUTRAL = "ABC Engineering College"        # an institution with no recognised prestige
BASE_RESUME = """Priya Nair
priya.nair@example.com | +91 98765 43210 | linkedin.com/in/priyanair
SUMMARY
Software engineer with 4 years of experience building backend services in Python and Java.
EXPERIENCE
Software Engineer - Acme Systems (2021 - Present)
Developed REST APIs using FastAPI and Django, improved response time by 35%.
Managed PostgreSQL and MongoDB databases and deployed services with Docker on AWS.
Led a team of 4 engineers and delivered 6 projects on schedule.
EDUCATION
B.Tech in Computer Science - {INSTITUTION} (2020)
SKILLS
Python, Java, SQL, Docker, Git, AWS, FastAPI, Django, React, PostgreSQL, MongoDB
"""
JD = ("We are hiring a Backend Engineer. Required: Python, FastAPI, Docker, AWS, PostgreSQL, REST API design. "
      "Preferred: Kubernetes, CI/CD, unit testing. 3+ years of experience. Bachelor's degree in Computer Science.")


def resume(institution=NEUTRAL):
    return BASE_RESUME.replace("{INSTITUTION}", institution)


# ── fake semantic models ─────────────────────────────────────────────────────

class RaisingModel:
    def encode(self, texts):
        raise RuntimeError("simulated runtime failure inside the encoder")


class NaNModel:
    def encode(self, texts):
        return np.full((len(texts), 8), np.nan)


class SimModel:
    """Embeddings whose cosine similarity between resume chunks and JD chunks is exactly `sim`."""
    def __init__(self, sim):
        self.sim, self.calls = sim, 0

    def encode(self, texts):
        self.calls += 1                                     # odd call = resume chunks, even call = JD chunks
        if self.calls % 2:
            return np.tile([1.0, 0.0], (len(texts), 1))
        s = max(-1.0, min(1.0, self.sim))
        return np.tile([s, math.sqrt(1.0 - s * s)], (len(texts), 1))


@pytest.fixture(scope="module")
def _shared_scorer():
    # No Hugging Face probe, and a single spaCy load for the whole module.
    with mock.patch.object(es.EnhancedScorer, "_load_model_safely", staticmethod(lambda *a, **k: None)):
        yield es.EnhancedScorer()


@pytest.fixture
def scorer(_shared_scorer):
    previous = _shared_scorer.model
    _shared_scorer.model = None
    yield _shared_scorer
    _shared_scorer.model = previous


def _component(result, name):
    for c in result["scoring_explanation"]["adjustments"][0]["components"]:
        if c["name"] == name:
            return c
    raise AssertionError(f"no {name} component")


# ════════════════════════════════════════════════════════════════════════════
# Weights and bounds
# ════════════════════════════════════════════════════════════════════════════

@needs_scorer
class TestWeights:
    def test_weight_tables_total_100_percent(self):
        assert sum(es.SCORE_WEIGHTS_WITH_SEMANTIC.values()) == pytest.approx(1.0)
        assert sum(es.SCORE_WEIGHTS_NO_SEMANTIC.values()) == pytest.approx(1.0)

    def test_the_documented_weights(self):
        assert es.SCORE_WEIGHTS_WITH_SEMANTIC == {
            "keyword": 0.20, "skill": 0.25, "structure": 0.15, "semantic": 0.20, "experience": 0.10, "education": 0.10}
        assert es.SCORE_WEIGHTS_NO_SEMANTIC == {
            "keyword": 0.25, "skill": 0.30, "structure": 0.20, "experience": 0.15, "education": 0.10}

    def test_each_dimension_carries_exactly_its_weight(self, scorer):
        order = ["keyword", "skill", "structure", "semantic", "experience", "education"]
        for i, name in enumerate(order):
            args = [0.0] * 6
            args[i] = 100.0
            assert scorer._final_score(*args) == pytest.approx(es.SCORE_WEIGHTS_WITH_SEMANTIC[name] * 100, abs=0.05)
            if name != "semantic":
                args[3] = None                                  # the no-semantic scheme
                assert scorer._final_score(*args) == pytest.approx(es.SCORE_WEIGHTS_NO_SEMANTIC[name] * 100, abs=0.05)

    def test_a_weight_table_that_does_not_total_100_is_rejected(self, monkeypatch):
        monkeypatch.setitem(es.SCORE_WEIGHTS_NO_SEMANTIC, "keyword", 0.30)
        with pytest.raises(ValueError, match="total 100%"):
            es._validate_weights()

    def test_final_score_stays_within_0_and_100(self, scorer):
        rng = random.Random(7)
        for _ in range(3000):
            v = [rng.uniform(0, 100) for _ in range(6)]
            semantic = rng.choice([None, v[3]])
            result = scorer._final_score(v[0], v[1], v[2], semantic, v[4], v[5], rng.uniform(0, 30))
            assert 0.0 <= result <= 100.0
        assert scorer._final_score(100, 100, 100, 100, 100, 100, 30) == 100.0       # bonus cannot push past 100
        assert scorer._final_score(0, 0, 0, None, 0, 0, 0) == 0.0

    @pytest.mark.parametrize("text", [
        "a", "   \n\t  ", "1234567890 " * 300, "नमस्ते 😀 résumé " * 200, "Python Java SQL " * 5000, "Lorem ipsum dolor sit amet " * 40,
    ], ids=["one char", "whitespace", "digits", "unicode", "huge repeated", "lorem ipsum"])
    @pytest.mark.parametrize("jd", [None, JD], ids=["no JD", "with JD"])
    def test_every_score_in_the_result_is_within_bounds(self, scorer, text, jd):
        result = scorer.calculate_ats_score(text, jd)
        for key in ("overall_score", "keyword_match_score", "skill_match_score", "structure_score", "experience_score",
                    "education_score", "extraction_confidence"):
            assert 0.0 <= result[key] <= 100.0, key
        assert 0.0 <= result["india_context_bonus"] <= settings.SCORING_INDIA_BONUS_CAP


# ════════════════════════════════════════════════════════════════════════════
# Missing information
# ════════════════════════════════════════════════════════════════════════════

NUMERIC_KEYS = ("overall_score", "keyword_match_score", "skill_match_score", "structure_score",
                "experience_score", "education_score", "india_context_bonus")


@needs_scorer
class TestMissingInformation:
    def test_an_empty_resume_returns_an_empty_result_with_no_invented_numbers(self, scorer):
        for text in ("", "   \n "):
            r = scorer.calculate_ats_score(text, JD)
            assert r["overall_score"] == 0
            assert r["extraction_confidence"] == 0.0 and r["accuracy_estimate"] == 0.0
            assert r["scoring_explanation"]["scheme"] == "none"
            assert r["scoring_explanation"]["notes"] and r["scoring_explanation"]["limitations"]

    def test_a_blank_job_description_is_the_same_as_no_job_description(self, scorer):
        none = scorer.calculate_ats_score(resume(), None)
        for blank in ("", "   ", "\n\t  \n"):
            r = scorer.calculate_ats_score(resume(), blank)
            assert {k: r[k] for k in NUMERIC_KEYS} == {k: none[k] for k in NUMERIC_KEYS}
            assert r["scoring_explanation"]["scheme"] == "no_job_description"

    def test_empty_or_malformed_required_skills_fall_back_to_the_defaults(self, scorer):
        none = scorer.calculate_ats_score(resume(), JD, None)
        for bad in ({}, [], ["python"], "python"):
            r = scorer.calculate_ats_score(resume(), JD, bad)
            assert {k: r[k] for k in NUMERIC_KEYS} == {k: none[k] for k in NUMERIC_KEYS}

    @pytest.mark.parametrize("text", [
        "Priya\nSKILLS\nPython\nEXPERIENCE\nWorked as a developer and will be responsible for APIs.",
        "Priya\nSKILLS\nPython, Adobe Photoshop\nEXPERIENCE\nWorked as a developer.",
        "Priya\nSKILLS\nPython\nEXPERIENCE\nMastered Python and handled webmaster duties.",
    ], ids=["verb 'be'", "'Adobe'", "'mastered' / 'webmaster'"])
    def test_a_resume_without_education_gets_no_education_credit(self, scorer, text):
        assert scorer._education_score(text) == 0.0

    @pytest.mark.parametrize("text, expected", [
        ("B.Tech Computer Science", 63.0), ("Bachelor of Technology in CS", 65.0), ("Bachelor's degree in CS", 65.0),
        ("Master's degree in Data Science", 80.0), ("MBA, Finance", 78.0), ("PhD in Physics", 95.0),
        ("BE Computer Science", 62.0), ("Diploma in Electronics", 30.0), ("Passed 12th with 90%", 20.0),
    ])
    def test_genuine_degrees_are_still_recognised(self, scorer, text, expected):
        assert scorer._education_score("Priya Nair\nEDUCATION\n" + text) == expected

    def test_the_lowercase_verb_be_is_not_a_degree_but_capital_BE_is(self, scorer):
        assert scorer._education_score("It will be fine") == 0.0
        assert scorer._education_score("BE Mechanical") == 62.0

    def test_executive_titles_match_whole_words_only(self, scorer):
        plain = scorer._experience_score("Asha\nEXPERIENCE\nIntern: built a small Flask app.")
        for phrase in ("configured a VPN for the lab", "worked in the banking sector", "delivered tasks ahead of schedule",
                       "administered Active Directory"):
            assert scorer._experience_score(f"Asha\nEXPERIENCE\nIntern: built a small Flask app; {phrase}.") < plain + 10, phrase
        assert scorer._experience_score("Asha\nEXPERIENCE\nReported to the Director of Engineering.") > plain + 20

    @pytest.mark.parametrize("method, placeholder, fragment", [
        ("_keyword_score", 50.0, "keyword score could not be computed"),
        ("_structure_score", 50.0, "structure score could not be computed"),
        ("_experience_score", 50.0, "experience score could not be computed"),
        ("_education_score", 25.0, "education score could not be computed"),
    ])
    def test_a_failing_dimension_uses_a_labelled_placeholder_and_says_so(self, scorer, method, placeholder, fragment):
        with mock.patch.object(es.EnhancedScorer, method, side_effect=RuntimeError("boom")):
            r = scorer.calculate_ats_score(resume(), JD)
        assert any(fragment in w for w in r["scoring_explanation"]["warnings"])
        assert "boom" not in " ".join(r["scoring_explanation"]["warnings"])          # no internal text leaks
        dimension = {"_keyword_score": "keyword_match_score", "_structure_score": "structure_score",
                     "_experience_score": "experience_score", "_education_score": "education_score"}[method]
        assert r[dimension] == placeholder
        assert 0 <= r["overall_score"] <= 100


# ════════════════════════════════════════════════════════════════════════════
# Semantic model failures
# ════════════════════════════════════════════════════════════════════════════

@needs_scorer
class TestSemanticFailures:
    def test_without_a_model_the_no_semantic_weights_are_used_and_explained(self, scorer):
        r = scorer.calculate_ats_score(resume(), JD)
        assert r["semantic_similarity"] is None
        e = r["scoring_explanation"]
        assert e["scheme"] == "jd_without_semantic" and e["weights"] == es.SCORE_WEIGHTS_NO_SEMANTIC
        assert any("not loaded" in n and "instead of scoring it as 0" in n for n in e["notes"])

    @pytest.mark.parametrize("broken", [RaisingModel(), NaNModel()], ids=["encoder raises", "non-finite similarity"])
    def test_a_runtime_model_failure_is_not_scored_as_zero(self, scorer, broken):
        offline = scorer.calculate_ats_score(resume(), JD)
        scorer.model = broken
        r = scorer.calculate_ats_score(resume(), JD)
        assert r["semantic_similarity"] is None, "a failure was reported as a genuine semantic score"
        assert r["overall_score"] == offline["overall_score"], "a model failure cost the candidate points"
        e = r["scoring_explanation"]
        assert e["scheme"] == "jd_without_semantic"
        assert any("failed at runtime" in w for w in e["warnings"])
        assert any("NOT counted as 0" in n for n in e["notes"])

    def test_the_similarity_function_raises_instead_of_returning_zero(self, scorer):
        scorer.model = RaisingModel()
        with pytest.raises(RuntimeError):
            scorer._semantic_similarity(resume(), JD)
        scorer.model = NaNModel()
        with pytest.raises(ValueError):
            scorer._semantic_similarity(resume(), JD)

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")], ids=["nan", "inf", "-inf"])
    def test_a_non_finite_similarity_is_rejected_not_clamped_into_a_score(self, scorer, bad):
        """scikit-learn rejects NaN embeddings itself; this forces the guard that protects against a non-finite value
        reaching the clamp (min(1.0, nan) is 1.0, which would have been scored as a perfect 100)."""
        scorer.model = SimModel(0.5)
        with mock.patch.object(es, "cosine_similarity", lambda a, b: [[bad]]):
            with pytest.raises(ValueError, match="not a finite number"):
                scorer._semantic_similarity(resume(), JD)
            offline = scorer.calculate_ats_score(resume(), JD)
        assert offline["semantic_similarity"] is None

    @pytest.mark.parametrize("sim, expected", [(-1.0, 0.0), (0.0, 0.0), (0.30, 0.0), (0.575, 50.0), (0.85, 100.0), (1.0, 100.0)])
    def test_a_working_model_is_calibrated_and_bounded(self, scorer, sim, expected):
        scorer.model = SimModel(sim)
        value = scorer._semantic_similarity(resume(), JD)
        assert 0.0 <= value <= 100.0
        assert value == pytest.approx(expected, abs=0.01)

    def test_semantic_similarity_carries_twenty_percent_of_the_score(self, scorer):
        scorer.model = SimModel(0.30)                       # calibrates to 0
        low = scorer.calculate_ats_score(resume(), JD)
        scorer.model = SimModel(0.85)                       # calibrates to 100
        high = scorer.calculate_ats_score(resume(), JD)
        assert (low["semantic_similarity"], high["semantic_similarity"]) == (0.0, 100.0)
        assert high["overall_score"] - low["overall_score"] == pytest.approx(20.0, abs=0.1)
        assert high["scoring_explanation"]["scheme"] == "jd_with_semantic"
        assert high["scoring_explanation"]["weights"] == es.SCORE_WEIGHTS_WITH_SEMANTIC

    def test_the_model_is_not_consulted_without_a_job_description(self, scorer):
        model = SimModel(0.9)
        scorer.model = model
        r = scorer.calculate_ats_score(resume(), None)
        assert r["semantic_similarity"] is None and model.calls == 0
        assert r["scoring_explanation"]["scheme"] == "no_job_description"


# ════════════════════════════════════════════════════════════════════════════
# Institution and certification fairness
# ════════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def extractor(_shared_scorer):
    return _shared_scorer.extractor


@needs_scorer
class TestInstitutionFairness:
    @pytest.mark.parametrize("institution", ["IIT Madras", "IIM Ahmedabad", "NIT Trichy", "VIT Vellore", "Anna University"])
    def test_institution_prestige_adds_no_points_by_default(self, scorer, institution):
        neutral = scorer.calculate_ats_score(resume(NEUTRAL), JD)
        r = scorer.calculate_ats_score(resume(institution), JD)
        assert r["overall_score"] == neutral["overall_score"]
        assert r["education_score"] == neutral["education_score"]
        assert r["india_context_bonus"] == 0.0
        # ... but it is still detected and shown, and the response says it was not scored
        ctx = r["indian_context"]
        assert ctx["has_premium_institute"] or ctx["has_good_institute"]
        assert ctx["bonus_policy"] == "evidence_only" and ctx["institution_bonus_applied"] is False
        assert _component(r, "institution")["points"] == 0.0 and _component(r, "institution")["applied"] is False

    def test_prestige_does_not_outrank_stronger_evidence(self, scorer):
        iit = scorer.calculate_ats_score(resume("IIT Madras"), JD)
        more_evidence = scorer.calculate_ats_score(resume(NEUTRAL).replace("SKILLS\n", "SKILLS\nKubernetes, "), JD)
        assert more_evidence["skill_match_score"] > iit["skill_match_score"]
        assert more_evidence["overall_score"] > iit["overall_score"]

    @pytest.mark.parametrize("sentence", [
        "Wrote unit tests and led a code-quality initiative.",
        "Built software for a chemical plant.",
        "Enjoys HIIT workouts and trekking.",
        "Based in Bengaluru, Karnataka.",
        "Received a bonus in lieu of overtime.",
        "Built a credit card payments module.",
    ])
    def test_ordinary_words_no_longer_look_like_institutions_or_employers(self, extractor, sentence):
        ctx = extractor.detect_indian_context(sentence)
        assert not ctx["has_premium_institute"] and not ctx["has_good_institute"]
        assert ctx["detected_tier"] is None

    @pytest.mark.parametrize("name", ["Sairam", "Sriram"])
    def test_a_first_name_is_not_an_institution(self, extractor, name):
        ctx = extractor.detect_indian_context(f"{name} Kumar\nSoftware engineer")
        assert not ctx["has_good_institute"] and not ctx["has_premium_institute"]

    @pytest.mark.parametrize("text, premium, good", [
        ("B.E. at IIT Madras", True, False), ("MBA from IIM Ahmedabad", True, False), ("studied at NIT Trichy", True, True),
        ("IIT-Bombay graduate", True, False), ("(NIT) Calicut", True, False), ("BITS Pilani", True, False),
        ("Anna University", False, True), ("Manipal Institute of Technology", False, True),
        ("Sri Sairam Engineering College, Chennai", False, True), ("VIT Vellore", False, True),
    ])
    def test_genuine_institution_names_are_still_detected(self, extractor, text, premium, good):
        ctx = extractor.detect_indian_context(text)
        assert bool(ctx["has_premium_institute"]) is premium and bool(ctx["has_good_institute"]) is good

    def test_false_positives_stay_out_of_the_score_even_under_the_legacy_policy(self, scorer, monkeypatch):
        monkeypatch.setattr(settings, "SCORING_PRESTIGE_BONUSES", "legacy")
        neutral = scorer.calculate_ats_score(resume(NEUTRAL), JD)
        noisy = scorer.calculate_ats_score(
            resume(NEUTRAL).replace("Led a team", "Wrote unit tests, enjoys HIIT, built a credit card module in a chemical plant. Led a team"), JD)
        assert noisy["india_context_bonus"] == 0.0
        assert noisy["education_score"] == neutral["education_score"]

    def test_the_legacy_policy_restores_prestige_points_and_says_so(self, scorer, monkeypatch):
        neutral = scorer.calculate_ats_score(resume(NEUTRAL), JD)
        monkeypatch.setattr(settings, "SCORING_PRESTIGE_BONUSES", "legacy")
        iit = scorer.calculate_ats_score(resume("IIT Madras"), JD)
        assert iit["india_context_bonus"] == 10.0
        assert iit["education_score"] == neutral["education_score"] + 15
        assert iit["overall_score"] - neutral["overall_score"] == pytest.approx(11.5, abs=0.1)     # 10 + 15 x 10%
        assert iit["indian_context"]["bonus_policy"] == "legacy_prestige"
        assert iit["indian_context"]["institution_bonus_applied"] is True
        assert any("ENABLED by configuration" in n for n in iit["scoring_explanation"]["notes"])

    def test_the_bonus_is_never_more_than_the_configured_cap(self, scorer, monkeypatch):
        monkeypatch.setattr(settings, "SCORING_PRESTIGE_BONUSES", "legacy")
        monkeypatch.setattr(settings, "SCORING_INDIA_BONUS_CAP", 3)
        r = scorer.calculate_ats_score(resume("IIT Madras") + "AWS Certified Solutions Architect\n", JD)
        assert r["india_context_bonus"] == 3.0

    def test_the_default_policy_is_evidence_only(self):
        assert settings.SCORING_PRESTIGE_BONUSES == "off"


@needs_scorer
class TestCertifications:
    AWS = "AWS Certified Solutions Architect"

    def _bonus(self, scorer, extractor, text, jd):
        ctx = extractor.detect_indian_context(text)
        return scorer._india_bonus_details(text, ctx, jd)

    def test_a_certification_counts_when_it_is_relevant_to_the_job(self, scorer, extractor):
        details = self._bonus(scorer, extractor, resume() + self.AWS + "\n", "Cloud engineer with AWS and Docker")
        comp = {c["name"]: c for c in details["components"]}["certifications"]
        assert comp["counted"] == ["aws certified"] and comp["points"] == 2.5

    def test_an_unrelated_certification_adds_nothing(self, scorer, extractor):
        details = self._bonus(scorer, extractor, resume() + self.AWS + "\n", "Accountant skilled in Excel and Tally")
        comp = {c["name"]: c for c in details["components"]}["certifications"]
        assert comp["counted"] == [] and comp["not_relevant_to_job"] == ["aws certified"] and comp["points"] == 0.0
        assert "none is relevant to the job description" in comp["reason"]

    def test_without_a_job_description_certifications_still_count(self, scorer, extractor):
        details = self._bonus(scorer, extractor, resume() + self.AWS + "\n", None)
        assert {c["name"]: c for c in details["components"]}["certifications"]["points"] == 2.5

    def test_the_legacy_policy_counts_certifications_whatever_the_job(self, scorer, extractor, monkeypatch):
        monkeypatch.setattr(settings, "SCORING_PRESTIGE_BONUSES", "legacy")
        details = self._bonus(scorer, extractor, resume() + self.AWS + "\n", "Accountant skilled in Excel and Tally")
        assert {c["name"]: c for c in details["components"]}["certifications"]["points"] == 2.5

    @pytest.mark.parametrize("sentence", ["published an npm package", "built with CMake", "complies with CCPA", "packaging scripts"])
    @pytest.mark.parametrize("jd", [None, JD], ids=["no JD", "with JD"])
    def test_ordinary_words_are_not_certifications(self, scorer, extractor, sentence, jd):
        details = self._bonus(scorer, extractor, resume() + sentence + "\n", jd)
        comp = {c["name"]: c for c in details["components"]}["certifications"]
        assert comp["detected"] == [] and comp["points"] == 0.0

    def test_a_mooc_credential_is_judged_by_the_line_it_appears_on(self, scorer, extractor):
        text = resume() + "NPTEL Certified - Cloud Computing (Elite)\n"
        relevant = {c["name"]: c for c in self._bonus(scorer, extractor, text, "We need cloud computing experience")["components"]}
        unrelated = {c["name"]: c for c in self._bonus(scorer, extractor, text, "Accountant skilled in Excel and Tally")["components"]}
        assert relevant["certifications"]["points"] == 2.5 and unrelated["certifications"]["points"] == 0.0

    def test_certification_points_are_capped_at_eight(self, scorer, extractor):
        text = resume() + "\n".join(f"{v} Certified" for v in ("AWS", "Azure", "GCP", "Oracle", "SAP")) + "\n"
        details = self._bonus(scorer, extractor, text, "AWS Azure GCP Oracle SAP engineer")
        assert {c["name"]: c for c in details["components"]}["certifications"]["points"] == 8.0
        assert details["total"] <= settings.SCORING_INDIA_BONUS_CAP

    def test_india_specific_skills_count_only_when_the_job_names_them(self, scorer, extractor):
        text = resume() + "Handled GST filing for clients.\n"
        ctx = extractor.detect_indian_context(text)
        assert "gst filing" in ctx["india_specific_skills"]
        off_topic = {c["name"]: c for c in scorer._india_bonus_details(text, ctx, JD)["components"]}["india_specific_skills"]
        on_topic = {c["name"]: c for c in scorer._india_bonus_details(text, ctx, "Accounts role: GST filing and Tally")["components"]}["india_specific_skills"]
        no_jd = {c["name"]: c for c in scorer._india_bonus_details(text, ctx, None)["components"]}["india_specific_skills"]
        assert (off_topic["points"], on_topic["points"], no_jd["points"]) == (0.0, 0.5, 0.5)

    def test_the_reported_bonus_equals_the_sum_of_its_components(self, scorer):
        r = scorer.calculate_ats_score(resume() + "AWS Certified Solutions Architect\nHandled GST filing.\n",
                                       "AWS engineer who also handles GST filing")
        parts = r["scoring_explanation"]["adjustments"][0]["components"]
        assert r["india_context_bonus"] == pytest.approx(min(sum(c["points"] for c in parts), settings.SCORING_INDIA_BONUS_CAP), abs=0.05)
        assert r["india_context_bonus"] == pytest.approx(3.0, abs=0.05)


class TestWholeWordMatching:
    @pytest.mark.parametrize("text, term, expected", [
        ("unit tests", "nit", False), ("NIT Trichy", "nit", True), ("(NIT)", "nit", True), ("HIIT", "iit", False),
        ("IIT-Madras", "iit", True), ("credit", "cred", False), ("CRED", "cred", True), ("package", "cka", False),
        ("CKA certified", "cka", True), ("VPN", "vp", False), ("Director of IT", "director", True), ("directory", "director", False),
        ("ahead of schedule", "head of", False), ("Head of Engineering", "head of", True), ("sri   sairam college", "sri sairam", True),
    ])
    def test_terms_match_whole_words_only(self, text, term, expected):
        assert has_term(text.lower(), term) is expected

    def test_plural_and_possessive_forms(self):
        for form in ("bachelor", "bachelors", "bachelor's"):
            assert has_term(form, "bachelor", plural=True)
        assert not has_term("mastered", "master", plural=True) and not has_term("webmaster", "master", plural=True)

    def test_find_terms_and_cached_patterns(self):
        assert find_terms("iit and nit", ["iit", "nit", "iim"]) == ["iit", "nit"]
        assert term_pattern("iit") is term_pattern("iit")


# ════════════════════════════════════════════════════════════════════════════
# Explanations and extraction confidence
# ════════════════════════════════════════════════════════════════════════════

@needs_scorer
class TestExplanations:
    def test_every_result_explains_how_it_was_produced(self, scorer):
        scorer.model = SimModel(0.575)
        r = scorer.calculate_ats_score(resume(), JD)
        e = r["scoring_explanation"]
        assert {"scheme", "weights", "weights_total", "components", "weighted_total", "adjustments", "caps", "final_score",
                "formula", "notes", "warnings", "extraction_confidence", "limitations"} <= set(e)
        assert e["scheme"] == "jd_with_semantic" and e["weights_total"] == 1.0
        assert [c["dimension"] for c in e["components"]] == ["keyword", "skill", "structure", "semantic", "experience", "education"]
        assert e["final_score"] == r["overall_score"]
        for c in e["components"]:
            assert c["label"] and c["basis"]
            assert c["points"] == pytest.approx(c["score"] * c["weight"], abs=0.06)

    def test_the_explanation_reproduces_the_overall_score(self, scorer):
        for model, jd in ((None, None), (None, JD), (SimModel(0.6), JD)):
            scorer.model = model
            r = scorer.calculate_ats_score(resume("IIT Madras") + "AWS Certified Architect\n", jd)
            e = r["scoring_explanation"]
            rebuilt = sum(c["score"] * c["weight"] for c in e["components"]) + e["adjustments"][0]["points"]
            assert r["overall_score"] == pytest.approx(min(100.0, rebuilt), abs=0.15)
            assert sum(e["weights"].values()) == pytest.approx(1.0)

    def test_a_formal_letter_cap_is_explained(self, scorer):
        r = scorer.calculate_ats_score("To whom it may concern\n" + resume(), JD)
        assert r["overall_score"] <= 12.0 and r["is_likely_resume"] is False
        cap = next(c for c in r["scoring_explanation"]["caps"] if c["name"] == "formal_letter_cap")
        assert cap["limit"] == 12.0 and "to whom it may concern" in cap["detail"]

    def test_an_institution_mention_is_explained_as_not_scored(self, scorer):
        r = scorer.calculate_ats_score(resume("IIT Madras"), JD)
        assert any("adds no points" in n for n in r["scoring_explanation"]["notes"])

    def test_the_limitations_say_the_score_is_not_validated(self, scorer):
        text = " ".join(scorer.calculate_ats_score(resume(), JD)["scoring_explanation"]["limitations"])
        assert "not been validated" in text and "never as the sole basis" in text

    def test_recommendations_no_longer_say_that_institute_tier_is_scored(self, scorer):
        r = scorer.calculate_ats_score(resume(NEUTRAL), JD)
        text = " ".join(str(x) for x in r["breakdown"]["recommendations"])
        assert "compensate for institute tier" not in text


@needs_scorer
class TestExtractionConfidence:
    def test_accuracy_estimate_is_a_labelled_alias_of_the_extraction_confidence(self, scorer):
        r = scorer.calculate_ats_score(resume(), JD)
        assert r["accuracy_estimate"] == r["extraction_confidence"]
        confidence = r["scoring_explanation"]["extraction_confidence"]
        assert confidence["is_measured_accuracy"] is False
        assert "NOT a measured accuracy" in confidence["note"] and "labelled data" in confidence["note"]

    def test_the_value_is_the_sum_of_its_stated_indicators(self, scorer):
        for text in (resume(), "Lorem ipsum dolor sit amet " * 30):
            r = scorer.calculate_ats_score(text, None)
            c = r["scoring_explanation"]["extraction_confidence"]
            assert r["extraction_confidence"] == round(min(100.0, max(15.0, sum(b["points"] for b in c["basis"]))), 1)
            assert [b["indicator"] for b in c["basis"]] == ["text_extracted", "candidate_name", "email", "resume_structure", "domain_match"]

    def test_the_value_does_not_depend_on_the_correctness_of_the_score(self, scorer):
        """It measures how much text was parsed, not whether the score is right: a poor match with a clean parse is 100."""
        irrelevant_jd = "Sous chef for a busy kitchen. Knife skills, sauces, banquet service."
        a = scorer.calculate_ats_score(resume(), JD)
        b = scorer.calculate_ats_score(resume(), irrelevant_jd)
        assert a["extraction_confidence"] == b["extraction_confidence"]
        assert a["overall_score"] != b["overall_score"]


def _stub_pipeline(include_confidence):
    def fake(text, job_description, skills_dict):
        score = {"overall_score": 61.0, "keyword_match_score": 50.0, "skill_match_score": 40.0, "structure_score": 70.0,
                 "semantic_similarity": None, "breakdown": {}, "detected_domain": "cse"}
        if include_confidence:
            score.update(extraction_confidence=77.0, accuracy_estimate=77.0, scoring_explanation={"scheme": "stub"})
        return ({"contact_info": {}, "candidate_name": None, "skills": [], "keywords": [], "indian_context": {}}, score)
    return fake


class TestApiTransparency:
    def test_there_is_no_invented_accuracy_when_the_scorer_supplies_none(self, monkeypatch):
        monkeypatch.setattr(ar, "_run_nlp_pipeline_sync", _stub_pipeline(False))
        body = client.post("/api/v1/analysis/analyze", files={"file": ("a.docx", fx.make_docx(), fx.DOCX_MIME)}).json()
        assert body["accuracy_estimate"] is None and body["extraction_confidence"] is None       # was a hard-coded 88 / 85.0
        assert body["scoring_explanation"] is None

    def test_both_names_carry_the_same_value_and_the_explanation_is_passed_through(self, monkeypatch):
        monkeypatch.setattr(ar, "_run_nlp_pipeline_sync", _stub_pipeline(True))
        body = client.post("/api/v1/analysis/analyze", files={"file": ("a.docx", fx.make_docx(), fx.DOCX_MIME)}).json()
        assert body["accuracy_estimate"] == body["extraction_confidence"] == 77.0
        assert body["scoring_explanation"] == {"scheme": "stub"}

    def test_the_batch_endpoint_passes_the_new_fields_through_too(self, monkeypatch):
        monkeypatch.setattr(ar, "_run_nlp_pipeline_sync", _stub_pipeline(True))
        body = client.post("/api/v1/analysis/analyze-batch",
                           files=[("files", ("a.docx", fx.make_docx(), fx.DOCX_MIME))]).json()
        entry = body["results"][0]
        assert entry["extraction_confidence"] == 77.0 and entry["accuracy_estimate"] == 77.0

    @needs_scorer
    def test_the_response_contract_is_a_superset_of_the_old_one(self):
        body = client.post("/api/v1/analysis/analyze", files={"file": ("a.docx", fx.make_docx(), fx.DOCX_MIME)}).json()
        old_keys = {"analysis_id", "filename", "extracted_text_length", "candidate_name", "candidate_email", "overall_score",
                    "keyword_match_score", "skill_match_score", "structure_score", "semantic_similarity", "experience_score",
                    "education_score", "accuracy_estimate", "nlp_analysis", "breakdown", "status", "created_at", "saved", "save_error"}
        assert old_keys <= set(body) and {"extraction_confidence", "scoring_explanation"} <= set(body)
        assert body["scoring_explanation"]["final_score"] == body["overall_score"]


@needs_scorer
class TestExplanationIsPersisted:
    def test_a_saved_analysis_stores_and_returns_the_explanation(self):
        tag = uuid.uuid4().hex[:10]
        email = f"s{tag}@scoring-tests.dev"
        client.post("/api/v1/auth/register", json={"email": email, "username": f"s{tag}", "password": "ScoringTest1", "role": "candidate"})
        token = client.post("/api/v1/auth/login", json={"email": email, "password": "ScoringTest1"}).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        r = client.post("/api/v1/analysis/analyze", headers=headers, data={"save_to_db": "true"},
                        files={"file": ("a.docx", fx.make_docx(), fx.DOCX_MIME)})
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["saved"] is True and body["analysis_id"] > 0

        stored = json.loads(db.fetch_one("SELECT analysis_data FROM resume_analyses WHERE id = ?", (body["analysis_id"],))["analysis_data"])
        assert stored["scoring_explanation"]["final_score"] == body["overall_score"]
        assert stored["extraction_confidence"] == body["extraction_confidence"]

        detail = client.get(f"/api/v1/analysis/{body['analysis_id']}", headers=headers)
        assert detail.status_code == 200, detail.text


# ════════════════════════════════════════════════════════════════════════════
# Fraud presentation
# ════════════════════════════════════════════════════════════════════════════

ACCUSATORY = ("fake", "fraud", "fabricat", "dishonest", "lying", "liar", "forged", "this is suspicious", "appears authentic",
              "proof of experience", "pay slips")

STUDENT = ("Anjali Rao\nanjali@example.com +91 90000 11111\nEDUCATION\nB.E. Computer Science, ABC Engineering College, "
           "Expected graduation: 2028\nSKILLS\nPython, SQL, Git\nPROJECTS\nBuilt a library management system using Python and SQL "
           "for the final-year mini project submission.\n")
# Triggers many indicators at once: an unexplained future year, overlapping jobs, buzzwords, a long skills list, a certification
# claim, and more years of experience than years since graduation.
FLAGGED = ("Raj Mehta\nraj@example.com\nEXPERIENCE\nAcme Corp 2015 - 2019\nBeta Ltd 2016 - 2020\nGamma Inc 2019 - 2031\n"
           "I am a synergy-driven rockstar ninja guru thought leader with 25 years of experience.\n"
           "EDUCATION\nB.Tech Computer Science 2014\nSKILLS\n" + ", ".join(f"Skill{i}" for i in range(60)) + "\n")


class TestFraudPresentation:
    def test_every_result_is_marked_for_human_review(self):
        for text in (STUDENT, FLAGGED, BASE_RESUME.replace("{INSTITUTION}", NEUTRAL)):
            r = fraud._analyze_fraud(text)
            assert r["requires_human_review"] is True
            assert "not proof of dishonesty" in r["disclaimer"] and "do not reject anyone" in r["disclaimer"]
            assert "not a probability" in r["score_basis"]
            assert r["risk_level_label"] == fraud.RISK_LABELS[r["risk_level"]]
            assert {"fraud_score", "risk_level", "flags", "recommendations", "summary", "total_flags"} <= set(r)   # contract kept

    def test_an_empty_result_makes_no_claim_that_the_resume_is_genuine(self):
        r = fraud._analyze_fraud("Priya\npriya@example.com +91 98765 43210\n" + "Software engineer with experience. " * 40)
        assert r["total_flags"] == 0
        assert "does not verify the resume" in r["summary"]
        assert not any(w in r["summary"].lower() for w in ("authentic", "genuine", "no fraud"))
        assert any("does not verify the resume" in x for x in r["recommendations"])

    def test_no_message_accuses_the_candidate(self):
        r = fraud._analyze_fraud(FLAGGED)
        assert r["total_flags"] >= 3
        text = " ".join([r["summary"], *r["recommendations"], *(f["message"] + " " + f.get("detail", "") for f in r["flags"])]).lower()
        for phrase in ACCUSATORY:
            assert phrase not in text, phrase

    def test_an_expected_graduation_date_is_a_low_severity_indicator_not_a_high_risk(self):
        r = fraud._analyze_fraud(STUDENT)
        future = [f for f in r["flags"] if f["type"] == "future_date"]
        assert [f["severity"] for f in future] == ["low"] and "expected completion date" in future[0]["message"]
        assert r["high_severity"] == 0 and r["risk_level"] not in ("high_risk", "very_high_risk")

    def test_an_unexplained_future_year_is_still_flagged_high_with_neutral_wording(self):
        r = fraud._analyze_fraud("Raj\nworked at Acme 2019 - 2031\n" + "word " * 100)
        future = [f for f in r["flags"] if f["type"] == "future_date"]
        assert [f["severity"] for f in future] == ["high"]
        assert "may be a typo or a planned date" in future[0]["message"]

    def test_an_expected_year_and_an_unexplained_year_are_reported_separately(self):
        r = fraud._analyze_fraud(STUDENT + "Worked at Acme 2019 - 2033\n")
        assert sorted(f["severity"] for f in r["flags"] if f["type"] == "future_date") == ["high", "low"]

    @pytest.mark.parametrize("severities, score, label", [
        ([], 0, "clean"), (["low"], 5, "low_risk"), (["medium", "low"], 17, "moderate_risk"),
        (["high", "medium", "low"], 42, "high_risk"), (["high"] * 3, 75, "very_high_risk"), (["high"] * 6, 100, "very_high_risk"),
    ])
    def test_the_score_arithmetic_and_thresholds_are_unchanged(self, severities, score, label):
        assert fraud._calculate_fraud_score([{"severity": s} for s in severities]) == (score, label)

    def test_the_endpoint_carries_the_review_metadata(self):
        body = client.post("/api/v1/fraud/detect", files={"file": ("a.docx", fx.make_docx(), fx.DOCX_MIME)}).json()
        assert body["requires_human_review"] is True and body["disclaimer"] and body["score_basis"]


# ════════════════════════════════════════════════════════════════════════════
# Bias presentation and the anonymizer
# ════════════════════════════════════════════════════════════════════════════

OVERCLAIMS = ("bias-free", "bias free", "unbiased", "ensure fair", "eliminat", "bias impact appears low", "no bias")


class _FixedScorer:
    def __init__(self, original, blind):
        self.scores, self.calls = [original, blind], 0

    def calculate_ats_score(self, text, jd=None, *a, **k):
        self.calls += 1
        return {"overall_score": self.scores[(self.calls - 1) % 2], "breakdown": {}}


class _CrashingScorer:
    def calculate_ats_score(self, *a, **k):
        raise RuntimeError("scorer crashed")


class TestBiasPresentation:
    @pytest.mark.parametrize("original, blind, shifted", [(70.0, 60.0, True), (60.0, 70.0, True), (60.0, 58.0, False), (60.0, 60.0, False)])
    def test_a_score_shift_is_an_indicator_for_review_never_proof(self, monkeypatch, original, blind, shifted):
        monkeypatch.setattr(bias, "_get_scorer", lambda: _FixedScorer(original, blind))
        r = bias._bias_score_comparison("a", "b", None)
        assert r["bias_detected"] is shifted and r["score_shift_flag"] is shifted      # old key kept, new name added
        assert r["comparison_available"] is True and r["indicator_only"] is True and r["requires_human_review"] is True
        assert r["score_difference"] == pytest.approx(original - blind)
        text = " ".join([r["bias_interpretation"], r["recommendation"], r["disclaimer"], r["bias_detected_note"]]).lower()
        for phrase in OVERCLAIMS:
            assert phrase not in text, phrase
        assert "not proof of bias" in r["bias_interpretation"] or "does not show the process is free of bias" in r["bias_interpretation"]

    def test_the_result_states_what_anonymization_does_not_do(self, monkeypatch):
        monkeypatch.setattr(bias, "_get_scorer", lambda: _FixedScorer(60, 60))
        r = bias._bias_score_comparison("a", "b", None)
        joined = " ".join(r["limitations"])
        assert "name is NOT removed" in joined and "Institution, employer" in joined and "structure part of the score" in joined
        assert "not a guarantee of fairness" in r["disclaimer"]

    def test_a_scoring_failure_is_reported_as_unavailable_not_as_low_bias(self, monkeypatch):
        monkeypatch.setattr(bias, "_get_scorer", lambda: _CrashingScorer())
        r = bias._bias_score_comparison("a", "b", None)
        assert r["comparison_available"] is False and r["score_difference"] is None
        assert r["bias_detected"] is False and "unavailable" in r["bias_interpretation"].lower()
        assert "low" not in r["bias_interpretation"].lower()

    def test_one_failing_side_is_also_unavailable(self, monkeypatch):
        class Half:
            calls = 0

            def calculate_ats_score(self, *a, **k):
                Half.calls += 1
                if Half.calls == 2:
                    raise RuntimeError("boom")
                return {"overall_score": 55.0, "breakdown": {}}
        monkeypatch.setattr(bias, "_get_scorer", lambda: Half())
        r = bias._bias_score_comparison("a", "b", None)
        assert r["comparison_available"] is False and r["original_score"] == 55.0 and r["blind_score"] is None

    def test_the_anonymizer_keeps_the_candidates_name_and_the_response_admits_it(self):
        anonymized, report = bias._anonymize_text(BASE_RESUME.replace("{INSTITUTION}", "Anna University"))
        assert "Priya Nair" in anonymized and "Anna University" in anonymized
        assert report["name_detected"] == "Priya Nair"

    def test_date_ranges_are_not_redacted_as_phone_numbers(self):
        text = "Acme Systems   2018 - 2022\nBeta Labs (2017-2018)\nB.Tech, 2014 - 2018, CGPA 8.4\nPhone: +91 98765 43210\n"
        anonymized, report = bias._anonymize_text(text)
        for kept in ("2018 - 2022", "(2017-2018)", "2014 - 2018"):
            assert kept in anonymized
        assert report["phones"] == ["+91 98765 43210"] and "[PHONE REDACTED]" in anonymized
        assert "Beta Labs" in anonymized and anonymized.count("\n") == text.count("\n"), "line structure was destroyed"

    @pytest.mark.parametrize("candidate, expected", [
        ("+91 98765 43210", True), ("98765 43210", True), ("9876543210", True), ("(044) 2345 6789", True),
        ("2018 - 2022", False), ("(2017-2018)", False), ("2014 - 2018 - 2022", False), ("12345", False),
    ])
    def test_phone_detection(self, candidate, expected):
        assert bias._looks_like_phone(candidate) is expected

    def test_single_sign_on_survives_but_a_marital_status_does_not(self):
        text = "Implemented single sign-on and a single-page app (single point of failure).\nMarital Status: Single | Married\n"
        anonymized, report = bias._anonymize_text(text)
        assert "single sign-on" in anonymized and "single-page" in anonymized and "single point of failure" in anonymized
        assert "[MARITAL STATUS REDACTED]" in anonymized and "Married" not in anonymized
        assert "Single" not in anonymized.split("\n")[1]

    def test_dates_of_birth_are_still_redacted_and_the_line_break_is_kept(self):
        anonymized, report = bias._anonymize_text("Priya\nDOB: 12/05/1998\nEXPERIENCE\nEngineer\nAge: 27\n\nSKILLS\nPython\n")
        assert "12/05/1998" not in anonymized and "\nEXPERIENCE\n" in anonymized
        assert "[AGE REDACTED]" in anonymized and "Age: 27" not in anonymized

    def test_the_endpoints_carry_the_limitations(self):
        files = {"file": ("a.docx", fx.make_docx(), fx.DOCX_MIME)}
        body = client.post("/api/v1/bias/anonymize", files=files).json()
        assert body["requires_human_review"] is True and any("name is NOT removed" in x for x in body["limitations"])

    def test_the_text_compare_endpoint_carries_the_indicator_framing(self, monkeypatch):
        monkeypatch.setattr(bias, "_get_scorer", lambda: _FixedScorer(72.0, 60.0))
        body = client.post("/api/v1/bias/compare-text", data={"resume_text": BASE_RESUME.replace("{INSTITUTION}", NEUTRAL)}).json()
        assert body["score_shift_flag"] is True and body["requires_human_review"] is True and body["indicator_only"] is True
        assert "not proof of bias" in body["bias_interpretation"]


# ════════════════════════════════════════════════════════════════════════════
# UI wording and config
# ════════════════════════════════════════════════════════════════════════════

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestUiWording:
    @pytest.fixture(scope="class")
    def html(self):
        return open(os.path.join(ROOT, "frontend", "index.html"), encoding="utf-8").read()

    @pytest.mark.parametrize("claim", [
        "Bias-Free", "No bias detected", "appears authentic", "No major fraud indicators detected", ">Fraud Score<",
        "Detect fake or inflated", "Anonymous Score", "— bonus applied'",
    ])
    def test_the_page_no_longer_makes_the_overclaim(self, html, claim):
        assert claim not in html, claim

    @pytest.mark.parametrize("wording", [
        "not proof of dishonesty", "does not verify the resume", "A bias-reduction aid, not a guarantee of fairness",
        "not proof of bias", "adds no points", "Indicator Score", "Blind Screening",
    ])
    def test_the_page_says_what_these_tools_are(self, html, wording):
        assert wording in html, wording

    def test_every_new_dynamic_value_is_escaped(self, html):
        for needle in ("escH(d.disclaimer", "escH(l)", "escH(d.bias_interpretation"):
            assert needle in html, needle

    def test_a_comparison_that_could_not_run_is_handled(self, html):
        assert "d.comparison_available===false" in html


class TestConfig:
    def test_valid_and_invalid_policy_values(self):
        assert read_choice_setting("X", "off", {"off", "legacy"}, environ={}) == "off"
        assert read_choice_setting("X", "off", {"off", "legacy"}, environ={"X": " LEGACY "}) == "legacy"
        with pytest.raises(ScoringConfigError, match="X must be one of"):
            read_choice_setting("X", "off", {"off", "legacy"}, environ={"X": "prestige"})

    @pytest.mark.parametrize("value", ["abc", "-1", "31", "1.5"])
    def test_the_bonus_cap_is_validated(self, value):
        with pytest.raises(ResourceLimitConfigError):
            read_int_setting("SCORING_INDIA_BONUS_CAP", 15, minimum=0, maximum=30, environ={"SCORING_INDIA_BONUS_CAP": value})

    def test_defaults_and_the_startup_description(self, monkeypatch):
        assert settings.SCORING_PRESTIGE_BONUSES == "off" and settings.SCORING_INDIA_BONUS_CAP == 15
        assert "evidence-only" in describe_scoring_policy(settings) and "adds no points" in describe_scoring_policy(settings)
        monkeypatch.setattr(settings, "SCORING_PRESTIGE_BONUSES", "legacy")
        assert "ENABLED" in describe_scoring_policy(settings)
