# EasyRecruit AI

**AI-assisted resume screening and candidate–job matching**  
**Hackathon:** Hack for Social Cause  
**Project team:** Surya K · Kavimani K · Anugayatre S

EasyRecruit AI is a recruitment-assistance project designed to help recruiters organise resume information and compare candidate qualifications with job requirements. It combines document parsing, natural-language processing (NLP), keyword and skill matching, and a weighted scoring approach to produce structured results for human review.

> **Responsible-use note:** EasyRecruit AI is a decision-support prototype, not an automated hiring authority. A score is not a probability of getting a job, and it must not be used as the sole basis for rejecting or selecting a candidate. Matching quality and fairness require further evaluation with representative data and human review.

## The social problem

Manual resume screening can take substantial time, and strict keyword matching can overlook relevant qualifications described in different words. EasyRecruit AI explores whether software-assisted analysis can make the initial review process more organised and help recruiters compare applications against stated job requirements.

The project is aligned in intent with **UN Sustainable Development Goal 8 (Decent Work and Economic Growth)** and **SDG 10 (Reduced Inequalities)**. This is an intended contribution, not evidence that the system has already improved employment outcomes or eliminated bias.

## Current capabilities

- Resume text extraction from supported PDF and DOCX files.
- Job-description and resume analysis using NLP-based processing.
- Keyword and skill matching, with semantic similarity when the model is available.
- Weighted candidate scoring with an explanation of scoring dimensions.
- Batch resume analysis for up to 15 files per request, with per-file results.
- User authentication and role/access controls.
- Upload validation and request limits intended to reduce unsafe or excessive input.
- Security and regression tests covering areas such as access control, CORS, uploads, scoring, and frontend injection protections.

Feature availability and results depend on configuration, input quality, and whether the semantic model can be loaded. See [DEPLOY.md](DEPLOY.md) and [FRONTEND_SECURITY.md](FRONTEND_SECURITY.md) for additional technical details.

## Technology

- **Backend:** Python, FastAPI, Uvicorn
- **NLP / machine learning:** spaCy, Sentence Transformers, scikit-learn, NumPy
- **Document processing:** pdfplumber, python-docx
- **Database:** SQLite by default in the supplied configuration
- **Frontend:** HTML, CSS, and JavaScript
- **Testing:** pytest and a JavaScript security-test harness

## Run locally

The supplied requirements and setup have been developed around **Python 3.11**. Use a virtual environment and do not use production credentials for local testing.

### 1. Get the code

```bash
git clone <YOUR-GITHUB-REPOSITORY-URL>
cd EasyRecruit_v20
```

Replace the repository URL with the actual GitHub URL after the repository is created. If you downloaded the source ZIP instead, extract it and open a terminal in the extracted project directory.

### 2. Create a virtual environment and install dependencies

**Windows (PowerShell):**

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m spacy download en_core_web_sm
```

**macOS / Linux:**

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m spacy download en_core_web_sm
```

Sentence Transformers may need to download its model on first use. Model availability depends on network access, installed dependencies, and local resources.

### 3. Configure environment variables

Copy `.env.example` to `.env` and update the values for your environment.

Windows:

```powershell
Copy-Item .env.example .env
```

macOS / Linux:

```bash
cp .env.example .env
```

For local development, replace the example `SECRET_KEY` with a random secret. Do not commit `.env`, real credentials, databases, uploaded resumes, or other personal data. Review `.env.production.example` and [DEPLOY.md](DEPLOY.md) before any deployment. Do not expose debug API documentation in production.

### 4. Start the application

From the project root:

```bash
python -m uvicorn main:app --host 127.0.0.1 --port 8001 --reload
```

Open **http://127.0.0.1:8001** in your browser. Do not open `frontend/index.html` directly using a `file://` URL; the frontend is intended to be served by the application.

## Run tests

From the project root, with development dependencies installed:

```bash
python -m pytest -q
```

Frontend security tests are included in the Python test suite. If collection fails because a dependency is missing, install the declared requirements in a clean virtual environment and rerun the command. Record failures, errors, and skipped tests accurately; do not treat skipped tests as passing tests.

## Known limitations and validation status

This repository is based on the **Task 7** release. The accompanying Task 7 verification report recorded that the real Sentence Transformers model was not loaded in that verification environment, one model-dependent test remained failing, and deployment to Docker/Render/Railway was not verified. The model-unavailable path was tested, but that does not prove that the real semantic model works.

The project has no supplied labelled evaluation dataset that establishes predictive accuracy or fairness. There are also no verified claims of recruiter adoption, commercial revenue, or measured hiring outcomes. These are areas for future testing, not established results.

## Project team

| Team member | Project role |
|---|---|
| **SURYA K** | Project lead, backend development, and system integration |
| **KAVIMANI K** | Frontend development and user interface |
| **ANUGAYATRE S** | Testing, documentation, and validation |

The team collaborates on planning, debugging, integration, and project documentation. Roles above describe the project's working responsibilities and can be updated by the team to reflect its final agreed division of work.

## Hackathon summary

EasyRecruit AI is being prepared for **Hack for Social Cause** as a technology prototype exploring how NLP-assisted resume analysis could support a more organised recruitment workflow. The project's social objective is to reduce repetitive screening effort while keeping decisions transparent and under human control. Any claim of real-world impact will require user feedback and structured evaluation.

See [HACKATHON_SUBMISSION.md](HACKATHON_SUBMISSION.md) for a concise project description, problem statement, proposed impact, and future work.

## Security and responsible reporting

- Never commit API keys, passwords, production `.env` files, database files, candidate resumes, or other personal information.
- Use synthetic or appropriately anonymised documents for demos and tests.
- Treat fraud or bias indicators as prompts for human investigation, not definitive findings about a person.
- Report suspected security issues privately to the project maintainers rather than publishing exploit details with personal data.

## License

No open-source license has been selected for this repository yet. Unless and until a license is added, the source is not explicitly granted an open-source reuse license. The team should choose and add a license before inviting external contributions or reuse.
