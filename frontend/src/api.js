/* =====================================================================
   API ADAPTER  (§3)

   The ONLY place the frontend talks to the backend. No fetch() calls
   anywhere else in the app.

   It deliberately contains no University formulas. Pools, clearing prices,
   refunds, win probabilities and bid recommendations are all computed
   server-side so there is exactly one authoritative implementation.

   Responsibilities:
     - normalise request payloads to the Pydantic contract
     - client-side pre-validation (cheap, non-authoritative)
     - structured error parsing for every failure mode
     - connection failure / timeout / malformed response handling
     - a monotonic run token so a stale response can never replace a newer one
     - real cancellation via the backend endpoint
   ===================================================================== */
(function (factory) {
  // Resolve the global object robustly. `self` is absent in some non-browser
  // DOM shims, which previously left the module attached to nothing at all.
  var g = (typeof globalThis !== 'undefined' && globalThis)
       || (typeof self !== 'undefined' && self)
       || (typeof window !== 'undefined' && window)
       || this;
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else g.API = factory();
}(function () {

  const API_VERSION = 'v1';
  // Production serves the UI and API from one origin. Only the separate local
  // development server on :5173 needs to jump to the local API on :8000.
  const IS_LOCAL_WEB = typeof location !== 'undefined'
    && (location.hostname === '127.0.0.1' || location.hostname === 'localhost')
    && location.port === '5173';
  const DEFAULT_BASE = IS_LOCAL_WEB ? 'http://127.0.0.1:8000' : '';
  const STORED_BASE = typeof localStorage !== 'undefined' && localStorage.getItem('snu.apiBase');
  const HOST_IS_PUBLIC = typeof location !== 'undefined'
    && location.hostname !== '127.0.0.1' && location.hostname !== 'localhost';
  // Do not carry a developer's loopback override onto a public deployment:
  // there it would point each visitor at their own computer.
  let BASE = (HOST_IS_PUBLIC && /^https?:\/\/(127\.0\.0\.1|localhost)(:|\/|$)/i.test(STORED_BASE || ''))
    ? DEFAULT_BASE : (STORED_BASE || DEFAULT_BASE);
  const TIMEOUT_MS = 20000;

  /* ---------- monotonic run token: kills stale results ---------- */
  let RUN_TOKEN = 0;
  function newRunToken() { return ++RUN_TOKEN; }
  function isCurrent(token) { return token === RUN_TOKEN; }
  function invalidateRuns() { RUN_TOKEN++; }

  /* independent counter for schedule search, so a new search or a running
     simulation never invalidates the other's in-flight run */
  let SCHED_RUN_TOKEN = 0;
  function newScheduleRunToken() { return ++SCHED_RUN_TOKEN; }
  function isScheduleCurrent(token) { return token === SCHED_RUN_TOKEN; }
  function invalidateScheduleRuns() { SCHED_RUN_TOKEN++; }

  /* ---------- structured errors ---------- */
  class ApiError extends Error {
    constructor(kind, message, status, detail) {
      super(message);
      this.name = 'ApiError';
      this.kind = kind;             // offline | timeout | http | malformed | validation | version
      this.status = status || null;
      this.detail = detail || null;
    }
  }

  function humanError(e) {
    if (!(e instanceof ApiError)) return 'Unexpected error: ' + (e && e.message ? e.message : e);
    switch (e.kind) {
      case 'offline':
        return 'The calculation service is unavailable. Your plan is safe, but simulations and bid '
             + 'recommendations cannot run until the service reconnects.';
      case 'timeout':
        return 'The calculation service did not respond in time. Your plan is unchanged. You can retry.';
      case 'validation':
        return 'The plan was rejected before running: ' + (e.detail || e.message);
      case 'version':
        return 'The calculation service is a different version to this page. Reload the page to resync.';
      case 'malformed':
        return 'The calculation service returned a response this page could not read. Nothing was applied.';
      default:
        return 'The calculation service reported an error' + (e.status ? ' (HTTP ' + e.status + ')' : '')
             + (e.detail ? ': ' + e.detail : '') + '. Your plan is unchanged.';
    }
  }

  async function req(path, opts) {
    opts = opts || {};
    const url = BASE + '/api/' + API_VERSION + path;
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort('timeout'), opts.timeout || TIMEOUT_MS);
    let res;
    try {
      res = await fetch(url, {
        method: opts.method || 'GET',
        headers: Object.assign({ 'Content-Type': 'application/json' }, opts.headers || {}),
        body: opts.body ? JSON.stringify(opts.body) : undefined,
        signal: ctl.signal
      });
    } catch (e) {
      clearTimeout(timer);
      if (String(e).includes('timeout') || (e && e.name === 'AbortError')) {
        throw new ApiError('timeout', 'request timed out', null, path);
      }
      throw new ApiError('offline', 'cannot reach the calculation service', null, path);
    }
    clearTimeout(timer);

    let payload = null;
    const text = await res.text();
    if (text) {
      try { payload = JSON.parse(text); }
      catch (e) {
        throw new ApiError('malformed', 'response was not valid JSON', res.status, text.slice(0, 120));
      }
    }
    if (!res.ok) {
      const detail = payload && (payload.detail || payload.message);
      const kind = res.status === 422 ? 'validation' : 'http';
      throw new ApiError(kind, 'backend returned ' + res.status,
                         res.status, typeof detail === 'string' ? detail : JSON.stringify(detail));
    }
    return payload;
  }

  /* ---------- health ---------- */
  let lastHealth = null;
  async function healthCheck() {
    try {
      const r = await fetch(BASE + '/health/ready', { method: 'GET' });
      const j = await r.json();
      lastHealth = { ok: r.ok, ...j, at: Date.now() };
    } catch (e) {
      lastHealth = { ok: false, status: 'unreachable', at: Date.now() };
    }
    return lastHealth;
  }
  function lastKnownHealth() { return lastHealth; }

  /* ---------- deterministic rule calls ---------- */
  function calculateProfileBudget(profile) {
    const body = {
      model_year: profile.model || 'y4',
      semester: Number(profile.semester) || 7,
      rem_me: num(profile.remME), rem_uwe: num(profile.remUWE),
      rem_ccc: num(profile.remCCC), floater: num(profile.floater),
      done_me: num(profile.doneME), done_uwe: num(profile.doneUWE), done_ccc: num(profile.doneCCC)
    };
    return req('/pools', { method: 'POST', body });
  }
  function settleAuction(seats, bids, cap, seed) {
    return req('/settlement', { method: 'POST', body: { seats, bids, cap: cap ?? null, seed: seed || 'demo' } });
  }
  function getRules() { return req('/rules'); }
  function getDataset() { return req('/dataset'); }

  /* ---------- timetable update service ---------- */
  function getTimetableUpdateStatus() { return req('/timetable-updates/status'); }
  function checkTimetableUpdate(force) {
    return req('/timetable-updates/check', { method: 'POST', body: { force: !!force }, timeout: 30000 });
  }
  function getTimetableCandidate() { return req('/timetable-updates/candidate'); }
  function getTimetableDiff() { return req('/timetable-updates/diff'); }
  function applyTimetableUpdate(candidateVersion, candidateChecksum) {
    return req('/timetable-updates/apply', { method: 'POST',
      body: { candidate_version: candidateVersion, candidate_checksum: candidateChecksum }, timeout: 15000 });
  }
  function discardTimetableCandidate(candidateVersion) {
    return req('/timetable-updates/discard', { method: 'POST', body: { candidate_version: candidateVersion } });
  }
  function rollbackTimetableUpdate(targetVersion) {
    return req('/timetable-updates/rollback', { method: 'POST', body: { target_version: targetVersion }, timeout: 15000 });
  }
  function getTimetableUpdateHistory(limit) {
    return req('/timetable-updates/history' + (limit ? '?limit=' + encodeURIComponent(limit) : ''));
  }
  function maxBid(credits) { return req('/max-bid?credits=' + encodeURIComponent(credits)); }

  /* ---------- plan normalisation ---------- */
  function num(v) { const n = Number(v); return Number.isFinite(n) ? n : 0; }

  function buildSimRequest(plan) {
    if (!plan || !Array.isArray(plan.courses) || !plan.courses.length) {
      throw new ApiError('validation', 'no courses selected', null, 'choose at least one course');
    }
    const seen = new Set();
    const courses = plan.courses.map(c => {
      if (seen.has(c.code)) {
        throw new ApiError('validation', 'duplicate course', null, 'duplicate course code ' + c.code);
      }
      seen.add(c.code);
      return {
        code: String(c.code).slice(0, 40),
        title: String(c.title || '').slice(0, 200),
        category: c.category,
        credits: num(c.credits) || 3,
        seats: Math.max(0, Math.round(num(c.seats))),
        priority: c.priority || 'STRONG',
        section_count: Math.max(1, Math.round(num(c.sectionCount) || 1)),
        open_as_uwe: !!c.openAsUwe,
        convenient_slot: !!c.convenientSlot,
        in_specialisation: !!c.inSpecialisation,
        user_popularity: num(c.userPopularity) || 1,
        live_bidders: c.liveBidders == null || c.liveBidders === '' ? null
                      : Math.max(0, Math.round(num(c.liveBidders))),
        live_round: c.liveRound || null,
        live_observed_at: c.liveObservedAt || null
      };
    });
    return {
      courses,
      pools: { ME: Math.round(num(plan.pools.ME)), UWE: Math.round(num(plan.pools.UWE)),
               CCC: Math.round(num(plan.pools.CCC)) },
      trials: Math.min(100000, Math.max(100, Math.round(num(plan.trials) || 8000))),
      seed: Math.max(0, Math.round(num(plan.seed) || 20260802)),
      headline_mode: plan.headlineMode || 'HIGH',
      budget_mode: plan.budgetMode || 'SHARED_LIVE',
      robust_method: plan.robustMethod || 'minimax',
      dispersion: Math.min(0.6, Math.max(0, num(plan.dispersion) ?? 0.18)),
      extra_scenarios: Array.isArray(plan.extraScenarios) ? plan.extraScenarios : ['LOW', 'MODERATE']
    };
  }

  function validatePlan(plan) { return req('/validate-plan', { method: 'POST', body: buildSimRequest(plan) }); }

  /* ---------- simulation job lifecycle ---------- */
  function startSimulation(plan) {
    return req('/simulations', { method: 'POST', body: buildSimRequest(plan) });
  }
  function getSimulationStatus(jobId) { return req('/simulations/' + jobId); }
  function getSimulationResult(jobId) { return req('/simulations/' + jobId + '/result'); }
  function cancelSimulation(jobId) {
    return req('/simulations/' + jobId + '/cancel', { method: 'POST', timeout: 5000 });
  }
  function stressTestPlan(jobId, body) {
    return req('/simulations/' + jobId + '/stress-test', { method: 'POST', body, timeout: 60000 });
  }

  /* ---------- schedule search (backend-authoritative; see app/services/scheduler.py) ---------- */
  function startScheduleSearch(request) {
    return req('/schedules/search', { method: 'POST', body: request });
  }
  function getScheduleStatus(jobId) { return req('/schedules/' + jobId); }
  function getScheduleResults(jobId, limit, offset) {
    const q = '?limit=' + encodeURIComponent(limit || 60) + '&offset=' + encodeURIComponent(offset || 0);
    return req('/schedules/' + jobId + '/results' + q);
  }
  function cancelScheduleSearch(jobId) {
    return req('/schedules/' + jobId + '/cancel', { method: 'POST', timeout: 5000 });
  }
  function explainExclusion(jobId, code) {
    return req('/schedules/' + jobId + '/explain-exclusion', { method: 'POST', body: { code }, timeout: 8000 });
  }

  /* ---------- profile / wishlist (scheduler v2) ---------- */
  function validateProfile(body) { return req('/profiles/validate', { method: 'POST', body }); }
  function validateWishlist(body) { return req('/wishlists/validate', { method: 'POST', body }); }
  function getProgrammes() { return req('/programmes', { timeout: 10000 }); }
  function runDegreeAudit(body) { return req('/degree-audit', { method: 'POST', body, timeout: 15000 }); }
  function parseAdvisementReport(filename, contentBase64) {
    return req('/advisement-report/parse', { method: 'POST',
      body: { filename, content_base64: contentBase64 }, timeout: 30000 });
  }

  function subscribeToScheduleProgress(jobId, onProgress, onDone, onError) {
    let closed = false;
    let es = null;
    let pollTimer = null;

    function stop() {
      closed = true;
      if (es) { try { es.close(); } catch (e) {} es = null; }
      if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
    }

    function startPolling(reason) {
      if (closed) return;
      if (es) { try { es.close(); } catch (e) {} es = null; }
      (function tick() {
        if (closed) return;
        getScheduleStatus(jobId).then(s => {
          if (closed) return;
          onProgress && onProgress(s);
          if (['completed', 'failed', 'cancelled', 'expired'].includes(s.state)) {
            stop(); onDone && onDone(s);
          } else {
            pollTimer = setTimeout(tick, 250);
          }
        }).catch(e => {
          if (closed) return;
          onError && onError(e, { recoverable: true, via: 'poll:' + (reason || '') });
          pollTimer = setTimeout(tick, 1200);
        });
      })();
    }

    if (typeof EventSource === 'function') {
      try {
        es = new EventSource(BASE + '/api/' + API_VERSION + '/schedules/' + jobId + '/events');
        es.addEventListener('progress', ev => {
          if (closed) return;
          try { onProgress && onProgress(JSON.parse(ev.data)); } catch (e) {}
        });
        es.addEventListener('done', ev => {
          if (closed) return;
          let s = null;
          try { s = JSON.parse(ev.data); } catch (e) {}
          stop(); onDone && onDone(s);
        });
        es.onerror = () => { if (!closed) startPolling('sse-error'); };
      } catch (e) { startPolling('sse-unavailable'); }
    } else {
      startPolling('no-eventsource');
    }
    return stop;
  }

  /**
   * Whole schedule-search run, same stale-guard shape as runSimulation but on
   * its own independent run token (see newScheduleRunToken). Resolves with
   * { token, jobId, results } or { stale: true } / { cancelled: true }.
   */
  async function runScheduleSearch(request, hooks) {
    hooks = hooks || {};
    const token = newScheduleRunToken();
    hooks.onPhase && hooks.onPhase('Submitting search');

    const job = await startScheduleSearch(request);
    if (!isScheduleCurrent(token)) {
      cancelScheduleSearch(job.job_id).catch(() => {});
      return { stale: true, token };
    }
    hooks.onJob && hooks.onJob(job);
    hooks.onPhase && hooks.onPhase(job.cache_hit ? 'Loading cached result' : 'Searching');

    const finalState = await new Promise((resolve, reject) => {
      const unsub = subscribeToScheduleProgress(
        job.job_id,
        s => { if (isScheduleCurrent(token)) hooks.onProgress && hooks.onProgress(s); },
        s => resolve(s),
        (e, meta) => { if (!meta || !meta.recoverable) { unsub(); reject(e); }
                       else hooks.onWarning && hooks.onWarning(e); }
      );
      hooks._unsub = unsub;
    });

    if (!isScheduleCurrent(token)) return { stale: true, token, jobId: job.job_id };
    if (finalState && finalState.state === 'cancelled') {
      return { cancelled: true, token, jobId: job.job_id };
    }
    if (finalState && finalState.state === 'failed') {
      throw new ApiError('http', 'schedule search failed', 500,
                         finalState.error ? String(finalState.error).slice(0, 300) : null);
    }
    hooks.onPhase && hooks.onPhase('Loading results');
    const first = await getScheduleResults(job.job_id, 60, 0);
    if (!isScheduleCurrent(token)) return { stale: true, token, jobId: job.job_id };
    return { token, jobId: job.job_id, results: first };
  }

  /**
   * Subscribe to progress. Prefers SSE; falls back to polling if EventSource
   * is unavailable or the stream dies. Returns an unsubscribe function.
   */
  function subscribeToSimulationProgress(jobId, onProgress, onDone, onError) {
    let closed = false;
    let es = null;
    let pollTimer = null;

    function stop() {
      closed = true;
      if (es) { try { es.close(); } catch (e) {} es = null; }
      if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
    }

    function startPolling(reason) {
      if (closed) return;
      if (es) { try { es.close(); } catch (e) {} es = null; }
      (function tick() {
        if (closed) return;
        getSimulationStatus(jobId).then(s => {
          if (closed) return;
          onProgress && onProgress(s);
          if (['completed', 'failed', 'cancelled', 'expired'].includes(s.state)) {
            stop(); onDone && onDone(s);
          } else {
            pollTimer = setTimeout(tick, 250);
          }
        }).catch(e => {
          if (closed) return;
          // a dropped connection must not strand the UI: keep retrying slowly
          onError && onError(e, { recoverable: true, via: 'poll:' + (reason || '') });
          pollTimer = setTimeout(tick, 1200);
        });
      })();
    }

    if (typeof EventSource === 'function') {
      try {
        es = new EventSource(BASE + '/api/' + API_VERSION + '/simulations/' + jobId + '/events');
        es.addEventListener('progress', ev => {
          if (closed) return;
          try { onProgress && onProgress(JSON.parse(ev.data)); } catch (e) {}
        });
        es.addEventListener('done', ev => {
          if (closed) return;
          let s = null;
          try { s = JSON.parse(ev.data); } catch (e) {}
          stop(); onDone && onDone(s);
        });
        es.onerror = () => { if (!closed) startPolling('sse-error'); };
      } catch (e) { startPolling('sse-unavailable'); }
    } else {
      startPolling('no-eventsource');
    }
    return stop;
  }

  /**
   * Whole run, guarded against stale results. Resolves with
   * { token, result } and rejects with ApiError. If the token is no longer
   * current when the result lands, it resolves with { stale: true }.
   */
  async function runSimulation(plan, hooks) {
    hooks = hooks || {};
    const token = newRunToken();
    hooks.onPhase && hooks.onPhase('Validating plan');

    const v = await validatePlan(plan);
    if (!isCurrent(token)) return { stale: true, token };
    if (!v.ok) {
      throw new ApiError('validation', 'plan rejected', 422, (v.blocking || []).join('; '));
    }
    hooks.onValidated && hooks.onValidated(v);

    hooks.onPhase && hooks.onPhase('Submitting job');
    const job = await startSimulation(plan);
    if (!isCurrent(token)) {
      // a newer run started while we were submitting: cancel this one server-side
      cancelSimulation(job.job_id).catch(() => {});
      return { stale: true, token };
    }
    hooks.onJob && hooks.onJob(job);
    hooks.onPhase && hooks.onPhase(job.cache_hit ? 'Loading cached result' : 'Starting worker');

    const finalState = await new Promise((resolve, reject) => {
      const unsub = subscribeToSimulationProgress(
        job.job_id,
        s => { if (isCurrent(token)) hooks.onProgress && hooks.onProgress(s); },
        s => resolve(s),
        (e, meta) => { if (!meta || !meta.recoverable) { unsub(); reject(e); }
                       else hooks.onWarning && hooks.onWarning(e); }
      );
      hooks._unsub = unsub;
    });

    if (!isCurrent(token)) return { stale: true, token, jobId: job.job_id };
    if (finalState && finalState.state === 'cancelled') {
      return { cancelled: true, token, jobId: job.job_id };
    }
    if (finalState && finalState.state === 'failed') {
      throw new ApiError('http', 'simulation failed', 500,
                         finalState.error ? String(finalState.error).slice(0, 300) : null);
    }
    hooks.onPhase && hooks.onPhase('Preparing results');
    const result = await getSimulationResult(job.job_id);
    if (!isCurrent(token)) return { stale: true, token, jobId: job.job_id };
    return { token, jobId: job.job_id, result };
  }

  function setBase(url) {
    BASE = url || '';
    try { localStorage.setItem('snu.apiBase', BASE); } catch (e) {}
  }
  function getBase() { return BASE; }

  return {
    API_VERSION, ApiError, humanError,
    setBase, getBase,
    healthCheck, lastKnownHealth,
    calculateProfileBudget, settleAuction, getRules, getDataset, maxBid,
    getTimetableUpdateStatus, checkTimetableUpdate, getTimetableCandidate, getTimetableDiff,
    applyTimetableUpdate, discardTimetableCandidate, rollbackTimetableUpdate, getTimetableUpdateHistory,
    buildSimRequest, validatePlan,
    startSimulation, getSimulationStatus, getSimulationResult,
    cancelSimulation, stressTestPlan, subscribeToSimulationProgress,
    runSimulation, newRunToken, isCurrent, invalidateRuns,
    startScheduleSearch, getScheduleStatus, getScheduleResults, cancelScheduleSearch,
    subscribeToScheduleProgress, runScheduleSearch, newScheduleRunToken, isScheduleCurrent,
    invalidateScheduleRuns, explainExclusion, validateProfile, validateWishlist,
    getProgrammes, runDegreeAudit, parseAdvisementReport
  };
}));
