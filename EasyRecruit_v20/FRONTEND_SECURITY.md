# Frontend HTML-injection safety

**Threat:** almost every string the UI shows is attacker-influenced. A job seeker controls their resume
text (candidate name, skills, filename, cover note), a recruiter controls job titles/descriptions, and
all of it is displayed to *other* users, including recruiters and admins. Rendering any of it as HTML
lets a hostile resume run script in a recruiter's session (stored XSS).

## Rules for new code (`frontend/index.html`, `frontend/dashboard.html`)
| Situation | Do this |
|---|---|
| Plain text into an element | `el.textContent = value` (or `createTextNode`) |
| A template string that needs markup | wrap **every** dynamic value: `escH(value)` |
| An id/number inside `onclick="…"` | `escId(value)` (digits only) |
| Any other value an inline handler needs | **don't.** Put it in `data-*` (escaped with `escH`) and read `this.dataset` (see `adminDelBtn`, `openApplyFromBtn`) |
| A URL (`href`, `src`) | `safeUrl(value)` (http(s) or root-relative only), plus `rel="noopener noreferrer"` for `_blank` |
| A colour / class token in an attribute | `safeColor(value)` / `cls(value)` |
| A path segment or query value in an API URL | `encodeURIComponent(value)` |
| Opening a server file as a blob URL | never trust its Content-Type (see `naViewResume`) |
| Toasts | `showToast(msg)` takes **plain text**; do not pass HTML |

`innerHTML` with a fully static literal (spinners, empty states, button labels) is fine and was left alone.
Never "fix" an escaping problem by escaping in `apiErr()`: it is also used with `textContent`, which would double-escape.

## Verifying (reproducible)
```bash
cd tests/frontend && npm install          # one-time: installs jsdom (Node 18+)
npm test                                  # runs the harness on index.html and dashboard.html
node xss_harness.js --selftest            # proves the detector flags vulnerable rendering
python -m pytest tests/test_frontend_xss.py   # static tripwires + the harness (skips if Node/jsdom absent)
```
The harness loads the real page in jsdom, answers every `fetch()` with harmless hostile payloads in *every*
string field (markup, attribute breakouts, `'`/`\`/entity-encoded JS-string breakouts, `javascript:` URLs, CSS),
drives the page's own loaders and modals, **clicks the generated buttons**, and scans for injected elements,
injected `on*` handlers, `javascript:` URLs, style injection, and actual payload execution. Each hostile case
has a benign control (`R&D <Lead> "O'Neil"`) that must appear as literal text, which proves values are
escaped and not just stripped. Adding a new API-driven renderer? Add a scenario to `xss_harness.js`.

## Known limitations / remaining risks
1. **JWT lives in `localStorage`** (`er_token`); any future XSS can read it. Moving to an HttpOnly cookie is a backend change.
2. **No Content-Security-Policy.** The page relies on inline `<script>` and ~200 inline `on*=` handlers, so a strict CSP is not yet possible.
   Next step: move handlers to `addEventListener`, then enforce a CSP and security headers server-side.
3. `dashboard.html` loads Chart.js from a CDN **without Subresource Integrity**.
4. The **backend stores and returns raw resume-derived text** and serves uploaded resumes with the stored `resume_mime` and an
   unsanitised `Content-Disposition` filename. The client no longer trusts them, but other API consumers must escape too.
5. The harness covers the renderers enumerated in it with fixtures; new endpoints/fields need scenarios. The static tripwires are
   heuristics, not a proof. jsdom does not load images, so `onerror` is detected structurally rather than by it firing.
6. `ivEsc()` does not escape quotes; it is only used for text nodes. Use `escH()` anywhere near an attribute.
