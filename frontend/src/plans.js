/* =====================================================================
   PLAN STORE  (§10, §11)

   Local persistence for named plans with a versioned schema, forward
   migrations, corrupted-storage recovery, and import validation that
   assumes the file is hostile.

   Transient backend job state is deliberately NOT persisted as though it
   were a finished result: only the recommendation payload is kept, tagged
   with the versions it was produced under.
   ===================================================================== */
(function (factory) {
  // Resolve the global object robustly. `self` is absent in some non-browser
  // DOM shims, which previously left the module attached to nothing at all.
  var g = (typeof globalThis !== 'undefined' && globalThis)
       || (typeof self !== 'undefined' && self)
       || (typeof window !== 'undefined' && window)
       || this;
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else g.PLANS = factory();
}(function () {

  const KEY = 'snu.plans.v1';
  const ACTIVE = 'snu.plans.active';
  const SCHEMA = 12;
  const MAX_IMPORT_BYTES = 2 * 1024 * 1024;
  const DANGEROUS = ['__proto__', 'constructor', 'prototype'];

  /* ---------- storage with recovery ---------- */
  function rawRead() {
    try {
      const s = localStorage.getItem(KEY);
      if (!s) return { schema: SCHEMA, plans: {}, seq: 0 };
      const j = JSON.parse(s);
      if (!j || typeof j !== 'object' || typeof j.plans !== 'object') throw new Error('shape');
      return migrate(j);
    } catch (e) {
      // corrupted storage: quarantine rather than silently destroy
      try {
        localStorage.setItem(KEY + '.corrupt.' + Date.now(), localStorage.getItem(KEY) || '');
        localStorage.removeItem(KEY);
      } catch (e2) {}
      return { schema: SCHEMA, plans: {}, seq: 0, recovered: true };
    }
  }
  function rawWrite(db) {
    db.schema = SCHEMA;
    try { localStorage.setItem(KEY, JSON.stringify(db)); return true; }
    catch (e) { return false; }   // quota exceeded etc.
  }

  function migrate(db) {
    let v = Number(db.schema) || 1;
    if (v < 2) {
      // v1 stored a bare course-code array; v2 stores course objects
      Object.values(db.plans).forEach(p => {
        if (Array.isArray(p.payload && p.payload.courses)
            && p.payload.courses.length && typeof p.payload.courses[0] === 'string') {
          p.payload.courses = p.payload.courses.map(code => ({ code, priority: 'STRONG', credits: 3 }));
        }
      });
      v = 2;
    }
    if (v < 3) {
      // v3 adds explicit assumption block so a plan records what produced its numbers
      Object.values(db.plans).forEach(p => {
        p.payload = p.payload || {};
        p.payload.assumptions = p.payload.assumptions || {
          headlineMode: 'HIGH', budgetMode: 'SHARED_LIVE', robustMethod: 'minimax',
          trials: 8000, seed: 20260802, dispersion: 0.18
        };
      });
      v = 3;
    }
    if (v < 4) {
      // v4 adds degree-progress fields (doneCr/degCr/remCore/remOther/csdBlock)
      // and the fixed/off-timetable course lists, none of which were persisted
      // before -- they used to silently reset to the page's hardcoded example
      // values on every reload instead of the student's own saved numbers.
      Object.values(db.plans).forEach(p => {
        p.payload = p.payload || {};
        p.payload.profile = p.payload.profile || {};
        if (p.payload.profile.doneCr == null) p.payload.profile.doneCr = 0;
        if (p.payload.profile.degCr == null) p.payload.profile.degCr = 160;
        if (p.payload.profile.remCore == null) p.payload.profile.remCore = 0;
        if (p.payload.profile.remOther == null) p.payload.profile.remOther = 0;
        if (p.payload.profile.csdBlock == null) p.payload.profile.csdBlock = '';
        if (!Array.isArray(p.payload.fixed)) p.payload.fixed = [];
        if (!Array.isArray(p.payload.manual)) p.payload.manual = [];
      });
      v = 4;
    }
    if (v < 5) {
      // v5 adds already-completed major electives (specialisation bucket
      // banking) -- this used to be a hardcoded array of the original
      // developer's own completed courses, baked in for every install.
      Object.values(db.plans).forEach(p => {
        p.payload = p.payload || {};
        if (!Array.isArray(p.payload.doneElectives)) p.payload.doneElectives = [];
      });
      v = 5;
    }
    if (v < 6) {
      // v6 adds the wishlist scheduler's choice groups and credit policy
      // (personal target / minimum / overload scenario) - previously there
      // was only one ambiguous "credit cap" field, no distinction between
      // the official ceiling, a personal target, and an overload what-if.
      Object.values(db.plans).forEach(p => {
        p.payload = p.payload || {};
        if (!Array.isArray(p.payload.choiceGroups)) p.payload.choiceGroups = [];
        if (!p.payload.creditPolicy || typeof p.payload.creditPolicy !== 'object') {
          p.payload.creditPolicy = { min: 0, target: 0, overloadOn: false, overloadCeiling: 30, overloadConfirmed: false };
        }
      });
      v = 6;
    }
    if (v < 7) {
      // v7 adds the selected official programme and the student's private
      // completed-work/audit data. Fresh installs remain completely empty.
      Object.values(db.plans).forEach(p => {
        p.payload = p.payload || {};
        p.payload.profile = p.payload.profile || {};
        if (p.payload.profile.programme == null) p.payload.profile.programme = '';
        if (p.payload.profile.cohortYear == null) p.payload.profile.cohortYear = null;
        if (p.payload.profile.completedCoursesText == null) p.payload.profile.completedCoursesText = '';
        if (!Array.isArray(p.payload.profile.completedMilestones)) p.payload.profile.completedMilestones = [];
        if (!Array.isArray(p.payload.auditRequirements)) p.payload.auditRequirements = [];
      });
      v = 7;
    }
    if (v < 8) {
      // v8 records advisement-report provenance and parsed totals locally.
      // It is empty for every fresh install and never contains a PDF's bytes.
      Object.values(db.plans).forEach(p => {
        p.payload = p.payload || {};
        if (!p.payload.advisement || typeof p.payload.advisement !== 'object') {
          p.payload.advisement = null;
        }
      });
      v = 8;
    }
    if (v < 9) {
      // v9 replaces the ambiguous single overload-confirmed flag with the
      // three confirmations required by the published Year IV policy.
      Object.values(db.plans).forEach(p => {
        p.payload = p.payload || {};
        const cp = p.payload.creditPolicy || (p.payload.creditPolicy = {});
        if (cp.overloadCeiling == null) cp.overloadCeiling = 27;
        cp.eligibilityConfirmed = !!cp.eligibilityConfirmed;
        cp.advisorRecommended = !!cp.advisorRecommended;
        cp.deanApproved = !!cp.deanApproved;
        delete cp.overloadConfirmed;
        if (p.payload.profile) p.payload.profile.creditCap = 25;
      });
      v = 9;
    }
    if (v < 10) {
      // v10 adds already-completed ME/UWE/CCC credit counts (POOL.Y2, rectified
      // 2026-08-05: each completed credit lowers the Second Year pool by 5 points).
      // Zero for every fresh install and for every plan saved before this rule
      // existed - matches "no deduction" exactly, so this migration changes no
      // existing plan's computed pool.
      Object.values(db.plans).forEach(p => {
        p.payload = p.payload || {};
        p.payload.profile = p.payload.profile || {};
        if (p.payload.profile.doneME == null) p.payload.profile.doneME = 0;
        if (p.payload.profile.doneUWE == null) p.payload.profile.doneUWE = 0;
        if (p.payload.profile.doneCCC == null) p.payload.profile.doneCCC = 0;
      });
      v = 10;
    }
    if (v < 11) {
      // v11 stores each programme's selected pathway/focus and an optional
      // private advising note. Empty maps preserve every older plan exactly.
      Object.values(db.plans).forEach(p => {
        p.payload = p.payload || {};
        if (!p.payload.pathwaySelections || typeof p.payload.pathwaySelections !== 'object') {
          p.payload.pathwaySelections = {};
        }
        if (!p.payload.pathwayNotes || typeof p.payload.pathwayNotes !== 'object') {
          p.payload.pathwayNotes = {};
        }
      });
      v = 11;
    }
    if (v < 12) {
      // v12 replaces synthetic market assumptions in the default recommender
      // with a transparent reserve and concentration posture.  The legacy
      // fields stay readable for old exports but no longer drive the UI.
      Object.values(db.plans).forEach(p => {
        p.payload = p.payload || {};
        p.payload.assumptions = p.payload.assumptions || {};
        if (p.payload.assumptions.reservePercent == null) p.payload.assumptions.reservePercent = 20;
        if (!['diversified', 'balanced', 'focused'].includes(p.payload.assumptions.posture)) {
          p.payload.assumptions.posture = 'balanced';
        }
      });
      v = 12;
    }
    db.schema = v;
    db.seq = Number(db.seq) || 0;
    return db;
  }

  // Date.now() has millisecond resolution: two saves in the same tick tie on
  // `updated`, leaving Array.sort's order for that tie unspecified. `seq` is a
  // persisted monotonic counter used only to break such ties deterministically.
  function nextSeq(db) {
    db.seq = (Number(db.seq) || 0) + 1;
    return db.seq;
  }

  /* ---------- CRUD ---------- */
  function list() {
    const db = rawRead();
    return Object.values(db.plans)
      .map(p => ({ id: p.id, name: p.name, created: p.created, updated: p.updated, seq: p.seq || 0 }))
      .sort((a, b) => (b.updated - a.updated) || (b.seq - a.seq));
  }
  function get(id) { return rawRead().plans[id] || null; }
  function activeId() { try { return localStorage.getItem(ACTIVE); } catch (e) { return null; } }
  function setActive(id) { try { localStorage.setItem(ACTIVE, id); } catch (e) {} }

  function save(name, payload, id) {
    const db = rawRead();
    const now = Date.now();
    const pid = id || ('p' + now.toString(36) + Math.random().toString(36).slice(2, 6));
    const existing = db.plans[pid];
    db.plans[pid] = {
      id: pid, name: String(name || 'Untitled').slice(0, 120),
      payload: sanitise(payload), created: existing ? existing.created : now, updated: now,
      seq: nextSeq(db)
    };
    const ok = rawWrite(db);
    if (ok) setActive(pid);
    return ok ? db.plans[pid] : null;
  }
  function rename(id, name) {
    const db = rawRead();
    if (!db.plans[id]) return false;
    db.plans[id].name = String(name).slice(0, 120);
    db.plans[id].updated = Date.now();
    db.plans[id].seq = nextSeq(db);
    return rawWrite(db);
  }
  function duplicate(id, name) {
    const p = get(id);
    if (!p) return null;
    return save(name || (p.name + ' copy'), JSON.parse(JSON.stringify(p.payload)));
  }
  function remove(id) {
    const db = rawRead();
    if (!db.plans[id]) return false;
    delete db.plans[id];
    const ok = rawWrite(db);
    if (activeId() === id) { try { localStorage.removeItem(ACTIVE); } catch (e) {} }
    return ok;
  }

  let autosaveTimer = null;
  function autosave(name, payloadFn, id, delay) {
    if (autosaveTimer) clearTimeout(autosaveTimer);
    autosaveTimer = setTimeout(() => {
      try { save(name, payloadFn(), id); } catch (e) {}
    }, delay == null ? 1200 : delay);
  }

  /* ---------- sanitising ---------- */
  function sanitise(o) {
    if (o === null || typeof o !== 'object') return o;
    if (Array.isArray(o)) return o.map(sanitise);
    const out = {};
    Object.keys(o).forEach(k => {
      if (DANGEROUS.includes(k)) return;             // prototype pollution
      out[k] = sanitise(o[k]);
    });
    return out;
  }

  /* ---------- export ---------- */
  function exportJson(planPayload, meta) {
    return JSON.stringify({
      schemaVersion: SCHEMA,
      exportedAt: new Date().toISOString(),
      generator: 'snu-bid-simulator',
      ruleVersion: (meta && meta.ruleVersion) || null,
      modelVersion: (meta && meta.modelVersion) || null,
      payload: sanitise(planPayload)
    }, null, 2);
  }

  function exportCsv(recommendations) {
    const strategic = (recommendations || []).some(r => r && r.strategic_ceiling != null);
    if (strategic) {
      const head = ['course', 'category', 'priority', 'credits', 'opening_bid', 'personal_ceiling',
                    'allocation_share_percent', 'pressure', 'live_bidders', 'seats', 'provenance', 'action'];
      const rows = recommendations.map(r => [
        r.code, r.category, r.priority, r.credits ?? '', r.opening_bid, r.strategic_ceiling,
        r.allocation_share_percent, r.pressure ? r.pressure.label : '',
        r.pressure ? r.pressure.live_bidders ?? '' : '', r.pressure ? r.pressure.seats : '',
        r.pressure ? r.pressure.provenance : '', r.action || ''
      ]);
      return [head, ...rows].map(r => r.map(csvCell).join(',')).join('\n');
    }
    const head = ['course', 'category', 'priority', 'credits', 'cap', 'recommended_bid',
                  'worst_tested_win', 'expected_charge', 'target_met', 'demand_source',
                  'modelled_rivals', 'seats'];
    const rows = (recommendations || []).map(r => [
      r.code, r.category, r.priority, r.credits ?? '', r.cap, r.bid,
      (r.worst_tested != null ? (r.worst_tested * 100).toFixed(1) + '%' : ''),
      r.expected_charge, r.target_met,
      r.demand ? r.demand.source : '', r.demand ? r.demand.expected_rivals : '',
      r.demand ? r.demand.seats : ''
    ]);
    return [head, ...rows].map(r => r.map(csvCell).join(',')).join('\n');
  }
  function exportObservationsCsv(obs) {
    const head = ['course_code', 'round', 'observed_at', 'seats', 'bidders', 'my_bid',
                  'clearing_price', 'outcome', 'notes'];
    const rows = (obs || []).map(o => head.map(h => o[h] ?? ''));
    return [head, ...rows].map(r => r.map(csvCell).join(',')).join('\n');
  }
  function csvCell(v) {
    const s = v == null ? '' : String(v);
    return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  }

  /* ---------- import: assume the file is hostile ---------- */
  function validateImport(text) {
    const errors = [];
    const warnings = [];
    if (typeof text !== 'string') return { ok: false, errors: ['not text'] };
    if (text.length > MAX_IMPORT_BYTES) {
      return { ok: false, errors: ['file too large (limit 2 MB)'] };
    }
    let j;
    try { j = JSON.parse(text); }
    catch (e) { return { ok: false, errors: ['not valid JSON: ' + e.message.slice(0, 80)] }; }
    if (!j || typeof j !== 'object' || Array.isArray(j)) {
      return { ok: false, errors: ['top level must be an object'] };
    }
    const sv = Number(j.schemaVersion);
    if (!Number.isFinite(sv)) errors.push('missing schemaVersion');
    else if (sv > SCHEMA) errors.push('file was written by a newer version (schema ' + sv + '); upgrade first');
    else if (sv < SCHEMA) warnings.push('older schema ' + sv + ' will be migrated to ' + SCHEMA);

    const p = j.payload;
    if (!p || typeof p !== 'object') errors.push('missing payload object');
    else {
      if (p.pools) {
        ['ME', 'UWE', 'CCC'].forEach(k => {
          const v = Number(p.pools[k]);
          if (!Number.isFinite(v) || v < 0) errors.push('pool ' + k + ' is not a non-negative number');
          if (v > 100000) errors.push('pool ' + k + ' is implausibly large');
        });
      }
      if (p.courses != null) {
        if (!Array.isArray(p.courses)) errors.push('courses must be an array');
        else {
          if (p.courses.length > 500) errors.push('too many courses (limit 500)');
          const seen = new Set();
          p.courses.forEach((c, i) => {
            if (!c || typeof c !== 'object') { errors.push('course[' + i + '] is not an object'); return; }
            if (!c.code) { errors.push('course[' + i + '] has no code'); return; }
            if (seen.has(c.code)) errors.push('duplicate course code ' + c.code);
            seen.add(c.code);
            const cr = Number(c.credits);
            if (c.credits != null && (!Number.isFinite(cr) || cr <= 0 || cr > 12)) {
              errors.push(c.code + ': credits must be between 0 and 12');
            }
            const st = Number(c.seats);
            if (c.seats != null && (!Number.isFinite(st) || st < 0 || st > 5000 || st % 1 !== 0)) {
              errors.push(c.code + ': seats must be a whole number 0-5000');
            }
            if (c.liveBidders != null && c.liveBidders !== '') {
              const lb = Number(c.liveBidders);
              if (!Number.isFinite(lb) || lb < 0) errors.push(c.code + ': liveBidders must be >= 0');
            }
            if (Array.isArray(c.meetings)) {
              c.meetings.forEach((m, k) => {
                if (!m || !Number.isFinite(Number(m.start)) || !Number.isFinite(Number(m.end))
                    || Number(m.end) <= Number(m.start)) {
                  errors.push(c.code + ': meeting[' + k + '] has an impossible time range');
                }
              });
            }
          });
        }
      }
      Object.keys(p).forEach(k => { if (DANGEROUS.includes(k)) errors.push('rejected unsafe key ' + k); });
    }
    return { ok: errors.length === 0, errors, warnings,
             payload: errors.length ? null : sanitise(p),
             schemaVersion: sv, courseCount: (p && Array.isArray(p.courses)) ? p.courses.length : 0 };
  }

  function importAsNewPlan(text, name) {
    const v = validateImport(text);
    if (!v.ok) return { ok: false, errors: v.errors };
    const rec = save(name || 'Imported plan', v.payload);
    return { ok: !!rec, plan: rec, warnings: v.warnings };
  }
  function importReplacingActive(text) {
    const v = validateImport(text);
    if (!v.ok) return { ok: false, errors: v.errors };
    const id = activeId();
    const cur = id ? get(id) : null;
    const rec = save(cur ? cur.name : 'Imported plan', v.payload, id || undefined);
    return { ok: !!rec, plan: rec, warnings: v.warnings };
  }

  /* ---------- printable summary ---------- */
  function printableSummary(plan, result) {
    const lines = [];
    lines.push('SNU Bid Plan Summary');
    lines.push('Generated ' + new Date().toLocaleString());
    if (result && Array.isArray(result.courses)) {
      lines.push('Strategy ' + (result.strategy_version || '?')
               + ' | posture ' + (result.posture || '?')
               + ' | carry-forward reserve ' + (result.reserve_percent || 0) + '%');
    } else if (result) {
      lines.push('Legacy simulation result');
    }
    lines.push('');
    lines.push('Pools: ME ' + plan.pools.ME + ' | UWE ' + plan.pools.UWE + ' | CCC ' + plan.pools.CCC);
    lines.push('');
    lines.push('Course      Cat  Priority   Opening  Personal ceiling');
    (result && Array.isArray(result.courses) ? result.courses : []).forEach(r => {
      lines.push([
        String(r.code).padEnd(11),
        String(r.category).padEnd(4),
        String(r.priority).padEnd(10),
        String(r.opening_bid).padEnd(8),
        String(r.strategic_ceiling)
      ].join(' '));
    });
    lines.push('');
    lines.push('Opening bids and ceilings are transparent planning heuristics, not guarantees.');
    lines.push('No win probability or expected clearing price is claimed without historical market data.');
    return lines.join('\n');
  }

  return {
    SCHEMA, list, get, save, rename, duplicate, remove, autosave,
    activeId, setActive, migrate, sanitise,
    exportJson, exportCsv, exportObservationsCsv,
    validateImport, importAsNewPlan, importReplacingActive,
    printableSummary, _rawRead: rawRead, _rawWrite: rawWrite
  };
}));
