# EasyRecruit AI — Hack for Social Cause

## Project overview

EasyRecruit AI is an AI-assisted recruitment prototype that analyses resumes against job descriptions. It uses document text extraction, natural-language processing, keyword and skill matching, and weighted scoring to present structured information that recruiters can review.

## Problem statement

Recruiters may need to review many resumes under time constraints. Manual screening can be repetitive, and exact keyword matching may miss relevant experience when applicants describe similar skills using different terminology. At the same time, recruitment tools must handle personal data responsibly and avoid presenting automated scores as unquestionable decisions.

## Proposed solution

EasyRecruit AI explores a workflow in which a recruiter provides a job description and one or more resumes, then receives structured candidate-analysis results. The system can analyse relevant keywords and skills, use semantic similarity when the model is available, and explain its weighted scoring dimensions. Batch processing is intended to reduce repetitive handling of multiple applications.

## Intended social value

- Help organise resume information for a more efficient initial review.
- Support consideration of relevant skills expressed in different terminology.
- Encourage structured assessment against job-related requirements.
- Keep final hiring decisions with human reviewers.
- Make limitations and scoring behaviour more visible to users.

The project is aligned in intent with **UN SDG 8: Decent Work and Economic Growth** and **UN SDG 10: Reduced Inequalities**. The alignment describes the project's intended direction; it is not proof of measured social impact, equal outcomes, or elimination of bias.

## Current implementation

The supplied Task 7 source includes a Python/FastAPI backend, a web frontend, resume parsing, NLP-based analysis, weighted scoring, batch analysis, authentication/access controls, upload safeguards, and automated tests. The project includes handling for non-finite scoring values and documents scoring behaviour.

## Team

- **SURYA K** — Project lead, backend development, and system integration
- **KAVIMANI K** — Frontend development and user interface
- **ANUGAYATRE S** — Testing, documentation, and validation

These are the team's working role descriptions and should be revised if they do not match the members' agreed responsibilities.

## Limitations and responsible use

This is a prototype and should not be used as the sole basis for hiring, rejection, or ranking decisions. Scores are not hiring probabilities. The real semantic model and deployment environments were not fully verified in the supplied Task 7 report. No labelled evaluation data was supplied to substantiate accuracy or fairness claims, and no real-world impact has been measured.

## Future work

1. Verify the real semantic model in a reproducible environment.
2. Evaluate matching quality with appropriately consented, representative, and job-relevant data.
3. Test usability with recruiters and incorporate their feedback.
4. Examine differences in results across relevant groups and investigate potential sources of bias.
5. Strengthen deployment, monitoring, privacy, and data-retention practices before any real-world pilot.

## Demo and setup

Follow the local setup instructions in [README.md](README.md). Use synthetic or anonymised resumes for demonstrations and do not upload personal data without appropriate permission and safeguards.
