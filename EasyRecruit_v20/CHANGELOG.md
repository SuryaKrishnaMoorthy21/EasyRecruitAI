# EasyRecruit ATS 3.0 — Changelog

## v20.7 (Current) — Release verification (Task 7)
Regression and release verification of Tasks 1-6. One defect class was found and fixed; nothing else changed behaviour.
- **NaN / Infinity can no longer break a response.** Injecting a non-finite number at the scorer boundary turned 27 of 36
  probes on `analysis/analyze` (36 of 36 on `analyze-batch`) into an unhandled HTTP 500, because Starlette refuses to render
  `NaN`; `json.dumps` would have stored it as the non-standard literal `NaN`; and a non-finite sub-score silently
  decided the overall score (`+inf` gave 100.0 for a resume that otherwise scored 32.6; `NaN` / `-inf` gave 0.0) while the raw
  `NaN` / `inf` was also returned as that dimension's score. Three layers now: the scorer treats a non-finite dimension like a failed one (the labelled
  placeholder and a `scoring_explanation.warnings` entry, never a made-up score) and skips a non-finite India adjustment;
  the app's default response class (`app/json_safety.py`) renders any remaining NaN / Infinity as `null`; the stored result
  blob is sanitised the same way. The warning log carries a count, never the payload.
- **Release packager** (`scripts/make_source_dist.py`) also excludes editor, coverage and OS leftovers (`.idea/`, `.vscode/`,
  `htmlcov/`, `*.egg-info/`, `.coverage`, `.DS_Store`, `Thumbs.db`, `*.swp`, `*.orig`, ...), in addition to secrets and databases.
- **Test fix, not a weakened test.** `test_shipped_templates_are_rejected_as_keys` failed on every clean checkout because it
  read `.env.production`, a local git-ignored file that is correctly never shipped. It now requires both shipped templates to
  exist and checks them, and checks `.env.production` only when present.
- **Documentation.** `.env.example` / `app/config.py` said `KEYWORD_WEIGHT` / `SKILL_WEIGHT` / `STRUCTURE_WEIGHT` /
  `SEMANTIC_WEIGHT` set the scoring weights. They are read but unused; the real weights are the `SCORE_WEIGHTS_*` constants.
  Marked as unused (the values were left alone so existing `.env` files keep working).
- New tests: `tests/test_task7_regression.py`. Still not testable here: the real sentence-transformer model (see the report).

## v20.6 — Scoring transparency and fairness
Nothing here claims improved accuracy: there is no labelled evaluation dataset, so none was measured. These are verified
defects and honesty fixes, each reproduced before it was changed.
- **Verified, unchanged.** The six JD-based weights (keyword 20 / skill 25 / structure 15 / semantic 20 / experience 10 /
  education 10) and the no-semantic scheme (25 / 30 / 20 / 15 / 10) both total 100%; every score stays within [0, 100]. They
  are now named, validated constants (`SCORE_WEIGHTS_*`), and the formula is unchanged (same arithmetic, same order).
- **Model failures no longer cost points.** `_semantic_similarity` swallowed an encoder error and returned `0.0`, which was
  scored at 20% weight and reported as a genuine zero match (44.8 instead of 54.8 on the test resume). Failures now leave the
  semantic term out, exactly like "model not loaded", and the result says so. A non-finite similarity is rejected too.
- **Missing information.** A whitespace-only job description (truthy) zeroed the keyword score; it now means "no job
  description". An empty or malformed `required_skills` falls back to the defaults. Education was credited to resumes with no
  education (the verb "be" or "Adobe" gave 62, "mastered" gave 80): degree keywords now match whole words. A dimension that
  fails internally is reported in `scoring_explanation.warnings` instead of silently using a placeholder.
- **Fairness.** Institution and employer *prestige* added up to +11.5 points for identical evidence (IIT Madras vs a neutral
  college) and outranked a candidate with more of the required skills. Under the new default (`SCORING_PRESTIGE_BONUSES=off`)
  prestige adds nothing (still detected and shown); certifications and India-specific skills count only when relevant to the
  job description. `legacy` restores the old points; `SCORING_INDIA_BONUS_CAP` bounds the adjustment. The recommendation that
  certifications "compensate for institute tier" was removed.
- **Exact detection.** Institution, employer, certification and executive-title detection used bare substring tests, so
  "unit tests" (+12.4), "HIIT" (+11.5), "chemical" (+10.2), "credit card" / "Postman" (+6.2), the first names "Sairam" /
  "Sriram", "Karnataka", "package" / "CMake" / "CCPA" (as certifications) and "VPN" / "sector" / "ahead of schedule" /
  "Active Directory" (executive band, +30 experience points) all moved scores. They now match whole words
  (`app/nlp/text_match.py`); the non-institution tokens were removed from the institution list.
- **Transparency.** New `scoring_explanation` on every result (scheme and weights used, per-dimension score and points,
  adjustments with reasons, caps, notes, warnings, limitations). `accuracy_estimate` was a heuristic (40 + name + email +
  resume-likeness + domain confidence) reported as an accuracy, with hard-coded fallbacks of 88 (API) and 85.0 (schema). It is
  now `extraction_confidence`; `accuracy_estimate` stays as a deprecated alias with the same value, is `null` rather than an
  invented number when the scorer supplies none, and the explanation states it is not a measured accuracy.
- **Fraud and bias are indicators, not findings.** Results carry `requires_human_review`, a `disclaimer` and the method's
  limits. Wording that accused ("This is suspicious.") or reassured ("appears authentic", "No bias detected", "ensure fair
  evaluation") was replaced; the fraud score is labelled a count-weighted indicator, not a probability. A genuine student's
  expected graduation year is a low-severity indicator instead of a high-risk flag. A scoring failure in the bias comparison
  is "unavailable", no longer a score of 0 reported as low bias. The anonymizer redacted date ranges ("2018 - 2022") as phone
  numbers, ate "single sign-on" as a marital status and swallowed line breaks; fixed. It still does **not** remove the
  candidate's name, college or employer, and the response and UI now say so. UI labels: "Bias-Free ..." became "Blind
  Score" / "Blind Screening"; "Fraud Score 45%" became "Indicator Score 45/100"; the India card says whether prestige scored.
- **Compatibility.** Every existing response key is kept; new keys are additive (`extraction_confidence`,
  `scoring_explanation`, `risk_level_label`, `requires_human_review`, `disclaimer`, `limitations`, `score_shift_flag`,
  `comparison_available`). Scores change only for resumes that benefited from prestige or a false positive. No schema change.
- New tests: `tests/test_scoring_transparency.py`. The two known failures (`.env.production` is a local-only file;
  `semantic_similarity` needs the downloaded model) pre-date Task 5 and are environment-specific.

## v20.5 — Security: resume-upload and batch-analysis hardening
- **Unbounded uploads fixed.** Every resume endpoint did `await file.read()` (the whole file into RAM) and checked the size
  afterwards; `bias/*` and `notifications/apply` never checked it (a 12 MB random file was stored as a DB blob). Uploads are now
  copied to disk in 256 KB chunks and refused the moment they pass `MAX_FILE_SIZE`. A request-size middleware (HTTP 413) and a
  multipart part limit stop oversized bodies and floods of parts before any route parses them. New: `app/uploads.py`,
  `app/request_limits.py`. All 11 resume endpoints now share one intake (`analysis/analyze`, `analysis/analyze-batch`,
  `analyze`, `analyze/batch`, `extract-text`, `bias/anonymize|blind-score|compare`, `fraud/detect`, `interview/generate`,
  `notifications/apply`).
- **Real validation.** Extension + declared MIME type + file contents (PDF header / ZIP / OLE2) + an actual parse. Encrypted,
  empty, corrupt, truncated, non-resume and **legacy `.doc`** files get a specific message with no temp paths or library text
  (`.doc` was never supported — python-docx cannot read it — and is now refused clearly everywhere).
- **Bombs and placeholders.** A 417 KB `.docx` expanded to 1.36 GB of RAM; DOCX expansion, DOCX part count, PDF pages (50) and
  extracted text are now capped, with a cooperative deadline. A text-less (scanned) PDF used to return a 76-character placeholder
  sentence that passed the 50-character check and was scored as a resume; it is now refused.
- **Bounded batches.** New `app/concurrency.py`: at most `ANALYSIS_CONCURRENCY` (default 2) parses/analyses run at once per
  worker, on a dedicated thread pool; per-step (`ANALYSIS_TIMEOUT_SECONDS`) and per-batch (`BATCH_TIMEOUT_SECONDS`) timeouts; a
  timed-out job keeps its slot until its thread really ends; `503` + `Retry-After` when busy. One bad file never affects the rest.
  Lazy NLP singletons are thread-safe (12 simultaneous first requests built 12 copies of the models). The public `/analyze`,
  `/analyze/batch` and the bias/interview endpoints no longer run CPU-heavy work on the event loop (it froze the whole server).
- **Persistence follows the API.** `save_to_db` was declared but never read, so every signed-in analysis was stored (candidate
  personal data) even with "Save result to history" unticked. It is now honoured (default `false`); both rows are written in one
  transaction; a failed save is reported (`saved: false`, `save_error`) instead of looking like success with `analysis_id: 0`;
  an unknown `job_id` no longer loses the result; batch files are stored only if they succeeded.
- **Compatibility.** Response shapes are unchanged (additive `saved` / `save_error`); the UI is unchanged. **API clients that relied
  on "signed in = always saved" must now send `save_to_db=true`.** Behind nginx, set `client_max_body_size` (see DEPLOY.md).
- New optional, startup-validated settings: `MAX_BATCH_FILES`, `MAX_BATCH_REQUEST_SIZE`, `MAX_PDF_PAGES`,
  `MAX_DOCX_UNCOMPRESSED_SIZE`, `MAX_EXTRACTED_TEXT_CHARS`, `ANALYSIS_CONCURRENCY`, `ANALYSIS_TIMEOUT_SECONDS`,
  `ANALYSIS_QUEUE_TIMEOUT_SECONDS`, `BATCH_TIMEOUT_SECONDS` (see DEPLOY.md, "Upload and analysis limits").
- New tests: `tests/test_upload_hardening.py` (documents are generated at test time by `tests/upload_fixtures.py`).

## v20.4 — Security: production CORS configuration
- **Fixed:** `main.py` hardcoded `allow_origins=["*"]` with `allow_credentials=True` and ignored `CORS_ORIGINS`, so
  any website was granted (credentialed) cross-origin access regardless of configuration.
- CORS now uses the validated `CORS_ORIGINS` allowlist (`app/config.py`). Production (`DEBUG=false`) accepts only
  an empty value (same-origin, the default) or exact https origins and **refuses to start** on `*`, `null`,
  `http://`, or malformed entries. Wildcards never get credentials; `DEBUG=true` keeps local development working.
- Shipped configs (`docker-compose.yml`, `render.yaml`, `.env.production.example`) no longer set `*`.
- **Action required:** remove `CORS_ORIGINS=*` from existing deployments before upgrading (see DEPLOY.md).
- New tests: `tests/test_cors.py`.

## v20.3 — Security: frontend HTML-injection fixes
- **Fixed stored/reflected HTML injection** in `frontend/index.html` and `dashboard.html`: resume-derived and API-provided
  text (names, filenames, skills, recommendations, cover notes, resume text, job titles/descriptions, companies, error
  messages) is now escaped (`escH`) or set via `textContent`. Added `escId`, `cls`, `safeUrl`, `safeColor` helpers.
- **Removed untrusted strings from inline JS handlers** (admin "delete user", "Apply" buttons) and the bypassable `escQ()`.
- `showToast()` now treats its message as plain text; job-market "Company Site" links and profile photos are URL-validated;
  recruiter resume viewing no longer opens a server-typed blob (only real PDFs render inline).
- Minor: the "Applied" button now shows ✓ instead of the literal text `&#10003;`.
- **New tests:** `tests/test_frontend_xss.py` (static tripwires) and `tests/frontend/xss_harness.js` (jsdom harness driving
  the real pages with hostile data). See `FRONTEND_SECURITY.md`.

## v20.2 — Security: exposed credentials & database protection
- **Removed hardcoded administrator passwords** from `app/db/company_seed.py`. Startup now seeds
  the company registry only and creates no user accounts (existing accounts are never modified).
- **New:** explicit admin provisioning (`python -m app.admin_cli provision|rotate|list-admins`) and
  opt-in `INITIAL_ADMIN_*` bootstrap for fresh deployments (only when no admin exists).
- **`SECRET_KEY` fails closed:** missing/short/placeholder keys abort startup unless `DEBUG=true`;
  no secret default remains in source. `docker-compose.yml` now requires `SECRET_KEY`.
- **Leak prevention:** `.gitignore` now covers `.env.*` and SQLite sidecars; added `.dockerignore`,
  `.env.production.example`, and `scripts/make_source_dist.py` (safe ZIP builder/verifier).
- Tests no longer touch the project database (temp DB per run). Added `tests/test_credential_hygiene.py`.
- **Action required for existing deployments:** rotate legacy company-admin passwords and
  `SECRET_KEY` (see DEPLOY.md).

## v20.1 — Fixed: Job Seeker Portal Broken After Page Refresh

### Root cause (`frontend/index.html`)
The script auto-restored a saved session with `if(_token&&_user)showApp();` on
line 6 — *before* most of the script's `let`/`const` state had been declared.
The profile-prompt feature added to `showApp()` read `_profilePromptShown`,
which was declared with `let` hundreds of lines further down, so it threw
`ReferenceError: Cannot access '_profilePromptShown' before initialization`
(temporal dead zone). That uncaught error aborted the **entire remaining
script** on every refresh for job-seeker accounts, leaving all later state
(`_notifData`, `_popupTimer`, `_jmAll`, `_applyBtn`, `_naAll`, ...) unusable.
Symptoms: live notifications, resume upload, resume scanning and applying all
failing. Recruiter/admin accounts were unaffected (they never reach that call).

### Fix
- The auto-restore call `if(_token&&_user)showApp();` now runs at the very END
  of the script, after every declaration has been initialised.
- `_profilePromptShown` is also declared at the top of the script as a
  second safeguard.
- No backend changes.

### Verified
- jsdom harness loading the real page with a saved job-seeker session:
  before the fix -> the ReferenceError above and later state unreachable;
  after -> zero script errors over 9 s of runtime, notification feed fetched,
  popup/upload zone present, page navigation and avatars working.
- Profile API end-to-end: save profile, photo upload (Pillow), photo served
  from /uploads/profile_photos, photo present on fresh login, delete.

## v20 — Auth Page: Professional Icons + Accessibility + Password Visibility

### Overview
Replaced the emoji role icons on the sign-up page with custom, professional
SVG icons, and used the opportunity to fix a real accessibility gap the
role selector had, plus add a password-visibility toggle to both the sign-in
and sign-up forms.

### `frontend/index.html`
- **Changed**: the Recruiter (🏢) and Job Seeker (🎓) role-card icons are now
  hand-drawn, stroke-based SVGs (a briefcase and a graduation cap,
  Feather/Lucide-style outline icons) instead of emoji, rendered in
  `currentColor` so they sit neutral (`--text3`) until a card is selected,
  then pick up that role's own accent color (`--accent` purple for
  Recruiter, `--teal` for Job Seeker) — the icon becomes part of the
  selection feedback rather than static decoration.
- **Fixed**: the role cards were `<div onclick="...">` — not natively
  focusable or keyboard-activatable, meaning a keyboard-only user could not
  select a role at all before creating an account. Added `tabindex="0"`,
  `role="button"`, `aria-pressed` (kept in sync by `selectRole()` on every
  change), and an Enter/Space keydown handler alongside the existing click
  handler.
- **Added**: a show/hide toggle (eye / eye-off icon, swapping the input's
  `type` between `password` and `text`) on both the sign-in and sign-up
  password fields — a standard, expected control on any modern auth form
  that this one was missing entirely.
- Removed a duplicate, superseded CSS rule for `.role-card-icon` left over
  from the emoji version (font-size-based; the new one is width/height +
  color-based for SVG) while making this change.

### Verified against
- Full HTML tag balance (including the new `<svg>` elements specifically),
  JS syntax (`node --check`), and CSS brace balance.
- A jsdom harness that loads the real page and executes its real script:
  confirmed both role cards actually contain an `<svg>` (and no emoji
  character) rather than trusting the markup by eye; confirmed
  `selectRole()` correctly flips `aria-pressed` on both cards together, not
  just the CSS class; confirmed the password toggle correctly flips the
  input's `type` and the button's `aria-label` back and forth across
  repeated clicks.
- The Enter/Space keydown logic was verified by direct invocation with a
  mock event, rather than by dispatching a real keyboard event at the
  element — jsdom's `runScripts: 'outside-only'` mode (used throughout this
  project's frontend tests, for good reason: it sandboxes untrusted script
  execution) does not compile *any* inline HTML event-handler attribute
  into a live listener, for `onclick` just as much as `onkeydown` — checked
  this against a minimal, isolated reproduction first, so as not to mistake
  a test-tooling limitation for a real bug (or vice versa).
- The full backend test suite: 51/52 passing, same single pre-existing,
  unrelated, sandbox-only limitation noted in every earlier entry — no
  backend files changed in this round.

## v19 — Fixed: Photo Upload Worked, Display Didn't (Two Real Bugs)

### Overview
Both the "small" and "big" issue reported were real, and both were in the
v18 profile-photo feature specifically — a photo would appear to upload
fine in the moment, but silently fail to display afterward (a fresh login,
or on any environment where a pre-existing, unrelated static mount happened
to fail). Neither was caught by v18's testing because that testing never
actually executed the frontend JavaScript or served a photo back over a
real static-file mount — it validated the backend endpoints directly and
read the frontend code, which wasn't sufficient. Found this time using a
combination that was: a jsdom harness that actually *runs* the real
`frontend/index.html` script against real captured backend responses, and
directly re-testing the static file mount in a way that could surface a
working-directory-dependent failure.

### The "big" bug — `app/models/schemas.py`
`UserResponse.photo_url` used `Field(None, alias="photo_path")` to map the
database column name onto a friendlier field name. What wasn't accounted
for: a Pydantic `alias` is used for *both* directions — input parsing AND
JSON output serialization — by default. So `/auth/register`, `/auth/login`,
and `/auth/me` were all serializing this field back out under the key
`photo_path`, not `photo_url`. Every *other* profile endpoint (`/profile/me`
and friends) was unaffected, since those build their JSON by hand rather
than through this model — which is exactly why the bug was inconsistent:
uploading a photo displayed it correctly right away (that response came
from the unaffected code path), but logging in again afterward showed
initials instead of the photo that was actually on file, because the login
response silently carried the field under the wrong key.

**Fix**: removed the alias entirely; the existing `@model_validator(mode="before")`
(already used for computing `profile_complete`) now explicitly copies
`photo_path` → `photo_url` on the input side only, so the field populates
correctly from a DB row without renaming itself back on the way out.

Verified directly: register → upload a photo → log in again as a fresh
session → the second login's response now correctly carries `photo_url`
with the real path (previously `photo_path`, and `photo_url` was
`undefined`). Also confirmed the pre-existing `/auth/me` endpoint, which
shares this model, is fixed the same way.

### The "small" bug, but with a "big" consequence — `main.py`
The new profile-photos static mount was added inside the *same*
`try/except` block as the pre-existing frontend static mount:
```python
try:
    app.mount("/static", StaticFiles(directory="frontend"), name="static")
    app.mount("/uploads/profile_photos", StaticFiles(directory=str(PHOTO_DIR)), name="profile_photos")
except Exception as exc:
    logger.warning(f"Static files not mounted: {exc}")
```
`directory="frontend"` is a relative path — it depends on the working
directory the app was launched from. If that mount fails for any reason,
the whole `try` block stops executing right there, and the *second* mount
(profile photos) is never even attempted — silently, with only a log line
nobody would see. The main page itself doesn't depend on this mount (it's
served through a separate `FileResponse` route), so the app would look and
work completely normally... except every uploaded photo 404s when the
browser tries to load it.

**Fix**: split into two independent `try/except` blocks, so a failure in
one can never prevent the other from being attempted. The profile-photos
mount uses `PHOTO_DIR`, an absolute path, so it doesn't share the
first mount's working-directory sensitivity at all once decoupled from it.

Verified directly: reproduced the exact failure (ran the app from a working
directory where `"frontend"` doesn't resolve, confirmed via log output that
the first mount fails) and confirmed the photo upload → static serve round
trip now succeeds regardless.

### Also fixed while investigating
- **`apiErr()` in `frontend/index.html`**: validation error messages for
  the profile fields (date of birth, phone, gender, address) were showing
  the raw snake_case field name (no label mapping existed for them) with
  Pydantic's default `"Value error, "` prefix left in, e.g. *"date_of_birth:
  Value error, Date of birth must be in YYYY-MM-DD format."* Added the
  missing labels and strip that redundant prefix, so this now reads
  *"Date of birth: Date of birth must be in YYYY-MM-DD format."*

### Verified against
- A from-scratch reproduction of the exact real-world scenario: register,
  upload a photo, log in again as a brand new session, confirm the photo
  now displays — run against the real Pydantic schema, real Pillow
  processing, and real SQLite storage (no mocks on the backend side).
- A jsdom harness that loads the actual `frontend/index.html`, executes its
  real inline script (not a rewritten test version of it), and drives it
  through `api()`, `showApp()`, `avatarHTML()`, and
  `maybeShowProfilePrompt()` using the real captured backend JSON: confirms
  the navbar avatar, the seeker-home avatar, and the complete-profile
  prompt's suppression logic all now behave correctly, with zero JS runtime
  errors — this is what caught the alias bug conclusively, after an
  earlier, less careful version of this same technique produced a false
  positive from a jsdom scoping quirk (documented in-line in the test code
  for future reference: separate `window.eval()` calls in jsdom do not
  share `let` bindings the way a single real `<script>` tag does — the
  fix was running the whole simulated session in one `eval` call).
- The full backend test suite: 51/52 passing, same single pre-existing,
  sandbox-only limitation as every prior changelog entry (no network path
  to the real sentence-transformers model here) — unrelated to this fix.
- Full HTML tag balance, JS syntax (`node --check` on the real extracted
  script), and Python syntax checks on every changed file.

## v18 — Job Seeker Profile: Photo + Details

### Overview
New "My Profile" section for job seekers: a passport-style photo and the
core identity details (name, DOB, gender, phone, address) that recruiters
and the application flow use, alongside the account's existing user ID and
email. A one-time prompt after login nudges job seekers to complete it if
they haven't.

### `app/db/database.py`
- Added 6 nullable columns to `users` via the existing safe
  `ALTER TABLE ... ADD COLUMN` migration pattern (fails silently per-column
  if already applied, so it's safe to run against an existing DB):
  `photo_path`, `date_of_birth`, `gender`, `phone`, `address`,
  `profile_updated_at`. Existing accounts are unaffected until the holder
  fills them in.

### `app/api/profile_routes.py` (new)
- `GET /api/v1/profile/me` — full profile including a computed
  `profile_complete` flag (true once name, DOB, phone, and address are all
  set) and `photo_url`.
- `PUT /api/v1/profile/me` — partial update; validates date-of-birth format
  and rejects future dates, validates phone has a sane digit count, and
  restricts gender to a known set.
- `POST /api/v1/profile/photo` — upload/replace the photo. Never trusts the
  browser's declared content-type alone (that's spoofable) — Pillow opens
  and verifies the file is a genuine image server-side, rejects anything
  under 150×150px, flattens PNG/WEBP transparency onto white before
  converting to JPEG (avoids a broken-looking black square), downsizes
  anything larger than 800px on the long edge, and cleans up the previous
  photo file once the new one is confirmed saved.
- `DELETE /api/v1/profile/photo` — remove the photo and its file, reverting
  the avatar to initials.
- Photos are saved under `app/data/uploads/profile_photos/` (same
  convention as the existing SQLite DB location) and served via a new
  static mount — plain URLs, not gated per-request the way resume viewing
  is; documented as a known tradeoff below.

### `app/models/schemas.py`
- `UserResponse` now carries `photo_url` (aliased from the `photo_path`
  column), `date_of_birth`, `gender`, `phone`, `address`, and
  `profile_complete`, so this data is already present on login/`/me`
  without an extra round trip.

### `frontend/index.html`
- New **My Profile** page (job-seeker sidebar, and a Quick Actions tile on
  the seeker home page): photo upload with live preview, the editable
  fields, and the account's user ID and email shown read-only.
- A shared `avatarHTML()` helper (photo-or-initials, with a real
  `onerror` fallback if a stored photo URL ever 404s) now drives *every*
  avatar in the app — the navbar chip, the seeker home banner, and a new
  "Applying as {photo} {name}" identity strip in the job-application modal
  — so uploading a photo once updates it everywhere immediately.
- A one-time "Complete your profile" prompt appears ~1s after login for
  job seekers missing a photo or the core fields, with a direct link to
  the new page and a "Maybe later" dismissal (reappears next login if
  still incomplete — not nagged on every page navigation within a session).

### Verified against
- A full request-level test suite (`tests/test_profile.py`, 11 new tests):
  auth is required; profile starts incomplete; future DOB and malformed
  phone numbers are rejected with 422; a valid update flips
  `profile_complete` to true; a genuine JPEG/PNG upload is accepted,
  resized, and served; corrupt bytes, undersized images, and wrong file
  types are all rejected with the correct status codes; replacing a photo
  deletes the old file from disk; deleting a photo removes both the DB
  reference and the file.
- Full HTML tag balance, a real JS syntax check (`node --check`) on the
  entire inline script, and CSS brace balance after every change.
- The complete existing test suite, run alongside the new tests: 51/52
  passing (the one failure is the pre-existing, sandbox-only
  sentence-transformers limitation noted in earlier changelog entries —
  unrelated to this feature).

### Bonus: two unrelated pre-existing bugs found and fixed
Discovered while confirming the new tests weren't just failing for
environment reasons — both were already broken for anyone running the
suite fresh, regardless of this feature:
- **Added `tests/conftest.py`**: `TestClient(app)` only runs FastAPI's
  lifespan startup (which calls `init_db()`) when used as a context
  manager (`with TestClient(app) as client:`) on Starlette 0.35.1 (the
  version `fastapi==0.109.0` pins) — verified directly against that exact
  version. `test_auth.py`/`test_jobs.py` construct the client at module
  level without one, so `init_db()` never ran and every DB-backed test
  failed with `no such table: users`. Fixed once, centrally, rather than
  changing how each test file builds its client.
- **Fixed `test_auth.py`/`test_jobs.py`**: both registered their test user
  as the default `recruiter` role against a fictional company name ("Test
  Corp" / none at all), which the recruiter-approval system correctly
  rejects with 400 — meaning the user was never actually created, and
  every test depending on it (login, get_me, update_profile,
  change_password, logout, and all of `test_jobs.py`) failed in turn.
  `test_register_new_user`'s own assertion (`in (201, 400)`) was tolerant
  enough to mask this. Switched both to register as `candidate`
  (auto-approved, no company needed), which is what these files actually
  intend to test — generic auth mechanics, not the recruiter-approval
  workflow.

### Known limitations / tradeoffs
- Profile photos are served from a plain static URL rather than an
  authenticated endpoint, for the practical reason that an `<img src>` tag
  can't attach an Authorization header — the alternative (fetch-then-blob
  in JS) would need to be wired into every render site individually. The
  saved filename includes a random token specifically so the URL itself
  isn't guessable, but this is "unlisted," not authenticated — reasonable
  for a local/demo project, worth revisiting before any real deployment
  with actual user data.
- "User ID" is shown as the account's existing internal ID rather than a
  new field — there was no separate identifier requested elsewhere in the
  system for this to map to, and inventing one felt like overreach beyond
  what was actually asked for.

## v17 — UI/UX Audit: Contrast, Focus, Loading States, Double-Submit

### Overview
A focused audit of `frontend/index.html` (all 14 pages, ~2500 lines) for
real, verifiable issues rather than a cosmetic pass — every item below was
confirmed with an objective check (computed contrast ratios, a grep across
every data-loading function for consistency, a JS syntax check with
`node --check`) before being fixed.

### Accessibility
- **Fixed — contrast**: `--text3` (used everywhere for secondary text,
  timestamps, helper copy) was `#6b7194`, measuring 3.6–4.2:1 against the
  app's actual backgrounds — fails WCAG AA for normal text (needs 4.5:1).
  Changed to `#8086ac` (same slate-blue hue, now 4.8–7.9:1 across every
  background in the theme) — computed with the real WCAG relative-luminance
  formula, not eyeballed.
- **Added**: a single `:focus-visible` rule covering buttons, nav items,
  cards, links, and form controls app-wide. Previously only `.finput` (text
  inputs) had any custom focus styling — everything else fell back to
  whatever the browser's default outline happens to be, which reads
  inconsistently against this theme and is easy to lose track of when
  navigating by keyboard. `:focus-visible` (not `:focus`) means this only
  appears for keyboard navigation, never on mouse clicks.
- **Fixed**: 4 icon-only buttons had no accessible label (a screen reader
  would announce them as just "button") — the two modal-close ✕ buttons,
  the toast dismiss ✕, and the per-file ✕ remove button on the Analyze
  Resume upload list (now labelled with the specific filename).

### Real bugs (not just polish)
- **Fixed**: `loadRecDash()` and `loadDashCands()`'s hardcoded fallback
  content was literally "No analyses yet." / "No candidates yet." — for a
  recruiter with plenty of existing data, that false-empty state was
  visible on *every single dashboard visit* for however long the fetch
  took, before flipping to the real numbers. Same issue on the stat
  cards (`loadSeekerStats()` too) showing stale/default values with no
  loading signal. All three now show an explicit loading state first, and
  a proper retry-capable error state if the fetch fails — matching the
  pattern already used correctly by `loadJobs()`/`loadCandidates()`.
- **Fixed**: `saveJob()` was the only create-a-resource action in the app
  with no button-disable guard during the request — every comparable
  action (`doAnalyze`, `submitApply`) already had one. A fast double-click
  could fire two POSTs and create a duplicate job posting. Also made
  `toggleJobForm()` reset the button's state whenever the form is opened,
  so it can never get stuck disabled from a previous save if something
  goes wrong.

### Verified
- Full HTML tag balance check (div/button open vs. close counts match).
- Full inline `<script>` block extracted and run through `node --check` —
  genuine JS syntax validation, not just a brace-counting heuristic.
- CSS block brace-balance check.
- Contrast ratios computed with the actual WCAG relative-luminance formula
  for every text/background color pair in the theme, before and after.

### Scope note
This pass covered app-wide consistency issues (contrast, focus, a shared
loading-state pattern) and the two concrete functional bugs found along
the way. It did not touch layout/visual design on pages beyond what v14–v16
already changed (Analyze Resume) — happy to do a deeper pass on any
specific page if something there still looks or behaves off; screenshots
of the exact issue make that much faster to nail down precisely.

## v16 — Fixed: ForwardRef/recursive_guard Crash on Every Analysis

### Overview
Every resume analysis (both `/analyze` and the new `/analyze-batch`) was
failing with `Analysis failed unexpectedly: ForwardRef._evaluate() missing
1 required keyword-only argument: 'recursive_guard'`. Root cause was three
layers deep and took real reproduction (not guesswork) to pin down —
documented here in full since two of the three things it initially looked
like turned out to be red herrings.

### Root cause, precisely
CPython 3.12.4 changed `typing.ForwardRef._evaluate()` to make
`recursive_guard` a required keyword-only argument. `install_and_run.bat`'s
embedded-Python fallback downloads Python **3.12.10** — well past that
change. The actual break, verified by importing this project's real code on
a genuine 3.12.10 interpreter: **spaCy 3.7.2** (a direct dependency, used
for resume text analysis) uses pydantic's *bundled* legacy `pydantic.v1`
compatibility layer internally for its own schema validation
(`spacy/schemas.py`), and that specific internal code path calls
`ForwardRef._evaluate()` the old way. This crashes the first time any
resume is actually analysed (`spacy.load()` runs lazily, on first NLP call,
not at server startup) — matching exactly what was observed: the app and
UI loaded fine, only analysis failed.

Two more specific things were checked and ruled out along the way, in case
this resurfaces differently later:
- This project's own `app/models/schemas.py` — not the cause. It has
  `from __future__ import annotations` (making every annotation in that
  file a forward reference), which was a very reasonable first suspect, but
  it was verified working correctly even on the real affected Python
  version with the original pinned pydantic. Left unchanged since it isn't
  actually broken, though it's not doing anything useful there either.
- Whether pydantic 2.7.4 (where pydantic's own PR #9612 fixed this for
  pydantic's *own* internals) was enough — it wasn't. 2.9.2 still failed
  when tested directly, matching a still-open pydantic issue for the same
  reason: the bundled `pydantic.v1` shim spaCy uses is separate, older code
  that wasn't touched by that fix.

### The fix
One line, in `requirements.txt`:
```
pydantic[email]==2.5.3   →   pydantic[email]==2.13.5
```
By 2.13.5, pydantic's bundled `pydantic.v1` compatibility layer has also
been patched, which resolves spaCy's internal usage too — no application
code changes, no monkeypatching, nothing else needed.

### Verified against
Not simulated — a genuine Python 3.12.10 interpreter (the exact same build
`install_and_run.bat` downloads, via python-build-standalone) was used
throughout:
- Before the fix: importing spaCy alone reproduced the exact reported
  error, at the exact line (`spacy/schemas.py:195`) shown above.
- After the fix (pydantic bumped, nothing else changed): spaCy imports
  cleanly, and a full round-trip through the real (unstubbed)
  `/analyze-batch` endpoint — real FastAPI app, real router, real
  `KeywordExtractor`/`EnhancedScorer`, two real `.docx` files — completed
  successfully for both files with no error.
- `tests/test_parsers.py` on this same real 3.12.10 + fixed-pydantic
  environment: 16/17 passing. The one failure
  (`test_score_with_jd_higher_than_without`) is unrelated — this sandbox
  has no network path to download the actual sentence-embedding model, so
  semantic similarity is skipped by design; it isn't reproducible from a
  real deployment with normal internet access.
- Full project-wide `py_compile` sweep: no syntax errors anywhere else in
  this version.

## v15 — Non-Resume Document Detection + Analyze Page Layout

### Overview
Follow-up to v14, triggered by a real test case: an offer letter (not a
resume) was scoring 49% with an "80% Keywords" bar and a recommendation to
learn Python — none of which made sense for a document that isn't a resume
at all. Root cause was that a well-written formal letter can satisfy several
*weak* resume-ish signals at once (real dates, some section-keyword overlap
in running prose, coherent vocabulary) even after the v14 fixes, without
being a resume. Also addressed: unused empty space on the Analyze Resume
page's left column.

### `app/nlp/keyword_extractor.py`
- **Fixed**: `analyze_text_structure()`'s section detection matched a
  keyword like "experience" or "employment" *anywhere* in the document —
  including inside ordinary prose ("...at all times during and after your
  employment.") — and counted it the same as an actual section header. Now
  requires the keyword to sit at the start of its own line (optionally after
  a bullet/number marker), matching how a real resume header actually looks.
- **Added**: `detect_formal_letter_marker()` — a high-precision check for
  phrases that are near-universal in HR correspondence and institutional
  certificates ("offer letter", "yours sincerely", "we are pleased to
  offer", "terms and conditions of employment"...) but essentially never
  appear in a genuine resume. Acts as a second, independent layer on top of
  the section-header fix above, since it doesn't rely on structure detection
  being perfect to catch this document class.
- **Fixed**: the document's own title ("OFFER LETTER", "EXPERIENCE
  CERTIFICATE", etc.) was passing every check in `extract_contact_info()`
  and getting returned as the candidate's name — same failure shape as the
  original "PK Placements Ltd" bug, different trigger. Added these titles to
  the name-rejection markers.

### `app/nlp/enhanced_scorer.py`
- **Added**: when `detect_formal_letter_marker()` fires, `calculate_ats_score()`
  now overrides `is_likely_resume` to false, caps the headline score at 12%,
  and replaces the recommendation list entirely with one specific message
  naming what was actually detected — instead of prepending that message
  above an unrelated, domain-mismatched "skills you're missing" suggestion
  (which is what made the previous fix's output look self-contradictory on
  this document class).

### `frontend/index.html`
- **Changed**: the Analyze Resume page's left column (upload + job
  description + button) was much shorter than the right column (score,
  skills, recommendations), leaving a large empty gap and an unbalanced page
  while scrolling. Added two cards below the Analyze button: a compact
  breakdown of what Keywords/Skills/Structure actually measure, and a short
  list of resume tips. Purely additive — no existing element IDs or DOM
  structure touched, so none of the existing JS needed to change.

### Verified against
- The exact reported case (a realistic offer-letter reproduction): overall
  score 35.4% → 12.0%, `is_likely_resume` true → false, recommendation
  changed from a misleading "Critical CSE skills not found: Python, Java..."
  to a specific "this reads like a formal letter, not a resume" notice.
  Candidate name no longer extracted as "OFFER LETTER".
- Re-ran the full v14 regression suite to confirm no regressions: all 72
  Kaggle resumes across 24 categories unchanged, both original bug-repro
  cases (letterhead-over-real-resume, pure agency boilerplate) unchanged,
  `tests/test_parsers.py` still 16/17 (same pre-existing sandbox-only
  failure, unrelated to this change).

### Known limitations
- The formal-letter marker list is, like the job-title list in v14,
  necessarily incomplete — it covers common Indian HR/institutional
  documents (offer, appointment, relieving, experience/salary/bonafide
  certificates) but a differently-worded formal document could still slip
  through onto the generic `is_likely_resume` check, which is weaker on its
  own now that section-detection is stricter but not foolproof against
  coincidental line-wrapping.

## v14 — Resume Parsing Accuracy Overhaul

### Overview
Resume analysis is the core of the product, and it was extracting company/agency
names as the candidate's name (e.g. a placement consultancy's letterhead —
"PK Placements Ltd" — instead of the actual candidate) and producing inflated
match scores (~58%) for documents with no real skills in them at all. Root cause
turned out to be several separate bugs across `keyword_extractor.py` and
`enhanced_scorer.py`, all fixed and verified against ~100 real resumes (a Kaggle
resume corpus spanning 24 job categories) plus the project's own test suite.

### `app/nlp/keyword_extractor.py` — contact extraction rewrite
- **Fixed**: `extract_contact_info()` picked the candidate name by grabbing the
  first short line in the top of the document — no check for whether that line
  was actually a person's name. A letterhead, job title, or section header with
  the right shape (short, no digits, no `@`) would win every time. Rewritten to
  try spaCy PERSON-entity recognition first (the project already depends on
  spaCy — it just wasn't being used for this), cross-checked against an
  organisation-keyword denylist (Ltd, Pvt, Placements, Consultancy, Solutions...)
  and a job-title denylist (Accountant, Manager, Coordinator, Engineer...,
  deliberately excluding occupational words that are also common surnames like
  Taylor, Baker, Cook, Hunter, Mason). Falls back to a tightened heuristic scan
  only if NER finds nothing, and returns `None` instead of guessing wrong when
  nothing plausible is found.
- Along the way, real-resume testing surfaced and fixed: NER occasionally
  grabbing a name *mentioned* inside a sentence (an award, a former employer)
  rather than an actual heading; a PDF line-wrap occasionally letting one NER
  entity span two unrelated lines; and the heuristic wandering past an excluded
  header line into body-text fragments once the obvious candidates were ruled
  out. The heuristic now stops at a real section header but only skips past an
  org/job-title line (so a name sitting below a letterhead is still found).
- **Fixed**: email/phone extraction took the *first* regex match anywhere in
  the document. Now collects every match, ranks emails so a generic role-based
  address (`info@`, `hr@`, `placements@`...) loses to a personal-looking one
  when both are present, and returns the full ranked list (`emails`, `phones`)
  alongside the best guess.
- **Fixed**: `detect_indian_context()` and `extract_skills()` both had an
  India-specific-skills scan that checked raw substring containment instead of
  the word-boundary-protected pattern used everywhere else — e.g. the skill
  "Consul" matched inside "consul**tancy**". The `extract_skills()` copy was
  also fully redundant with the properly-guarded scan already running earlier
  in the same method. Removed the redundant scan; fixed the other to reuse the
  protected patterns.
- **Fixed** a broken test: `extract_candidate_name()` was called by
  `tests/test_parsers.py` but never actually existed on the class. Added as a
  thin wrapper over `extract_contact_info()`.
- Added GitHub/LinkedIn/portfolio URL extraction to `extract_contact_info()`,
  a `score` field to `extract_keywords()` items, an `all_skills` flat list to
  `extract_skills()`, a `has_contact_info` flag to `analyze_text_structure()`,
  and a `structure` key to `full_analysis()` — all fields the test suite
  already expected but the implementation had never actually produced.
- More permissive phone-number matching (`analyze_text_structure` and
  `extract_contact_info` both had the same gap) — the old pattern only matched
  a contiguous run of digits, so common formats like `+1 (555) 123-4567`
  weren't recognised as a phone number at all.

### `app/nlp/enhanced_scorer.py` — score inflation fixes
- **Fixed**: several sub-scores had an unconditional floor regardless of
  actual relevance — keyword score started at +40, structure at +30,
  experience at +20, education defaulted to 20 with zero signal. These are
  additive across the weighted final score, which is how a document with
  *zero* real skill overlap could still clear 50%+. Floors removed or greatly
  reduced; a document with no real signal on an axis now scores near 0 on
  that axis instead of a third-to-half credit by default.
- **Fixed**: JD-keyword matching counted any shared word ≥4 characters,
  including pure connector/filler words ("with", "team", "role", "years") that
  say nothing about actual skill overlap. Added a filler-word set to exclude.
- **Fixed**: semantic similarity (sentence-embedding cosine similarity between
  resume and JD) was reported as a raw percentage. Generic embeddings of two
  *unrelated* pieces of professional English text routinely land around
  0.3–0.5 cosine similarity just from shared register/vocabulary — that noise
  floor was passing straight through as a 30–50% "semantic match". Now
  rescaled against an empirical noise floor so only similarity meaningfully
  above baseline contributes.
- **Added**: `is_likely_resume` — a document with no detected name, no
  meaningful skill matches, and no resume-shaped sections isn't a *weak*
  resume, it's not a resume. When this flag is false, the top recommendation
  is now an explicit "this doesn't look like a resume" notice instead of
  generic coaching tips built on top of a low-confidence extraction.
- **Fixed**: `accuracy_estimate` was never actually set by this function, so
  the API route's `.get("accuracy_estimate", 88)` fallback meant every
  response silently reported a hardcoded 88 regardless of what was actually
  found. Now computed from real extraction confidence (name found, email
  found, domain-detection confidence, resume plausibility).

### Verified against
- Direct reproduction of the reported bug (consultancy letterhead above a real
  candidate's resume, and a pure agency-boilerplate document with no candidate
  at all) — name extraction and match score both now behave correctly in both
  cases.
- ~100 real resumes from a public Kaggle resume dataset across all 24 job
  categories it covers — zero company/job-title names returned as the
  candidate across the full sample after the fixes above.
- `tests/test_parsers.py`: 16/17 passing. The one remaining failure
  (`test_score_with_jd_higher_than_without`) only fails in this development
  sandbox because it has no network access to download the real
  sentence-transformers model — unrelated to any of the above and expected to
  pass in the real environment.

### Known limitations
- Job titles are an open-ended vocabulary across industries — the denylist
  catches everything found during testing (spanning accounting, healthcare,
  aviation, construction, HR, and more) but a sufficiently unusual title from
  an untested industry could still slip through. This is a fundamental limit
  of keyword-based disambiguation, not something a bigger list fully solves.
- Not tested against DOCX resumes that use a table layout with name, email,
  and phone squeezed onto a single row — the name-detection heuristic skips
  any line containing `@`, which could miss a name in that specific case. No
  evidence this is currently happening; flagging it as a watch-item.

## v13 — Interview Generator: 100-Programme Picker + Curated Q&A Bank

### Overview
"By Department" mode in the Interview Generator now covers all 100 programmes
(the original 50 + the 50 added in this update) through a proper cascading
picker, and returns real question **+ answer** pairs from a locally-stored,
originally-authored Q&A bank instead of a keyword-matching hack.

### New Datasets
- `datasets/departments_100.json` — 100 programmes across 15 degree types (see `datasets/README.md`)
- `datasets/department_qa_bank.json` — 188 original Q&A pairs (common/HR, aptitude,
  and 12 subject-cluster categories). Authored from scratch — not sourced from any
  third-party website, so it's safe to ship and submit as project work.

### Backend (`app/api/interview_routes.py`)
- `GET /api/v1/interview/departments` — 100 programmes grouped by degree type
- `POST /api/v1/interview/generate-by-department` — takes `department_id` +
  `questions_per_category`, returns General/HR + Aptitude (common to all 100
  programmes) plus the 1–2 technical categories mapped to that specific programme,
  each with a model answer included
- **Fixed a pre-existing bug**: `_generate_questions()` (used by Upload Resume /
  Paste Text modes) returned `categories` as a flat list of category *names*, but
  the frontend expected `{category: [questions]}`. This mismatch made the results
  panel throw a JS error after a successful generation. Now fixed — `categories`
  is the actual grouped dict, and the old flat list is kept as `category_list`
  for anything that wants it.

### Frontend (`frontend/index.html`)
- "By Department" tab is now two cascading selects — Degree Type, then Programme
  — populated live from `/api/v1/interview/departments` (single source of truth,
  no department list duplicated in the frontend).
- **Fixed**: the old flow built a fake resume string like `"Candidate applying
  for Backend Developer role"` and sent it to the resume-text endpoint — for
  short role names this string fell under the endpoint's 50-character minimum,
  producing the `Error: Resume text too short.` message. Department mode now
  calls its own endpoint directly, so this can't happen.
- Results panel renders question + answer together for department mode, and
  still renders plain question lists for the Upload/Paste-Text modes.

## v12 — Job Market Notifications + Two-Way Portal

### Overview
Full job notification system with real Tamil Nadu & India company openings.
Candidates see a popup on login; recruiters see incoming applications — both linked through a shared backend table.

### New Dataset: `datasets/job_notifications.json`
- 20 real companies across TN and India: Zoho, Freshworks, TCS, Infosys, Cognizant,
  Hexaware, Sutherland, TVS Motor, Rane Group, Wipro, ELGI, Amazon India,
  Chargebee, Ramco Systems, Ford India, Intellect Design, Ashok Leyland,
  Standard Chartered GBS, Tata Elxsi, NatWest Group India
- 60 positions total, 1000+ openings across sectors
- Sectors: Product, IT Services, MNC, Manufacturing, Banking/FinTech
- Each position has: title, type, openings count, experience range, required skills
- Deadline and official apply link per company

### New Backend: `app/api/notification_routes.py`
- `GET  /api/v1/notifications/feed`            — full notification list with applied flags
- `GET  /api/v1/notifications/stats`           — company/position/opening counts
- `POST /api/v1/notifications/apply`           — candidate submits resume (multipart)
- `GET  /api/v1/notifications/my-applications` — candidate sees their submissions
- `GET  /api/v1/notifications/applications`    — recruiter sees received applications
- `GET  /api/v1/notifications/applications/{id}` — recruiter detail view
- New SQLite table: `notification_applications` (auto-created on startup)
- Privacy rule: recruiters only see applications for their `company_name`
- Duplicate guard: one application per (candidate × notification × position)

### Frontend — Job Seeker Portal
- **Notification Popup**: appears 6–10 seconds after login
  - Shows one company at a time with all positions, skills, and openings
  - Paginated via Prev/Next buttons and dot indicators
  - Progress bar showing position in the feed (20 companies)
  - Auto-advances every 12 seconds
  - "Apply Now" button → opens Apply Modal inline
  - Applied positions marked with ✓ Applied (disabled), persisted across refresh
- **Apply Modal**: file picker (PDF/DOCX) + optional cover note
  - Full validation: file type, empty file, duplicate check
  - Error messages inline; success toast notification
- **Job Market page** (nav: 🏙️ Job Openings):
  - Grid of all 20 company cards with all positions
  - Search bar + sector filter (product/it_services/mnc/manufacturing/banking)
  - Live stat strip: companies, positions, openings, my applications
  - Company site link per card
- **My Applications page** (nav: 📄 My Applications):
  - Table of all submitted applications with status badge and date
  - Empty state with direct link to Job Market

### Frontend — Recruiter Portal
- **Market Applications page** (nav: 📬 Market Applications):
  - Stats: total companies, positions, openings, applications received
  - Searchable/filterable table of all incoming applications
  - Columns: ID, Company, Position, Candidate Name, Resume Filename, Status, Date
  - Privacy: only shows applications for recruiter's registered company

### Two-Way Connection
Candidate applies → stored in `notification_applications` DB table → recruiter
at matching company sees it in their "Market Applications" portal. The candidate
and recruiter never share an account or see each other's credentials. The link
is purely by company name matching.

## v11 — (Previous) Feature Upgrades
## v9 — India-Aware NLP + Error Hardening
## v8 — NLP Accuracy Overhaul (Kaggle KB)
## v6 — Dual Role UI
## v3.0 — Initial Release
