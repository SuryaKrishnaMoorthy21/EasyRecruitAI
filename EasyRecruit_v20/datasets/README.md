# EasyRecruit ATS 3.0 — Datasets

## Files

### `kaggle_skill_kb.json`
- **Source**: Kaggle Resume Dataset (2,484 resumes, 24 categories)
- **Contains**: Domain-level skill stats, discriminative phrases, pattern skills
- **Used by**: `KeywordExtractor` for domain-aware skill weighting

### `india_hiring_kb.json` *(NEW in v9)*
- **Source**: Curated Indian & South Indian job market data (2024-25)
- **Contains**:
  - 100+ Indian companies across IT services / product / MNC / South India categories
  - South Indian city profiles: Bengaluru, Chennai, Hyderabad, Coimbatore, Kochi/Trivandrum
  - Indian education institute tiers (IIT/IIM → NIT/IIIT → Tier-2)
  - India-specific skills: SAP, Mainframe, Core Banking, Fintech, Zoho stack
  - ATS scoring bonuses for premium institutes, certifications, company tiers
  - Fresher/campus hire detection patterns
  - Indian compliance/legal keywords (GST, PF/ESIC, Labour law, RBI/SEBI)
- **Used by**: `KeywordExtractor.detect_indian_context()` and `EnhancedScorer._india_score_bonus()`

### `departments_100.json` *(NEW in v13)*
- **Contains**: All 100 programmes (S.No 1–100) offered across the Interview Generator's
  "By Department" mode — B.E./B.Tech./B.Sc./B.C.A./B.B.A./B.Com./B.A./B.Arch./B.Pharm./
  B.Des./M.C.A./M.B.A./M.Tech./M.E./M.Sc. (15 degree types).
- **Fields per programme**: `id`, `sno`, `degree`, `programme`, `full_name`, `categories`
  (1–2 keys into `department_qa_bank.json`'s `categories`).
- **Used by**: `GET /api/v1/interview/departments` — powers the cascading
  Degree Type → Programme picker (choose "B.E." and only B.E. programmes show, etc).

### `department_qa_bank.json` *(NEW in v13)*
- **Contains**: 188 original interview Q&A pairs (question + model answer), authored
  for this project — **not** scraped or copied from GeeksforGeeks or any other site.
  Organised as `common` (18 HR/behavioural, applies to all 100 programmes),
  `aptitude` (10 quantitative/logical reasoning), and 12 subject-cluster `categories`
  (e.g. `cs_it`, `ece_eee`, `mech_prod`, `biomed_health`, `business_mgmt`, ...).
- **Used by**: `POST /api/v1/interview/generate-by-department` — every programme gets
  General/HR + Aptitude (shared across all 100) plus its own mapped technical
  category/categories, so answers are included and nothing needs an external API call.
- **To grow this bank**: add `{"q": "...", "a": "..."}` entries to the relevant category
  (or `common`/`aptitude`) in the JSON — no code changes needed, the endpoint reads it live.

### `cse/resources.json`
Learning resources for Computer Science & Engineering domain.

### `ece_eee/resources.json`
Learning resources for Electronics / Electrical Engineering domain.

### `commerce/resources.json`
Learning resources for Commerce, Accounting & Receptionist tracks.

## Adding Custom Datasets

To add a new domain dataset, place a `resources.json` in a new subfolder
and register the domain in `app/api/recommendation_routes.py`.
