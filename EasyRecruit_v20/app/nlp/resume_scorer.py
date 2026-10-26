"""
EasyRecruit ATS 3.0 — Basic Resume Scorer
Lightweight scorer used by the public /analyze endpoint (no job description required).
Delegates to EnhancedScorer internally so scoring logic is centralised.
"""
from typing import Dict, Optional
import logging

logger = logging.getLogger(__name__)


class ResumeScorer:
    """Thin wrapper around EnhancedScorer for the public quick-analyse endpoint."""

    def __init__(self, model_name: Optional[str] = None):
        from app.nlp.enhanced_scorer import EnhancedScorer
        self._scorer = EnhancedScorer(model_name)
        logger.info("ResumeScorer initialised (delegates to EnhancedScorer)")

    def calculate_ats_score(
        self,
        resume_text: str,
        job_description: Optional[str] = None,
        required_skills: Optional[Dict] = None,
    ) -> Dict:
        return self._scorer.calculate_ats_score(resume_text, job_description, required_skills)

    def compare_with_job(self, resume_text: str, job_description: str) -> Dict:
        """Convenience method: full analysis with a mandatory job description."""
        return self._scorer.calculate_ats_score(resume_text, job_description)
