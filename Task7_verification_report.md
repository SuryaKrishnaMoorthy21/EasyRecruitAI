# EasyRecruit v20 — Task 7 verification report

Baseline: the uploaded archive (`EasyRecruit_v20_task6__1_.zip`, SHA-256 `9a467e81…e6fca`), used as the Task 6 baseline. It contained **no Task 7 changes**
(no Task 7 mention anywhere; changelog stopped at v20.6). An untouched copy was kept; no patch was blindly re-applied.
Deliverable: `EasyRecruit_v20_task7.zip` (78 files, SHA-256 `546809c8…0150`) = baseline's 76 files + `app/json_safety.py` + `tests/test_task7_regression.py`.

## Results

| Check | Result | Evidence |
|---|---|---|
| Full Python suite, baseline (Task 6), with test stub* | 658 passed, **2 failed** | the 2 known failures, see below |
| Full Python suite, final, **clean extraction of the ZIP**, with stub* | **765 passed, 1 failed, 0 skipped** | `test_score_with_jd_higher_than_without` only |
| Same, no stub (no `sentence-transformers` installed) | 578 passed, 178 skipped, 3 failed, 7 errors | all 10 are `ModuleNotFoundError: sentence_transformers` (environment-blocked); skips are scorer tests that need it |
| Frontend security tests (jsdom 24.1.3) | **passed**: index.html 230 cases, dashboard.html 10 cases; 0 injection findings, 0 run errors | run directly and inside pytest |
| Python compile / import of every `app.*` module and `main` | passed, 0 failures | clean extraction |
| Account privilege protection | passed | suite + live probes: self-registering as `admin`/`company_admin` → 403; `PUT /me` with `role`, `is_active`, `company_id`, `is_admin` leaves role unchanged; `/admin/users` 401 anonymous / 403 candidate; forged JWT → 401 |
| Credential hygiene | passed | no hard-coded secrets found in source/config; templates hold placeholders only; app refuses to start without `SECRET_KEY` when `DEBUG=false` |
| CORS | passed | default config refuses foreign origins (no `Access-Control-Allow-Origin`); suite covers allow-lists |
| Upload hardening | passed | suite + probes: 11 MB body → 413; EXE renamed `.pdf` → 415; legacy `.doc` → 415; traversal filename → 422 |
| Scoring transparency / API compatibility | passed | Task 6 tests unchanged and green; finite results are unchanged |
| NaN / Infinity → invalid JSON or 500 | **defect found, fixed** | injection probe over 5 endpoints × 12 fields × (nan, inf, −inf): **105 of 180 bad before → 0 of 180 after** |
| Release contents | passed | packager `--verify`: no `.env`/`.env.production`, databases, `app/data`, caches, logs, `node_modules`, credential docs; member diff vs Task 6 = the 2 new files |
| Patch integrity | passed | applying the patch to the untouched Task 6 tree reproduces the extracted release exactly (`diff -r` clean) |

\* **Test stub (not shipped):** the sandbox cannot install torch / `sentence-transformers` or reach huggingface.co. For the "with stub" runs a stand-in `sentence_transformers` package, kept **outside** the project and added only via `PYTHONPATH`, whose model constructor always raises — i.e. the real library's "no cached model, no network" behaviour. It lets scorer tests import and run with semantic scoring **unavailable**.

## Defect found and fixed
A non-finite number from the scorer was not handled anywhere (only the semantic-similarity term had a guard). Before: HTTP 500 on most score endpoints (Starlette refuses to render `NaN`); `json.dumps` would store the non-standard literal `NaN`; on the original scorer a resume scoring 32.6 became **100.0** with an `inf` sub-score and **0.0** with `NaN`/`−inf`, and the raw value was returned as that dimension's score.
Fix, minimal and layered: (1) the scorer treats a non-finite dimension like a failed one — same labelled placeholder and `scoring_explanation.warnings` entry — and skips a non-finite India adjustment / zeroes a non-finite domain confidence; (2) `SafeJSONResponse` (app default) renders any remaining NaN/Infinity as `null`, logging only a count; (3) the stored `analysis_data` blob is sanitised. Normal results are unchanged (no new warnings, same scores).

## Other changes
- `tests/test_credential_hygiene.py`: `test_shipped_templates_are_rejected_as_keys` read the local, git-ignored `.env.production` and so failed on every clean checkout. It now **requires** `.env.example` and `.env.production.example` to exist and checks them, and checks `.env.production` only if present. Mutation-checked: a real-looking key in a template, or a missing template, still fails it.
- `scripts/make_source_dist.py`: also excludes `.idea/ .vscode/ htmlcov/ .tox/ .nox/ .ruff_cache/ .hypothesis/ temp_uploads/ *.egg-info/ .coverage .DS_Store Thumbs.db *.swp *.swo *.pyo *.orig *.rej`.
- `.env.example`, `app/config.py`: the four `*_WEIGHT` settings are read but used nowhere; now documented as unused (values untouched).
- `CHANGELOG.md`: v20.7 entry.

## Changed files
`app/json_safety.py` (new) · `main.py` · `app/api/analysis_routes.py` · `app/nlp/enhanced_scorer.py` · `app/config.py` · `.env.example` · `scripts/make_source_dist.py` · `tests/test_credential_hygiene.py` · `tests/test_task7_regression.py` (new, 106 tests; 71 of them fail on the unmodified Task 6 code) · `CHANGELOG.md`.

## Remaining failures, skips and limitations
- **Still failing (1):** `tests/test_parsers.py::TestEnhancedScorer::test_score_with_jd_higher_than_without` — asserts `semantic_similarity is not None`, which needs the downloaded sentence-transformer model. Fails identically on Task 4/5/6 code. Not changed.
- **The real semantic model was never loaded or tested.** Semantic logic was exercised only through the existing deterministic fake models and with the model unavailable. Nothing here says anything about real-model behaviour or quality.
- No labelled evaluation data exists: no accuracy, precision, recall or fairness improvement is claimed or measured.
- Without the stub, 178 tests are skipped and 10 fail/error for the environment reason above; the ZIP's `requirements.txt` install (torch etc.) was not exercised.
- Not run: Docker build, Render/Railway deployment, a real browser, multi-worker load, `pip install -r requirements.txt` end to end. Only a `.docx` was used for the endpoint probes (PDF paths are covered by the existing suite).
- The NaN injection probes inject at the scorer boundary; they show the API tolerates non-finite values, not that the real scorer produces them. A fuzz of the real scorer found none, but that is evidence of absence only for the inputs tried.
- Two throwaway probe scripts and stub live outside the release and are not included.
