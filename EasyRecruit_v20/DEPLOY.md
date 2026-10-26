# EasyRecruit ATS 3.0 — Deployment Guide
## Run Online So Anyone Can Use It (Data Stored & Persistent)

---

## Which Platform Should I Use?

| Platform | Free? | Sleep on idle? | Persistent DB | Best for |
|---|---|---|---|---|
| **Render** | ✅ Yes | Yes (free tier) | ✅ Yes (disk) | **Recommended — easiest** |
| **Railway** | ✅ $5 credit/mo | No | ✅ Yes (volume) | Best free always-on |
| **Fly.io** | ✅ Yes | No | ✅ Yes (volume) | Most reliable free |
| **Docker VPS** | ❌ ~$5/mo | No | ✅ Yes | Full control |

**Recommendation:** Start with **Render** (completely free, 5-minute setup). If you need the app to always be awake (no cold start), move to **Railway**.

---

## Option A — Render.com (Recommended, 100% Free)

### Step 1 — Push to GitHub
1. Create a free account at [github.com](https://github.com)
2. Create a **new repository** (e.g. `easyrecruit-ats`) — set it to **Private** if you want
3. Upload all the project files into the repository

   **Easiest way using GitHub website:**
   - Click "uploading an existing file"
   - Drag and drop all files from the `EasyRecruit3.0_v9` folder
   - Click "Commit changes"

   **Or using Git on your computer:**
   ```bash
   cd EasyRecruit3.0_v9
   git init
   git add .
   git commit -m "Initial deploy"
   git remote add origin https://github.com/YOUR_USERNAME/easyrecruit-ats.git
   git push -u origin main
   ```

### Step 2 — Deploy on Render
1. Go to [render.com](https://render.com) → Sign up free (use GitHub login)
2. Click **"New +"** → **"Web Service"**
3. Connect your GitHub account → select your `easyrecruit-ats` repository
4. Fill in the form:

   | Field | Value |
   |---|---|
   | **Name** | `easyrecruit-ats` |
   | **Region** | Singapore (closest to India) |
   | **Branch** | `main` |
   | **Runtime** | `Python 3` |
   | **Build Command** | `pip install -r requirements.txt && python -m spacy download en_core_web_sm` |
   | **Start Command** | `uvicorn main:app --host 0.0.0.0 --port $PORT` |
   | **Plan** | Free |

5. Scroll down to **"Environment Variables"** → click **"Add Environment Variable"**:

   | Key | Value |
   |---|---|
   | `SECRET_KEY` | (click "Generate"; must be 32+ random characters) |
   | `DEBUG` | `false` |
   | `DATABASE_PATH` | `/app/app/data/easyrecruit.db` |
   | `LOG_LEVEL` | `INFO` |

6. Scroll to **"Disks"** → click **"Add Disk"**:

   | Field | Value |
   |---|---|
   | **Name** | `easyrecruit-data` |
   | **Mount Path** | `/app/app/data` |
   | **Size** | `1 GB` |

   > ⚠️ This disk is critical — it stores your SQLite database with all users and analyses. Without it, data resets on every deploy.

7. Click **"Create Web Service"**

### Step 3 — Wait for Build (~5–8 minutes)
Render installs Python packages (spaCy + sentence-transformers are large). Watch the logs. When you see:
```
EasyRecruit ATS 3.0  —  Starting up
Database ready.
```
Your app is live!

### Step 4 — Your Live URL
Render gives you a URL like: `https://easyrecruit-ats.onrender.com`

Share this with anyone — they can register and use it from any device.

### ⚠️ Free Tier Limitation
On the free tier, Render **sleeps the app after 15 minutes of inactivity**. The first person to visit after a sleep period will wait ~30–60 seconds for it to wake up. If you want it always-on, upgrade to the "Starter" plan ($7/month) or use Railway below.

---

## Option B — Railway.app (Free $5 credit/month, No Sleep)

1. Go to [railway.app](https://railway.app) → Sign up with GitHub
2. Click **"New Project"** → **"Deploy from GitHub repo"** → select your repo
3. Railway auto-detects the `Dockerfile` and builds it
4. Go to **Variables** tab → add:
   - `SECRET_KEY` = a random string of 32+ characters (the app refuses to start with a short or placeholder key when `DEBUG=false`)
   - `DATABASE_PATH` = `/data/easyrecruit.db`
   - `DEBUG` = `false`
5. Go to **Volumes** tab → add a volume:
   - Mount path: `/data`
6. Go to **Settings** → **Domains** → click "Generate Domain"

Your app is live at `https://your-app.railway.app`

The $5 free credit covers ~500 hours/month — enough for a small team.

---

## Option C — Docker on a VPS (~$5/month, Full Control)

If you want complete control and guaranteed uptime, rent a cheap server:
- **DigitalOcean Droplet** — $4/month (1 GB RAM)
- **Hetzner Cloud** — €3.29/month (best value in India region)
- **AWS Lightsail** — $3.50/month

### Setup on your VPS:
```bash
# 1. Install Docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER

# 2. Clone your repo
git clone https://github.com/YOUR_USERNAME/easyrecruit-ats.git
cd easyrecruit-ats

# 3. Set your secret key
echo "SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')" > .env

# 4. Start the app
docker-compose up -d

# 5. View logs
docker-compose logs -f

# App runs at http://YOUR_SERVER_IP:8001
```

### Make it available at a domain name (optional):
```bash
# Install nginx
sudo apt install nginx

# Create config
sudo nano /etc/nginx/sites-available/easyrecruit
```
Paste:
```nginx
server {
    listen 80;
    server_name yourdomain.com;

    # nginx's default is 1m, which rejects most resumes before the app sees them.
    # Keep this at or above MAX_BATCH_REQUEST_SIZE (default about 151 MB; see "Upload and analysis limits").
    client_max_body_size 160m;

    location / {
        proxy_pass http://localhost:8001;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```
```bash
sudo ln -s /etc/nginx/sites-available/easyrecruit /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

# Free SSL certificate
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d yourdomain.com
```

---

## Data Storage & Persistence

### Where is user data stored?
All data lives in a **SQLite database** at the path set by `DATABASE_PATH`:
- Users, passwords (hashed), roles
- All resume analyses and scores
- Job descriptions
- Login history

### Is data safe across updates?
Yes — when you update and redeploy:
- **Render/Railway:** The persistent disk/volume is separate from the code. Redeploys never touch the database.
- **Docker VPS:** The `easyrecruit_data` Docker volume persists across container restarts and image rebuilds.

### How to back up the database?

**On Render:** Go to your service → "Disks" → Download the file, or SSH in:
```bash
# Download using render shell
cat /app/app/data/easyrecruit.db > backup.db
```

**On Docker VPS:**
```bash
docker cp easyrecruit-ats-web-1:/app/app/data/easyrecruit.db ./backup_$(date +%Y%m%d).db
```

**Automated daily backup (VPS):**
```bash
# Add to crontab: crontab -e
0 2 * * * docker cp easyrecruit-ats-web-1:/app/app/data/easyrecruit.db /home/user/backups/easyrecruit_$(date +\%Y\%m\%d).db
```

---

## Creating the First Admin Account

EasyRecruit **no longer creates any user accounts automatically.** Older versions
pre-created one `company_admin` per company with passwords stored in source code;
that has been removed. Admin accounts are provisioned explicitly, with a password
you choose, and `/auth/register` will never create one (it returns 403).

**Path 1 — environment variables (works without a shell, e.g. Render free tier)**

Set these *once* on a fresh deployment, start the app, then **delete them**:

```
INITIAL_ADMIN_EMAIL=you@yourdomain.example
INITIAL_ADMIN_PASSWORD=<your own strong password: 12+ chars, upper+lower+digit>
# optional: INITIAL_ADMIN_ROLE=admin (default) | company_admin  (+ INITIAL_ADMIN_COMPANY)
```

It only acts if **no** admin/company_admin exists yet, so leaving it set cannot create
extra accounts later (but remove it anyway: if every admin were deleted it would
recreate that one). A rejected password is logged as an error and nothing is created.

**Path 2 — CLI (any host with a shell: VPS, Docker, Railway shell, your PC)**

```bash
python -m app.admin_cli provision --role admin --email you@yourdomain.example
python -m app.admin_cli provision --role company_admin --company "Zoho Corporation" --email ops@zoho.example
python -m app.admin_cli list-admins
```
The password is prompted twice (not echoed), or read from `EASYRECRUIT_ADMIN_PASSWORD`,
or generated with `--generate` (printed once to your terminal). It is never accepted
as a command-line argument. In Docker: `docker-compose exec easyrecruit python -m app.admin_cli ...`
(use the service name from `docker-compose.yml`).

Password policy for these accounts: 12–72 bytes, upper + lower + digit, and no common
words or parts of the e-mail/username.

---

## 🔐 Rotating Existing Administrator Credentials

**Who needs this:** anyone running a database created by an earlier version (including the
`easyrecruit.db` that shipped in earlier ZIPs). Those databases contain 20 `company_admin`
accounts (`admin@<company recruiter domain>`) whose passwords were published in source code
and in a credentials hand-out. **Treat all 20 as compromised.** Editing source code does *not*
change passwords already stored in a database, and nothing is rotated or disabled automatically.

1. **Back up first.** Stop the app (or use SQLite's online backup) and copy the DB:
   `sqlite3 app/data/easyrecruit.db ".backup 'easyrecruit-backup.db'"`. Keep the backup private:
   it contains password hashes and personal data.
2. **Inventory.** `python -m app.admin_cli list-admins` shows every admin, whether it is active,
   `login_count` and `last_login`. Unexpected logins on accounts nobody uses are a warning sign;
   also review recently approved recruiters.
3. **Decide per account.**
   * *Real person uses it* → rotate (step 4).
   * *Nobody uses it* (sample/demo companies) → deactivate it rather than keep a known-password
     account. Deactivation needs a platform admin, and databases from older versions have none, so
     first run `provision --role admin ...`, log in, then call
     `PATCH /api/v1/auth/admin/users/{id}/toggle`. Deactivate, don't delete; nothing re-creates it.
4. **Rotate.** For each account in use:
   `python -m app.admin_cli rotate --email admin@<domain> --generate`
   Give the one-time password to the account's owner through a secure channel (password manager
   share or in person; not plain e-mail/chat) and ask them to change it immediately via
   `POST /api/v1/auth/change-password`. The tool changes only the password hash and verifies it.
   *No shell available?* The owner can log in with the old password and call `change-password`
   right away. This works but races any attacker who already knows the old one, so prefer the CLI.
5. **End existing sessions.** A password change does **not** revoke login tokens already issued
   (valid up to `ACCESS_TOKEN_EXPIRE_MINUTES`, default 24 h). After rotating, set a **new
   `SECRET_KEY`** and restart: every token becomes invalid and all users sign in again.
6. **Retire the old secrets.** Delete `CompanyAdmin_Credentials.docx` and any copies (drives,
   e-mail, chat). If a repository or ZIP containing the old `company_seed.py` was shared, its
   history still contains the old passwords; rotation is what makes them useless.
7. **Verify.** Try logging in with an old password: it must return 401.

---

## 📦 Building a Safe Source Distribution

`.gitignore` only protects git. A ZIP made by zipping the project folder includes whatever is in
it: a populated database, `.env.production`, uploaded photos. To share or archive the project use:

```bash
python scripts/make_source_dist.py                    # -> dist/<project>-source.zip
python scripts/make_source_dist.py --verify some.zip  # audit any existing ZIP
```
It refuses `.env*` (except the `*.example` templates), everything under `app/data/`, SQLite files
(detected by content, whatever the name), logs, keys and credential hand-outs, then re-checks the
finished archive. Copy `.env.production.example` to `.env` on the server and fill it in; the
`.dockerignore` keeps the same files out of Docker images. A fresh install creates its own empty
database on first start.

---

## 🌐 CORS: which websites may call the API

EasyRecruit serves its own frontend, so browsers talk to the API **from the same origin** and no CORS
setup is needed. That is why the shipped default is an **empty** `CORS_ORIGINS` (cross-origin access off).

* **Separate frontend site?** Set `CORS_ORIGINS` to its exact origins, comma-separated:
  `CORS_ORIGINS=https://app.example.com,https://admin.example.com` (scheme + host [+ port]; no path, no `*`).
* **`DEBUG=false` (production) refuses to start** with `*`, `null`, `http://` origins, paths, wildcards inside an
  origin, credentials in the URL, or any malformed entry. The error names the problem (never the value) and the fix.
* **`DEBUG=true` (local development):** unset = loopback origins only (`localhost`/`127.0.0.1` on the app port,
  3000, 5173, 5500). `CORS_ORIGINS=*` is still accepted so `frontend/index.html` can be opened from disk,
  but credentials are always disabled for it.
* Credentialed cross-origin access is enabled only for an explicit allowlist, never together with `*`.
* The effective policy is logged once at startup (`CORS : ...`); origins are not secrets, nothing else is logged.

**Upgrading an existing deployment:** earlier versions ignored `CORS_ORIGINS` and allowed every origin. Before
redeploying, remove or replace any `CORS_ORIGINS=*` you set: Render → *Environment* tab, Railway → *Variables*,
Docker → your shell/`.env`, a copied `.env.production`. Otherwise the new version will refuse to start and tell you why.

---

## 📤 Upload and analysis limits

Resume uploads are checked **before** they cost memory or CPU. Every limit has a safe default and needs no setting;
the defaults keep the documented "10 MB per file, up to 15 files per batch".

* **Size.** Each file is copied to disk in small chunks and refused the moment it passes `MAX_FILE_SIZE` (10 MB); the
  whole request is capped before it is parsed (`MAX_BATCH_REQUEST_SIZE` for batches, default `MAX_FILE_SIZE × MAX_BATCH_FILES
  + 1 MB`, about 151 MB). Real resumes are small, so **consider lowering it** (e.g. `MAX_BATCH_REQUEST_SIZE=52428800`).
  Oversized requests get `413` with a message that names the limit.
* **Types.** PDF and DOCX only, judged by extension, declared MIME type **and the file's actual contents**. Legacy `.doc`
  (never supported), password-protected, empty, corrupt, scanned-only and non-resume files are refused with a specific message.
  A DOCX may expand to at most `MAX_DOCX_UNCOMPRESSED_SIZE` (20 MB) and a PDF may have at most `MAX_PDF_PAGES` (50) pages.
* **CPU and memory.** At most `ANALYSIS_CONCURRENCY` documents are parsed/scored at the same time **per worker process**
  (default 2; the Docker image runs 2 workers, so 4 in total). One step may run `ANALYSIS_TIMEOUT_SECONDS` (120 s); a batch
  `BATCH_TIMEOUT_SECONDS` (300 s), after which unfinished files are reported as failed and finished ones are kept. A
  single-file request that cannot get a slot within `ANALYSIS_QUEUE_TIMEOUT_SECONDS` (60 s) receives `503` + `Retry-After`.
  A step that exceeds its timeout cannot be killed, so it keeps its slot until it really ends. **On small hosts (about 512 MB
  RAM, e.g. Render free) set `ANALYSIS_CONCURRENCY=1`.**
* **Saving.** An analysis is stored in a user's history only when the request sends `save_to_db=true` (the UI's
  "Save result to history" box does). Earlier versions stored every signed-in analysis whatever the box said, so API clients that
  relied on that must now send the flag. Responses report `saved` and, if saving was requested but failed, `save_error`.
* **Reverse proxy.** nginx rejects bodies over 1 MB by default; set `client_max_body_size` to at least your
  `MAX_BATCH_REQUEST_SIZE` (the example above does).
* Invalid values (`MAX_BATCH_FILES=abc`, `ANALYSIS_CONCURRENCY=0`, ...) stop startup with a message naming the variable.
  The effective limits are logged once at startup (`Uploads : ...`).

---

## ⚖️ Scoring policy and transparency

The ATS score is a **screening aid built from heuristics**. It has not been validated against labelled hiring outcomes, so
nothing in the product claims a measured accuracy, and every result says so (`scoring_explanation.limitations`).

* **Weights** (unchanged): with a job description and a working embedding model, keyword 20% / skill 25% / structure 15% /
  semantic 20% / experience 10% / education 10%. Without a job description, **or when the embedding model is unavailable or
  fails**, the semantic term is left out and the weights are keyword 25% / skill 30% / structure 20% / experience 15% /
  education 10%, so a missing model never costs a candidate points. Both tables total 100% and are checked at startup.
* **Explanations.** Every result carries `scoring_explanation`: the scheme and weights actually used, each dimension's score
  and points, the India-context adjustment and why, any caps, notes and warnings. A dimension that fails internally is shown as
  a labelled placeholder in `warnings`, never silently.
* **`extraction_confidence`** (formerly `accuracy_estimate`, kept as a deprecated alias with the same value) is a heuristic
  about how completely the text was parsed (name, email, resume-like structure, domain match). It is **not** a measured accuracy.
* **Fairness: `SCORING_PRESTIGE_BONUSES`** (default `off`). Institution and employer *prestige* add **no** points: where
  someone studied or worked is not evidence about the job. Certifications and India-specific skills still earn points, but
  only when they are relevant to the job description (always when there is none). Institution and employer names are still
  detected and shown for information. Set `legacy` to restore the pre-Task-6 prestige points (institution +10/+5, employer
  +6/+3, education-tier bumps +15/+10/+5, relevance-free certification credit); detection stays exact either way, and the
  explanation then carries a note saying prestige bonuses are enabled. `SCORING_INDIA_BONUS_CAP` (default 15) bounds the
  India-context adjustment.
* **Fraud and bias tools** report *indicators for human review*, never findings: results carry `requires_human_review`,
  a `disclaimer` and the limits of the method. The blind-screening comparison is a bias-**reduction** aid; the candidate's
  name, college and employer remain in the text, and a score difference is not proof of bias.
* **Behaviour change to expect.** Scores of resumes that mention a prestigious institution or employer, or that used to trip a
  substring false positive ("unit tests", "HIIT", "credit", "VPN", "ahead of schedule"...), go down by the points those had
  wrongly added; other resumes score exactly as before. Use `SCORING_PRESTIGE_BONUSES=legacy` to restore the old policy.

---

## Environment Variables Reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `SECRET_KEY` | ✅ Yes | none | JWT signing key, 32+ random chars. With `DEBUG=false` the app **refuses to start** if it is missing, short or a placeholder |
| `INITIAL_ADMIN_EMAIL` / `INITIAL_ADMIN_PASSWORD` | No | unset | Creates ONE first admin on a fresh deployment (see below). Remove after first start |
| `INITIAL_ADMIN_ROLE` / `INITIAL_ADMIN_COMPANY` | No | `admin` / unset | `company_admin` needs a registered company name |
| `DATABASE_PATH` | ✅ Yes | `app/data/easyrecruit.db` | Full path to SQLite file |
| `DEBUG` | No | `false` | Enable API docs at `/api/docs` |
| `PORT` | No | `8001` | HTTP port (Render/Railway inject this) |
| `LOG_LEVEL` | No | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | No | `1440` | JWT lifetime (1440 = 24 hours) |
| `CORS_ORIGINS` | No | empty | Browser origins allowed to call the API. **Empty = same-origin only** (right for this app). Exact https origins only; `*`/`http://` abort startup when `DEBUG=false` (see CORS section) |
| `MAX_FILE_SIZE` | No | `10485760` | Largest accepted resume file, in bytes (see Upload and analysis limits) |
| `MAX_BATCH_FILES` | No | `15` | Files per batch request |
| `MAX_BATCH_REQUEST_SIZE` | No | `MAX_FILE_SIZE × MAX_BATCH_FILES + 1 MB` | Largest batch request body, in bytes. Lower it for small resumes |
| `MAX_PDF_PAGES` | No | `50` | PDFs with more pages are refused |
| `MAX_DOCX_UNCOMPRESSED_SIZE` | No | `20971520` | Decompression-bomb guard: largest size a `.docx` may expand to, in bytes |
| `MAX_EXTRACTED_TEXT_CHARS` | No | `300000` | More extracted text than this is refused |
| `ANALYSIS_CONCURRENCY` | No | `2` | Documents parsed/scored at once, **per worker process**. Use `1` on ~512 MB hosts |
| `ANALYSIS_TIMEOUT_SECONDS` | No | `120` | Longest a single parse or scoring step may run |
| `ANALYSIS_QUEUE_TIMEOUT_SECONDS` | No | `60` | How long a single-file request waits for a free slot before `503` |
| `BATCH_TIMEOUT_SECONDS` | No | `300` | Whole-batch deadline; unfinished files are reported as failed |
| `SCORING_PRESTIGE_BONUSES` | No | `off` | `off`: institution/employer prestige adds no points. `legacy`: restore the pre-Task-6 prestige points (see Scoring policy) |
| `SCORING_INDIA_BONUS_CAP` | No | `15` | Most points the India-context adjustment may add (0-30) |

---

## Troubleshooting

**Build fails with "No module named spacy"**
→ Make sure the build command includes `python -m spacy download en_core_web_sm`

**App starts but database errors appear**
→ Check `DATABASE_PATH` env var and make sure the disk/volume is mounted at the right path

**"Internal server error" on analysis**
→ The spaCy model may not have downloaded. Check the build logs for the spacy download step.

**App sleeps on Render free tier**
→ Use [cron-job.org](https://cron-job.org) to ping `https://your-app.onrender.com/api/v1/health` every 14 minutes — this keeps it awake for free.

**Users can't register — "Email already registered"**
→ Normal behaviour. Direct them to the login tab.
