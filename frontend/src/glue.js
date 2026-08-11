/* =====================================================================
   BACKEND-DRIVEN GLUE  (§4, §5, §6, §8, §9, §12)

   This file replaces the old in-browser simulation engine. Everything
   authoritative is now an API call:

     pools          -> POST /api/v1/pools
     bid strategy   -> POST /api/v1/bid-strategy
     settlement     -> POST /api/v1/settlement
     rules          -> GET  /api/v1/rules

   The browser keeps only: form state, course selection, schedule display,
   clash previews, formatting, progress rendering and export controls.
   ===================================================================== */

let LIVE = {}, PRIO = {}, USERPOP = {};
let RULES_CACHE = null;   // declared here to avoid a temporal-dead-zone error at boot
let RESULT = null;           // last completed backend result
let BACKEND_OK = null;       // null = unknown, true/false = last health probe
let HEALTH_TIMER = null;
let HEALTH_PROBE_SEQ = 0;    // a late older failure must not overwrite a newer success
let RUNNING = false;
let DATASET_INFO = null;     // active institutional timetable dataset identity (see /api/v1/dataset)
let OUTLINE_CODES = new Set(); // course codes with a real Academic Office outline on file
let OUTLINE_CACHE = {};        // code -> fetched outline body, so re-opening the same course is instant

// The outline set is keyed by the Office's own per-department filename code
// (e.g. "AMP1001"), while the catalogue often joins cross-listed sections with
// "/" (e.g. "ART202/AMP1001") - mirrors OutlineCatalog.get()'s own component
// match on the backend, so a picker row for either code finds the same file.
function hasOutline(code) {
  if (OUTLINE_CODES.has(code)) return true;
  const parts = code.split('/').map(p => p.trim()).filter(Boolean);
  return parts.some(p => OUTLINE_CODES.has(p));
}

function curPosture() { const e = $('bidPosture'); return e ? e.value : 'balanced'; }
function curReserve() { const e = $('bidReserve'); return e ? Math.max(0, Math.min(90, Math.round(+e.value || 0))) : 20; }
function prioOf(code) { return PRIO[code] || 'STRONG'; }

/* ---------------- plan assembly for the API ---------------- */
function buildPlan() {
  const courses = Object.keys(PICK).map(code => {
    const c = BY[code];
    const pk = c.pk && c.pk[PICK[code].pkg];
    const conv = !!(pk && pk.m.some(m => m[0] <= 4 && m[1] >= 570 && m[1] <= 900));
    return {
      code, title: c.title, category: c.cat, credits: c.cr, seats: c.seats,
      priority: prioOf(code), sectionCount: (c.pk || []).length || 1,
      openAsUwe: c.cat === 'UWE', convenientSlot: conv,
      inSpecialisation: false,
      userPopularity: USERPOP[code] != null ? USERPOP[code] : 1,
      liveBidders: LIVE[code] ? LIVE[code].bidders : null,
      liveRound: LIVE[code] ? LIVE[code].round : null,
      liveObservedAt: LIVE[code] ? LIVE[code].at : null
    };
  });
  return {
    courses,
    pools: { ME: BUD.ME, UWE: BUD.UWE, CCC: BUD.CCC },
    reservePercent: curReserve(), posture: curPosture(),
    semester: $('sem') ? +$('sem').value : 7
  };
}
/* ---------------- §9 backend availability ---------------- */
function setBackendState(ok, info) {
  const changed = BACKEND_OK !== ok;
  BACKEND_OK = ok;
  const bar = $('backendBar');
  if (bar) {
    if (ok) {
      bar.className = 'flag f-ok';
      bar.innerHTML = `<b>Calculation service connected.</b> <span class="tiny mono">`
        + `rules ${esc((info && info.rule_version) || '?')} · strategy ${esc((info && info.strategy_version) || 'allocation-v1')}`
        + ` · ${esc(API.getBase() || 'same origin')}</span>`;
    } else {
      bar.className = 'flag f-bad';
      // The most common real-world cause of this banner is not a bug or a
      // network problem on the visitor's end - it's this app's own free-tier
      // hosting putting the backend to sleep after ~15 minutes of no traffic,
      // then taking up to a minute to wake back up on the next request. That
      // is unrelated to which network/device is asking; whoever happens to be
      // first back after a quiet spell pays the wake-up cost. Said explicitly
      // here so it doesn't read as a mysterious, possibly network-specific
      // failure - without softening or removing the required phrases below
      // (e2e.test.js's §9 checks these exact strings on the very first probe
      // failure, simulating a hard network abort, so this can only ADD
      // context, never replace the direct language).
      // 'blocked' means a response came back but wasn't the JSON this route
      // always returns - almost never this app's backend being down, since
      // the origin server has no other way to answer this route. It means
      // something between this browser and the origin substituted its own
      // page: a Cloudflare bot/security challenge, a captive Wi-Fi login
      // portal, or a corporate/campus proxy. That needs different advice
      // than "our server is asleep", so it gets its own sentence instead of
      // silently folding into the generic wording below.
      const extra = (info && info.status === 'blocked')
        ? ` Your browser got a response, but not from this app - something on your network (a captive Wi-Fi `
          + `login page, a security filter, or a browser extension) intercepted the request. Try a different `
          + `network or browser, or check for a Wi-Fi login page that needs accepting first.`
        : ` This is usually the free hosting tier waking back up after being idle, not a problem with your `
          + `network - it is retrying automatically and should reconnect within about a minute.`;
      bar.innerHTML = `<b>The calculation service is unavailable.</b> Your plan is safe, but the schedule and
        bid recommendations cannot run until the service reconnects.${extra}
        <button class="btn2 sm" onclick="void retryBackend()" style="margin-left:8px">Retry connection</button>
        <span class="tiny mut" id="healthNote" style="margin-left:8px"></span>`;
    }
  }
  ['runBtn'].forEach(id => {
    const b = $(id);
    if (b) {
      b.disabled = !ok || RUNNING;
      b.title = ok ? '' : 'Unavailable while the calculation service is disconnected';
    }
  });
  if (changed && ok) { void refreshPoolsFromBackend(); }
}

async function probeHealth() {
  const seq = ++HEALTH_PROBE_SEQ;
  const h = await API.healthCheck();
  if (seq !== HEALTH_PROBE_SEQ) return h;
  setBackendState(!!h.ok, h);
  return h;
}
async function retryBackend() {
  const n = $('healthNote'); if (n) n.textContent = 'checking…';
  const h = await probeHealth();
  if (!h.ok && n) n.textContent = 'still unreachable at ' + new Date().toLocaleTimeString();
}
function startHealthLoop() {
  if (HEALTH_TIMER) clearTimeout(HEALTH_TIMER);
  // Self-rescheduling rather than a fixed setInterval so the cadence can
  // shorten while the backend is down: this app's free-tier hosting can
  // take up to about a minute to wake from an idle-triggered sleep, and a
  // fixed 15s interval means whoever hits it first after a quiet spell
  // could sit on the "unavailable" banner for a full 15s between checks
  // before even the first automatic retry lands. Polling every 4s while
  // down (back to the normal 15s once healthy) gets a woken-up backend
  // detected roughly 3-4x sooner, without spamming a healthy one any harder
  // than before.
  //
  // The try/finally is load-bearing, not defensive noise. A self-rescheduling
  // timeout only survives as long as the reschedule line is actually reached,
  // so ANY rejection out of probeHealth() (a throw inside healthCheck, or DOM
  // work in setBackendState) permanently kills the retry loop - the banner
  // then sits on "unavailable" forever and never recovers on its own, which
  // is precisely the "it still doesn't work for her" symptom. The setInterval
  // this replaced was immune to that, so the conversion to setTimeout is what
  // introduced the exposure; reschedule in finally so it cannot regress.
  const tick = async () => {
    try {
      if (!RUNNING) await probeHealth();
    } catch (e) {
      setBackendState(false, { status: 'unreachable' });
    } finally {
      HEALTH_TIMER = setTimeout(tick, BACKEND_OK ? 15000 : 4000);
    }
  };
  HEALTH_TIMER = setTimeout(tick, 15000);
}

/* ---------------- pools now come from the backend ---------------- */
async function refreshPoolsFromBackend() {
  if (!BACKEND_OK) return;
  if (!$('model') || !$('model').value) return;   // no year picked yet — recalc() already shows the prompt
  try {
    const p = await API.calculateProfileBudget({
      model: $('model').value,
      semester: $('sem') ? +$('sem').value : 7,
      remME: +$('remME').value, remUWE: +$('remUWE').value,
      remCCC: +$('remCCC').value, floater: +$('remFL').value,
      doneME: +($('doneME') ? $('doneME').value : 0),
      doneUWE: +($('doneUWE') ? $('doneUWE').value : 0),
      doneCCC: +($('doneCCC') ? $('doneCCC').value : 0)
    });
    BUD = { ME: p.ME, UWE: p.UWE, CCC: p.CCC };
    if ($('bME')) $('bME').textContent = p.ME;
    if ($('bUWE')) $('bUWE').textContent = p.UWE;
    if ($('bCCC')) $('bCCC').textContent = p.CCC;
    if ($('formula')) {
      $('formula').innerHTML = Object.values(p.detail).map(esc).join(' &middot; ')
        + ` <span class="pill p-uwe">from backend · ${esc(p.rule_id)} · ${esc(p.rule_version)}</span>`;
    }
    // No per-course bid cap exists (AUC.MAX_BID, rectified 2026-08-05) - a bid may run up to
    // the whole category pool on one course, so "courses at a fixed cap" is no longer a
    // meaningful figure. Report the pool itself instead.
    if ($('fME')) $('fME').textContent = `${p.ME} points available — no per-course cap`;
    if ($('fUWE')) $('fUWE').textContent = `${p.UWE} points available — no per-course cap`;
    if ($('fCCC')) $('fCCC').textContent = `${p.CCC} points available — no per-course cap`;
    budFlags(); posFlags(); renderChosen();
  } catch (e) {
    if (e instanceof API.ApiError && (e.kind === 'offline' || e.kind === 'timeout')) setBackendState(false);
  }
}

/* ---------------- §4/§6 run lifecycle ---------------- */
function setRunning(on) {
  RUNNING = on;
  const run = $('runBtn'), cancel = $('cancelBtn');
  if (run) { run.disabled = on || !BACKEND_OK; run.textContent = on ? 'Planning…' : 'Build strategic preference plan'; }
  if (cancel) cancel.disabled = !on;
}

async function runOpt() {
  if (RUNNING) return;                                   // no duplicate submission
  if (!$('model') || !$('model').value) {
    $('bidOut').innerHTML = '<div class="card"><div class="note">Select your year (Profile &amp; budget tab) first — '
      + 'your pools and this plan both depend on which allocation model applies to you.</div></div>';
    return;
  }
  if (!Object.keys(PICK).length) {
    $('bidOut').innerHTML = '<div class="card"><div class="note">Choose some courses first on the Course picker tab.</div></div>';
    return;
  }
  if (!BACKEND_OK) { await probeHealth(); if (!BACKEND_OK) return; }

  setRunning(true);
  $('optStat').textContent = 'Allocating category envelopes…';
  $('bidOut').innerHTML = '<div class="card"><div class="note"><span class="spin" aria-hidden="true"></span> Building a deterministic plan…</div></div>';

  try {
    RESULT = await API.getBidStrategy(buildPlan());
    drawResult(RESULT);
    const st = $('optStat');
    if (st) st.textContent = `${RESULT.courses.length} course(s) · ${RESULT.reserve_percent}% reserve · ${RESULT.posture} posture`;
    void autosaveNow();
  } catch (e) {
    if (e instanceof API.ApiError && (e.kind === 'offline' || e.kind === 'timeout')) setBackendState(false);
    $('bidOut').innerHTML = `<div class="card"><div class="flag f-bad">
      <b>Could not build the bid plan.</b><br>${esc(API.humanError(e))}
      <br><button class="btn2 sm" style="margin-top:8px" onclick="void runOpt()">Retry</button></div></div>`;
  } finally {
    setRunning(false);
  }
}

/* ---------------- results rendering ---------------- */
function probLabel(p) {
  if (p == null) return '—';
  // The cutoffs must be the values that would *round* to 100.0 / 0.0 at the
  // precision printed below, not 0.9999/0.0001. They were the latter, so any
  // probability from 0.9995 up printed a bare "100.0%" - which broke the rule
  // that this UI never claims certainty, and produced the visible nonsense of a
  // course reading "100.0%" beside its own band of ">99.9%".
  if (p >= 0.9995) return '&gt;99.9% <span class="tiny mut">in this model</span>';
  if (p <= 0.0005) return '&lt;0.1% <span class="tiny mut">in this model</span>';
  return (p * 100).toFixed(p > 0.995 || p < 0.005 ? 1 : 0) + '%';
}
const PRIO_LABEL = { MUST: 'Must have', STRONG: 'Strongly preferred', BACKUP: 'Useful backup', OPTIONAL: 'Optional' };

function drawResult(res) {
  const categoryClass = { ME: 'p-me', UWE: 'p-uwe', CCC: 'p-ccc' };
  const pressureLabel = {
    unknown: 'Live count needed', spare_capacity: 'Spare capacity', near_capacity: 'Near capacity',
    oversubscribed: 'Oversubscribed', heavily_oversubscribed: 'Heavily oversubscribed'
  };
  const openingByCategory = {};
  res.courses.forEach(course => {
    openingByCategory[course.category] = (openingByCategory[course.category] || 0) + course.opening_bid;
  });

  let h = `<div class="card" style="border-color:var(--sig)"><h2>Your category envelopes</h2>
    <div class="note">The reserve is protected before any course receives points. A personal ceiling is a
    resource-management boundary, <b>not</b> a prediction of the clearing price and not a University bid cap.</div>
    <div class="grid g3" style="margin-top:12px">`;
  res.categories.filter(category => category.course_count > 0).forEach(category => {
    h += `<div class="stat ${category.category === 'CCC' ? 'c' : category.category === 'UWE' ? 'u' : ''}">
      <div class="k">${esc(category.category)} live balance</div><div class="v mono">${category.pool}</div>
      <div class="f">opening bids ${openingByCategory[category.category] || 0} · personal ceilings
      ${category.strategic_ceiling_total}<br>protected reserve ${category.carry_forward_reserve} ·
      uncommitted ${category.uncommitted_in_envelope}</div></div>`;
  });
  h += `</div><div class="flag f-ok" style="margin-top:12px"><b>Budget check passed.</b>
    Every category's simultaneous personal ceilings fit inside its current-round envelope, and the selected
    carry-forward reserve remains untouched.</div>`;

  // Why the split inside a category looks the way it does. The backend has
  // always computed these; nothing displayed them, which is why an uneven split
  // between two similar courses read as arbitrary rather than as a decision with
  // a stated reason.
  res.categories.filter(category => category.course_count > 1).forEach(category => {
    h += `<details style="margin-top:10px"><summary class="tiny">Why the ${esc(category.category)}
      split is shaped this way</summary><div class="tiny mut" style="margin-top:6px">
      ${esc(category.breadth_note)}<br>${esc(category.reserve_note)}
      ${category.tie_broken_by_scarcity ? `<br><b>Two of these courses are too close for this model to
        separate.</b> The plan still puts more on one of them, because splitting evenly would lose real
        expected value, but which one is your call rather than the model's: the larger share went to the
        course with fewer seats, on the reasoning that the scarcer seat is the one least likely to still be
        available in a later round. Swap them if you would rather have the other.` : ''}
      </div></details>`;
  });
  h += `</div>`;

  h += `<div class="card"><div class="hd"><div><h2>Opening bids and stop points</h2>
    <div class="note">Start at the opening bid, watch the portal, and revise before the round closes. Never
    chase a course beyond the ceiling unless you deliberately change your posture or reserve.</div></div></div>
    <div style="overflow-x:auto"><table><thead><tr><th>Course</th><th>Priority</th>
    <th title="Seats, and how many other bidders the plan assumes are chasing them">Seats vs rivals</th>
    <th>Live pressure</th>
    <th>Opening bid</th><th>Personal ceiling</th>
    <th title="What a seat is modelled to actually cost. You are charged this, not your bid.">Modelled price</th>
    <th title="Chance your ceiling clears the price, across three market readings">Chance at ceiling</th>
    <th>Action</th></tr></thead><tbody>`;
  res.courses.forEach(course => {
    const liveValue = course.pressure.live_bidders == null ? '' : course.pressure.live_bidders;
    const ratio = course.pressure.bidder_to_seat_ratio == null ? ''
      : ` <span class="tiny mut">(${course.pressure.bidder_to_seat_ratio.toFixed(2)}× seats)</span>`;
    // Seat count is what separates two courses that are otherwise identical on
    // every column shown here, so it has to be one of the columns shown here.
    // Without it, a plan of 108 on one Major Elective and 130 on another looks
    // arbitrary even when it is not.
    const rivals = course.modelled_rivals || {};
    const rivalSpan = course.pressure.provenance === 'live'
      ? `<span class="tiny mut">${rivals.central} rival(s), observed</span>`
      : `<span class="tiny mut">${rivals.calm}&ndash;${rivals.tight} rivals, modelled</span>`;
    const price = course.clearing_price_band;
    const band = course.win_probability_band;
    const affordable = course.strategic_ceiling >= price.central;
    // A ceiling of exactly 0 is a deliberate allocation choice, not necessarily a
    // budget wall - the planner may have genuinely spare, uncommitted points in
    // this category and still put none here because they buy more elsewhere on
    // this objective (see bid_strategy.py::_rationale). Saying "more points
    // needed" in that case is actively wrong, since more points may already be
    // sitting unused rather than unavailable - only the >0-but-short case is a
    // real "raise this to help" situation.
    const zeroByChoice = course.strategic_ceiling === 0 && price.central > 0;
    const underfunded = !affordable && !zeroByChoice;
    let flag = '';
    if (underfunded) {
      flag = `<div class="flag f-bad" style="margin:0 0 6px;padding:5px 8px">Your ceiling
        (${course.strategic_ceiling}) is below the modelled price (${price.central}). Raising it would need
        more of this category's balance, likely from another course here.</div>`;
    } else if (zeroByChoice) {
      flag = `<div class="flag f-warn" style="margin:0 0 6px;padding:5px 8px">0 points allocated here by
        design, not because the balance ran out - see "Why these numbers?" below. Raise this course's
        priority if that trade-off is wrong for you.</div>`;
    }
    h += `<tr><td><b>${esc(course.code)}</b><div class="tiny mut">${esc(course.title || '')}</div></td>
      <td><span class="pill p-m">${esc(PRIO_LABEL[course.priority] || course.priority)}</span></td>
      <td class="num tiny"><b style="font-size:14px">${course.pressure.seats}</b> seats<div>${rivalSpan}</div></td>
      <td><span class="pill ${course.pressure.provenance === 'live' ? 'p-uwe' : 'p-w'}">
        ${esc(pressureLabel[course.pressure.label] || course.pressure.label)}</span>${ratio}<br>
        <label class="sr-only" for="live-${esc(course.code)}">Live bidders for ${esc(course.code)}</label>
        <input type="number" id="live-${esc(course.code)}" min="0" style="width:92px;margin-top:7px"
          value="${liveValue}" placeholder="bidders" onchange="setLive('${esc(course.code)}',this.value)"></td>
      <td class="num" style="font-size:18px;color:var(--sig);font-weight:700">${course.opening_bid}</td>
      <td class="num" style="font-size:18px;font-weight:700">${course.strategic_ceiling}</td>
      <td class="num tiny">${price.central}<div class="mut">${price.low}&ndash;${price.high}</div></td>
      <td class="num tiny">${probLabel(band.central)}<div class="mut">${probLabel(band.low)}&ndash;${probLabel(band.high)}</div></td>
      <td>${flag}${esc(course.action)}
        <details style="margin-top:7px"><summary class="tiny">Why these numbers?</summary>
        <div class="tiny mut" style="margin-top:5px">${course.rationale.map(esc).join('<br>')}</div></details></td></tr>`;
  });
  h += `</tbody></table></div></div>`;

  h += `<div class="card"><h2>What is known, and what is not</h2>
    <div class="flag f-warn"><b>Every probability and price here is a model output, not a measurement.</b>
      ${esc(res.uncertainty)}</div>
    <div class="grid g2" style="margin-top:12px"><div><h3>Official mechanism used</h3><ol>`
    + res.official_mechanism.map(item => `<li>${esc(item)}</li>`).join('')
    + `</ol></div><div><h3>How to use this plan</h3><ol>`
    + res.next_steps.map(item => `<li>${esc(item)}</li>`).join('')
    + `</ol></div></div><div class="tiny mut">strategy ${esc(res.strategy_version)} · deterministic ·
      no synthetic opponents · no random seed</div></div>`;
  $('bidOut').innerHTML = h;
}

function setLive(code, v) {
  if (v === '' || v == null) delete LIVE[code];
  else LIVE[code] = { bidders: Math.max(0, Math.floor(+v) || 0),
                      at: new Date().toISOString(),
                      round: $('liveRound') ? $('liveRound').value : '' };
  void autosaveNow();
  if (RESULT) void runOpt();                     // re-run so the live count takes effect
}
function setPriority(code, v) { PRIO[code] = v; renderChosen(); void autosaveNow(); }

/* ---------------- schedule builder (backend-authoritative search) ----------------
   Replaces the old in-browser combination search: it used to run synchronously
   on this page's main thread (measured: ~100s at its old default budget) and
   now runs as a real cancellable backend job, same pattern as runOpt(). */
let ALLSCHED = [];
let SCHED_JOB = null;
let SCHED_TOTAL = 0;
let SCHED_TRUNC = false;
let SCHED_MODE = 'exact';    // 'exact' (clash-free) or 'least_conflict' (best available)
let SCHED_CLASHES = 0;       // remaining clash count per result when SCHED_MODE is least_conflict
let SCHED_RUNNING = false;
let SCHED_CURRENT_JOB = null;

function buildScheduleRequest() {
  const shortlist = Object.keys(PICK).filter(c => BY[c] && BY[c].pk.length);
  const fixed = FIXED.filter(f => BY[f.code] && BY[f.code].pk.length)
    // These are pre-enrolled/core courses in the personalised plan. Their
    // current package is mandatory here; the separate clash resolver owns swaps.
    .map(f => ({ code: f.code, pkg: f.pkg, locked: true }));
  return {
    shortlist, fixed,
    max_nodes: +$('bMaxNodes').value,
    max_results: +$('bMaxResults').value,
    sort: $('bSort').value,
    allow_least_conflict: true,
  };
}
function setBuildRunning(on) {
  SCHED_RUNNING = on;
  const btn = $('buildBtn'), cancel = $('buildCancelBtn');
  if (btn) { btn.disabled = on; btn.textContent = on ? 'Searching…' : 'Generate all schedules'; }
  if (cancel) cancel.disabled = !on;
}
function setBuildProgress(pct, phase) {
  const pb = $('buildProgBar'), fill = $('buildProgFill');
  if (pb) {
    pb.style.display = 'block';
    if (pct != null) { pb.classList.remove('indet'); pb.setAttribute('aria-valuenow', String(Math.round(pct))); }
    else { pb.classList.add('indet'); pb.removeAttribute('aria-valuenow'); }
    pb.setAttribute('aria-valuetext', phase || '');
  }
  if (fill) fill.style.width = Math.max(2, Math.min(100, pct == null ? 2 : pct)) + '%';
  const s = $('buildStat');
  if (s) s.textContent = pct != null ? `${Math.round(pct)}% — ${phase || 'searching'}` : (phase || 'searching');
}
function clearBuildProgress() {
  const pb = $('buildProgBar'); if (pb) { pb.style.display = 'none'; pb.classList.remove('indet'); }
  const fill = $('buildProgFill'); if (fill) fill.style.width = '0%';
}

async function buildSchedules() {
  if (SCHED_RUNNING) return;                             // no duplicate submission
  const codes = Object.keys(PICK).filter(c => BY[c] && BY[c].pk.length);
  if (!codes.length) {
    $('buildOut').innerHTML = '<div class="card"><div class="note">Add courses to your bid shortlist on the Course selection tab first.</div></div>';
    return;
  }
  if (!BACKEND_OK) { await probeHealth(); if (!BACKEND_OK) return; }

  setBuildRunning(true);
  setBuildProgress(null, 'Submitting search');
  try {
    const out = await API.runScheduleSearch(buildScheduleRequest(), {
      onPhase: t => setBuildProgress(null, t),
      onJob: j => { SCHED_CURRENT_JOB = j.job_id; },
      onProgress: s => { const p = s.progress || {}; setBuildProgress(p.percent, p.phase); },
      onWarning: () => {
        const s = $('buildStat'); if (s) s.textContent += ' (connection interrupted, retrying…)';
      },
    });

    if (out.stale) { clearBuildProgress(); return; }       // a newer run superseded this one
    if (out.cancelled) {
      clearBuildProgress();
      $('buildOut').innerHTML = '<div class="card"><div class="flag f-warn">'
        + '<b>Search cancelled.</b> No results were applied to your plan.</div></div>';
      return;
    }
    SCHED_JOB = out.jobId;
    ALLSCHED = out.results.schedules;
    SCHED_TOTAL = out.results.total_found;
    SCHED_TRUNC = out.results.truncated;
    SCHED_MODE = out.results.mode || 'exact';
    SCHED_CLASHES = out.results.clash_count || 0;
    clearBuildProgress();
    const s = $('buildStat');
    if (s) {
      s.textContent = `${out.results.nodes.toLocaleString()} combinations tested · `
        + (SCHED_MODE === 'least_conflict'
            ? `no clash-free combination found — best available leaves ${SCHED_CLASHES} clash${SCHED_CLASHES === 1 ? '' : 'es'}`
            : `${SCHED_TOTAL} valid clash-free schedule${SCHED_TOTAL === 1 ? '' : 's'} found`)
        + (out.results.cache_hit ? ' · served from cache' : '');
    }
    renderSchedules();
  } catch (e) {
    clearBuildProgress();
    if (e instanceof API.ApiError && (e.kind === 'offline' || e.kind === 'timeout')) setBackendState(false);
    $('buildOut').innerHTML = `<div class="card"><div class="flag f-bad">
      <b>Could not complete the search.</b><br>${esc(API.humanError(e))}
      <br><button class="btn2 sm" style="margin-top:8px" onclick="void buildSchedules()">Retry</button></div></div>`;
  } finally {
    setBuildRunning(false);
    SCHED_CURRENT_JOB = null;
  }
}

async function cancelBuild() {
  API.invalidateScheduleRuns();
  setBuildProgress(null, 'Cancelling…');
  const jid = SCHED_CURRENT_JOB;
  setBuildRunning(false);
  clearBuildProgress();
  $('buildOut').innerHTML = '<div class="card"><div class="flag f-warn">'
    + '<b>Search cancelled.</b> No results were applied to your plan.</div></div>';
  if (!jid) return;
  try {
    const r = await API.cancelScheduleSearch(jid);
    const s = $('buildStat'); if (s) s.textContent = `cancelled · backend acknowledged in ${r.ack_ms} ms`;
  } catch (e) {
    const s = $('buildStat'); if (s) s.textContent = 'cancelled locally; the backend did not confirm (' + API.humanError(e) + ')';
  }
}

async function loadMoreSchedules() {
  if (!SCHED_JOB || ALLSCHED.length >= SCHED_TOTAL) return;
  const btn = $('loadMoreBtn'); if (btn) { btn.disabled = true; btn.textContent = 'Loading…'; }
  try {
    const page = await API.getScheduleResults(SCHED_JOB, 60, ALLSCHED.length);
    ALLSCHED = ALLSCHED.concat(page.schedules);
    renderSchedules();
  } catch (e) {
    const s = $('buildStat'); if (s) s.textContent = 'could not load more: ' + API.humanError(e);
    if (btn) { btn.disabled = false; btn.textContent = `Load more (${ALLSCHED.length} of ${SCHED_TOTAL})`; }
  }
}

/* ---------------- choice groups (wishlist scheduler) ---------------- */
const CG_KIND_LABEL = { exactly_one: 'Exactly one of', at_least_one: 'At least one of',
  at_most_one: 'At most one of', min_credits: 'At least N credits from' };
function renderChoiceGroups() {
  const box = $('choiceGroupList'); if (!box) return;
  if (!CHOICE_GROUPS.length) { box.innerHTML = '<div class="tiny mut">No choice groups yet.</div>'; return; }
  box.innerHTML = CHOICE_GROUPS.map((g, i) => `<div class="rec" style="padding:8px 10px">
    <b>${esc(CG_KIND_LABEL[g.kind] || g.kind)}</b>${g.kind === 'min_credits' ? ' (' + f1(g.min_credits) + ' cr)' : ''}:
    ${g.members.map(esc).join(', ')}
    <button class="btn2 sm" style="margin-left:8px"
      onclick="CHOICE_GROUPS.splice(${i},1);renderChoiceGroups();void autosaveNow()">remove</button>
    </div>`).join('');
}
async function addChoiceGroup() {
  const codes = Object.keys(PICK);
  if (codes.length < 2) { alert('Choose at least 2 courses on this tab first.'); return; }
  const kind = $('cgKind').value;
  const picked = [];
  for (const c of codes) {
    if (confirm(`Include ${c} in this group?`)) {
      picked.push(c);
      if (picked.length >= 2 && !confirm('Add another course to the group?')) break;
    }
  }
  if (picked.length < 2) { alert('A choice group needs at least 2 courses.'); return; }
  let minCredits = null;
  if (kind === 'min_credits') {
    minCredits = parseFloat($('cgMinCredits').value);
    if (isNaN(minCredits) || minCredits <= 0) { alert('Enter a positive minimum credit value.'); return; }
  }
  CHOICE_GROUPS.push({ kind, members: picked, min_credits: minCredits });
  renderChoiceGroups();
  void autosaveNow();
}

/* ---------------- credit policy (three numbers, never one) ---------------- */
function toggleOverload() {
  const on = $('overloadOn').checked;
  if ($('overloadCeilingWrap')) $('overloadCeilingWrap').style.display = on ? '' : 'none';
  if ($('overloadConfirmWrap')) $('overloadConfirmWrap').style.display = on ? '' : 'none';
}
function creditPolicyPayload() {
  const overloadOn = !!($('overloadOn') && $('overloadOn').checked);
  const officialCeiling = +($('capCr') ? $('capCr').value : 25) || 25;
  const overloadCeiling = +($('overloadCeiling') ? $('overloadCeiling').value : 27) || 27;
  return {
    min: +($('credMin') ? $('credMin').value : 0) || 0,
    target: +($('credTarget') ? $('credTarget').value : 0) || 0,
    max: overloadOn ? overloadCeiling : officialCeiling,
    officialCeiling, overloadOn, overloadCeiling,
    eligibilityConfirmed: !!($('extensionEligible') && $('extensionEligible').checked),
    advisorRecommended: !!($('advisorRecommended') && $('advisorRecommended').checked),
    deanApproved: !!($('deanApproved') && $('deanApproved').checked),
  };
}
async function refreshCreditPolicy() {
  const box = $('creditPolicyBox'); if (!box) return;
  if (!BACKEND_OK) { await probeHealth(); if (!BACKEND_OK) return; }
  box.innerHTML = '<div class="note"><span class="spin"></span> checking…</div>';
  const cp = creditPolicyPayload();
  const body = { credit_policy: {
    fixed_credits: fixedCredits(), personal_target: cp.target, min_credits: cp.min,
    overload_ceiling: cp.overloadOn ? cp.overloadCeiling : null,
    overload_mode: 'what_if', overload_confirmed: false,
    current_year: !$('model')||!$('model').value ? null : ($('model').value==='y4'?4:($('model').value==='y3'?3:2)),
    eligibility_confirmed: cp.eligibilityConfirmed,
    advisor_recommended: cp.advisorRecommended, dean_approved: cp.deanApproved,
  }};
  try {
    const r = await API.validateProfile(body);
    const cls = r.ceiling_mode === 'standard' ? 'f-ok' : (r.ceiling_mode === 'what_if' ? 'f-bad' : 'f-warn');
    let h = `<div class="flag ${cls}"><b>${esc(r.summary)}</b></div>`;
    (r.warnings || []).forEach(w => { h += `<div class="tiny mut" style="margin-top:4px">${esc(w)}</div>`; });
    box.innerHTML = h;
  } catch (e) {
    box.innerHTML = `<div class="flag f-bad">${esc(API.humanError(e))}</div>`;
  }
}

/* ---------------- wishlist-mode schedule generation (backend CP-SAT) ----------------
   Reuses API.runScheduleSearch as-is: the backend's /schedules/search accepts
   wishlist/choice_groups/credit_* fields on the same request shape the plain
   shortlist search uses (see app/models/schedule_schemas.py). */
const INTENT_MAP = { MUST: 'must_have', STRONG: 'strong', BACKUP: 'backup', OPTIONAL: 'optional' };
let WISH_RESULT = null;
let WISH_JOB = null;

function buildWishlistRequest() {
  const codes = Object.keys(PICK).filter(c => BY[c] && BY[c].pk.length);
  const wishlist = codes.map(code => ({
    code, intent: INTENT_MAP[prioOf(code)] || 'strong', priority: PICK[code].want || 5,
  }));
  const fixed = FIXED.filter(f => BY[f.code] && BY[f.code].pk.length)
    // Wishlist mode must include every pre-enrolled/core course. The lock
    // toggle belongs to the clash explorer; omitting an unlocked fixed course
    // here previously made the personalised schedule undercount the load.
    .map(f => ({ code: f.code, pkg: f.pkg, locked: true }));
  const cp = creditPolicyPayload();
  return {
    shortlist: [], fixed,
    external_fixed: MANUAL.map(m => ({ name: m.name, credits: m.cr })), wishlist,
    choice_groups: CHOICE_GROUPS.filter(g => g.members.every(m => codes.includes(m))),
    credit_min: cp.min, credit_target: cp.target || cp.max, credit_max: cp.max,
    max_nodes: 2000000, max_results: 10, sort: 'compact', allow_least_conflict: true,
  };
}

async function generateWishlistSchedule() {
  const codes = Object.keys(PICK).filter(c => BY[c] && BY[c].pk.length);
  if (!codes.length) {
    $('wishOut').innerHTML = '<div class="card"><div class="note">Add courses to your wishlist on the '
      + 'Course selection tab first.</div></div>';
    return;
  }
  if (!BACKEND_OK) { await probeHealth(); if (!BACKEND_OK) return; }
  const btn = $('wishBtn'); if (btn) { btn.disabled = true; btn.textContent = 'Generating…'; }
  if ($('wishStat')) $('wishStat').textContent = 'Submitting…';
  try {
    const out = await API.runScheduleSearch(buildWishlistRequest(), {
      onPhase: t => { if ($('wishStat')) $('wishStat').textContent = t; },
      onJob: j => { WISH_JOB = j.job_id; },
      onProgress: s => { const p = s.progress || {}; if ($('wishStat')) $('wishStat').textContent = p.phase || s.state; },
    });
    if (out.stale) return;
    if (out.cancelled) {
      $('wishOut').innerHTML = '<div class="card"><div class="flag f-warn"><b>Cancelled.</b></div></div>';
      return;
    }
    WISH_RESULT = out.results; WISH_JOB = out.jobId;
    renderWishlistResult();
  } catch (e) {
    $('wishOut').innerHTML = `<div class="card"><div class="flag f-bad"><b>Could not generate a schedule.</b>
      <br>${esc(API.humanError(e))}</div></div>`;
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Generate personalised schedule'; }
    if ($('wishStat')) $('wishStat').textContent = '';
  }
}

function renderWishlistResult() {
  const r = WISH_RESULT;
  if (!r || !r.schedules || !r.schedules.length) {
    $('wishOut').innerHTML = `<div class="card"><div class="flag f-bad"><b>No schedule found.</b>
      ${r && r.cp_status === 'infeasible'
        ? ' Your must-have courses, choice groups, and fixed courses cannot all fit together at once — '
        + 'try relaxing a lock, a choice group, or the minimum-credit floor.' : ''}</div></div>`;
    return;
  }
  const s = r.schedules[0];
  let h = `<div class="card" style="border-color:var(--sig)"><div class="hd"><div><h2>Your personalised schedule</h2>
    <div class="note">Solver status: <b>${esc(r.cp_status)}</b>${r.min_relaxed
      ? ' · minimum-credit floor was relaxed because no schedule could meet it' : ''}</div></div></div>
    <div class="grid g4">
      <div class="stat"><div class="k">Included</div><div class="v mono">${r.included.length}</div></div>
      <div class="stat u"><div class="k">Excluded</div><div class="v mono">${r.excluded.length}</div></div>
      <div class="stat c"><div class="k">Total credits</div><div class="v mono">${f1(r.total_credits)}</div>
        <div class="f">fixed ${f1(r.fixed_credits)} + wishlist ${f1(r.total_credits - r.fixed_credits)}</div></div>
      <div class="stat"><div class="k">Target</div><div class="v mono">${f1(r.credit_target)}</div>
        <div class="f">min ${f1(r.credit_min)} · max ${f1(r.credit_max)}</div></div></div>`;
  h += '<table style="margin-top:12px"><thead><tr><th>Course</th><th>Status</th><th>Why</th></tr></thead><tbody>';
  (r.fixed_course_codes||[]).forEach(c=>{
    h += `<tr><td><b>${esc(c)}</b> ${BY[c]?esc(BY[c].title.slice(0,30)):''}</td><td><span class="pill p-m">fixed / core</span></td><td class="tiny">Pre-enrolled or fixed in this plan</td></tr>`;
  });
  (r.external_fixed||[]).forEach(m=>{
    h += `<tr><td><b>${esc(m.name)}</b></td><td><span class="pill p-m">fixed off-timetable</span></td><td class="tiny">${f1(m.credits)} credits already committed</td></tr>`;
  });
  [...r.included, ...r.excluded].forEach(c => {
    const inc = r.included.includes(c);
    const wn = (r.why_not || []).find(w => w.code === c);
    h += `<tr><td><b>${esc(c)}</b> ${BY[c] ? esc(BY[c].title.slice(0, 30)) : ''}</td>
      <td>${inc ? '<span class="pill p-uwe">included</span>' : '<span class="pill p-w">excluded</span>'}</td>
      <td class="tiny">${inc ? '—' : (wn ? esc(wn.reason)
        : `<button class="btn2 sm" onclick="void explainWishlistExclusion('${esc(c)}')">why not?</button>`)}</td></tr>`;
  });
  h += '</tbody></table>';
  h += '<div style="margin-top:12px"><button class="btn2" onclick="void useWishlistSchedule()">Use this schedule</button></div></div>';
  h += '<div id="wishExplainBox"></div>';
  $('wishOut').innerHTML = h;
}

async function explainWishlistExclusion(code) {
  if (!WISH_JOB) return;
  const box = $('wishExplainBox'); if (!box) return;
  box.innerHTML = '<div class="note"><span class="spin"></span> checking…</div>';
  try {
    const ex = await API.explainExclusion(WISH_JOB, code);
    box.innerHTML = `<div class="flag f-warn"><b>${esc(code)}:</b> ${esc(ex.reason)}`
      + (ex.relaxation ? `<br><i>Try: ${esc(ex.relaxation)}</i>` : '') + '</div>';
  } catch (e) {
    box.innerHTML = `<div class="flag f-bad">${esc(API.humanError(e))}</div>`;
  }
}
function useWishlistSchedule() {
  const r = WISH_RESULT; if (!r || !r.schedules || !r.schedules.length) return;
  const assign = r.schedules[0].assign;
  Object.keys(assign).forEach(code => { if (PICK[code]) PICK[code].pkg = assign[code]; });
  renderChosen(); renderPick();
  // "Generate a personalised schedule" and the weekly grid both live on the
  // Timetable tab now (split out of Course selection) - scroll to the grid
  // in place, the same pattern i_build.html's useSchedule() already uses for
  // the same reason, rather than switching tabs away from where this button is.
  const grid = $('ttGrid'); if (grid) grid.scrollIntoView({ behavior: 'smooth', block: 'start' });
  if (typeof drawTT === 'function') drawTT();
}

/* ---------------- rules from the backend ---------------- */
const STATUS_LABEL = {
  official: ['Official', 'p-uwe'], prospectus: ['Prospectus', 'p-uwe'],
  timetable: ['From timetable', 'p-ccc'], inferred: ['Inferred', 'p-me'],
  disputed: ['Disputed', 'p-w'], unknown: ['Unknown', 'p-w']
};
const RULE_SOURCE_DOCUMENTS = [
  {id:'faq', title:'Course Enrolment — Frequently Asked Questions', kind:'Official PDF · Dean of Academics',
   href:'docs/Course_Enrolment_FAQ_1.pdf', local:true,
   aliases:['course_enrolment_faq_1.pdf','course enrolment faq','frequently asked questions']},
  {id:'intro', title:'Course Bidding — Process, Rounds and Schedule', kind:'Official PDF · Dean of Academics',
   href:'docs/Course_Bidding_Introduction.pdf', local:true,
   aliases:['course_bidding_introduction.pdf','introduction s.','guide s.','eligibility checks']},
  {id:'concept', title:'Course Bid-Point Allocation Concept Note', kind:'Official revised PDF',
   href:'docs/Course_Bid_Point_Allocation_Concept_Note_revised_final.pdf', local:true,
   aliases:['course_bid_point_allocation_concept_note_revised_final.pdf','concept note s.']},
  {id:'credit-policy', title:'Student credit-limit policy', kind:'Official SNU policy portal',
   href:'https://snulinks.snu.edu.in/application/student-policy/index?pId=0&cId1=1&cId2=9', local:false,
   aliases:['credit-limit policy 4.7.9','credit-limit policy 4.7.10','credit-limit policy 4.8']},
  {id:'cse-prospectus', title:'B.Tech. CSE prospectus (2022 onward)', kind:'Official SNU prospectus',
   href:'https://snu.edu.in/site/assets/files/3888/prospectus_b_tech__-_cse_2022onwards.pdf?v=2', local:false,
   aliases:['prospectus, minimum requirement','prospectus footnote','prospectus ai bucket','prospectus major electives']},
  {id:'timetable', title:'Monsoon 2026 published timetable', kind:'Official timetable source used by this dataset',
   href:'https://snioe-monsoon2026-tt.netlify.app/', local:false,
   aliases:['scheduler workbook','monsoon 2026 timetable','timetable term field']},
];
function sourceDocumentsForRule(rule){
  const hay=(rule.source+' '+(rule.note||'')+' '+(rule.verified||'')).toLowerCase();
  return RULE_SOURCE_DOCUMENTS.filter(doc=>doc.aliases.some(alias=>hay.includes(alias)));
}
function sourceAction(doc,label){
  const download=doc.local?' download':'';
  return `<a class="btn2 sm" href="${esc(doc.href)}" target="_blank" rel="noopener"${download}>${label}</a>`;
}
function renderSourceLibrary(rules){
  const cards=RULE_SOURCE_DOCUMENTS.map(doc=>{
    const count=rules.filter(rule=>sourceDocumentsForRule(rule).some(match=>match.id===doc.id)).length;
    if(!count)return '';
    return `<article class="source-card"><div class="source-card-head"><span class="source-icon">${doc.local?'PDF':'LINK'}</span><div><b>${esc(doc.title)}</b><small>${esc(doc.kind)} · cited by ${count} rule${count===1?'':'s'}</small></div></div><div class="source-actions">${sourceAction(doc,doc.local?'Open / download PDF':'Open official source')}</div></article>`;
  }).join('');
  return `<div class="hd" style="margin-bottom:9px"><div><h2>Source document library</h2><div class="note">Repeated citations are grouped here. Open one document once, then compare every rule that cites it below.</div></div></div><div class="source-library">${cards}</div>`;
}
async function drawRules() {
  if (!$('ruleList')) return;
  if (!RULES_CACHE) {
    if (!BACKEND_OK) {
      $('ruleSummary').innerHTML = '<div class="flag f-bad">Rules are served by the calculation service, '
        + 'which is currently unavailable. Reconnect to view them.</div>';
      $('ruleList').innerHTML = '';
      return;
    }
    try { RULES_CACHE = await API.getRules(); }
    catch (e) {
      $('ruleSummary').innerHTML = `<div class="flag f-bad">${esc(API.humanError(e))}</div>`;
      return;
    }
  }
  const R = RULES_CACHE;
  const total = R.rules.length;
  let sum = '<div class="grid g5">';
  ['official', 'prospectus', 'inferred', 'disputed', 'unknown'].forEach(k => {
    const n = R.counts[k] || 0;
    const cls = (k === 'disputed' || k === 'unknown') ? 'r' : (k === 'inferred' ? '' : 'u');
    sum += `<div class="stat ${cls}"><div class="k">${STATUS_LABEL[k][0]}</div>
      <div class="v mono">${n}</div><div class="f">of ${total} rules</div></div>`;
  });
  sum += '</div>';
  sum += `<div class="flag f-ok" style="margin-top:12px"><b>BUDGET.SHARED_LIVE is officially confirmed
    (2026-08-05).</b> Bids in the same category share one live balance while a round is open.
    Rule version <span class="mono">${esc(R.version)}</span>, served by the backend.</div>`;
  $('ruleSummary').innerHTML = sum;
  if($('sourceLibrary'))$('sourceLibrary').innerHTML=renderSourceLibrary(R.rules);

  const st = $('rStatus') ? $('rStatus').value : '';
  const q = $('rQ') ? $('rQ').value.trim().toLowerCase() : '';
  const showAllProgrammes = !!($('rAllProgrammes') && $('rAllProgrammes').checked);
  const programme = $('programme') ? $('programme').value : '';
  let list = R.rules.slice();
  // Most rules apply to every programme (pool formulas, settlement, rounds). A handful are
  // specific to one programme's own prospectus (e.g. CSE's specialisation buckets) - hide
  // those by default so the Rules tab doesn't read as CS-specific to everyone else.
  const scopedOut = list.filter(r => r.programme_scope && !r.programme_scope.includes(programme));
  if (!showAllProgrammes) list = list.filter(r => !r.programme_scope || r.programme_scope.includes(programme));
  if (st) list = list.filter(r => r.status === st);
  if (q) list = list.filter(r => (r.id + ' ' + r.name + ' ' + r.desc + ' ' + r.source).toLowerCase().includes(q));
  if (!showAllProgrammes && scopedOut.length) {
    sum = $('ruleSummary').innerHTML + `<div class="flag f-q" style="margin-top:8px">${scopedOut.length}
      programme-specific rule(s) hidden (e.g. CSE's specialisation buckets) — tick "Also show other
      programmes' rules" to see them.</div>`;
    $('ruleSummary').innerHTML = sum;
  }
  const order = { unknown: 0, disputed: 1, inferred: 2, timetable: 3, prospectus: 4, official: 5 };
  list.sort((a, b) => (order[a.status] - order[b.status]) || a.id.localeCompare(b.id));
  $('ruleList').innerHTML = list.map(r => {
    const [lbl, cls] = STATUS_LABEL[r.status] || ['?', 'p-m'];
    const documents=sourceDocumentsForRule(r);
    const sourceLinks=documents.length?`<div class="rule-source-links">${documents.map(doc=>sourceAction(doc,doc.local?'Open PDF':'Official source')).join('')}</div>`:'';
    const extras = [
      r.verified ? `<div class="tiny" style="color:var(--ok)"><b>Verified:</b> ${esc(r.verified)}</div>` : '',
      r.resolution ? `<div class="flag f-ok" style="margin-top:0"><b>How this tool resolves it:</b> ${esc(r.resolution)}</div>` : '',
      r.note ? `<div class="tiny"><b>Note:</b> ${esc(r.note)}</div>` : '',
      r.impact ? `<div class="tiny"><b>Impact:</b> ${esc(r.impact)}</div>` : '',
    ].filter(Boolean).join('');
    return `<div class="rec">
      <div style="display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;margin-bottom:6px">
        <div><b>${esc(r.name)}</b> <span class="tiny mono mut">${esc(r.id)}</span></div>
        <div><span class="pill ${cls}">${lbl}</span>
          ${r.configurable ? '<span class="pill p-m">configurable</span>' : ''}</div></div>
      <div style="font-size:12.5px;color:var(--dim)">${esc(r.desc)}</div>
      <details class="more" style="margin-top:8px"><summary>Source &amp; details</summary>
        <div class="tiny"><b>Exact citation:</b> ${esc(r.source)}</div>${sourceLinks}${extras}</details>
    </div>`;
  }).join('') || '<div class="note">No rules match.</div>';
}


/* =====================================================================
   BOOT

   Runs last, after every UI layer and every client module is defined.
   The previous arrangement ran the boot sequence from a UI layer, which
   executed BEFORE this file was parsed and produced a temporal-dead-zone
   error on RULES_CACHE and undefined-function calls.
   ===================================================================== */
async function boot() {
  initBlocks();
  if (typeof initDeptFilter === 'function') initDeptFilter();
  // neutral by default -- a fresh install (anyone this app is shared with) starts
  // with nothing pre-filled; restoreActivePlan() below fills these back in from
  // this browser's own saved plan, if one exists, so a returning student on their
  // own machine sees their own numbers again without them ever being baked into
  // the shipped source.
  MANUAL = [];
  FIXED = [];

  ['bidPosture', 'bidReserve'].forEach(id => {
    const e = $(id); if (e) e.onchange = () => { if (RESULT) void runOpt(); };
  });
  ['model', 'sem', 'remME', 'remUWE', 'remCCC', 'remFL', 'doneME', 'doneUWE', 'doneCCC'].forEach(id => {
    const e = $(id); if (e) e.addEventListener('change', () => void refreshPoolsFromBackend());
  });
  // ME/Core now resolve through the workbook's <DEPT><YEAR>YR scoping tokens,
  // so the year is an input to categorisation and not just to the pools.
  if ($('model')) $('model').addEventListener('change', () => applyProgrammeCategories());
  if ($('rStatus')) $('rStatus').onchange = () => void drawRules();
  if ($('rQ')) $('rQ').oninput = () => void drawRules();
  if ($('rAllProgrammes')) $('rAllProgrammes').onchange = () => void drawRules();
  if ($('cgKind')) $('cgKind').onchange = () => {
    if ($('cgMinCredits')) $('cgMinCredits').style.display = $('cgKind').value === 'min_credits' ? '' : 'none';
  };
  renderChoiceGroups();
  toggleOverload();

  // local-only rendering first, so the page is usable before any network call.
  // auto-resolve-clashes now needs the backend (see autoSolve() in d_sched.html),
  // so it's an explicit button action only - it never ran silently on boot in a
  // way that did anything anyway, since it used to run before restoreActivePlan()
  // had even populated FIXED/PICK from the saved plan.
  recalc(); renderFixed(); renderPick(); renderChosen();
  drawData(); drawSpec(); drawLearn();
  loadCompletedCoursesFromTextarea(); renderCompletedCoursesTable();
  restoreActivePlan();
  refreshPlanPicker();
  if ($('planPicker')) $('planPicker').onchange = e => {
    if (e.target.value) { PLANS.setActive(e.target.value); restoreActivePlan(); revalidatePlanAgainstDataset(); }
  };

  // then connect
  await probeHealth();
  if (BACKEND_OK) {
    await loadProgrammeCatalog();
    // Programme options now exist, so restore programme-dependent category
    // classification and private audit fields once more.
    restoreActivePlan();
  }
  startHealthLoop();
  void drawRules();
  if (BACKEND_OK) {
    try { DATASET_INFO = await API.getDataset(); revalidatePlanAgainstDataset(); }
    catch (e) { /* dataset banner is best-effort; never block boot on it */ }
    void checkTimetableOnOpen();  // every app open/refresh; backend lock deduplicates simultaneous tabs
    try {
      const outlines = await API.getCourseOutlineCodes();
      OUTLINE_CODES = new Set(outlines.codes || []);
      renderPick();   // re-render so "view outline" affordances appear on rows already drawn
    } catch (e) { /* outline availability is a nice-to-have; never block boot on it */ }
  }
  // expose a minimal surface for tests and debugging (no compute, just state)
  try {
    Object.defineProperty(window, 'RESULT', { get: () => RESULT, configurable: true });
    Object.defineProperty(window, 'BACKEND_OK', { get: () => BACKEND_OK, configurable: true });
    window.probeHealth = probeHealth;
    window.runOpt = runOpt;
    window.currentPlanPayload = currentPlanPayload;
  } catch (e) {}
}

/* ---------------- §10 autosave / restore ---------------- */
function currentPlanPayload() {
  return {
    pools: { ME: BUD.ME, UWE: BUD.UWE, CCC: BUD.CCC },
    profile: {
      model: $('model') ? $('model').value : 'y4',
      semester: $('sem') ? +$('sem').value : 7,
      remME: +$('remME').value, remUWE: +$('remUWE').value,
      remCCC: +$('remCCC').value, floater: +$('remFL').value,
      doneME: +($('doneME') ? $('doneME').value : 0),
      doneUWE: +($('doneUWE') ? $('doneUWE').value : 0),
      doneCCC: +($('doneCCC') ? $('doneCCC').value : 0),
      creditCap: +$('capCr').value,
      doneCr: +$('doneCr').value, degCr: +$('degCr').value,
      remCore: +$('remCore').value, remOther: +$('remOther').value,
      csdBlock: $('blk') ? $('blk').value : '',
      programme: $('programme') ? $('programme').value : '',
      cohortYear: +($('cohortYear') ? $('cohortYear').value : 0) || null,
      completedCoursesText: $('completedCourses') ? $('completedCourses').value : '',
      completedMilestones: ($('completedMilestones') ? $('completedMilestones').value : '').split(',').map(x => x.trim()).filter(Boolean)
    },
    fixed: FIXED.filter(f => !f.auto).map(f => ({ code: f.code, pkg: f.pkg, lock: f.lock })),
    manual: MANUAL.map(m => ({ name: m.name, cr: m.cr })),
    doneElectives: DONE_ME.map(d => ({ code: d.code, name: d.name, cr: d.cr })),
    pathwaySelections: Object.assign({}, PATHWAY_SELECTIONS),
    pathwayNotes: Object.assign({}, PATHWAY_NOTES),
    datasetVersion: DATASET_INFO ? DATASET_INFO.active_version : null,
    choiceGroups: CHOICE_GROUPS.map(g => ({ kind: g.kind, members: g.members.slice(), min_credits: g.min_credits })),
    creditPolicy: {
      min: +($('credMin') ? $('credMin').value : 0), target: +($('credTarget') ? $('credTarget').value : 0),
      overloadOn: !!($('overloadOn') && $('overloadOn').checked),
      overloadCeiling: +($('overloadCeiling') ? $('overloadCeiling').value : 27),
      eligibilityConfirmed: !!($('extensionEligible') && $('extensionEligible').checked),
      advisorRecommended: !!($('advisorRecommended') && $('advisorRecommended').checked),
      deanApproved: !!($('deanApproved') && $('deanApproved').checked),
    },
    advisement: ADVISEMENT_INFO ? JSON.parse(JSON.stringify(ADVISEMENT_INFO)) : null,
    auditRequirements: PROFILE_AUDIT_OVERRIDES.map(r => Object.assign({}, r)),
    courses: Object.keys(PICK).map(code => ({
      code, credits: (BY[code] || {}).cr, seats: (BY[code] || {}).seats,
      category: (BY[code] || {}).cat, priority: prioOf(code),
      pkg: PICK[code].pkg,
      liveBidders: LIVE[code] ? LIVE[code].bidders : null
    })),
    assumptions: {
      posture: curPosture(), reservePercent: curReserve()
    }
  };
}
async function autosaveNow() {
  const id = PLANS.activeId();
  const cur = id ? PLANS.get(id) : null;
  PLANS.autosave(cur ? cur.name : 'Working plan', currentPlanPayload, id || undefined, 800);
}
function restoreActivePlan() {
  const id = PLANS.activeId();
  if (!id) return;
  const p = PLANS.get(id);
  if (!p || !p.payload) return;
  const pay = p.payload;
  try {
    if (pay.profile) {
      const set = (k, v) => { const e = $(k); if (e && v != null) e.value = v; };
      // refreshSem() must run between these two: it rebuilds #sem's own <option>
      // list to match whichever model was just restored (y2/y3 each expose a
      // different set of valid semesters), so setting #sem's value first would
      // silently fail to match anything whenever the restored plan's model isn't
      // whatever model happened to be selected on the page before this call.
      set('model', pay.profile.model); refreshSem(); set('sem', pay.profile.semester);
      set('remME', pay.profile.remME); set('remUWE', pay.profile.remUWE);
      set('remCCC', pay.profile.remCCC); set('remFL', pay.profile.floater);
      set('doneME', pay.profile.doneME || 0); set('doneUWE', pay.profile.doneUWE || 0);
      set('doneCCC', pay.profile.doneCCC || 0);
      set('capCr', 25); // institutional rule; never trust a stale per-profile ceiling
      set('doneCr', pay.profile.doneCr); set('degCr', pay.profile.degCr);
      set('remCore', pay.profile.remCore); set('remOther', pay.profile.remOther);
      set('programme', pay.profile.programme); set('cohortYear', pay.profile.cohortYear);
      set('completedCourses', pay.profile.completedCoursesText);
      loadCompletedCoursesFromTextarea(); renderCompletedCoursesTable();
      if ($('completedMilestones')) $('completedMilestones').value = (pay.profile.completedMilestones || []).join(', ');
      if (pay.profile.csdBlock) { refreshSem(); set('blk', pay.profile.csdBlock); applyBlock(); }
    }
    if (Array.isArray(pay.fixed)) {
      const autoBlockEntries = FIXED.filter(f => f.auto);
      FIXED = autoBlockEntries.concat(
        pay.fixed.filter(f => BY[f.code]).map(f => ({ code: f.code, pkg: f.pkg || 0, auto: false, lock: !!f.lock })));
    }
    if (Array.isArray(pay.manual)) {
      MANUAL = pay.manual.map(m => ({ name: m.name, cr: m.cr }));
    }
    if (Array.isArray(pay.doneElectives)) {
      DONE_ME = pay.doneElectives.map(d => ({ code: d.code, name: d.name, cr: d.cr }));
    }
    PATHWAY_SELECTIONS = pay.pathwaySelections && typeof pay.pathwaySelections === 'object'
      ? Object.assign({}, pay.pathwaySelections) : {};
    PATHWAY_NOTES = pay.pathwayNotes && typeof pay.pathwayNotes === 'object'
      ? Object.assign({}, pay.pathwayNotes) : {};
    if (Array.isArray(pay.choiceGroups)) {
      CHOICE_GROUPS = pay.choiceGroups.map(g => ({ kind: g.kind, members: g.members.slice(), min_credits: g.min_credits }));
    }
    PROFILE_AUDIT_OVERRIDES = Array.isArray(pay.auditRequirements) ? pay.auditRequirements.map(r => Object.assign({}, r)) : [];
    ADVISEMENT_INFO = pay.advisement && typeof pay.advisement === 'object'
      ? JSON.parse(JSON.stringify(pay.advisement)) : null;
    if (pay.creditPolicy) {
      const set = (k, v) => { const e = $(k); if (e && v != null) e.value = v; };
      set('credMin', pay.creditPolicy.min); set('credTarget', pay.creditPolicy.target);
      set('overloadCeiling', pay.creditPolicy.overloadCeiling);
      if ($('overloadOn')) $('overloadOn').checked = !!pay.creditPolicy.overloadOn;
      if ($('extensionEligible')) $('extensionEligible').checked = !!pay.creditPolicy.eligibilityConfirmed;
      if ($('advisorRecommended')) $('advisorRecommended').checked = !!pay.creditPolicy.advisorRecommended;
      if ($('deanApproved')) $('deanApproved').checked = !!pay.creditPolicy.deanApproved;
      toggleOverload();
    }
    if (Array.isArray(pay.courses)) {
      PICK = {}; PRIO = {}; LIVE = {};
      pay.courses.forEach(c => {
        if (!BY[c.code]) return;                       // course no longer offered
        PICK[c.code] = { want: 5, pkg: c.pkg || 0 };
        if (c.priority) PRIO[c.code] = c.priority;
        if (c.liveBidders != null) LIVE[c.code] = { bidders: c.liveBidders, at: null, round: null };
      });
    }
    if (pay.assumptions) {
      const set = (k, v) => { const e = $(k); if (e && v != null) e.value = v; };
      set('bidPosture', pay.assumptions.posture || 'balanced');
      set('bidReserve', pay.assumptions.reservePercent == null ? 20 : pay.assumptions.reservePercent);
    }
    applyProgrammeCategories(); recalc(); renderFixed(); renderPick(); renderChosen(); renderChoiceGroups();
    if (typeof renderAdvisementInfo === 'function') renderAdvisementInfo();
  } catch (e) { /* a corrupt plan must never block boot */ }
}

/* ---------------- timetable revision detection ----------------
   Spec: "Do not silently mutate a saved schedule underneath the student."
   Only a definite fact (a saved course code no longer exists in the active
   catalog) is asserted specifically; anything else is a general "review
   before trusting this" notice, since detecting a moved-but-still-valid
   package needs the old catalog for comparison, which the browser doesn't
   have - that comparison already happened once, server-side, and is
   recorded in docs/TIMETABLE_REVISION_DIFF_2026-08-04.md. */
function revalidatePlanAgainstDataset() {
  const box = $('datasetBanner'); if (!box) return;
  if (!DATASET_INFO) { box.style.display = 'none'; return; }
  const id = PLANS.activeId();
  const p = id ? PLANS.get(id) : null;
  if (!p || !p.payload) { box.style.display = 'none'; return; }
  const pay = p.payload;
  const fixedCodes = (pay.fixed || []).map(f => f.code);
  const wishCodes = (pay.courses || []).map(c => c.code);
  const allCodes = [...new Set([...fixedCodes, ...wishCodes])];
  if (!allCodes.length) { box.style.display = 'none'; return; }
  if (pay.datasetVersion && pay.datasetVersion === DATASET_INFO.active_version) {
    box.style.display = 'none';
    return;
  }

  const missing = allCodes.filter(c => !BY[c]);
  let msg;
  if (missing.length) {
    msg = `${missing.length} selected course${missing.length === 1 ? '' : 's'} `
        + `${missing.length === 1 ? 'is' : 'are'} no longer in the timetable `
        + `(${missing.slice(0, 6).map(esc).join(', ')}${missing.length > 6 ? '…' : ''}).`;
  } else {
    msg = `Section times, rooms, or instructors may have moved for your selected courses.`;
  }
  box.style.display = '';
  box.className = 'flag ' + (missing.length ? 'f-bad' : 'f-warn');
  box.innerHTML = `<b>The Monsoon 2026 timetable has changed since this plan was created.</b> ${msg} `
    + `Review and regenerate your schedule before trusting a previously-saved package selection. `
    + `<button class="btn2 sm" onclick="acknowledgeDatasetRevision()">Mark reviewed</button>`;
}
function acknowledgeDatasetRevision() {
  const id = PLANS.activeId();
  if (id) { const cur = PLANS.get(id); PLANS.save(cur ? cur.name : 'Working plan', currentPlanPayload(), id); }
  const box = $('datasetBanner'); if (box) box.style.display = 'none';
}

/* ---------------- timetable update service (backend-owned poller) ----------------
   The backend polls on its own schedule (see app/timetable_updates/poller.py) -
   this code only displays status and lets the student trigger a manual check
   or review/apply a staged candidate. It deliberately does NOT run its own
   setInterval() poll loop: that would duplicate checks across tabs and hammer
   the source site, exactly what the backend-owned poller exists to avoid. */
let TT_UPDATE_STATUS = null;

const TT_STATE_MESSAGE = {
  idle: 'Timetable is current.',
  checking: 'Checking the timetable source…',
  not_modified: 'Checked just now. Your timetable is current.',
  source_changed_only: 'The website changed, but no course or section data changed.',
  normalizing: 'Checking the timetable source…',
  validating: 'Validating the revised timetable…',
  no_dataset_change: 'The published data changed in formatting only; your timetable is current.',
  update_available: 'A revised timetable is available.',
  applying: 'Applying the revised timetable…',
  applied: 'Timetable updated.',
  failed: 'The new timetable could not be validated, so your existing timetable was kept.',
  offline: 'The timetable source is temporarily unavailable. Your existing data remains safe.',
  rollback_available: 'Rolled back to a previous timetable version.',
};

function fmtWhen(ts) {
  if (!ts) return 'not completed yet';
  const d = new Date(ts * 1000);
  return d.toLocaleString();
}

async function refreshTimetableUpdateStatus() {
  try {
    TT_UPDATE_STATUS = await API.getTimetableUpdateStatus();
    renderTimetableUpdateBar();
    if (TT_UPDATE_STATUS.update_available) await refreshTimetableUpdateReview();
    else { const box = $('ttUpdateReview'); if (box) box.style.display = 'none'; }
  } catch (e) { /* best-effort; never block the page on this */ }
}

function renderTimetableUpdateBar() {
  const bar = $('ttUpdateBar'), text = $('ttUpdateText');
  if (!bar || !TT_UPDATE_STATUS) return;
  const s = TT_UPDATE_STATUS;
  const msg = TT_STATE_MESSAGE[s.state] || s.state;
  bar.className = 'flag ' + (s.state === 'update_available' ? 'f-warn'
    : (s.state === 'offline' || s.state === 'failed') ? 'f-bad' : 'f-ok');
  text.innerHTML = `<b>${esc(msg)}</b> <span class="tiny mut">last checked ${esc(fmtWhen(s.last_check_completed))}`
    + (s.next_scheduled_check ? ` · next check ${esc(fmtWhen(s.next_scheduled_check))}` : '')
    + ` · source ${esc(s.source_url)}</span>`;
  const btn = $('ttCheckBtn'); if (btn) btn.disabled = (s.state === 'checking' || s.state === 'applying');
}

async function checkTimetableNow() {
  const btn = $('ttCheckBtn'); if (btn) { btn.disabled = true; btn.textContent = 'Checking…'; }
  const text = $('ttUpdateText'); if (text) text.innerHTML = '<span class="spin"></span> Checking the timetable source…';
  try {
    await API.checkTimetableUpdate(false);
  } catch (e) {
    if (text) text.innerHTML = `<b>Could not check for a timetable update.</b> ${esc(API.humanError(e))}`;
  } finally {
    await refreshTimetableUpdateStatus();
    if (btn) { btn.disabled = false; btn.textContent = 'Check timetable now'; }
  }
}

async function checkTimetableOnOpen() {
  // A page load/refresh is a freshness event. The backend owns the lock and
  // rejects overlapping checks, so multiple open tabs cannot race the source.
  try { await API.checkTimetableUpdate(false); }
  catch (e) { /* the status call below keeps the last validated dataset visible */ }
  await refreshTimetableUpdateStatus();
}

async function refreshTimetableUpdateReview() {
  const box = $('ttUpdateReview'); if (!box) return;
  try {
    const [candidate, diff] = await Promise.all([API.getTimetableCandidate(), API.getTimetableDiff()]);
    const s = diff.summary;
    box.style.display = '';
    box.innerHTML = `<div class="card tight" style="border-color:var(--sig)"><h2>Revised timetable available</h2>
      <div class="note">Candidate <span class="mono">${esc(candidate.version_id)}</span> · retrieved
        ${esc((candidate.manifest_entry || {}).retrieved_at || '?')} ·
        ${candidate.error_count} error(s) · ${candidate.warning_count} warning(s)</div>
      <div class="grid g5" style="margin-top:8px">
        <div class="stat"><div class="k">Renamed</div><div class="v mono">${s.renamed}</div></div>
        <div class="stat u"><div class="k">Added</div><div class="v mono">${s.added}</div></div>
        <div class="stat r"><div class="k">Removed</div><div class="v mono">${s.removed}</div></div>
        <div class="stat c"><div class="k">Changed</div><div class="v mono">${s.changed}</div></div>
        <div class="stat"><div class="k">Unchanged</div><div class="v mono">${s.unchanged}</div></div>
      </div>
      <div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap">
        <button class="btn" onclick="void applyTimetableUpdateAction('${esc(candidate.version_id)}','${esc(candidate.dataset_checksum)}')">Apply update</button>
        <button class="btn2" onclick="void discardTimetableCandidateAction('${esc(candidate.version_id)}')">Discard candidate</button>
        <button class="btn2" onclick="downloadTimetableDiff()">Download diff</button>
      </div>
      <div id="ttDiffDetail" style="margin-top:10px"></div></div>`;
    window._TT_LAST_DIFF = diff;
  } catch (e) {
    box.style.display = 'none';
  }
}

function downloadTimetableDiff() {
  if (!window._TT_LAST_DIFF) return;
  download('timetable-diff.json', JSON.stringify(window._TT_LAST_DIFF, null, 2), 'application/json');
}

let TT_CHANGELOG_LOADED = false;
/* Public, always-available history of every revision the University has
   actually published - reads dataset_manifest.json's persisted version
   list via GET /timetable-updates/changelog, not this process's own
   in-memory check log (renderTimetableUpdateBar's #ttUpdateReview only ever
   shows the current candidate, if any). Added 2026-08-09/10 after the
   active dataset was found silently sitting 5+ days behind the live site
   because nobody had been reviewing staged candidates - this makes every
   past change visible without anyone having to remember to check. Lazy: the
   <details> element only calls this on first expand, not on every page load. */
async function renderTimetableChangelog() {
  if (TT_CHANGELOG_LOADED) return;
  const box = $('ttChangelog'); if (!box) return;
  try {
    const { changelog } = await API.getTimetableChangelog();
    TT_CHANGELOG_LOADED = true;
    if (!changelog.length) { box.innerHTML = 'No revision history yet.'; return; }
    box.innerHTML = changelog.map(e => {
      const s = e.summary;
      const named = [...e.renamed_courses.map(r => `${esc(r.old_code)} → ${esc(r.new_code)} (renamed)`),
        ...e.added_courses.map(c => `${esc(c)} (added)`),
        ...e.removed_courses.map(c => `${esc(c)} (removed)`)];
      return `<div class="card tight" style="margin-top:8px">
        <div><b>${esc(e.from_version)}</b> → <b>${esc(e.to_version)}</b>
          <span class="tiny mut">${e.retrieved_at ? esc(fmtWhen(Date.parse(e.retrieved_at) / 1000)) : ''}</span></div>
        <div class="tiny mut" style="margin-top:4px">${s.renamed} renamed · ${s.added} added · ${s.removed} removed ·
          ${s.changed} changed · ${s.unchanged} unchanged</div>
        ${named.length ? `<div class="tiny" style="margin-top:4px">${named.join(' · ')}</div>` : ''}
      </div>`;
    }).join('');
  } catch (e) {
    box.innerHTML = 'Could not load the change history: ' + esc(API.humanError(e));
  }
}

async function applyTimetableUpdateAction(versionId, checksum) {
  if (!confirm(`Apply timetable ${versionId}? Your saved plans will be revalidated afterward, not silently rewritten.`)) return;
  try {
    await API.applyTimetableUpdate(versionId, checksum);
    await refreshTimetableUpdateStatus();
    DATASET_INFO = await API.getDataset();
    revalidatePlanAgainstDataset();
    alert('Timetable updated. Review the banner above if your plan needs attention.');
  } catch (e) {
    alert('Could not apply the update: ' + API.humanError(e));
  }
}

async function discardTimetableCandidateAction(versionId) {
  if (!confirm(`Discard candidate ${versionId}? You can check again later.`)) return;
  try {
    await API.discardTimetableCandidate(versionId);
    await refreshTimetableUpdateStatus();
  } catch (e) {
    alert('Could not discard: ' + API.humanError(e));
  }
}

/* ---------------- §10/§11 plan toolbar ---------------- */
function planMsg(text, bad) {
  const e = $('planMsg');
  if (e) { e.textContent = text; e.style.color = bad ? 'var(--bad)' : 'var(--ok)'; }
}
function refreshPlanPicker() {
  const sel = $('planPicker'); if (!sel) return;
  const active = PLANS.activeId();
  sel.innerHTML = '<option value="">(working plan)</option>'
    + PLANS.list().map(p => `<option value="${esc(p.id)}"${p.id === active ? ' selected' : ''}>`
        + `${esc(p.name)}</option>`).join('');
}
function planSave() {
  const name = ($('planName') && $('planName').value.trim()) || 'Working plan';
  const rec = PLANS.save(name, currentPlanPayload(), PLANS.activeId() || undefined);
  if (!rec) { planMsg('Could not save: browser storage is full or unavailable.', true); return; }
  refreshPlanPicker(); planMsg('Saved "' + rec.name + '".');
}
function planDuplicate() {
  const id = PLANS.activeId();
  if (!id) { planMsg('Save the plan first, then duplicate it.', true); return; }
  const rec = PLANS.duplicate(id);
  if (!rec) { planMsg('Could not duplicate.', true); return; }
  refreshPlanPicker(); planMsg('Duplicated as "' + rec.name + '".');
}
function planDelete() {
  const id = PLANS.activeId();
  if (!id) { planMsg('Nothing to delete.', true); return; }
  const p = PLANS.get(id);
  if (!confirm('Delete plan "' + (p ? p.name : id) + '"? This cannot be undone.')) return;
  PLANS.remove(id); refreshPlanPicker(); planMsg('Deleted.');
}
function download(name, text, mime) {
  const blob = new Blob([text], { type: mime || 'text/plain' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob); a.download = name;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}
function planExportJson() {
  const meta = RESULT ? { ruleVersion: RESULT.rule_version, modelVersion: RESULT.model_version } : {};
  download('snu-plan.json', PLANS.exportJson(currentPlanPayload(), meta), 'application/json');
  planMsg('Exported JSON.');
}
function planExportCsv() {
  if (!RESULT) { planMsg('Build a strategic bid plan first, then export it.', true); return; }
  const recs = (RESULT.courses || []).map(r => ({ ...r, credits: (BY[r.code] || {}).cr }));
  download('snu-strategic-bid-plan.csv', PLANS.exportCsv(recs), 'text/csv');
  planMsg('Exported CSV.');
}
function planPrint() {
  const txt = PLANS.printableSummary(currentPlanPayload(), RESULT);
  const w = window.open('', '_blank');
  if (!w) { planMsg('Pop-up blocked; allow pop-ups to print.', true); return; }
  w.document.write('<pre style="font:12px/1.5 monospace;white-space:pre-wrap">'
    + txt.replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c])) + '</pre>');
  w.document.close();
}
/* ---------------- "Load my existing work": preview before committing ----------------
   Accepts the same file shape planExportJson() produces and the private
   profile bootstrap (tools/import_personal_profile.py) uses - both are
   PLANS.exportJson()'s shape, so no separate parser is needed. Spec: "Do not
   silently overwrite current work" - shows counts and the dataset version it
   was built against before anything is committed, and offers a backup
   download of the CURRENT plan first if one exists. */
let PENDING_IMPORT = null;
let PENDING_ADVISEMENT = null;

async function planImport(input) {
  const f = input && input.files && input.files[0];
  if (!f) return;
  if (/\.pdf$/i.test(f.name) || f.type === 'application/pdf') {
    await advisementImport(f);
    input.value = '';
    return;
  }
  if (f.size > 2 * 1024 * 1024) { planMsg('File is larger than the 2 MB limit.', true); input.value = ''; return; }
  const text = await f.text();
  const v = PLANS.validateImport(text);
  if (!v.ok) {
    planMsg('Import rejected: ' + v.errors.slice(0, 3).join('; ')
            + (v.errors.length > 3 ? ' (+' + (v.errors.length - 3) + ' more)' : ''), true);
    input.value = ''; return;
  }
  PENDING_IMPORT = { text, name: f.name.replace(/\.json$/i, ''), warnings: v.warnings };
  renderImportPreview(v.payload || {});
  input.value = '';
}

function fileAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error('The file could not be read.'));
    reader.onload = () => resolve(String(reader.result || '').split(',')[1] || '');
    reader.readAsDataURL(file);
  });
}

async function advisementImport(file) {
  if (file.size > 8 * 1024 * 1024) { planMsg('PDF is larger than the 8 MB limit.', true); return; }
  if (!BACKEND_OK) { await probeHealth(); if (!BACKEND_OK) { planMsg('Connect to the calculation service before importing a PDF.', true); return; } }
  const box = $('importPreviewBox');
  if (box) box.innerHTML = '<div class="card tight"><span class="spin"></span> Reading the advisement report locally through the calculation service…</div>';
  try {
    const parsed = await API.parseAdvisementReport(file.name, await fileAsBase64(file));
    PENDING_ADVISEMENT = parsed;
    renderAdvisementPreview(parsed);
  } catch (e) {
    if (box) box.innerHTML = `<div class="flag f-bad"><b>PDF import failed.</b> ${esc(API.humanError(e))}</div>`;
    planMsg('The PDF was not imported.', true);
  }
}

function renderAdvisementPreview(a) {
  const box = $('importPreviewBox'); if (!box) return;
  const oldDone = +($('doneCr') ? $('doneCr').value : 0) || 0;
  const newDone = Number((a.totals || {}).used) || 0;
  const mismatch = oldDone && Math.abs(oldDone - newDone) > 0.01;
  const ipCredits=(a.in_progress_courses||[]).reduce((n,c)=>n+(+c.credits||0),0);
  const inferredPending=oldDone>newDone?Math.min(ipCredits,Math.max(0,oldDone-newDone)):0;
  const inferredTransfer=oldDone>newDone?Math.max(0,oldDone-newDone-inferredPending):0;
  const s = a.student || {};
  box.innerHTML = `<div class="card tight" style="border-color:var(--sig)"><h2>Review advisement PDF before importing</h2>
    <div class="note">The PDF was parsed in memory and was not stored by the service. Confirming creates a new local plan; it does not overwrite the current one.</div>
    <table style="margin-top:8px"><tbody>
      <tr><td>Student</td><td>${esc(s.name || 'not detected')}</td></tr>
      <tr><td>Programme</td><td>${esc(a.programme_title || 'not detected')}</td></tr>
      <tr><td>Report date</td><td>${esc(s.report_date || 'not detected')}</td></tr>
      <tr><td>Completed courses used by requirements</td><td class="num">${a.completed_courses.length}</td></tr>
      <tr><td>Credits used / required</td><td class="num">${f1((a.totals || {}).used || 0)} / ${f1((a.totals || {}).required || 0)}</td></tr>
      <tr><td>Failed attempts excluded</td><td class="num">${a.failed_courses.length}</td></tr>
      <tr><td>In-progress courses excluded</td><td class="num">${a.in_progress_courses.length}</td></tr>
    </tbody></table>
    ${mismatch ? `<div class="flag f-warn" style="margin-top:8px"><b>The report is a partial view.</b> Your current profile says ${f1(oldDone)} planning credits while this report shows ${f1(newDone)} used. The importer preserves the existing total by separating omitted transfer and pending credits; review the two fields below.</div>` : ''}
    <div class="grid g2" style="margin-top:8px">
      <div><label>Accepted transfer credits not shown</label><input id="pdfTransferCredits" type="number" min="0" step="0.5" value="${f1(inferredTransfer)}"></div>
      <div><label>Confirmed supplemental / summer credits</label><input id="pdfPendingCredits" type="number" min="0" step="0.5" value="${f1(inferredPending)}"></div>
    </div>
    <div class="tiny mut" style="margin-top:5px">Failed courses are always excluded. In-progress credits count only when you explicitly confirm them here.</div>
    <div class="flag f-q" style="margin-top:8px">${(a.warnings || []).map(esc).join(' ')}</div>
    <div style="margin-top:10px;display:flex;gap:8px"><button class="btn" onclick="void confirmAdvisementImport()">Create plan from this report</button>
      <button class="btn2" onclick="cancelPlanImport()">Cancel</button></div></div>`;
}

async function confirmAdvisementImport() {
  const a = PENDING_ADVISEMENT; if (!a) return;
  const payload = currentPlanPayload();
  const sg = a.profile_suggestions || {}, student = a.student || {};
  const reportUsed=Number((a.totals||{}).used)||0;
  const transferred=Math.max(0,+($('pdfTransferCredits')?$('pdfTransferCredits').value:0)||0);
  const pending=Math.max(0,+($('pdfPendingCredits')?$('pdfPendingCredits').value:0)||0);
  const planningTotal=reportUsed+transferred+pending;
  const preferExisting=(current,suggested)=>Number(current)>0?Number(current):(suggested==null?current:suggested);
  // Derive the student's actual year/semester from the report's own "Bachelor of
  // Technology Program Monsoon <cohort_year>" line - this app is itself scoped to
  // one specific semester (Monsoon 2026, see RULE_VERSION/DATASET_VERSION and the
  // page's own title), so that reference point is the tool's documented scope, not
  // a personal fact. Previously this hardcoded every imported report to model:'y4',
  // semester:7 regardless of what the report actually said - a 2nd or 3rd-year
  // student's own advisement report would have been silently mis-modelled as a
  // graduating 4th-year's. Falls back to leaving the existing profile value alone
  // (never a guessed year) when the report has no parseable cohort year.
  const CURRENT_MONSOON_YEAR = 2026;
  let derivedModel = payload.profile.model, derivedSemester = payload.profile.semester;
  if (Number(a.cohort_year) > 0) {
    const yearsIn = CURRENT_MONSOON_YEAR - Number(a.cohort_year);   // 0 = just admitted this Monsoon
    derivedSemester = yearsIn * 2 + 1;
    derivedModel = derivedSemester >= 7 ? 'y4' : (derivedSemester === 5 ? 'y3' : (derivedSemester >= 2 ? 'y2' : payload.profile.model));
  }
  payload.profile = Object.assign({}, payload.profile, {
    model: derivedModel, semester: derivedSemester, creditCap: 25,
    programme: a.programme_id || payload.profile.programme,
    cohortYear: a.cohort_year || payload.profile.cohortYear,
    completedCoursesText: (a.completed_courses || []).map(c =>
      `${c.code} | ${c.credits} | ${c.category} | ${String(c.title || '').replace(/\|/g, '/')}`).join('\n'),
    doneCr: planningTotal,
    degCr: sg.degree_credits == null ? payload.profile.degCr : sg.degree_credits,
    remCore: preferExisting(payload.profile.remCore,sg.remaining_major_core),
    remME: preferExisting(payload.profile.remME,sg.remaining_major_elective),
    remUWE: preferExisting(payload.profile.remUWE,sg.remaining_uwe),
    remCCC: preferExisting(payload.profile.remCCC,sg.remaining_ccc),
    floater: preferExisting(payload.profile.floater,sg.remaining_floater),
    remOther: preferExisting(payload.profile.remOther,sg.remaining_project),
  });
  payload.creditPolicy = { min: 12, target: 22, overloadOn: false, overloadCeiling: 27,
    eligibilityConfirmed: false, advisorRecommended: false, deanApproved: false };
  payload.advisement = {
    filename: a.filename, reportDate: student.report_date || null, cgpa: student.cgpa,
    name: student.name || null, rollNumber: student.roll_number || null,
    totals: a.totals, inProgress: a.in_progress_courses, failed: a.failed_courses,
    reportVisibleUsed: reportUsed, transferredCredits: transferred, pendingCredits: pending,
    planningTotal, requirementProgress: Object.fromEntries((a.requirements||[]).map(r=>[r.id,{used:r.used,required:r.required,needed:r.needed}])),
    warnings: a.warnings || []
  };
  const label = student.roll_number ? `Advisement ${student.roll_number}` : a.filename.replace(/\.pdf$/i, '');
  const rec = PLANS.save(label, payload);
  if (!rec) { planMsg('Could not create the local plan.', true); return; }
  PLANS.setActive(rec.id); restoreActivePlan(); refreshPlanPicker();
  PENDING_ADVISEMENT = null; PENDING_IMPORT = null;
  if ($('importPreviewBox')) $('importPreviewBox').innerHTML = '';
  planMsg(`Created "${rec.name}" from the advisement report.`);
  await refreshPoolsFromBackend();
  await runDegreeAudit();
}

function renderImportPreview(p) {
  const box = $('importPreviewBox'); if (!box) return;
  const hasCurrentWork = Object.keys(PICK).length > 0 || FIXED.some(f => !f.auto);
  const dv = p.datasetVersion || 'unknown (predates timetable version tracking)';
  const dvMismatch = DATASET_INFO && p.datasetVersion && p.datasetVersion !== DATASET_INFO.active_version;
  box.innerHTML = `<div class="card tight" style="border-color:var(--sig)"><h2>Review before importing</h2>
    <table><tbody>
      <tr><td>Fixed courses</td><td class="num">${(p.fixed || []).length}</td></tr>
      <tr><td>Wishlist courses</td><td class="num">${(p.courses || []).length}</td></tr>
      <tr><td>Choice groups</td><td class="num">${(p.choiceGroups || []).length}</td></tr>
      <tr><td>Completed electives on file</td><td class="num">${(p.doneElectives || []).length}</td></tr>
      <tr><td>Credit policy included</td><td>${p.creditPolicy ? 'yes' : 'no'}</td></tr>
      <tr><td>Built for timetable version</td><td class="tiny">${esc(dv)}</td></tr>
    </tbody></table>
    ${dvMismatch ? `<div class="flag f-warn" style="margin-top:8px">This was built against a different
      timetable version than the one active now. Courses/packages will be revalidated after import.</div>` : ''}
    ${hasCurrentWork ? `<div class="flag f-bad" style="margin-top:8px"><b>You have existing work in this
      browser.</b> Importing creates a separate new plan - it will not overwrite your current one - but
      switching to it will make it your working plan. <button class="btn2 sm"
      onclick="planExportJson()">Download a backup of my current plan first</button></div>` : ''}
    <div style="margin-top:10px;display:flex;gap:8px">
      <button class="btn" onclick="void confirmPlanImport()">Import as a new plan</button>
      <button class="btn2" onclick="cancelPlanImport()">Cancel</button>
    </div></div>`;
}

async function confirmPlanImport() {
  if (!PENDING_IMPORT) return;
  const r = PLANS.importAsNewPlan(PENDING_IMPORT.text, PENDING_IMPORT.name);
  if (!r.ok) { planMsg('Import failed: ' + (r.errors || []).join('; '), true); cancelPlanImport(); return; }
  PLANS.setActive(r.plan.id);
  restoreActivePlan(); refreshPlanPicker();
  revalidatePlanAgainstDataset();
  planMsg('Imported "' + r.plan.name + '"' + (PENDING_IMPORT.warnings && PENDING_IMPORT.warnings.length
          ? ' (' + PENDING_IMPORT.warnings.join('; ') + ')' : '') + '.');
  cancelPlanImport();
}
/* ---------------- course outlines (Academic Office PDFs) ---------------- */
let OUTLINE_RETURN_FOCUS = null;

function outlineField(v) {
  // The Office's own forms use "None"/"NA"/"N/A" for a genuinely-answered
  // blank; treat those the same as null rather than printing them as if they
  // were real content.
  if (v === null || v === undefined) return null;
  const s = String(v).trim();
  return (!s || /^(none|na|n\/a)$/i.test(s)) ? null : s;
}

function renderOutlineWeeks(weeklyModules) {
  if (!weeklyModules) return '';
  const weeks = Object.keys(weeklyModules).map(Number).filter(n => Number.isFinite(n)).sort((a, b) => a - b);
  if (!weeks.length) return '';
  // Consecutive weeks sharing identical text describe one module; collapse
  // them into a single row instead of repeating the same paragraph N times.
  const rows = [];
  weeks.forEach(w => {
    const text = outlineField(weeklyModules[w]); if (!text) return;
    const last = rows[rows.length - 1];
    if (last && last.text === text && w === last.to + 1) last.to = w;
    else rows.push({ from: w, to: w, text });
  });
  return `<div class="outline-section"><h3>Weekly modules</h3><div class="outline-weeks">` +
    rows.map(r => `<div class="outline-week"><b>Wk ${r.from === r.to ? r.from : r.from + '–' + r.to}</b><span>${esc(r.text)}</span></div>`).join('') +
    `</div></div>`;
}

function renderOutlineGrading(outline) {
  const comps = Array.isArray(outline.assessment_components) ? outline.assessment_components : [];
  const notes = outlineField(outline.grading_notes);
  const type = outlineField(outline.grading_type);
  if (!comps.length && !notes && !type) return '';
  let h = '<div class="outline-section"><h3>Grading</h3>';
  if (type) h += `<p><span class="outline-tag">${esc(type)} grading</span></p>`;
  if (comps.length) {
    h += `<table class="outline-grade-table"><thead><tr><th>Component</th><th>Weight</th></tr></thead><tbody>` +
      comps.map(c => `<tr><td>${esc(c.component || '—')}</td><td>${c.weightage_pct != null ? c.weightage_pct + '%' : '—'}</td></tr>`).join('') +
      `</tbody></table>`;
  }
  if (notes) h += `<p>${esc(notes)}</p>`;
  return h + '</div>';
}

function renderOutlineBody(outline) {
  const title = outlineField(outline.title_from_outline) || (BY[outline.code] ? BY[outline.code].title : outline.code);
  $('outlineTitle').textContent = title;
  $('outlineCode').textContent = 'Course outline · ' + outline.code;
  const tags = [];
  if (outlineField(outline.credits_from_outline) != null) tags.push(outline.credits_from_outline + ' credits');
  if (outlineField(outline.semester)) tags.push(outline.semester);
  if (outlineField(outline.method_of_instruction)) tags.push(outline.method_of_instruction);
  if (outlineField(outline.seats_from_outline)) tags.push(outline.seats_from_outline + ' seats (outline)');
  if (outlineField(outline.department)) tags.push(outline.department);
  const para = (label, field) => { const v = outlineField(outline[field]); return v ? `<div class="outline-section"><h3>${esc(label)}</h3><p>${esc(v)}</p></div>` : ''; };
  const faculty = outlineField(outline.faculty);
  const facultyEmail = outlineField(outline.faculty_email);
  let h = `<div class="outline-meta">${tags.map(t => `<span class="outline-tag">${esc(t)}</span>`).join('')}</div>`;
  if (faculty) h += `<div class="outline-section"><h3>Faculty</h3><p>${esc(faculty)}${facultyEmail ? ' · <a href="mailto:' + esc(facultyEmail) + '">' + esc(facultyEmail) + '</a>' : ''}</p></div>`;
  h += para('Prerequisites', 'prerequisites');
  h += para('Introduction', 'introduction');
  h += para('Objectives', 'objectives');
  h += para('Learning outcomes', 'learning_outcomes');
  h += para('Skill development', 'skill_development');
  h += para('Programme learning goals', 'program_learning_goals');
  h += renderOutlineWeeks(outline.weekly_modules);
  h += renderOutlineGrading(outline);
  h += para('Textbooks & references', 'textbooks');
  if (!h.trim()) h = '<div class="note">The Office\'s outline for this course did not contain any readable content beyond its code and title.</div>';
  $('outlineBody').innerHTML = h;
}

async function openCourseOutline(code) {
  const backdrop = $('outlineBackdrop'); if (!backdrop) return;
  OUTLINE_RETURN_FOCUS = document.activeElement;
  backdrop.hidden = false; document.body.style.overflow = 'hidden';
  $('outlineTitle').textContent = BY[code] ? BY[code].title : code;
  $('outlineCode').textContent = 'Course outline · ' + code;
  $('outlineBody').innerHTML = '<div class="note">Loading…</div>';
  if (OUTLINE_CACHE[code]) { renderOutlineBody(OUTLINE_CACHE[code]); return; }
  try {
    const outline = await API.getCourseOutline(code);
    OUTLINE_CACHE[code] = outline;
    if (!backdrop.hidden) renderOutlineBody(outline);
  } catch (e) {
    if (!backdrop.hidden) $('outlineBody').innerHTML = `<div class="flag f-bad">${esc(API.humanError(e))}</div>`;
  }
}
function closeCourseOutline() {
  const backdrop = $('outlineBackdrop'); if (!backdrop || backdrop.hidden) return;
  backdrop.hidden = true; document.body.style.overflow = '';
  if (OUTLINE_RETURN_FOCUS?.focus) OUTLINE_RETURN_FOCUS.focus();
}

function cancelPlanImport() {
  PENDING_IMPORT = null;
  PENDING_ADVISEMENT = null;
  const box = $('importPreviewBox'); if (box) box.innerHTML = '';
}

if (typeof document !== 'undefined') {
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => void boot());
  else void boot();
}
