/* ClearPath dashboard. No inline handlers (strict CSP); every dynamic string goes
   through esc() before it touches innerHTML, including model output (OWASP LLM05). */
'use strict';
(function () {
  const $ = id => document.getElementById(id);
  const AUTH_ENABLED = document.body.dataset.authEnabled === 'true';
  let ME = {authenticated: false, role: 'anonymous'};
  let SAMPLES = [];
  let QUEUE = [];
  let CURRENT = null;

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"'`]/g, c => (
      {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;', '`': '&#96;'}[c]));
  }
  const pct = x => Math.round((Number(x) || 0) * 100);
  const DECISIONS = new Set(['approve', 'deny', 'undetermined']);
  const decisionClass = d => (DECISIONS.has(d) ? d : 'undetermined');

  async function api(path, body) {
    const opts = body === undefined ? {credentials: 'same-origin'} : {
      method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)};
    let r = await fetch(path, opts);
    if (r.status === 401 && ME.authenticated && path !== '/auth/refresh') {
      const rr = await fetch('/auth/refresh', {method: 'POST', credentials: 'same-origin'});
      if (rr.ok) r = await fetch(path, opts); else { ME = {authenticated: false, role: 'anonymous'}; renderAuth(); }
    }
    let data = {};
    try { data = await r.json(); } catch (e) { data = {error: 'Unexpected response.'}; }
    if (!r.ok && !data.error) data.error = 'Request failed (' + r.status + ').';
    return {ok: r.ok, status: r.status, data};
  }

  // ── auth ──────────────────────────────────────────────────────────────
  function renderAuth() {
    const signedIn = ME.authenticated;
    $('authbar').classList.toggle('hidden', !AUTH_ENABLED);
    $('signedout').classList.toggle('hidden', signedIn);
    $('signedin').classList.toggle('hidden', !signedIn);
    $('whorole').textContent = ME.role;
    const canSubmit = signedIn && (ME.role === 'submitter' || ME.role === 'admin');
    $('submitnote').classList.toggle('hidden', canSubmit);
    $('submitnote').textContent = signedIn
      ? 'Your role (' + ME.role + ') cannot submit cases. Sample cases still run in demo mode.'
      : 'Visitors can run the sample cases below. Sign in as a submitter to review and store your own case.';
    $('queuetitle').textContent = ME.role === 'reviewer' ? 'Cases assigned to you'
      : ME.role === 'submitter' ? 'Your cases' : ME.role === 'admin' ? 'All cases' : 'Human review queue';
  }
  async function loadMe() {
    const r = await api('/auth/me');
    ME = r.ok ? r.data : {authenticated: false, role: 'anonymous'};
    renderAuth();
    if (ME.authenticated) await loadCases(); else { QUEUE = []; renderQueue(); }
  }
  async function login() {
    $('autherr').textContent = '';
    const r = await api('/auth/login', {email: $('a_email').value.trim(), password: $('a_pass').value});
    $('a_pass').value = '';
    if (!r.ok) { $('autherr').textContent = r.data.error; return; }
    await loadMe();
  }
  async function logout() { await api('/auth/logout', {}); await loadMe(); }

  // ── samples + submit ──────────────────────────────────────────────────
  function renderChips() {
    $('samplechips').innerHTML = SAMPLES.map((c, i) => {
      const cls = c.expected === 'deny' ? 'deny' : (c.expected === 'escalate' ? 'esc' : '');
      const label = esc(c.id) + ' · ' + esc(String(c.requested_procedure).split(' ').slice(0, 2).join(' '));
      return '<span class="chip ' + cls + '" data-sample="' + i + '" role="button" tabindex="0">' + label + '</span>';
    }).join('');
  }
  function loading(msg) {
    $('result').innerHTML = '<div class="placeholder"><i class="fa-solid fa-spinner fa-spin"></i><div>' + esc(msg) + '</div></div>';
  }
  function showError(msg) { $('result').innerHTML = '<div class="errbox">' + esc(msg) + '</div>'; }
  function busy(on) {
    const btn = $('runbtn'); btn.disabled = on;
    btn.innerHTML = on ? '<i class="fa-solid fa-spinner fa-spin"></i> Running pipeline…'
                       : '<i class="fa-solid fa-robot"></i> Run agent review';
  }
  async function runSample(i) {
    const c = SAMPLES[i]; if (!c) return;
    $('f_proc').value = c.requested_procedure; $('f_plan').value = c.plan; $('f_cond').value = c.condition;
    $('f_age').value = c.age; $('f_sex').value = c.sex; $('f_notes').value = c.notes;
    busy(true); loading('Retrieving criteria → proposer → critic → routing…');
    try {
      const r = await api('/demo/run', {sample_id: c.id});
      if (!r.ok) return showError(r.data.error);
      renderResult(r.data);
    } catch (e) { showError('Network error.'); } finally { busy(false); }
  }
  async function submitCase() {
    if (!(ME.authenticated && (ME.role === 'submitter' || ME.role === 'admin'))) {
      return showError('Sign in as a submitter to review your own case, or load a sample case below.');
    }
    const age = $('f_age').value.trim();
    const c = {requested_procedure: $('f_proc').value.trim(), plan: $('f_plan').value,
      condition: $('f_cond').value.trim(), age: age === '' ? null : Number(age),
      sex: $('f_sex').value, notes: $('f_notes').value.trim()};
    if (!c.requested_procedure || !c.notes || !c.condition) return showError('Enter a procedure, condition and clinical notes.');
    busy(true); loading('Retrieving criteria → proposer → critic → routing…');
    try {
      const r = await api('/submit', {case: c});
      if (!r.ok) return showError(r.data.error);
      renderResult(r.data); await loadCases();
    } catch (e) { showError('Network error.'); } finally { busy(false); }
  }

  // ── queue ─────────────────────────────────────────────────────────────
  async function loadCases() {
    const r = await api('/cases');
    QUEUE = r.ok && Array.isArray(r.data) ? r.data : [];
    renderQueue();
  }
  function renderQueue() {
    $('qcount').textContent = QUEUE.length ? '(' + QUEUE.length + ')' : '';
    if (!QUEUE.length) { $('queue').innerHTML = '<div class="empty">' + (ME.authenticated ? 'No cases yet.' : 'Sign in to see stored cases.') + '</div>'; return; }
    $('queue').innerHTML = QUEUE.map((d, i) => '<div class="qitem" data-q="' + i + '" role="button" tabindex="0">' +
      '<div class="t">' + esc(d.id) + ' · ' + esc(String(d.decision).toUpperCase()) + '<span class="status">' + esc(d.status) + '</span></div>' +
      '<div class="s">' + esc(d.case && d.case.requested_procedure) + ' · ' + pct(d.confidence) + '% conf</div></div>').join('');
  }

  // ── result ────────────────────────────────────────────────────────────
  function stepDetail(s) {
    const n = s.node, det = s.detail || {};
    if (n === 'retrieve') return (Array.isArray(det) ? det : []).map(x => '<span class="tag">' + esc(x.id) + ' · ' + esc(x.score) + '</span>').join('');
    if (n === 'proposer') return '<div class="kv"><b>Rationale:</b> ' + esc(det.rationale) + '</div>' +
      '<div class="kv"><b>Cited:</b> ' + (det.cited_criteria || []).map(x => '<span class="tag">' + esc(x) + '</span>').join('') + '</div>' +
      ((det.unmet_requirements || []).length ? '<div class="kv"><b>Unmet:</b> ' + det.unmet_requirements.map(esc).join('; ') + '</div>' : '');
    if (n === 'critic') return '<div class="kv"><b>Confidence:</b> ' + pct(det.confidence) + '% · ' + (det.agree ? 'agrees' : 'disagrees') + '</div>' +
      ((det.gaps || []).length ? '<div class="kv"><b>Gaps:</b> ' + det.gaps.map(esc).join('; ') + '</div>' : '') +
      (det.concern ? '<div class="kv"><b>Concern:</b> ' + esc(det.concern) + '</div>' : '');
    if (n === 'flag_for_review') return '<div class="kv">' + (det.reasons || []).map(esc).join('<br>') + '</div>';
    return '';
  }
  const ICONS = {retrieve: 'fa-magnifying-glass', proposer: 'fa-pen-to-square', critic: 'fa-user-shield',
                 auto_finalize: 'fa-circle-check', flag_for_review: 'fa-user-doctor'};
  const NODES = new Set(Object.keys(ICONS));

  function renderResult(d) {
    CURRENT = d;
    const isAuto = d.route === 'auto_approved';
    const dc = decisionClass(d.decision);
    const traceHtml = (d.trace || []).map(s => {
      const node = NODES.has(s.node) ? s.node : 'other';
      const dt = stepDetail(s);
      return '<div class="step ' + node + '"><div class="st"><i class="fa-solid ' + (ICONS[node] || 'fa-circle') + '"></i> ' + esc(s.title) +
        '<span class="meta">' + (s.tokens ? esc(s.tokens) + ' tok · ' : '') + esc(s.latency_ms) + 'ms</span></div>' +
        '<div class="ss">' + esc(s.summary) + '</div>' + (dt ? '<div class="det">' + dt + '</div>' : '') + '</div>';
    }).join('');
    const reasons = (d.escalation_reasons || []).length ? '<div class="reasons"><b><i class="fa-solid fa-triangle-exclamation"></i> Routed to human review</b><ul>' +
      d.escalation_reasons.map(r => '<li>' + esc(r) + '</li>').join('') + '</ul></div>' : '';
    const pending = d.route === 'needs_human' && (d.demo || d.status === 'pending_review');
    const isReviewer = ME.authenticated && ME.role === 'reviewer' && d.assigned_to_me;
    let actions = '';
    if (pending) {
      const finalBtns = isReviewer
        ? '<button class="ab app" type="button" data-act="approve"><i class="fa-solid fa-check"></i> Approve</button>' +
          '<button class="ab den" type="button" data-act="deny"><i class="fa-solid fa-xmark"></i> Deny</button>' : '';
      const note = isReviewer ? '' : '<div class="notice">' + (d.demo
        ? 'Demo mode: you can send this back for self-correction. Recording an approve or deny decision requires signing in as the assigned reviewer.'
        : 'Only the assigned reviewer can approve, deny or send back this case.') + '</div>';
      const canSendBack = d.demo || isReviewer;
      actions = '<div class="actions"><h3><i class="fa-solid fa-gavel"></i> Reviewer decision</h3>' +
        (canSendBack ? '<textarea id="revnotes" maxlength="1000" placeholder="Optional notes (used if you Send Back for self-correction)…"></textarea>' : '') +
        '<div class="abtns">' + finalBtns + (canSendBack ? '<button class="ab snd" type="button" data-act="send_back"><i class="fa-solid fa-rotate-left"></i> Send back</button>' : '') + '</div>' + note + '</div>';
    }
    const traceUrl = typeof d.trace_url === 'string' && /^https:\/\//.test(d.trace_url) ? d.trace_url : null;
    const lf = traceUrl ? '<a class="lf" href="' + esc(traceUrl) + '" target="_blank" rel="noopener noreferrer"><i class="fa-solid fa-satellite-dish"></i> View full trace in Langfuse</a>' : '';
    const cost = d.usage ? esc((d.usage.in || 0) + (d.usage.out || 0)) + ' tokens · $' + (Number(d.usage.cost) || 0).toFixed(4) : '';
    const icon = dc === 'approve' ? 'circle-check' : (dc === 'deny' ? 'circle-xmark' : 'circle-question');
    const routeLabel = d.status === 'approved' || d.status === 'denied' ? 'Finalized by reviewer: ' + d.status : (isAuto ? 'Auto-resolved' : 'Escalated to human');
    $('result').innerHTML =
      '<div class="decision"><span class="dpill ' + dc + '"><i class="fa-solid fa-' + icon + '"></i> ' + esc(String(d.decision).toUpperCase()) + '</span>' +
      '<span class="route ' + (isAuto ? 'auto' : 'human') + '">' + esc(routeLabel) + '</span>' +
      '<span class="conf">' + pct(d.confidence) + '% confidence' + (d.attempt > 1 ? ' · attempt ' + esc(d.attempt) : '') + '</span></div>' +
      '<div class="ss mono idline">' + esc(d.id) + ' · ' + esc(d.case && d.case.requested_procedure) + ' · ' + esc(d.case && d.case.plan) + (cost ? ' · ' + cost : '') + (d.demo ? ' · demo (not stored)' : '') + '</div>' +
      reasons + '<div class="trace"><h3><i class="fa-solid fa-diagram-project"></i> Agent audit trail</h3>' + traceHtml + '</div>' +
      lf + actions + '<div id="finalslot"></div>';
  }

  async function act(action) {
    const d = CURRENT; if (!d) return;
    const notes = $('revnotes') ? $('revnotes').value.trim() : '';
    const slot = $('finalslot');
    if (action === 'send_back') {
      slot.innerHTML = '<div class="ss"><i class="fa-solid fa-spinner fa-spin"></i> Re-running with your notes (self-correction)…</div>';
      const r = d.demo
        ? await api('/demo/run', {sample_id: d.id, notes: notes || 'Please re-check against the criteria.', attempt: (d.attempt || 1) + 1})
        : await api('/review', {action: 'send_back', id: d.id, notes});
      if (!r.ok) { slot.innerHTML = '<div class="errbox">' + esc(r.data.error) + '</div>'; return; }
      renderResult(r.data); if (!d.demo) await loadCases();
      return;
    }
    const r = await api('/review', {action, id: d.id, notes});
    if (!r.ok) { slot.innerHTML = '<div class="errbox">' + esc(r.data.error) + '</div>'; return; }
    await loadCases();
    slot.innerHTML = '<div class="finalized"><i class="fa-solid fa-clipboard-check"></i> You <b>' +
      (action === 'approve' ? 'APPROVED' : 'DENIED') + '</b> ' + esc(r.data.id) + '. Decision recorded in the audit log.</div>';
  }

  // ── events (delegated) ────────────────────────────────────────────────
  function onActivate(el, fn) {
    el.addEventListener('click', fn);
    el.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fn(e); } });
  }
  document.addEventListener('DOMContentLoaded', async () => {
    $('runbtn').addEventListener('click', submitCase);
    $('loginbtn').addEventListener('click', login);
    $('a_pass').addEventListener('keydown', e => { if (e.key === 'Enter') login(); });
    $('logoutbtn').addEventListener('click', logout);
    onActivate($('samplechips'), e => { const t = e.target.closest('[data-sample]'); if (t) runSample(Number(t.dataset.sample)); });
    onActivate($('queue'), e => { const t = e.target.closest('[data-q]'); if (t) renderResult(QUEUE[Number(t.dataset.q)]); });
    $('result').addEventListener('click', e => { const t = e.target.closest('[data-act]'); if (t) act(t.dataset.act); });
    const r = await api('/samples');
    SAMPLES = r.ok && Array.isArray(r.data) ? r.data : [];
    renderChips();
    renderAuth();
    if (AUTH_ENABLED) await loadMe(); else renderQueue();
  });
})();
