/* =====================================================================
   BACKEND-DRIVEN GLUE  (§4, §5, §6, §8, §9, §12)

   This file replaces the old in-browser simulation engine. Everything
   authoritative is now an API call:

     pools          -> POST /api/v1/pools
     validation     -> POST /api/v1/validate-plan
     simulation     -> POST /api/v1/simulations  (+ SSE progress)
     cancellation   -> POST /api/v1/simulations/{id}/cancel
     result         -> GET  /api/v1/simulations/{id}/result
     stress test    -> POST /api/v1/simulations/{id}/stress-test
     settlement     -> POST /api/v1/settlement
     rules          -> GET  /api/v1/rules

   The browser keeps only: form state, course selection, schedule display,
   clash previews, formatting, progress rendering and export controls.
   ===================================================================== */

let LIVE = {}, PRIO = {}, USERPOP = {};
let LAST_JOB_ID = null;   // job id of the last COMPLETED run, for the stress test
let RULES_CACHE = null;   // declared here to avoid a temporal-dead-zone error at boot
let RESULT = null;           // last completed backend result
let CURRENT_JOB = null;      // job id of the in-flight run
let UNSUB = null;            // progress unsubscribe
let BACKEND_OK = null;       // null = unknown, true/false = last health probe
let HEALTH_TIMER = null;
let RUNNING = false;
let DATASET_INFO = null;     // active institutional timetable dataset identity (see /api/v1/dataset)

function curMode() { const e = $('compMode'); return e ? e.value : 'HIGH'; }
function curMethod() { const e = $('robustMethod'); return e ? e.value : 'minimax'; }
function curBudgetMode() { const e = $('budgetMode'); return e ? e.value : 'SHARED_LIVE'; }
function prioOf(code) { return PRIO[code] || 'STRONG'; }

/* ---------------- plan assembly for the API ---------------- */
function randomizeSeed() {
  // A UI convenience only: picks a fresh seed value to try. The compute path itself never
  // uses unseeded randomness (design decision - see CLAUDE.md §5.6) - whatever seed ends up
  // in the field afterward still produces a fully deterministic, reproducible result.
  const e = $('seed'); if (!e) return;
  e.value = Math.floor(Math.random() * 90000000) + 10000000;
  e.dispatchEvent(new Event('change'));
}

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
    trials: +($('nsim') ? $('nsim').value : 8000),
    seed: +($('seed') ? $('seed').value : 20260802),
    headlineMode: curMode(), budgetMode: curBudgetMode(), robustMethod: curMethod(),
    dispersion: +($('disp') ? $('disp').value : 0.18),
    extraScenarios: extraScenarios()
  };
}
function extraScenarios() {
  const picked = ['showLow', 'showModerate', 'showOptimistic']
    .filter(id => $(id) && $(id).checked)
    .map(id => ({ showLow: 'LOW', showModerate: 'MODERATE', showOptimistic: 'OPTIMISTIC' }[id]));
  // The headline scenario must always be one of the simulated tiers - if it's a
  // comparison-only mode whose own checkbox happens to be unchecked, include it
  // anyway rather than sending a request the backend would reject.
  const headline = curMode();
  if (['LOW', 'MODERATE', 'OPTIMISTIC'].includes(headline) && !picked.includes(headline)) picked.push(headline);
  return picked;
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
        + `rules ${esc((info && info.rule_version) || '?')} · model ${esc((info && info.model_version) || '?')}`
        + ` · ${esc(API.getBase() || 'same origin')}</span>`;
    } else {
      bar.className = 'flag f-bad';
      bar.innerHTML = `<b>The calculation service is unavailable.</b> Your plan is safe, but simulations and
        bid recommendations cannot run until the service reconnects.
        <button class="btn2 sm" onclick="void retryBackend()" style="margin-left:8px">Retry connection</button>
        <span class="tiny mut" id="healthNote" style="margin-left:8px"></span>`;
    }
  }
  ['runBtn', 'stressBtn'].forEach(id => {
    const b = $(id);
    if (b) {
      b.disabled = !ok || RUNNING;
      b.title = ok ? '' : 'Unavailable while the calculation service is disconnected';
    }
  });
  if (changed && ok) { void refreshPoolsFromBackend(); }
}

async function probeHealth() {
  const h = await API.healthCheck();
  setBackendState(!!h.ok, h);
  return h;
}
async function retryBackend() {
  const n = $('healthNote'); if (n) n.textContent = 'checking…';
  const h = await probeHealth();
  if (!h.ok && n) n.textContent = 'still unreachable at ' + new Date().toLocaleTimeString();
}
function startHealthLoop() {
  if (HEALTH_TIMER) clearInterval(HEALTH_TIMER);
  // controlled interval: no request spamming
  HEALTH_TIMER = setInterval(() => { if (!RUNNING) void probeHealth(); }, 15000);
}

/* ---------------- pools now come from the backend ---------------- */
async function refreshPoolsFromBackend() {
  if (!BACKEND_OK) return;
  try {
    const p = await API.calculateProfileBudget({
      model: $('model') ? $('model').value : 'y4',
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
function setPhase(text, indeterminate) {
  const s = $('optStat');
  if (s) s.innerHTML = (indeterminate ? '<span class="spin" aria-hidden="true"></span> ' : '') + esc(text);
  const pb = $('progBar');
  if (pb) {
    pb.style.display = 'block';
    pb.setAttribute('aria-valuetext', text);
    if (indeterminate) { pb.classList.add('indet'); pb.removeAttribute('aria-valuenow'); }
  }
}
function setProgress(pct, phase) {
  const pb = $('progBar'), fill = $('progFill');
  if (pb) {
    pb.classList.remove('indet');
    pb.setAttribute('aria-valuenow', String(Math.round(pct)));
    pb.setAttribute('aria-valuetext', phase || (Math.round(pct) + '%'));
  }
  if (fill) fill.style.width = Math.max(2, Math.min(100, pct)) + '%';
  const s = $('optStat');
  if (s) s.textContent = `${Math.round(pct)}% — ${phase || 'running'}`;
}
function clearProgress() {
  const pb = $('progBar'); if (pb) { pb.style.display = 'none'; pb.classList.remove('indet'); }
  const fill = $('progFill'); if (fill) fill.style.width = '0%';
}
function setRunning(on) {
  RUNNING = on;
  const run = $('runBtn'), cancel = $('cancelBtn');
  if (run) { run.disabled = on || !BACKEND_OK; run.textContent = on ? 'Running…' : 'Run simulation'; }
  if (cancel) cancel.disabled = !on;
}

async function runOpt() {
  if (RUNNING) return;                                   // no duplicate submission
  if (!Object.keys(PICK).length) {
    $('bidOut').innerHTML = '<div class="card"><div class="note">Choose some courses first on the Course picker tab.</div></div>';
    return;
  }
  if (!BACKEND_OK) { await probeHealth(); if (!BACKEND_OK) return; }

  setRunning(true);
  // §6: an immediate indeterminate indicator, before any backend event
  setPhase('Validating plan', true);
  $('bidOut').innerHTML = '<div class="card"><div class="note" id="runNote">Preparing…</div></div>';

  try {
    const out = await API.runSimulation(buildPlan(), {
      onPhase: t => setPhase(t, true),
      onValidated: v => {
        const note = $('runNote');
        if (note) {
          note.innerHTML = 'Plan accepted: ' + v.course_count + ' course(s), '
            + v.scenarios + ' stress scenarios, ' + v.trials.toLocaleString() + ' trials each.'
            + (v.warnings && v.warnings.length
                ? '<br><span style="color:var(--sig)">' + v.warnings.map(esc).join('<br>') + '</span>' : '');
        }
      },
      onJob: j => { CURRENT_JOB = j.job_id; setPhase(j.cache_hit ? 'Loading cached result' : 'Starting worker', true); },
      onProgress: s => {
        const p = s.progress || {};
        if (p.percent > 0) setProgress(p.percent, p.phase);
        else setPhase(p.phase || s.state, true);
      },
      onWarning: e => {
        const n = $('runNote');
        if (n) n.innerHTML += '<br><span style="color:var(--sig)">Connection interrupted; still retrying…</span>';
      }
    });

    if (out.stale) { clearProgress(); return; }           // a newer run superseded this one
    if (out.cancelled) {
      clearProgress();
      $('bidOut').innerHTML = `<div class="card"><div class="flag f-warn">
        <b>Simulation cancelled.</b> No result was applied to your plan.</div></div>`;
      return;
    }
    RESULT = out.result;
    LAST_JOB_ID = out.jobId;
    clearProgress();
    drawResult(RESULT);
    const st = $('optStat');
    if (st) {
      st.textContent = `${Math.round(RESULT.runtime_ms)} ms server-side · `
        + `${RESULT.trials.toLocaleString()} trials × ${RESULT.scenarios_run.length} scenarios · `
        + `seed ${RESULT.seed}${RESULT.cache_hit ? ' · served from cache' : ''}`;
    }
    void autosaveNow();
    void stressPlan();  // stress card sits right below on the merged Bid simulator tab now
  } catch (e) {
    clearProgress();
    if (e instanceof API.ApiError && (e.kind === 'offline' || e.kind === 'timeout')) setBackendState(false);
    $('bidOut').innerHTML = `<div class="card"><div class="flag f-bad">
      <b>Could not complete the simulation.</b><br>${esc(API.humanError(e))}
      <br><button class="btn2 sm" style="margin-top:8px" onclick="void runOpt()">Retry</button></div></div>`;
  } finally {
    setRunning(false);
    CURRENT_JOB = null;
  }
}

/* ---------------- §5 real cancellation ---------------- */
async function cancelRun() {
  // frontend responds immediately, regardless of network latency
  API.invalidateRuns();
  setPhase('Cancelling…', true);
  const jid = CURRENT_JOB;
  setRunning(false);
  clearProgress();
  $('bidOut').innerHTML = `<div class="card"><div class="flag f-warn">
    <b>Simulation cancelled.</b> No result was applied to your plan.</div></div>`;
  if (!jid) return;
  try {
    const r = await API.cancelSimulation(jid);
    const st = $('optStat');
    if (st) st.textContent = `cancelled · backend acknowledged in ${r.ack_ms} ms`;
  } catch (e) {
    const st = $('optStat');
    if (st) st.textContent = 'cancelled locally; the backend did not confirm (' + API.humanError(e) + ')';
  }
}

/* ---------------- results rendering ---------------- */
function probLabel(p) {
  if (p == null) return '—';
  if (p >= 0.9999) return '&gt;99.9% <span class="tiny mut">in this model</span>';
  if (p <= 0.0001) return '&lt;0.1% <span class="tiny mut">in this model</span>';
  return (p * 100).toFixed(p > 0.995 || p < 0.005 ? 1 : 0) + '%';
}
const PRIO_LABEL = { MUST: 'Must have', STRONG: 'Strongly preferred', BACKUP: 'Useful backup', OPTIONAL: 'Optional' };

function drawResult(res) {
  const CL = { ME: 'p-me', UWE: 'p-uwe', CCC: 'p-ccc' };
  let h = `<div class="card" style="border-color:var(--sig)"><h2>What to enter</h2>
    <div class="note" style="margin-bottom:10px">${esc(res.disclaimer)}</div>
    <table><thead><tr><th>Course</th><th>Priority</th><th>Enter</th><th>Range</th>
      <th>Worst tested</th><th>Modelled rivals</th><th>Status</th></tr></thead><tbody>`;
  res.recommendations.forEach(r => {
    const st = !r.target_met ? '<span class="pill p-w">target not reachable</span>'
             : (r.reduced_for_budget ? '<span class="pill p-w">cut for budget</span>'
                                     : '<span class="pill p-uwe">target met</span>');
    h += `<tr><td><b>${esc(r.code)}</b></td>
      <td><span class="pill p-m">${esc(PRIO_LABEL[r.priority] || r.priority)}</span></td>
      <td class="num" style="font-size:17px;color:var(--sig);font-weight:600">${r.bid}</td>
      <td class="num tiny">${r.bid_range[0]}&ndash;${r.bid_range[1]} <span class="mut">/ cap ${r.cap}</span></td>
      <td class="num">${probLabel(r.worst_tested)}</td>
      <td class="tiny">${r.demand.source === 'live'
          ? '<span class="pill p-uwe">live</span>' : '<span class="pill p-w">stress default</span>'}
        ${Math.round(r.demand.expected_rivals)} vs ${r.demand.seats} seats</td>
      <td>${st}</td></tr>`;
  });
  h += '</tbody></table>';
  res.allocations.forEach(a => {
    const bad = !a.feasible || (a.sacrificed || []).length;
    h += `<div class="flag ${bad ? 'f-bad' : 'f-ok'}" style="margin-top:10px"><b>${esc(a.category)}:</b>
      committing ${a.committed} of ${a.pool} points. ${esc(a.note)}
      ${(a.sacrificed || []).length ? '<br>Reduced: '
        + a.sacrificed.map(s => esc(s.code) + ' by ' + s.cut).join(', ') : ''}</div>`;
  });
  h += `<div class="tiny mut" style="margin-top:10px">rules ${esc(res.rule_version)} ·
    model ${esc(res.model_version)} · dataset ${esc(res.dataset_version)} ·
    budget rule ${esc(res.budget_mode)} ${res.budget_mode === 'INDEPENDENT'
      ? '(hypothetical comparison only)' : '(officially confirmed, rule BUDGET.SHARED_LIVE)'} ·
    ${res.trials.toLocaleString()} trials · seed ${res.seed}</div></div>`;

  /* §12 shared vs independent, from the same simulation */
  if (res.budget_comparison) {
    const bc = res.budget_comparison;
    h += `<div class="card"><div class="hd"><div><h2>Shared live pool vs independent bids</h2>
      <div class="note">${esc(bc.why_it_matters)}</div></div></div>
      <table><thead><tr><th>Interpretation</th><th>Total committed</th><th>Expected charge</th>
      <th>Feasible</th></tr></thead><tbody>
      <tr><td><b>${esc(bc.primary_mode)}</b> <span class="pill p-m">shown above</span></td>
        <td class="num">${bc.primary.total_committed}</td>
        <td class="num">${bc.primary.expected_charge}</td>
        <td>${bc.primary.allocations.every(a => a.feasible) ? 'yes' : 'no'}</td></tr>
      <tr><td><b>${esc(bc.alternate_mode)}</b></td>
        <td class="num">${bc.alternate.total_committed}</td>
        <td class="num">${bc.alternate.expected_charge}</td>
        <td>${bc.alternate.allocations.every(a => a.feasible) ? 'yes' : 'no'}</td></tr>
      </tbody></table>`;
    h += bc.courses_changed.length
      ? `<div class="flag f-warn"><b>${bc.courses_changed.length} recommendation(s) change between the two
         readings:</b><br>` + bc.courses_changed.map(c =>
           `&nbsp;&nbsp;${esc(c.code)}: ${c[bc.primary_mode.toLowerCase()]} → ${c[bc.alternate_mode.toLowerCase()]}`
         ).join('<br>') + `<br><br>${esc(bc.primary_mode)} is the officially confirmed reading (rule
         ${esc(bc.rule_id)}); ${esc(bc.alternate_mode)} is shown only as a hypothetical comparison.</div>`
      : `<div class="flag f-ok">No recommendation changes between the two readings for this plan, so the
         confirmed budget rule (${esc(bc.rule_id)}) does not affect your decision here.</div>`;
    h += '</div>';
  }

  /* per-course scenario detail */
  h += `<div class="card"><div class="hd"><div><h2>How each recommendation holds up under stress</h2>
    <div class="note">Every row is a different assumption about how many rivals turn up and how hard they bid.
    Model uncertainty dominates sampling error here.</div></div></div>`;
  res.recommendations.forEach(r => {
    h += `<div class="rec ${r.target_met ? 'top' : ''}">
      <div style="display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;margin-bottom:8px">
        <div><b>${esc(r.code)}</b> <span class="pill ${CL[r.category]}">${esc(r.category)}</span>
          <span class="pill p-m">${esc(PRIO_LABEL[r.priority] || r.priority)}</span></div>
        <div class="mono tiny">${r.demand.seats} seats · cap ${r.cap}</div></div>
      <table><thead><tr><th>Scenario</th><th>Rivals</th><th>Win at ${r.bid}</th>
        <th>At ${Math.max(0, r.bid - 1)}</th><th>At cap ${r.cap}</th></tr></thead><tbody>`;
    r.scenarios.forEach(s => {
      // A dimmed row (opacity) used to distinguish comparison-only scenarios, but that
      // fades the text along with everything else - fine when this row was opt-in and
      // rare, not fine now that Low/Moderate run by default and this row is common.
      // The pill label alone (full-contrast text) carries the same distinction.
      h += `<tr${s.comparison_only ? ' class="comparison-row"' : ''}>
        <td>${esc(s.label)}${s.comparison_only ? ' <span class="pill p-m">comparison only</span>' : ''}</td>
        <td class="num tiny">${Math.round(s.expected_rivals)}</td>
        <td class="num">${probLabel(s.win_at_bid)}</td>
        <td class="num tiny mut">${probLabel(s.win_one_below)}</td>
        <td class="num tiny">${probLabel(s.win_at_cap)}</td></tr>`;
    });
    h += `</tbody></table>
      <div class="grid g4" style="margin-top:10px">
        <div class="kv"><span>Temporary commitment</span><span style="color:var(--sig)">${r.bid}</span></div>
        <div class="kv"><span>Expected charge</span><span>${r.expected_charge}</span></div>
        <div class="kv"><span>Expected refund</span><span style="color:var(--ok)">${r.expected_refund}</span></div>
        <div class="kv"><span>Sampling error</span><span>±${(r.ci_halfwidth * 100).toFixed(2)}pp</span></div>
      </div>
      <div style="display:flex;gap:8px;align-items:center;margin-top:10px;flex-wrap:wrap">
        <label for="live-${esc(r.code)}" style="margin:0;text-transform:none;font-size:11.5px">Live bidder count from the platform</label>
        <input type="number" id="live-${esc(r.code)}" min="0" style="width:90px"
          value="${LIVE[r.code] ? LIVE[r.code].bidders : ''}" placeholder="not seen yet"
          onchange="setLive('${esc(r.code)}',this.value)">
        <button class="btn2 sm" onclick="setLive('${esc(r.code)}','')">clear</button>
        <span class="tiny mut">a real count replaces the stress assumption entirely</span></div>`;
    if (r.demand.source === 'live') {
      const obs = Math.round(r.demand.expected_rivals) + 1, margin = r.demand.seats - obs;
      h += `<div class="flag ${margin > 0 ? 'f-ok' : 'f-bad'}" style="margin:8px 0 0">
        <b>Live data in use.</b> ${obs} bidders observed against ${r.demand.seats} seats.
        Safety margin: <b>${margin > 0 ? margin + ' seats spare' : Math.abs(margin) + ' bidders over capacity'}</b>.
        ${margin > 0 ? 'The round may still be open, so more bidders can arrive before it closes.' : ''}</div>`;
    } else if ((r.demand.factors || []).length) {
      h += `<details style="margin-top:8px"><summary>Why this course is modelled as
        ${Math.round(r.demand.expected_rivals)} rivals</summary>
        <div class="tiny" style="margin-top:6px">${r.demand.factors.map(f =>
          `× ${f.multiplier} — ${esc(f.why)} <span class="pill p-m">${esc(f.provenance)}</span>`).join('<br>')}
        <br><br><b>None of this is observed data.</b> It is a stress assumption designed to stop you
        underbidding.</div></details>`;
    }
    if (!r.target_met) {
      h += `<div class="flag f-bad" style="margin:8px 0 0"><b>Reliability target not reachable.</b>
        Even at the legal cap of ${r.cap}, this course does not reach the
        ${esc((PRIO_LABEL[r.priority] || '').toLowerCase())} target in every tested scenario
        (worst tested ${probLabel(r.worst_tested)}). The cap is a hard legal limit, so no bid fixes this.</div>`;
    }
    h += '</div>';
  });
  h += '</div>';

  h += `<div class="card"><h2>What the numbers do and do not mean</h2>
    <table><tbody>
    <tr><td style="width:210px"><b>Sampling uncertainty</b></td><td>How much the answer would move if the
      simulation were re-run with a different seed. Small: shown per course as ±pp.</td></tr>
    <tr><td><b>Parameter uncertainty</b></td><td>How many rivals turn up and how hard they bid. Large, and
      the dominant source of doubt. Compare the scenario rows above.</td></tr>
    <tr><td><b>Model uncertainty</b></td><td>Whether synthetic rivals behave like real students at all.
      Unquantified, because no historical data exists.</td></tr>
    <tr><td><b>Rule uncertainty</b></td><td>Whether bids share one live pool. Unresolved; both readings
      shown above.</td></tr>
    </tbody></table></div>`;
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

/* ---------------- §16 whole-plan stress test ---------------- */
async function stressPlan() {
  if (!RESULT || !RESULT.recommendations) {
    $('stressOut').innerHTML = '<div class="note">Run the bid simulation first, then come back here.</div>';
    return;
  }
  const btn = $('stressBtn'); if (btn) btn.disabled = true;
  $('stressOut').innerHTML = '<div class="note"><span class="spin"></span> Running synthetic cohorts on the server…</div>';
  try {
    const credits = {};
    RESULT.recommendations.forEach(r => { credits[r.code] = (BY[r.code] || {}).cr || 3; });
    const st = await API.stressTestPlan(LAST_JOB_ID, {
      credits, credit_cap: +$('capCr').value || 25,
      fixed_credits: fixedCredits(), cohorts: 4000, seed: +$('seed').value || 0
    });
    let h = `<div class="grid g4">
      <div class="stat ${st.all_must_have_rate != null && st.all_must_have_rate < 0.9 ? 'r' : 'u'}">
        <div class="k">All must-haves</div>
        <div class="v mono">${st.all_must_have_rate == null ? 'n/a' : (st.all_must_have_rate * 100).toFixed(1) + '%'}</div>
        <div class="f">${st.must_have_count} marked must-have</div></div>
      <div class="stat"><div class="k">At least one must-have</div>
        <div class="v mono">${st.any_must_have_rate == null ? 'n/a' : (st.any_must_have_rate * 100).toFixed(1) + '%'}</div></div>
      <div class="stat c"><div class="k">Expected elective credits</div>
        <div class="v mono">${st.expected_elective_credits}</div></div>
      <div class="stat r"><div class="k">Worst-case credits</div>
        <div class="v mono">${st.worst_case_credits}</div>
        <div class="f">of ${st.cohorts.toLocaleString()} cohorts</div></div></div>`;
    h += '<table style="margin-top:12px"><thead><tr><th>Course</th><th>Failed in</th><th>Reading</th></tr></thead><tbody>';
    st.failure_rates.forEach(f => {
      h += `<tr><td><b>${esc(f.code)}</b></td>
        <td class="num" style="color:${f.rate > 0.25 ? 'var(--bad)' : 'var(--dim)'}">${(f.rate * 100).toFixed(1)}%</td>
        <td class="tiny">${f.rate > 0.4 ? 'most likely to cost you a seat'
          : f.rate > 0.15 ? 'meaningful risk' : 'holding up well'}</td></tr>`;
    });
    h += `</tbody></table><div class="note" style="margin-top:10px">${esc(st.note)}</div>`;
    $('stressOut').innerHTML = h;
  } catch (e) {
    $('stressOut').innerHTML = `<div class="flag f-bad">${esc(API.humanError(e))}</div>`;
  } finally {
    if (btn) btn.disabled = !BACKEND_OK;
  }
}

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
    $('buildOut').innerHTML = '<div class="card"><div class="note">Add courses to your bid shortlist on the Course Picker tab first.</div></div>';
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
    current_year: $('model')&&$('model').value==='y4'?4:($('model')&&$('model').value==='y3'?3:2),
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
      + 'Course Picker tab first.</div></div>';
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
  const tab = document.querySelector('[data-p="courses"]'); if (tab) tab.click();
}

/* ---------------- rules from the backend ---------------- */
const STATUS_LABEL = {
  official: ['Official', 'p-uwe'], prospectus: ['Prospectus', 'p-uwe'],
  timetable: ['From timetable', 'p-ccc'], inferred: ['Inferred', 'p-me'],
  disputed: ['Disputed', 'p-w'], unknown: ['Unknown', 'p-w']
};
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
        <div class="tiny"><b>Source:</b> ${esc(r.source)}</div>${extras}</details>
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
  // neutral by default -- a fresh install (anyone this app is shared with) starts
  // with nothing pre-filled; restoreActivePlan() below fills these back in from
  // this browser's own saved plan, if one exists, so a returning student on their
  // own machine sees their own numbers again without them ever being baked into
  // the shipped source.
  MANUAL = [];
  FIXED = [];

  ['compMode', 'robustMethod', 'disp', 'showLow', 'showModerate', 'showOptimistic', 'budgetMode'].forEach(id => {
    const e = $(id); if (e) e.onchange = () => { if (RESULT) void runOpt(); };
  });
  ['model', 'sem', 'remME', 'remUWE', 'remCCC', 'remFL', 'doneME', 'doneUWE', 'doneCCC'].forEach(id => {
    const e = $(id); if (e) e.addEventListener('change', () => void refreshPoolsFromBackend());
  });
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
  }
  // expose a minimal surface for tests and debugging (no compute, just state)
  try {
    Object.defineProperty(window, 'RESULT', { get: () => RESULT, configurable: true });
    Object.defineProperty(window, 'BACKEND_OK', { get: () => BACKEND_OK, configurable: true });
    window.probeHealth = probeHealth;
    window.runOpt = runOpt;
    window.cancelRun = cancelRun;
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
      headlineMode: curMode(), budgetMode: curBudgetMode(), robustMethod: curMethod(),
      trials: +($('nsim') ? $('nsim').value : 8000),
      seed: +($('seed') ? $('seed').value : 20260802),
      dispersion: +($('disp') ? $('disp').value : 0.18)
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
      set('model', pay.profile.model); set('sem', pay.profile.semester);
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
      set('compMode', pay.assumptions.headlineMode);
      set('budgetMode', pay.assumptions.budgetMode);
      set('robustMethod', pay.assumptions.robustMethod);
      set('nsim', pay.assumptions.trials); set('seed', pay.assumptions.seed);
      set('disp', pay.assumptions.dispersion);
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
  if (!RESULT) { planMsg('Run a simulation first, then export the recommendations.', true); return; }
  const recs = RESULT.recommendations.map(r => ({ ...r, credits: (BY[r.code] || {}).cr }));
  download('snu-recommendations.csv', PLANS.exportCsv(recs), 'text/csv');
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
  payload.profile = Object.assign({}, payload.profile, {
    model: 'y4', semester: 7, creditCap: 25,
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
function cancelPlanImport() {
  PENDING_IMPORT = null;
  PENDING_ADVISEMENT = null;
  const box = $('importPreviewBox'); if (box) box.innerHTML = '';
}

if (typeof document !== 'undefined') {
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => void boot());
  else void boot();
}
