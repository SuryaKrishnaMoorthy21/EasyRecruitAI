"""
Task 3 regression tests: HTML-injection safety of the frontend.

Two layers
  1. Static tripwires (always run, milliseconds): fail fast if the known-dangerous patterns come back
     (untrusted strings inside inline JS handlers, raw API text in templates, innerHTML in showToast...).
  2. Behavioural harness (tests/frontend/xss_harness.js): runs the REAL pages in jsdom, feeds hostile
     payloads through every API-driven renderer, clicks the generated buttons and scans the DOM.
     Needs Node 18+ and `npm install` in tests/frontend (or jsdom on NODE_PATH). If those are missing
     the harness tests are SKIPPED with a clear reason (environment-blocked, not passed).

Payloads are harmless: they only try to set window.__pwn or inject marker elements.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
HARNESS_DIR = ROOT / "tests" / "frontend"
INDEX = (FRONTEND / "index.html").read_text(encoding="utf-8")
DASH = (FRONTEND / "dashboard.html").read_text(encoding="utf-8")


def _script(html: str) -> str:
    return re.search(r"<script>(.*)</script>", html, re.S).group(1)


JS = _script(INDEX)

# ── 1. static tripwires ───────────────────────────────────────────────────────────────────────
# Dynamic values allowed inside an inline on*="" handler: digit-only ids and loop counters. Nothing else.
_HANDLER = re.compile(r"""\bon(?:click|change|input|submit|mouseenter|mouseleave|mouseover|mouseout|error|load|key\w+)="((?:[^"\\]|\\.)*)\"""")
_DYN = re.compile(r"""(?<!\\)'\+(.+?)\+(?<!\\)'|\$\{(.+?)\}""")
_ALLOWED_HANDLER_EXPR = re.compile(r"^(escId\([^()]*\)|i|idx|index)$")


def _handler_dynamic_exprs(js: str):
    out = []
    for h in _HANDLER.finditer(js):
        for d in _DYN.finditer(h.group(1)):
            out.append((h.group(1)[:80], (d.group(1) or d.group(2)).strip()))
    return out


def test_no_untrusted_values_inside_inline_handlers():
    bad = [(h, e) for h, e in _handler_dynamic_exprs(JS) if not _ALLOWED_HANDLER_EXPR.match(e)]
    assert not bad, f"dynamic values in inline handlers must be escId()/loop indices only: {bad}"


def test_handler_lint_actually_sees_handlers():
    # guards against the regex silently matching nothing
    exprs = [e for _, e in _handler_dynamic_exprs(JS)]
    assert len(exprs) >= 8 and any(e.startswith("escId(") for e in exprs)


def test_handler_lint_flags_the_old_vulnerable_forms():
    old_admin = """<button onclick="adminDel('+u.id+',\\''+u.email.replace(/'/g,"\\\\'")+'\\')">x</button>"""
    old_apply = "onclick=\"openApplyModal(${n.id},'${escQ(n.company)}','${escQ(p.title)}',this)\""
    for snippet in (old_admin, old_apply):
        exprs = [e for _, e in _handler_dynamic_exprs(snippet)]
        assert any(not _ALLOWED_HANDLER_EXPR.match(e) for e in exprs), snippet


def test_broken_escq_helper_is_gone():
    assert "escQ" not in INDEX


def test_escH_escapes_quotes_and_ampersand():
    m = re.search(r"function escH\(s\)\{(.*?)\}\n", JS)
    assert m, "escH missing"
    for needle in ("&amp;", "&lt;", "&gt;", "&quot;", "&#39;"):
        assert needle in m.group(1), needle


def test_helpers_exist():
    for fn in ("escH", "escId", "cls", "safeUrl", "safeColor"):
        assert re.search(rf"function {fn}\(", JS), fn


def test_showToast_does_not_use_innerHTML():
    m = re.search(r"function showToast\(.*?\n\}\n", JS, re.S)
    assert m and "innerHTML" not in m.group(0) and "textContent" in m.group(0)


def test_no_raw_error_text_concatenated_into_html():
    # server/exception text must be wrapped in escH() when it goes into a template
    # (showToast/showAlert take plain text, so `'Failed: '+apiErr(data)` there is fine; only a value that is
    #  spliced back into an HTML string -- i.e. followed by +'...' -- must be escaped)
    assert not re.search(r"'\+apiErr\(data(?:,'[^']*')?\)\+'", JS), "raw apiErr() in an HTML template"
    assert not re.search(r"'\+e\.message\+'", JS), "raw e.message in an HTML template"
    assert not re.search(r"'\+\(d\.detail\|\|", JS), "raw d.detail in an HTML template"


def test_dynamic_hrefs_and_colours_are_validated():
    assert "${n.apply_link}" not in JS and "safeUrl(n.apply_link)" in JS
    assert "background:${n.color}" not in JS and "safeColor(n.color)" in JS
    assert "background:${cm.color}" not in JS
    assert 'rel="noopener noreferrer"' in JS


def test_resume_blob_is_not_opened_with_server_supplied_type():
    m = re.search(r"async function naViewResume.*?\n\}\n", JS, re.S)
    assert m and "application/octet-stream" in m.group(0) and "createObjectURL(blob)" in m.group(0)


def test_user_facing_dynamic_templates_use_escH():
    # spot checks on the highest-risk, applicant-controlled fields
    for needle in ("escH(a.cover_note)", "escH(a.candidate_name||'Anonymous')", "escH(resumeText.slice(0,3000))",
                   "escH(c.name)", "escH(j.title)", "escH(r.suggestion)", "escH(u.email)"):
        assert needle in JS, needle


def test_dashboard_page_escapes_resume_derived_fields():
    s = _script(DASH)
    assert "function escH(" in s
    assert "${r.filename ||" not in s and "${r.candidate_name ||" not in s
    assert "escH(r.filename" in s and "escH(r.candidate_name" in s


# ── 2. behavioural harness (jsdom) ────────────────────────────────────────────────────────────
def _node_with_jsdom():
    node = shutil.which("node")
    if not node:
        return None, "node is not installed"
    probe = subprocess.run([node, "-e", "require('jsdom')"], cwd=HARNESS_DIR, capture_output=True, text=True)
    if probe.returncode != 0:
        return None, "jsdom not installed (run `npm install` in tests/frontend)"
    return node, ""


def _run_harness(args, timeout):
    node, why = _node_with_jsdom()
    if not node:
        pytest.skip(f"environment-blocked: {why}")
    return subprocess.run([node, "xss_harness.js", *args], cwd=HARNESS_DIR, capture_output=True, text=True, timeout=timeout)


def test_harness_detector_selftest():
    r = _run_harness(["--selftest"], 120)
    assert r.returncode == 0 and "SELFTEST OK" in r.stdout, r.stdout + r.stderr


def test_harness_dashboard_page_is_clean():
    r = _run_harness(["../../frontend/dashboard.html"], 120)
    assert r.returncode == 0 and "injection findings: 0" in r.stdout, r.stdout[-3000:] + r.stderr[-1000:]


def test_harness_index_page_is_clean():
    r = _run_harness(["../../frontend/index.html"], 280)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-1000:]
    assert "injection findings: 0" in r.stdout and "benign-render failures: 0" in r.stdout
