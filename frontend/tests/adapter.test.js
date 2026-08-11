/* §14 API adapter tests — run in Node with a stubbed global fetch.
   Real-browser behaviour is covered separately by the Playwright suite. */
const path = require('path');
let pass = 0, fail = 0;
const ck = (n, c, x) => { console.log((c ? '  PASS  ' : '  FAIL  ') + n + (x ? '   [' + x + ']' : '')); c ? pass++ : fail++; };

// minimal localStorage so the adapter can persist its base URL
const store = {};
globalThis.localStorage = { getItem: k => store[k] ?? null, setItem: (k, v) => store[k] = String(v),
                            removeItem: k => delete store[k] };

function freshApi(fetchImpl, opts) {
  delete require.cache[require.resolve('../src/api.js')];
  globalThis.fetch = fetchImpl;
  if (opts && opts.noEventSource) delete globalThis.EventSource;
  else globalThis.EventSource = undefined;
  return require('../src/api.js');
}
const okJson = body => async () => ({ ok: true, status: 200, text: async () => JSON.stringify(body) });

(async () => {
console.log('=== §14 API ADAPTER ===');

{
  const API = freshApi(okJson({ ME: 297, UWE: 215, CCC: 492, detail: {}, rule_id: 'POOL.Y4', rule_version: 'x' }));
  const r = await API.calculateProfileBudget({ model: 'y4', semester: 7, remME: 9, remUWE: 4, remCCC: 11, floater: 6 });
  ck('successful request returns parsed body', r.ME === 297 && r.CCC === 492);
}
{
  const API = freshApi(async () => ({ ok: false, status: 500, text: async () => JSON.stringify({ detail: 'boom' }) }));
  try { await API.getRules(); ck('HTTP 500 raises', false); }
  catch (e) { ck('HTTP 500 -> kind=http with detail', e.kind === 'http' && e.status === 500 && e.detail === 'boom'); }
}
{
  const API = freshApi(async () => ({ ok: false, status: 422, text: async () => JSON.stringify({ detail: 'bad field' }) }));
  try { await API.getRules(); ck('HTTP 422 raises', false); }
  catch (e) { ck('HTTP 422 -> kind=validation', e.kind === 'validation', e.kind); }
}
{
  const API = freshApi(async () => { throw new Error('ECONNREFUSED'); });
  try { await API.getRules(); ck('connection failure raises', false); }
  catch (e) {
    ck('connection failure -> kind=offline', e.kind === 'offline', e.kind);
    ck('offline message is student-readable', /calculation service is unavailable/.test(API.humanError(e)));
    ck('offline message promises the plan is safe', /plan is safe/.test(API.humanError(e)));
  }
}
{
  const API = freshApi(async () => { const e = new Error('aborted'); e.name = 'AbortError'; throw e; });
  try { await API.getRules(); ck('timeout raises', false); }
  catch (e) { ck('timeout -> kind=timeout', e.kind === 'timeout', e.kind); }
}
{
  const API = freshApi(async () => ({ ok: true, status: 200, text: async () => 'not json{' }));
  try { await API.getRules(); ck('malformed raises', false); }
  catch (e) { ck('malformed JSON -> kind=malformed', e.kind === 'malformed', e.kind); }
}
{
  const API = freshApi(okJson({}));
  try { API.buildSimRequest({ courses: [], pools: { ME: 1, UWE: 1, CCC: 1 } }); ck('empty plan rejected', false); }
  catch (e) { ck('empty course list rejected before any request', e.kind === 'validation'); }
  try {
    API.buildSimRequest({ courses: [{ code: 'A', category: 'ME', credits: 3, seats: 1 },
                                    { code: 'A', category: 'ME', credits: 3, seats: 1 }],
                          pools: { ME: 1, UWE: 1, CCC: 1 } });
    ck('duplicate codes rejected', false);
  } catch (e) { ck('duplicate course codes rejected client-side', /duplicate/.test(e.detail || e.message)); }
}
{
  const API = freshApi(okJson({}));
  const req = API.buildSimRequest({
    courses: [{ code: 'A', category: 'ME', credits: 3, seats: 120, liveBidders: '' }],
    pools: { ME: 297, UWE: 215, CCC: 492 }, trials: 99999999, dispersion: 9, seed: -5
  });
  ck('trials clamped to contract maximum', req.trials === 100000, String(req.trials));
  ck('dispersion clamped', req.dispersion === 0.6, String(req.dispersion));
  ck('negative seed normalised', req.seed >= 0);
  ck('empty liveBidders becomes null, not 0', req.courses[0].live_bidders === null);
}
{
  let requestBody = null, requestUrl = '';
  const API = freshApi(async (url, opts) => {
    requestUrl = url;
    requestBody = JSON.parse(opts.body);
    return { ok: true, status: 200, text: async () => JSON.stringify({ strategy_version: 'allocation-v1' }) };
  });
  const plan = {
    courses: [{ code: 'CSD361', category: 'ME', credits: 4, seats: 80 }],
    pools: { ME: 297, UWE: 215, CCC: 492 }, reservePercent: 25,
    posture: 'diversified', semester: 7
  };
  const built = API.buildBidStrategyRequest(plan);
  ck('strategy adapter keeps explicit reserve and posture',
     built.reserve_percent === 25 && built.posture === 'diversified');
  ck('strategy request contains no simulation controls',
     built.trials == null && built.seed == null && built.dispersion == null);
  await API.getBidStrategy(plan);
  ck('strategy uses the dedicated endpoint', requestUrl.endsWith('/api/v1/bid-strategy'), requestUrl);
  ck('strategy POST sends the deterministic contract',
     requestBody.reserve_percent === 25 && requestBody.courses[0].code === 'CSD361');
}
{
  let seen = '';
  const API = freshApi(async (url) => { seen = url; return { ok: true, status: 200, text: async () => '{}' }; });
  await API.getRules();
  ck('every request carries the API version', seen.includes('/api/v1/'), seen);
}
{
  // A public deployment is one same-origin service.  A stale override from an
  // older split deployment must not make only this browser appear offline.
  store['snu.apiBase'] = 'https://retired-api.example.invalid';
  globalThis.location = { hostname: 'snu-scheduler.onrender.com', port: '' };
  let seen = '';
  const API = freshApi(async (url) => {
    seen = url;
    return { ok: true, status: 200, text: async () => '{}' };
  });
  await API.getRules();
  ck('public deployment ignores a stale saved API origin', seen === '/api/v1/rules', seen);
  ck('public deployment removes the stale API override', store['snu.apiBase'] == null);
  API.setBase('https://another-retired-api.example.invalid');
  ck('public deployment cannot reintroduce a cross-origin API override',
     API.getBase() === '' && store['snu.apiBase'] == null, API.getBase());
  delete globalThis.location;
}
{
  const API = freshApi(okJson({}));
  const t1 = API.newRunToken(), t2 = API.newRunToken();
  ck('a newer token invalidates the older one', !API.isCurrent(t1) && API.isCurrent(t2));
  API.invalidateRuns();
  ck('invalidateRuns kills the current token too', !API.isCurrent(t2));
}
{
  let cancelled = null;
  let API;
  API = freshApi(async (url) => {
    if (url.includes('/validate-plan')) return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, warnings: [] }) };
    if (url.includes('/cancel')) { cancelled = url; return { ok: true, status: 200, text: async () => '{}' }; }
    if (url.endsWith('/simulations')) {
      API.newRunToken();                     // a newer run starts mid-flight
      return { ok: true, status: 200, text: async () => JSON.stringify({ job_id: 'J1', state: 'queued' }) };
    }
    return { ok: true, status: 200, text: async () => '{}' };
  });
  const out = await API.runSimulation({ courses: [{ code: 'A', category: 'ME', credits: 3, seats: 10 }],
                                        pools: { ME: 1, UWE: 1, CCC: 1 } }, {});
  ck('a superseded run resolves as stale, not as a result', out.stale === true);
  ck('the superseded job is cancelled server-side', !!cancelled, String(cancelled));
}
{
  const API = freshApi(async () => { throw new Error('down'); });
  const h = await API.healthCheck();
  ck('healthCheck resolves rather than throwing when offline', h.ok === false && h.status === 'unreachable');
  ck('lastKnownHealth is retained for the UI', API.lastKnownHealth().ok === false);
}
{
  let polls = 0;
  const API = freshApi(async (url) => {
    if (url.includes('/simulations/J1')) {
      polls++;
      const state = polls < 3 ? 'running' : 'completed';
      return { ok: true, status: 200, text: async () => JSON.stringify({ job_id: 'J1', state,
        progress: { percent: polls * 30, phase: 'p', scenarios_done: polls, scenarios_total: 3,
                    courses_done: 0, courses_total: 1, trials_done: 0, trials_total: 1 } }) };
    }
    return { ok: true, status: 200, text: async () => '{}' };
  }, { noEventSource: true });
  const done = await new Promise(res => API.subscribeToSimulationProgress('J1', () => {}, s => res(s), () => {}));
  ck('falls back to polling with no EventSource', done.state === 'completed' && polls >= 3, 'polls=' + polls);
}
{
  // a dropped connection must not strand the UI
  let calls = 0;
  const API = freshApi(async (url) => {
    if (url.includes('/simulations/J2')) {
      calls++;
      if (calls <= 2) throw new Error('network blip');
      return { ok: true, status: 200, text: async () => JSON.stringify({ job_id: 'J2', state: 'completed',
        progress: { percent: 100, phase: 'done', scenarios_done: 3, scenarios_total: 3,
                    courses_done: 1, courses_total: 1, trials_done: 1, trials_total: 1 } }) };
    }
    return { ok: true, status: 200, text: async () => '{}' };
  }, { noEventSource: true });
  let warned = 0;
  const done = await new Promise(res =>
    API.subscribeToSimulationProgress('J2', () => {}, s => res(s), () => warned++));
  ck('recovers from a dropped connection and still completes', done.state === 'completed');
  ck('surfaces recoverable warnings while retrying', warned >= 1, 'warnings=' + warned);
}

console.log('\n=== §16 SCHEDULE SEARCH API ADAPTER ===');
{
  // full happy path: submit -> poll (no EventSource) -> fetch first page of results
  let polls = 0;
  const API = freshApi(async (url, opts) => {
    if (url.includes('/schedules/search') && opts && opts.method === 'POST') {
      return { ok: true, status: 202, text: async () => JSON.stringify({ job_id: 'S1', state: 'starting', cache_hit: false }) };
    }
    if (url.includes('/schedules/S1/results')) {
      return { ok: true, status: 200, text: async () => JSON.stringify({
        schedules: [{ assign: { A: 0 }, stats: { gap: 0, days: 1 } }], total_found: 1,
        truncated: false, nodes: 42, sort: 'compact', cache_hit: false }) };
    }
    if (url.includes('/schedules/S1')) {
      polls++;
      const state = polls < 2 ? 'running' : 'completed';
      return { ok: true, status: 200, text: async () => JSON.stringify({ job_id: 'S1', state,
        progress: { nodes_done: polls * 1000, nodes_total: 2000, percent: polls * 50, phase: 'searching' } }) };
    }
    return { ok: true, status: 200, text: async () => '{}' };
  }, { noEventSource: true });
  const out = await API.runScheduleSearch({ shortlist: ['A'], fixed: [], max_nodes: 2000, max_results: 60, sort: 'compact' }, {});
  ck('schedule search resolves with the first page of results', out.results && out.results.total_found === 1);
  ck('falls back to polling with no EventSource', polls >= 2, 'polls=' + polls);
}
{
  // a superseded search must be cancelled server-side, not silently applied
  let cancelledJobId = null;
  let API;
  API = freshApi(async (url, opts) => {
    if (url.includes('/schedules/search') && opts && opts.method === 'POST') {
      API.newScheduleRunToken();               // a newer search starts mid-flight
      return { ok: true, status: 202, text: async () => JSON.stringify({ job_id: 'S2', state: 'starting', cache_hit: false }) };
    }
    if (url.includes('/schedules/S2/cancel')) { cancelledJobId = 'S2'; return { ok: true, status: 200, text: async () => '{}' }; }
    return { ok: true, status: 200, text: async () => '{}' };
  });
  const out = await API.runScheduleSearch({ shortlist: ['A'], fixed: [], max_nodes: 2000, max_results: 60, sort: 'compact' }, {});
  ck('a superseded schedule search resolves as stale', out.stale === true);
  ck('the superseded schedule job is cancelled server-side', cancelledJobId === 'S2', String(cancelledJobId));
}
{
  const API = freshApi(async (url, opts) => {
    if (url.includes('/schedules/search') && opts && opts.method === 'POST') {
      return { ok: true, status: 202, text: async () => JSON.stringify({ job_id: 'S3', state: 'starting', cache_hit: false }) };
    }
    if (url.includes('/schedules/S3')) {
      return { ok: true, status: 200, text: async () => JSON.stringify({ job_id: 'S3', state: 'cancelled' }) };
    }
    return { ok: true, status: 200, text: async () => '{}' };
  }, { noEventSource: true });
  const out = await API.runScheduleSearch({ shortlist: ['A'], fixed: [], max_nodes: 2000, max_results: 60, sort: 'compact' }, {});
  ck('a cancelled schedule search is reported distinctly, not as empty results', out.cancelled === true);
}
{
  const API = freshApi(okJson({}));
  const st1 = API.newScheduleRunToken();
  API.newRunToken();     // bump the unrelated simulation counter
  ck('bumping the simulation run token leaves an in-flight schedule token valid', API.isScheduleCurrent(st1));
  const st2 = API.newScheduleRunToken();
  ck('a newer schedule token invalidates the older one', !API.isScheduleCurrent(st1) && API.isScheduleCurrent(st2));
}

console.log('\n=== §21 WISHLIST/PROFILE API ADAPTER ===');
{
  const API = freshApi(okJson({ ceiling_mode: 'standard', active_ceiling: 25, summary: 'x' }));
  const r = await API.validateProfile({ credit_policy: { fixed_credits: 15, personal_target: 22, min_credits: 18 } });
  ck('validateProfile hits /profiles/validate and returns the body', r.active_ceiling === 25);
}
{
  const API = freshApi(okJson({ count: 2, num_must_have: 1 }));
  const r = await API.validateWishlist({ items: [{ code: 'A' }, { code: 'B' }], fixed_credits: 0 });
  ck('validateWishlist hits /wishlists/validate and returns the body', r.count === 2);
}
{
  const API = freshApi(okJson({ code: 'B', blocker: 'credit_ceiling', reason: 'x', relaxation: 'y' }));
  const r = await API.explainExclusion('JOB1', 'B');
  ck('explainExclusion returns the blocker payload', r.blocker === 'credit_ceiling');
}
{
  let seenBody = null;
  const API = freshApi(async (url, opts) => {
    seenBody = opts && opts.body;
    return { ok: true, status: 200, text: async () => JSON.stringify({ code: 'B', blocker: null }) };
  });
  await API.explainExclusion('JOB1', 'B');
  const parsedBody = seenBody ? JSON.parse(seenBody) : null;
  ck('explainExclusion POSTs the course code in the body', parsedBody && parsedBody.code === 'B', seenBody);
}
{
  // wishlist-mode schedule search reuses runScheduleSearch unchanged - just a
  // request shape with wishlist/choice_groups/credit_target instead of a shortlist
  let sentReq = null;
  const API = freshApi(async (url, opts) => {
    if (url.includes('/schedules/search') && opts && opts.method === 'POST') {
      sentReq = JSON.parse(opts.body);
      return { ok: true, status: 202, text: async () => JSON.stringify({ job_id: 'W1', state: 'starting', cache_hit: false }) };
    }
    if (url.includes('/schedules/W1/results')) {
      return { ok: true, status: 200, text: async () => JSON.stringify({
        schedules: [{ assign: { A: 0 }, stats: { gap: 0, days: 1 } }], total_found: 1,
        mode: 'optimized', included: ['A'], excluded: ['B'], total_credits: 10,
        truncated: false, nodes: 0, sort: 'compact', cache_hit: false }) };
    }
    if (url.includes('/schedules/W1')) {
      return { ok: true, status: 200, text: async () => JSON.stringify({ job_id: 'W1', state: 'completed' }) };
    }
    return { ok: true, status: 200, text: async () => '{}' };
  }, { noEventSource: true });
  const out = await API.runScheduleSearch({
    shortlist: [], fixed: [], wishlist: [{ code: 'A', intent: 'must_have' }, { code: 'B', intent: 'strong' }],
    credit_min: 0, credit_target: 10, credit_max: 25, max_nodes: 2000, max_results: 60, sort: 'compact',
  }, {});
  ck('wishlist-mode request reaches the backend with its wishlist fields intact',
     sentReq && sentReq.wishlist && sentReq.wishlist.length === 2, JSON.stringify(sentReq));
  ck('wishlist-mode result carries mode=optimized and included/excluded', out.results.mode === 'optimized'
     && out.results.included[0] === 'A' && out.results.excluded[0] === 'B');
}

{
  const API = freshApi(okJson({ active_version: 'monsoon-2026-netlify-revision-2026-08-04', course_count: 326 }));
  const r = await API.getDataset();
  ck('getDataset hits /dataset and returns the body', r.course_count === 326);
}

console.log('\n=== §22 TIMETABLE UPDATE SERVICE API ADAPTER ===');
{
  const API = freshApi(okJson({ state: 'update_available', update_available: true }));
  const r = await API.getTimetableUpdateStatus();
  ck('getTimetableUpdateStatus hits /timetable-updates/status', r.state === 'update_available');
}
{
  let seenBody = null;
  const API = freshApi(async (url, opts) => { seenBody = opts.body; return { ok: true, status: 200, text: async () => '{"state":"no_dataset_change"}' }; });
  await API.checkTimetableUpdate(true);
  ck('checkTimetableUpdate POSTs force=true', JSON.parse(seenBody).force === true, seenBody);
}
{
  const API = freshApi(okJson({ version_id: 'v2', dataset_checksum: 'abc' }));
  const r = await API.getTimetableCandidate();
  ck('getTimetableCandidate returns the staged candidate', r.version_id === 'v2');
}
{
  let seenBody = null;
  const API = freshApi(async (url, opts) => { seenBody = opts.body; return { ok: true, status: 200, text: async () => '{}' }; });
  await API.applyTimetableUpdate('v2', 'abc');
  const parsed = JSON.parse(seenBody);
  ck('applyTimetableUpdate sends candidate_version and candidate_checksum',
     parsed.candidate_version === 'v2' && parsed.candidate_checksum === 'abc', seenBody);
}
{
  const API = freshApi(okJson({ discarded: 'v2' }));
  const r = await API.discardTimetableCandidate('v2');
  ck('discardTimetableCandidate returns the ack', r.discarded === 'v2');
}
{
  const API = freshApi(okJson({ result: { version_id: 'v1' } }));
  const r = await API.rollbackTimetableUpdate('v1');
  ck('rollbackTimetableUpdate returns the result', r.result.version_id === 'v1');
}

console.log('\n=== §23 COURSE OUTLINE API ADAPTER ===');
{
  const API = freshApi(okJson({ codes: ['AMP1001', 'CSD358'] }));
  const r = await API.getCourseOutlineCodes();
  ck('getCourseOutlineCodes hits /course-outlines', r.codes.includes('CSD358'), JSON.stringify(r.codes));
}
{
  let seenBody = null;
  const API = freshApi(async (url, opts) => {
    seenBody = opts.body;
    return { ok: true, status: 200, text: async () => JSON.stringify({ code: 'AMP1001', title_from_outline: 'Critical Art History' }) };
  });
  const r = await API.getCourseOutline('ART202/AMP1001');
  ck('getCourseOutline POSTs the exact code given, slash and all',
     JSON.parse(seenBody).code === 'ART202/AMP1001', seenBody);
  ck('getCourseOutline returns the outline body', r.code === 'AMP1001');
}
{
  const API = freshApi(async () => ({ ok: false, status: 404, text: async () => JSON.stringify({ detail: 'no outline on file' }) }));
  try { await API.getCourseOutline('NOT-A-REAL-COURSE'); ck('unknown code raises', false); }
  catch (e) { ck('unknown code -> kind=http, status=404', e.kind === 'http' && e.status === 404); }
}

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
})();
