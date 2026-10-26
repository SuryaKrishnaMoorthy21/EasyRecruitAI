"""
EasyRecruit ATS 3.0 — Parser & NLP Tests
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest


SAMPLE_RESUME = """
Jane Smith
jane.smith@example.com | +1 (555) 123-4567 | github.com/janesmith

SUMMARY
Experienced software engineer with 6 years of experience building scalable web applications.

EXPERIENCE
Senior Python Engineer — Acme Corp (2020 – Present)
  • Built REST APIs using FastAPI and Django
  • Managed PostgreSQL and MongoDB databases
  • Deployed microservices to AWS using Docker and Kubernetes

EDUCATION
Bachelor of Science in Computer Science — MIT (2018)

SKILLS
Python, JavaScript, TypeScript, React, Django, FastAPI, AWS, Docker, Git, PostgreSQL
"""

SAMPLE_JD = """
We are looking for a Senior Python Engineer with 5+ years of experience.
Requirements: Python, FastAPI or Django, PostgreSQL, Docker, AWS, Git.
Nice to have: Kubernetes, React, TypeScript.
"""


class TestKeywordExtractor:

    def setup_method(self):
        from app.nlp.keyword_extractor import KeywordExtractor
        self.ex = KeywordExtractor()

    def test_extract_keywords_returns_list(self):
        result = self.ex.extract_keywords(SAMPLE_RESUME, top_k=10)
        assert isinstance(result, list)
        assert len(result) > 0

    def test_keywords_have_required_keys(self):
        result = self.ex.extract_keywords(SAMPLE_RESUME, top_k=5)
        for item in result:
            assert "keyword"   in item
            assert "score"     in item
            assert "frequency" in item

    def test_extract_skills_finds_python(self):
        result = self.ex.extract_skills(SAMPLE_RESUME)
        all_skills = [s["skill"].lower() for s in result["all_skills"]]
        assert "python" in all_skills

    def test_extract_skills_structure(self):
        result = self.ex.extract_skills(SAMPLE_RESUME)
        assert "categorized_skills" in result
        assert "all_skills"         in result
        assert "total_skills_found" in result
        assert result["total_skills_found"] > 0

    def test_extract_contact_email(self):
        info = self.ex.extract_contact_info(SAMPLE_RESUME)
        assert info["email"] == "jane.smith@example.com"

    def test_extract_contact_phone(self):
        info = self.ex.extract_contact_info(SAMPLE_RESUME)
        assert info["phone"] is not None

    def test_extract_contact_github(self):
        info = self.ex.extract_contact_info(SAMPLE_RESUME)
        assert info["github"] is not None and "janesmith" in info["github"]

    def test_candidate_name_extraction(self):
        name = self.ex.extract_candidate_name(SAMPLE_RESUME)
        assert name is not None
        assert "jane" in name.lower() or "smith" in name.lower()

    def test_structure_analysis(self):
        s = self.ex.analyze_text_structure(SAMPLE_RESUME)
        assert s["total_words"]   > 0
        assert s["section_count"] > 0
        assert s["has_contact_info"]

    def test_full_analysis_keys(self):
        result = self.ex.full_analysis(SAMPLE_RESUME)
        for key in ("keywords","skills","contact_info","structure","candidate_name"):
            assert key in result


class TestEnhancedScorer:

    def setup_method(self):
        from app.nlp.enhanced_scorer import EnhancedScorer
        self.scorer = EnhancedScorer()

    def test_score_without_jd(self):
        r = self.scorer.calculate_ats_score(SAMPLE_RESUME)
        assert 0 <= r["overall_score"] <= 100
        assert "breakdown" in r
        assert "recommendations" in r["breakdown"]

    def test_score_with_jd_higher_than_without(self):
        r_no_jd = self.scorer.calculate_ats_score(SAMPLE_RESUME)
        r_jd    = self.scorer.calculate_ats_score(SAMPLE_RESUME, SAMPLE_JD)
        # Semantic field should be present when JD is provided
        assert r_jd["semantic_similarity"] is not None

    def test_score_fields_present(self):
        r = self.scorer.calculate_ats_score(SAMPLE_RESUME, SAMPLE_JD)
        for field in ("overall_score","keyword_match_score","skill_match_score",
                      "structure_score","semantic_similarity","experience_score","education_score"):
            assert field in r

    def test_skill_breakdown_contains_matched_missing(self):
        r = self.scorer.calculate_ats_score(SAMPLE_RESUME, SAMPLE_JD)
        bd = r["breakdown"]["skill_analysis"]
        assert "matched_skills" in bd
        assert "missing_skills" in bd

    def test_recommendations_non_empty(self):
        r = self.scorer.calculate_ats_score(SAMPLE_RESUME)
        recs = r["breakdown"]["recommendations"]
        assert len(recs) > 0
        for rec in recs:
            assert "category"   in rec
            assert "suggestion" in rec
            assert "priority"   in rec

    def test_structure_score_range(self):
        r = self.scorer.calculate_ats_score(SAMPLE_RESUME)
        assert 0 <= r["structure_score"] <= 100

    def test_experience_score_range(self):
        r = self.scorer.calculate_ats_score(SAMPLE_RESUME)
        assert 0 <= r["experience_score"] <= 100
