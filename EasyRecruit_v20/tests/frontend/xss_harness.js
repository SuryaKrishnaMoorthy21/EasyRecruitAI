#!/usr/bin/env node
/*
 * HTML-injection regression harness for the EasyRecruit frontend.
 *
 *   cd tests/frontend && npm install && npm test
 *   node xss_harness.js ../../frontend/index.html [--only <scenario>] [--list]
 *
 * It loads the REAL page in jsdom (scripts enabled), answers every fetch() from a stub that
 * returns hostile-but-harmless payloads in EVERY string field, drives the page's own loaders
 * and modals, CLICKS the buttons they generate, then scans the DOM for:
 *   - injected elements (.pwn-marker / [data-pwn]) and injected <script>
 *   - on* attributes containing the payload (handler breakout / attribute injection)
 *   - javascript: URLs, style-attribute injection
 *   - actual payload execution (window.__pwn)
 * Each hostile case has a benign control ( R&D <Lead> "O'Neil" ) that must render as literal text,
 * which proves the fix escapes rather than deletes. Payloads only set window.__pwn - nothing harmful.
 * Exit code 0 = clean, 1 = findings, 3 = jsdom not installed.
 */
'use strict';
const fs = require('fs'), path = require('path');
let JSDOM, VirtualConsole;
try { ({ JSDOM, VirtualConsole } = require('jsdom')); }
catch (e) { console.error('JSDOM_MISSING: run "npm install" in tests/frontend'); process.exit(3); }
process.on('unhandledRejection', () => {});          // page-internal async errors are not what we test

const BEN = `R&D <Lead> "O'Neil"`;
const PAYLOADS = {
  tag:    `<i class="pwn-marker" data-pwn="tag">x</i>`,
  imgerr: `<img src=x onerror="window.__pwn='imgerr'">`,
  attrq:  `"><i class="pwn-marker" data-pwn="attrq"></i>`,
  attrev: `x" onmouseover="window.__pwn='attrev'" data-x="`,
  js_sq:  `');window.__pwn='js_sq';//`,
  js_bs:  `\\');window.__pwn='js_bs';//`,
  js_ent: `&#39;);window.__pwn=&#39;js_ent&#39;;//`,
  jsurl:  `javascript:window.__pwn='jsurl'`,
  css:    `red;background:url(https://pwn.invalid/x)`,
};
const sleep = ms => new Promise(r => setTimeout(r, ms));

// ── fixtures: every string field carries the value under test ───────────────────────────────
const A = v => ({ id: 7, filename: v, candidate_name: v, candidate_email: v, candidate_phone: v,
  overall_score: 80, keyword_score: 70, skill_score: 60, structure_score: 50, created_at: '2026-01-01T00:00:00',
  matched_skills: JSON.stringify([v]), missing_skills: JSON.stringify([v]),
  recommendations: JSON.stringify([{ priority: v, category: v, suggestion: v }]),
  breakdown: { skill_analysis: { matched_skills: [v], missing_skills: [v] }, recommendations: [{ priority: v, category: v, suggestion: v }] } });
const RES1 = v => ({ overall_score: 80, keyword_match_score: 70, skill_match_score: 60, structure_score: 50, semantic_similarity: 40,
  candidate_name: v, candidate_email: v, detected_domain: v, filename: v, batch_index: v,
  breakdown: { skill_analysis: { matched_skills: [v], missing_skills: [v] }, recommendations: [{ priority: v, category: v, suggestion: v }] },
  indian_context: { is_indian_resume: true, detected_city: v, india_specific_skills: [v], has_premium_institute: true, detected_tier: 'product', is_fresher: true },
  india_context_bonus: 2 });
const N = v => ({ id: 11, company: v, color: v, logo_initial: v, location: v, deadline: v, tier: v, apply_link: v,
  positions: [{ title: v, experience: v, openings: v, type: v, skills: [v] }], already_applied: [] });
const NA = (v, full) => ({ id: 21, company: v, position_title: v, candidate_name: v, candidate_email: v, resume_filename: v,
  resume_snippet: v, cover_note: v + ' note', created_at: '2026-01-01T00:00:00', status: v, ...(full ? { resume_text: v } : {}) });
const RESRC = v => ({ title: v, subject: v, channel: v, platform: v, duration: v, price: v, branch: v, career_track: v,
  why: v, tags: [v], url: v, rating: 4.5, certificate: true });

function routes(v) {
  return [
    [/\/api\/v1\/analysis\/stats\/summary/, 200, { total_analyses: 1, average_score: 80, highest_score: 80, total_jobs: 1, recent_activity_7_days: 1, score_distribution: { '75-100': 1 } }],
    [/\/api\/v1\/analysis\/\d+/, 200, A(v)],
    [/\/api\/v1\/analysis\//, 200, { items: [A(v)] }],
    [/\/api\/v1\/jobs\//, 200, { items: [{ id: 3, title: v, company: v, description: v, created_at: '2026-01-01T00:00:00' }] }],
    [/\/api\/v1\/auth\/admin\/stats/, 200, { total_users: 1, active_users: 1, total_analyses: 1, avg_score: 1, total_jobs: 1, users_by_role: { [v]: 3 } }],
    [/\/api\/v1\/auth\/admin\/users$/, 200, [{ id: 5, full_name: v, email: v, username: v, role: 'candidate', login_count: 2, is_active: 1, created_at: '2026-01-01T00:00:00' }]],
    [/\/api\/v1\/auth\/company-admin\/team/, 200, { company: v, recruiters: [{ id: 5, full_name: v, email: v, approval_status: 'pending', created_at: '2026-01-01T00:00:00' }] }],
    [/\/api\/v1\/auth\/companies/, 200, [{ name: v, recruiter_domain: v }]],
    [/\/api\/v1\/notifications\/stats/, 200, { total_companies: 1, total_positions: 1, total_openings: 1 }],
    [/\/api\/v1\/notifications\/feed/, 200, { notifications: [N(v)] }],
    [/\/api\/v1\/notifications\/my-applications/, 200, { total: 1, applications: [{ position_title: v, company: v, status: v, created_at: '2026-01-01T00:00:00' }] }],
    [/\/api\/v1\/notifications\/applications\/\d+/, 200, NA(v, true)],
    [/\/api\/v1\/notifications\/applications/, 200, { applications: [NA(v)] }],
    [/\/api\/v1\/recommendations\/domains/, 200, { domains: [{ domain: 'cse', domain_full: v, available: true }, { domain: 'zzz', domain_full: v, available: false }] }],
    [/\/api\/v1\/recommendations\/[^/?]+\/subjects/, 200, { subjects: [v] }],
    [/\/api\/v1\/recommendations\//, 200, { meta: { domain_full: v }, total_free: 1, total_premium: 1, filtered_by_skills: true, free: [RESRC(v)], premium: [RESRC(v)] }],
    [/\/api\/v1\/interview\/departments/, 200, { degree_order: [v], grouped: { [v]: [{ id: v, name: v, sno: 1 }] } }],
    [/\/api\/v1\/interview\/generate-by-department/, 200, { department: v, total_questions: v, categories: { [v]: [{ question: v, answer: v }] } }],
    [/\/api\/v1\/fraud\//, 200, { risk_level: v, fraud_score: 50, flags: [{ severity: v, message: v }], recommendations: [v] }],
    [/\/api\/v1\/bias\//, 200, { biased_score: 50, anonymous_score: 60, removed_fields: [v] }],
    [/\/api\/v1\/notifications\/resume\//, 200, '__HTMLBLOB__'],
  ];
}

// ── scenarios ───────────────────────────────────────────────────────────────────────────────
const OVERLAYS = ['.amod-ov', '.notif-overlay', '#candModal', '#naModalOverlay'];
const setProgramme = w => { w.document.getElementById('ivDeptProgramme').innerHTML = '<option value="1">x</option>'; };
const INDEX = [
  { n: 'dashboard-recent-and-candidates', scopes: ['#dashRecent', '#dashCands'], run: async w => { await w.loadRecDash(); await sleep(40); } },
  { n: 'candidates-list-and-detail-modal', scopes: ['#candsGrid'], run: async w => { await w.loadCandidates(); } },
  { n: 'single-analysis-result', scopes: ['#recsDiv'], run: async (w, v) => { w.renderRes(RES1(v)); } },
  { n: 'batch-analysis-results', scopes: ['#resBatchContent'], run: async (w, v) => { w.renderBatchRes({ succeeded: 1, failed: 1, total: 2, results: [RES1(v), { error: v, filename: v, batch_index: v }] }); } },
  { n: 'history-tables', scopes: ['#histWrap', '#sHistWrap'], run: async w => { await w.loadHist(); await w.loadSeekerHist(); } },
  { n: 'jobs-list', scopes: ['#jobsList'], run: async w => { await w.loadJobs(); } },
  { n: 'admin-users-table', scopes: ['#aUsersWrap', '#aRoleChart'], run: async w => { await w.loadAdminU(); await w.loadAdminStats(); } },
  { n: 'team-approvals', scopes: ['#taWrap'], run: async w => { await w.loadTeamApprovals(); } },
  { n: 'notification-popup-and-apply', scopes: ['#npCards'], run: async w => { await w.fetchAndShowPopup(); } },
  { n: 'job-market-and-apply', scopes: ['#jmGrid'], run: async w => { await w.loadJobMarket(); } },
  { n: 'my-applications', scopes: ['#myAppsWrap'], run: async (w, v) => { w.eval(`_jmAll=[{company:${JSON.stringify(v)},color:${JSON.stringify(v)},logo_initial:${JSON.stringify(v)}}]`); await w.loadMyApps(); } },
  { n: 'recruiter-applications-and-modal', scopes: ['#naWrap'], run: async w => { await w.loadNotifApps(); } },
  { n: 'learning-resources', scopes: ['#resResults', '#resDomPills'], run: async w => { await w.initResPage('cse'); await sleep(60); await w.loadRes(); } },
  { n: 'company-select-and-hint', scopes: [], run: async w => { await w.loadCompaniesList(); const s = w.document.getElementById('regCompany'); s.selectedIndex = 1; w.onRegCompanyChange(); } },
  { n: 'interview-generator', scopes: ['#ivResults'], run: async w => { await w.loadInterviewPage(); const d = w.document.getElementById('ivDeptDegree'); d.selectedIndex = 1; w.ivOnDegreeChange(); await w.generateInterview(); } },
  { n: 'fraud-check-result', scopes: ['#fraudResults'], run: async w => { w.fraudSwitchTab('text'); w.document.getElementById('fraudText').value = 'resume text'; await w.detectFraud(); } },
  { n: 'bias-compare-result', scopes: ['#biasResults'], run: async w => { w.biasSwitchTab('text'); w.document.getElementById('biasJdText').value = 'jd'; w.document.getElementById('biasResumeText').value = 'resume'; await w.runBiasCompare(); } },
  { n: 'avatar-and-profile-photo', scopes: [],
    benignCheck: w => Array.from(w.document.querySelectorAll('img')).some(i => i.getAttribute('alt') === BEN),
    run: async (w, v) => {
      const photo = v === BEN ? '/uploads/profile_photos/a.jpg' : v;   // benign control uses a valid URL so the <img alt> path is exercised
      w.eval(`_user.full_name=${JSON.stringify(v)};_user.photo_url=${JSON.stringify(photo)};`);
      const d = w.document.createElement('div'); w.document.body.appendChild(d); d.innerHTML = w.avatarHTML(32, 'userAv');
      w.renderProfilePhoto(v); } },
  { n: 'toast-messages', scopes: ['#toastContainer'], run: async (w, v) => { w.showToast(v, 'err'); w.showToast('Failed: ' + v, 'ok'); } },
  { n: 'api-error-messages', mode: 'err', scopes: ['#jobsList', '#histWrap', '#candsGrid', '#aUsersWrap', '#taWrap', '#jmGrid', '#myAppsWrap', '#naWrap', '#resResults', '#sHistWrap', '#dashRecent'],
    run: async w => { for (const f of ['loadJobs', 'loadHist', 'loadCandidates', 'loadAdminU', 'loadTeamApprovals', 'loadJobMarket', 'loadMyApps', 'loadNotifApps', 'loadRes', 'loadSeekerHist', 'loadRecDash']) { try { await w[f](); } catch (e) {} } } },
  { n: 'tool-error-responses', mode: 'err', scopes: ['#ivResults', '#fraudResults', '#biasResults'], run: async w => {
      setProgramme(w); await w.generateInterview();
      w.fraudSwitchTab('text'); w.document.getElementById('fraudText').value = 'x'; await w.detectFraud();
      w.biasSwitchTab('text'); w.document.getElementById('biasJdText').value = 'j'; w.document.getElementById('biasResumeText').value = 'r'; await w.runBiasCompare(); } },
  { n: 'resume-view-blob-type', scopes: [], benignCheck: w => w.__lastBlobType === 'application/octet-stream', run: async w => { await w.naViewResume(21); } },
  { n: 'tool-network-exceptions', mode: 'throw', scopes: ['#ivResults', '#fraudResults', '#biasResults'], run: async w => {
      setProgramme(w); await w.generateInterview();
      w.fraudSwitchTab('text'); w.document.getElementById('fraudText').value = 'x'; await w.detectFraud();
      w.biasSwitchTab('text'); w.document.getElementById('biasJdText').value = 'j'; w.document.getElementById('biasResumeText').value = 'r'; await w.runBiasCompare(); } },
];
const DASH = [
  { n: 'dashboard-history-table', scopes: ['#historyWrap'], run: async () => { await sleep(150); } },
];

// ── engine ──────────────────────────────────────────────────────────────────────────────────
function scan(win) {
  const d = win.document, found = [];
  d.querySelectorAll('.pwn-marker,[data-pwn]').forEach(e => found.push(`injected <${e.tagName.toLowerCase()}> element`));
  d.querySelectorAll('*').forEach(e => {
    for (const a of Array.from(e.attributes)) {
      if (/^on/i.test(a.name) && /__pwn/.test(a.value)) found.push(`payload inside ${a.name}= on <${e.tagName.toLowerCase()}>`);
      if (a.name === 'style' && /pwn\.invalid/.test(a.value)) found.push(`style injection on <${e.tagName.toLowerCase()}>`);
      if ((a.name === 'href' || a.name === 'src') && /^\s*javascript:/i.test(a.value)) found.push(`javascript: URL in ${a.name}=`);
    }
  });
  d.querySelectorAll('script').forEach(s => { if (/__pwn/.test(s.textContent)) found.push('injected <script>'); });
  if (win.__pwn) found.push(`PAYLOAD EXECUTED (${win.__pwn})`);
  if (win.__lastBlobType !== undefined && !/^(application\/pdf|application\/octet-stream)$/.test(win.__lastBlobType)) found.push(`resume blob opened with active content-type (${win.__lastBlobType || 'none'})`);
  return found;
}
async function clickScopes(win, scopes, reverse) {
  const set = new Set();
  for (const sel of scopes) win.document.querySelectorAll(sel).forEach(sc => {
    if (sc.matches('[onclick],.cand-card')) set.add(sc);
    sc.querySelectorAll('[onclick],.cand-card').forEach(e => set.add(e));
  });
  const els = reverse ? Array.from(set).reverse() : Array.from(set);
  for (const e of els) { try { e.click(); } catch (_) {} await sleep(8); }
  return els.length;
}
async function runCase(html, file, sc, label, v, user) {
  const table = { current: [] };
  const vc = new VirtualConsole();
  const resp = (w, status, body) => ({ ok: status < 400, status, json: async () => body, text: async () => JSON.stringify(body), blob: async () => body === '__HTMLBLOB__' ? new w.Blob(['<script>window.__pwn=1</script>'], { type: 'text/html' }) : new w.Blob(['x']), headers: { get: () => null } });
  const dom = new JSDOM(html, { url: 'http://localhost:8001/', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.localStorage.setItem('er_token', 'test-token'); w.localStorage.setItem('er_user', JSON.stringify(user));
      w.confirm = () => false; w.alert = () => {}; w.open = () => null; w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = () => {}; w.URL.createObjectURL = (b) => { w.__lastBlobType = (b && b.type) || ''; return 'blob:x'; }; w.URL.revokeObjectURL = () => {};
      w.matchMedia = w.matchMedia || (() => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }));
      w.Chart = function () { return { destroy() {}, update() {} }; };
      table.current = file === 'dashboard.html' ? routes(v) : [];
      w.fetch = async (url) => {
        const p = String(url).replace(/^https?:\/\/[^/]+/, '');
        if (sc.mode === 'throw' && /\/api\/v1\/(interview|fraud|bias)/.test(p)) throw new Error(v);
        for (const [rx, st, body] of table.current) if (rx.test(p)) return resp(w, sc.mode === 'err' ? 500 : st, sc.mode === 'err' ? { detail: v } : body);
        return resp(w, 200, {});
      };
    } });
  const w = dom.window; await sleep(60);                      // let showApp() boot with empty data
  if (file !== 'dashboard.html') table.current = routes(v);
  let runError = null;
  try { await sc.run(w, v); } catch (e) { runError = String(e && e.message || e); }
  await sleep(40);
  const findings = new Set(scan(w));
  const text = () => w.document.body.textContent;
  const benignOk = label !== 'benign' ? true : (sc.benignCheck ? sc.benignCheck(w) : text().includes(BEN));
  if (label !== 'benign') {
    await clickScopes(w, sc.scopes, false); await sleep(40); scan(w).forEach(f => findings.add(f));
    await clickScopes(w, OVERLAYS, true); await sleep(40); scan(w).forEach(f => findings.add(f));
    await clickScopes(w, ['.toast'], false); await sleep(20); scan(w).forEach(f => findings.add(f));
  }
  w.close();
  return { findings: Array.from(findings), benignOk, runError };
}


// ── detector self-test: prove scan() really flags vulnerable rendering and passes safe rendering ──
async function selftest() {
  const page = (render) => `<!doctype html><body><div id="o"></div><script>
    function f(x){}  function g(el){}
    function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');}
    ${render}</script></body>`;
  const VULN = `function render(v){document.getElementById('o').innerHTML='<b>'+v+'</b><button onclick="f(\\''+v+'\\')">go</button><a href="'+v+'">l</a>';}`;
  const SAFE = `function render(v){const o=document.getElementById('o');o.innerHTML='<b>'+esc(v)+'</b><button data-v="'+esc(v)+'" onclick="g(this)">go</button>';}`;
  let ok = true;
  for (const [label, src, expectFindings] of [['vulnerable', VULN, true], ['safe', SAFE, false]]) {
    const hit = [];
    for (const [pn, pv] of Object.entries(PAYLOADS)) {
      const dom = new JSDOM(page(src), { runScripts: 'dangerously', virtualConsole: new VirtualConsole() });
      dom.window.render(pv);
      await clickScopes(dom.window, ['#o'], false);
      if (scan(dom.window).length) hit.push(pn);
      dom.window.close();
    }
    const good = expectFindings ? hit.length >= 5 : hit.length === 0;
    console.log(`selftest ${label}: flagged payloads = [${hit.join(',')}] -> ${good ? 'as expected' : 'UNEXPECTED'}`);
    ok = ok && good;
  }
  console.log(ok ? 'SELFTEST OK' : 'SELFTEST FAILED');
  process.exit(ok ? 0 : 1);
}

(async () => {
  const args = process.argv.slice(2);
  if (args.includes('--selftest')) return selftest();
  const file = args.find(a => !a.startsWith('--')); if (!file) { console.error('usage: node xss_harness.js <page.html> [--only name] [--list]'); process.exit(2); }
  const html = fs.readFileSync(file, 'utf8'), base = path.basename(file);
  const scenarios = base === 'dashboard.html' ? DASH : INDEX;
  if (args.includes('--list')) { scenarios.forEach(s => console.log(s.n)); return; }
  const oi = args.indexOf('--only'); const only = oi >= 0 ? args[oi + 1] : null;
  const user = { id: 1, email: 'tester@example.org', username: 'tester', full_name: 'Test User', role: 'recruiter' };
  let cases = 0; const bad = [], benignBad = [], runErrors = [];
  for (const sc of scenarios) {
    if (only && sc.n !== only) continue;
    const u = sc.n === 'my-applications' ? { ...user, role: 'candidate' } : user;
    const row = [];
    for (const [pn, pv] of [...Object.entries(PAYLOADS), ['benign', BEN]]) {
      cases++;
      const r = await runCase(html, base, sc, pn === 'benign' ? 'benign' : 'hostile', pv, u);
      if (r.runError) runErrors.push(`${sc.n}/${pn}: ${r.runError}`);
      if (pn === 'benign') { if (!r.benignOk) { benignBad.push(sc.n); row.push('benign-text-missing'); } }
      else if (r.findings.length) { bad.push({ scenario: sc.n, payload: pn, findings: r.findings }); row.push(`${pn}:${r.findings.length}`); }
    }
    console.log(`${row.length ? 'FAIL' : ' ok '}  ${sc.n}${row.length ? '   ' + row.join(' ') : ''}`);
  }
  for (const b of bad) console.log(`  [${b.scenario} / ${b.payload}] ${b.findings.join('; ')}`);
  if (runErrors.length) console.log(`  (harness notes: ${runErrors.length} scenario run errors, first: ${runErrors[0]})`);
  console.log(`RESULT ${base}: ${cases} cases | injection findings: ${bad.length} | benign-render failures: ${benignBad.length} | run errors: ${runErrors.length}`);
  process.exit(bad.length || benignBad.length ? 1 : 0);
})();
