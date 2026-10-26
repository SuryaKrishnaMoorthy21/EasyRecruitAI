# EasyRecruit ATS 3.0

Smart Applicant Tracking System — AI-powered resume analysis.

---

## ⚡ Quick Start (Windows)

### Option A — Automatic (Recommended)
1. Extract the ZIP
2. Double-click **`install_and_run.bat`**
3. Wait for setup (first run downloads ~500 MB of NLP models)
4. Browser → **http://localhost:8001**

### Option B — Manual
```cmd
cd EasyRecruit3.0

:: Create & activate virtual environment
python -m venv venv
venv\Scripts\activate

:: Install packages
pip install -r requirements.txt

:: Download spaCy model
python -m spacy download en_core_web_sm

:: Create .env
copy .env.example .env

:: Create data folder
mkdir app\data

:: Start server
python -m uvicorn main:app --host 0.0.0.0 --port 8001 --reload
```

Then open **http://localhost:8001** in your browser.

---

## ⚠️ IMPORTANT: Always open via http://localhost:8001

**Do NOT open `frontend/index.html` directly in your browser** (file:// protocol).
The frontend must be served through the FastAPI server — open `http://localhost:8001` instead.

---

## Troubleshooting

| Error | Fix |
|---|---|
| `ModuleNotFoundError: fastapi` | Run `venv\Scripts\activate` first, then `pip install -r requirements.txt` |
| `starlette version conflict` | Already fixed — requirements.txt no longer pins starlette |
| `bcrypt AttributeError` | Already fixed — requirements.txt uses `bcrypt==4.0.1` |
| Login/Register spins but nothing happens | You opened `index.html` as a file. Use `http://localhost:8001` |
| `Port 8001 in use` | Change `PORT=8001` to `PORT=8002` in `.env` and restart |
| spaCy model not found | Run: `python -m spacy download en_core_web_sm` in your venv |

---

## First administrator & security notes

* No admin accounts are created automatically. On a fresh install create one with
  `python -m app.admin_cli provision --role admin --email you@example.com` (see DEPLOY.md).
* Upgrading from an older database? **Rotate the legacy company-admin passwords**: see
  "Rotating Existing Administrator Credentials" in DEPLOY.md.
* `SECRET_KEY` must be 32+ random characters unless `DEBUG=true` (the `.env.example` used by
  `install_and_run.bat` already sets `DEBUG=true` for local use).
* Never zip the project folder to share it: run `python scripts/make_source_dist.py`.

## API Docs (Debug mode only)

Set `DEBUG=true` in `.env`, then visit:
- Swagger: http://localhost:8001/api/docs
- ReDoc:   http://localhost:8001/api/redoc

