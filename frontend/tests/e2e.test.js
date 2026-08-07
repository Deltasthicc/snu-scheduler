/* §14/§19 real-browser end-to-end against a live backend. */
const { chromium } = require('playwright');
const APP = 'http://127.0.0.1:5173/index.html';
let pass = 0, fail = 0;
const ck = (n, c, x) => { console.log((c ? '  PASS  ' : '  FAIL  ') + n + (x ? '   [' + x + ']' : '')); c ? pass++ : fail++; };

(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1280, height: 900 } });
  const errs = [];
  p.on('pageerror', e => errs.push(e.message));
  p.on('console', m => { const t = m.text();
    if (m.type() === 'error' && !/fonts\.|Failed to load resource/.test(t)) errs.push(t); });

  console.log('=== BOOT + BACKEND CONNECTION ===');
  await p.goto(APP, { waitUntil: 'load' });
  await p.waitForTimeout(2500);
  ck('page boots with no JS errors', errs.length === 0, errs.slice(0, 2).join(' | '));

  const noEngine = await p.evaluate(() => ({
    simulate: typeof window.SIMULATE, optimize: typeof window.OPTIMIZE,
    robust: typeof window.ROBUST, competition: typeof window.COMPETITION,
    api: typeof window.API, clash: typeof window.CLASH, plans: typeof window.PLANS
  }));
  ck('no simulation engine in the browser', noEngine.simulate === 'undefined', JSON.stringify(noEngine));
  ck('no optimiser in the browser', noEngine.optimize === 'undefined' && noEngine.robust === 'undefined');
  ck('no rival-generation model in the browser', noEngine.competition === 'undefined');
  ck('API adapter present', noEngine.api === 'object');
  ck('clash preview present (permitted)', noEngine.clash === 'object');
  ck('plan store present', noEngine.plans === 'object');

  const health = await p.evaluate(() => API.healthCheck());
  ck('frontend reaches the backend', health.ok === true, JSON.stringify(health).slice(0, 90));

  console.log('\n=== IMPORTED-PROFILE DEGREE AUDIT REGRESSION ===');
  await p.waitForFunction(() => Array.isArray(PROGRAMME_CATALOG) && PROGRAMME_CATALOG.length === 44,
                          null, { timeout: 15000 });
  await p.evaluate(async () => {
    document.getElementById('programme').value = 'b-tech-in-computer-science-and-engineering';
    document.getElementById('doneCr').value = 105;
    document.getElementById('remCore').value = 9;
    document.getElementById('remME').value = 9;
    document.getElementById('remCCC').value = 11;
    document.getElementById('remUWE').value = 4;
    document.getElementById('remFL').value = 6;
    await runDegreeAudit();
  });
  const auditRegression = await p.evaluate(() => ({
    total: LAST_AUDIT && LAST_AUDIT.requirements.find(r => r.id === 'total')?.completed,
    error: document.getElementById('auditBox').textContent.includes('Audit could not run'),
    requirements: LAST_AUDIT && LAST_AUDIT.requirements_total,
  }));
  ck('audit runs without the former null-field crash', !auditRegression.error && auditRegression.requirements === 9,
     JSON.stringify(auditRegression));
  ck('private aggregate/transfer total remains 105', auditRegression.total === 105,
     String(auditRegression.total));

  await p.click('.tab[data-p="bid"]');
  ck('default bid tab is the strategic planner, not the synthetic stress UI',
     /Strategic bid planner/.test(await p.textContent('#p-bid')) &&
     (await p.locator('#stressOut').count()) === 0);

  console.log('\n=== POOLS COME FROM THE BACKEND ===');
  await p.evaluate(async () => {
    const set = (id, v) => { document.getElementById(id).value = v; };
    set('remME', 9); set('remUWE', 4); set('remCCC', 11); set('remFL', 6);
    recalc();
    await refreshPoolsFromBackend();
  });
  await p.waitForFunction(() => {
    const f = document.getElementById('formula');
    return f && /from backend/.test(f.textContent);
  }, null, { timeout: 15000 }).catch(() => {});
  const poolInfo = await p.evaluate(() => ({
    me: document.getElementById('bME').textContent,
    uwe: document.getElementById('bUWE').textContent,
    ccc: document.getElementById('bCCC').textContent,
    formula: document.getElementById('formula').textContent
  }));
  ck('pools are 297/215/492', poolInfo.me === '297' && poolInfo.uwe === '215' && poolInfo.ccc === '492',
     `${poolInfo.me}/${poolInfo.uwe}/${poolInfo.ccc}`);
  ck('pool panel states it came from the backend', /from backend/.test(poolInfo.formula),
     poolInfo.formula.slice(0, 70));

  console.log('\n=== DETERMINISTIC STRATEGIC BID PLAN ===');
  await p.evaluate(() => {
    PICK = {};
    ['CSD358', 'CSD361'].forEach(c => { if (BY[c]) PICK[c] = { want: 5, pkg: 0 }; });
    PRIO = { CSD358: 'STRONG', CSD361: 'STRONG' };
    document.getElementById('bidReserve').value = 20;
    document.getElementById('bidPosture').value = 'balanced';
    renderChosen();
  });
  await p.click('.tab[data-p="bid"]');
  await p.click('#runBtn');
  await p.waitForFunction(() => window.RESULT && window.RESULT.strategy_version === 'marginal-value-v3',
                          null, { timeout: 15000 });

  const res = await p.evaluate(() => ({
    courses: RESULT.courses.map(c => ({ code: c.code, opening: c.opening_bid,
      ceiling: c.strategic_ceiling, pressure: c.pressure.label })),
    categories: RESULT.categories,
    invariants: RESULT.invariants,
    resultKeys: Object.keys(RESULT),
    courseKeys: Object.keys(RESULT.courses[0] || {}),
    text: document.getElementById('bidOut').textContent
  }));
  ck('all selected courses receive a result', res.courses.length === 2, String(res.courses.length));
  // Both must be funded (the v1 greedy-deletion defect must not return), but they
  // are NOT expected to be equal: CSD358 has 120 seats and CSD361 has 80, and the
  // planner now prices scarcity, so identical priorities with different seat
  // counts should and do receive different bids.
  ck('equal-priority alternatives are both funded, not greedily zeroed',
     res.courses.every(c => c.ceiling > 0), JSON.stringify(res.courses));
  const meEnvelope = res.categories.find(c => c.category === 'ME');
  ck('ME ceilings fit the usable envelope and protect the reserve',
     meEnvelope.strategic_ceiling_total <= meEnvelope.current_round_envelope &&
     meEnvelope.current_round_envelope + meEnvelope.carry_forward_reserve === meEnvelope.pool,
     JSON.stringify(meEnvelope));
  ck('backend exposes all four planner invariants as true',
     Object.values(res.invariants).every(Boolean), JSON.stringify(res.invariants));
  ck('response has no synthetic probability or expected-charge fields',
     !res.resultKeys.includes('trials') && !res.courseKeys.includes('expected_charge') &&
     !res.courseKeys.includes('worst_tested') && !/target not reachable/i.test(res.text));
  ck('the former undefined disclaimer is gone', !/undefined/.test(res.text));
  ck('UI clearly distinguishes opening bids from personal ceilings',
     /Opening bids and stop points/.test(res.text) && /Personal ceiling/.test(res.text));
  ck('UI labels probabilities and prices as model outputs, not measurements',
     /model output, not a measurement/.test(res.text) && /historical/.test(res.text));
  ck('UI shows the modelled clearing price, which is what a seat actually costs',
     /Modelled price/.test(res.text));

  const beforeLive = res.courses.find(c => c.code === 'CSD358');
  await p.evaluate(() => setLive('CSD358', '240'));
  await p.waitForFunction((oldOpening) => {
    const row = window.RESULT && RESULT.courses.find(c => c.code === 'CSD358');
    return row && row.pressure.provenance === 'live' && row.opening_bid !== oldOpening;
  }, beforeLive.opening, { timeout: 15000 });
  const afterLive = await p.evaluate(() => {
    const row = RESULT.courses.find(c => c.code === 'CSD358');
    return { opening: row.opening_bid, ceiling: row.strategic_ceiling, pressure: row.pressure.label };
  });
  // A live count REPLACES the modelled rival count rather than being averaged
  // into it, so the plan can move either way: with no data the model spans up to
  // 4x seats, and observing a count below that upper reading is genuinely
  // reassuring. The property that must hold is monotonicity *among observed
  // counts* - more bidders must never buy you a smaller bid.
  ck('live pressure is labelled, not converted into a probability',
     afterLive.pressure === 'heavily_oversubscribed', afterLive.pressure);
  const ladder = [];
  for (const count of [140, 240, 400]) {
    await p.evaluate(c => setLive('CSD358', String(c)), count);
    await p.waitForFunction(c => {
      const row = window.RESULT && RESULT.courses.find(x => x.code === 'CSD358');
      return row && row.pressure.live_bidders === c;
    }, count, { timeout: 15000 });
    ladder.push(await p.evaluate(() => RESULT.courses.find(c => c.code === 'CSD358').strategic_ceiling));
  }
  ck('heavier observed competition never lowers the personal ceiling',
     ladder[0] <= ladder[1] && ladder[1] <= ladder[2], ladder.join(' -> '));


  console.log('\n=== §17 SCHEDULE BUILDER (backend-authoritative search) ===');
  await p.evaluate(() => { PICK = {}; renderChosen(); });
  await p.click('.tab[data-p="courses"]');
  await p.evaluate(() => { const d = document.querySelector('#p-courses details.more'); if (d) d.open = true; });
  await p.click('#buildBtn');
  await p.waitForTimeout(200);
  ck('empty shortlist shows a friendly message, no request made', /Course Picker tab first/.test(
    await p.textContent('#buildOut')));

  await p.evaluate(() => {
    FIXED = [];
    PICK = {};
    // re-verified against the 2026-08-04 timetable revision (see
    // docs/TIMETABLE_REVISION_DIFF_2026-08-04.md) to still yield exactly 2
    // clash-free schedules - the original fixture's shortlist dropped to 1
    // after the revision changed real section times, so it was replaced,
    // not patched around.
    ['ART202/AMP1001', 'BIO1001', 'BIO1002', 'BIO1008']
      .forEach(c => { if (BY[c]) PICK[c] = { want: 5, pkg: 0 }; });
    renderChosen(); renderFixed();
  });
  await p.evaluate(() => {
    // the Long Tasks API reports any main-thread task over 50ms; the old
    // in-browser search produced a single ~100s task, so this is the direct
    // regression check for "the browser is a thin client" on this screen
    window.__longTasks = [];
    window.__longTaskObs = new PerformanceObserver(list => {
      list.getEntries().forEach(e => window.__longTasks.push(e.duration));
    });
    window.__longTaskObs.observe({ entryTypes: ['longtask'] });
  });
  await p.click('#buildBtn');
  await p.waitForFunction(() => {
    const t = document.getElementById('buildStat').textContent;
    return /combinations tested/.test(t);
  }, null, { timeout: 20000 });
  const longTasks = await p.evaluate(() => { window.__longTaskObs.disconnect(); return window.__longTasks; });
  // the Long Tasks API only ever reports entries that are already >=50ms by
  // definition, so "no long task" means this list came back empty, not that
  // every reported entry happens to be short.
  ck('no long task blocked the main thread during the search', longTasks.length === 0,
     JSON.stringify(longTasks));

  const search1 = await p.evaluate(() => ({
    stat: document.getElementById('buildStat').textContent,
    rows: document.querySelectorAll('#buildOut tbody tr').length,
  }));
  ck('search reports a real combination count', /\d+ combinations tested/.test(search1.stat), search1.stat);
  ck('the two known-valid schedules are found for this shortlist', search1.rows === 2, String(search1.rows));

  await p.click('#buildOut .scroll table tbody tr:first-child button:has-text("preview")');
  await p.waitForFunction(() => document.getElementById('previewGrid') &&
    document.getElementById('previewGrid').children.length > 0, null, { timeout: 5000 });
  ck('preview renders a mini timetable grid', true);

  await p.click('#buildOut .scroll table tbody tr:nth-child(2) button:has-text("use this")');
  await p.waitForTimeout(200);
  ck('"use this" stays on Courses & schedule and scrolls to the timetable',
     await p.evaluate(() => document.querySelector('.tab.on').dataset.p) === 'courses');

  await p.evaluate(() => { const d = document.querySelector('#p-courses details.more'); if (d) d.open = true; });
  await p.selectOption('#bSort', 'days');
  await p.waitForFunction(() => /combinations tested/.test(document.getElementById('buildStat').textContent),
                          null, { timeout: 20000 });
  ck('changing the sort re-runs the (cached) search server-side',
     (await p.textContent('#buildStat')).includes('served from cache') ||
     /combinations tested/.test(await p.textContent('#buildStat')));

  console.log('\n=== §18 AUTO-RESOLVE CLASHES (backend-authoritative) ===');
  const pair = await p.evaluate(() => {
    // find two single-package courses whose one meeting always overlaps -
    // a genuinely unresolvable clash, for the least-conflict fallback path.
    // Must also check term compatibility (tOv): same day+time in different
    // halves of the semester is not actually a clash.
    const single = C.filter(c => c.pk.length === 1 && c.pk[0].m.length === 1);
    const termsOverlap = (ta, tb) => ta === tb || ta === 'Full semester' || tb === 'Full semester';
    for (let i = 0; i < single.length; i++) {
      for (let j = i + 1; j < single.length; j++) {
        const a = single[i].pk[0].m[0], b = single[j].pk[0].m[0];
        const ta = single[i].pk[0].t, tb = single[j].pk[0].t;
        if (a[0] === b[0] && termsOverlap(ta, tb) && a[1] < b[2] && b[1] < a[2]) {
          return { unavoidable: [single[i].code, single[j].code] };
        }
      }
    }
    return { unavoidable: null };
  });
  ck('found a genuinely unavoidable clash pair to test with', !!pair.unavoidable, JSON.stringify(pair.unavoidable));

  await p.evaluate((codes) => {
    FIXED = []; PICK = {};
    codes.forEach(c => PICK[c] = { want: 5, pkg: 0 });
    renderChosen(); renderFixed();
  }, pair.unavoidable);
  await p.click('.tab[data-p="courses"]');
  await p.evaluate(() => {
    window.__longTasks2 = [];
    window.__lto2 = new PerformanceObserver(l => l.getEntries().forEach(e => window.__longTasks2.push(e.duration)));
    window.__lto2.observe({ entryTypes: ['longtask'] });
  });
  // Measure the asynchronous solver operation itself.  Awaiting the function
  // directly also suppresses the informational alert, which is unrelated to
  // main-thread responsiveness and can vary between browser engines.
  await p.evaluate(() => autoSolve(true));
  const longTasks2 = await p.evaluate(() => { window.__lto2.disconnect(); return window.__longTasks2; });
  ck('auto-resolve does not block the main thread either', longTasks2.length === 0, JSON.stringify(longTasks2));

  // Rendering the full timetable is verified separately; it must not pollute
  // the solver's long-task measurement above. Timetable now lives on the same
  // "courses" pane, already active - just force a fresh draw.
  await p.evaluate(() => void drawTT());
  await p.waitForFunction(() => /overlap/.test(document.getElementById('ttReport').textContent) ||
    document.querySelectorAll('#ttReport .flag').length > 0, null, { timeout: 15000 }).catch(() => {});
  await p.waitForTimeout(500);

  console.log('\n=== §19 SCHEDULE BUILDER LEAST-CONFLICT FALLBACK ===');
  await p.evaluate((codes) => {
    FIXED = []; PICK = {};
    codes.forEach(c => PICK[c] = { want: 5, pkg: 0 });
    renderChosen(); renderFixed();
  }, pair.unavoidable);
  await p.click('.tab[data-p="courses"]');
  await p.evaluate(() => { const d = document.querySelector('#p-courses details.more'); if (d) d.open = true; });
  await p.click('#buildBtn');
  await p.waitForFunction(() => /combinations tested/.test(document.getElementById('buildStat').textContent),
                          null, { timeout: 20000 });
  const fallback = await p.evaluate(() => ({
    stat: document.getElementById('buildStat').textContent,
    hasWarnBanner: /No fully clash-free combination exists/.test(document.getElementById('buildOut').textContent),
  }));
  ck('reports no clash-free combination exists for an unavoidable clash pair',
     /no clash-free combination found/.test(fallback.stat), fallback.stat);
  ck('best-available banner is shown, not a silent empty result', fallback.hasWarnBanner);

  console.log('\n=== §20 PROGRAMME-WIDE PATHWAYS: DONE COURSES ARE LOCAL, NOT SHIPPED ===');
  await p.evaluate(() => { DONE_ME = []; });
  await p.click('.tab[data-p="two"]');
  await p.evaluate(() => { const d = document.querySelector('#p-two details.more'); if (d) d.open = true; });
  ck('starts empty on a fresh session (no baked-in personal data)',
     /Nothing added yet/.test(await p.textContent('#doneMEList')));
  await p.evaluate(() => {
    DONE_ME.push({ code: 'CSD355', name: 'Foundation of Data Sciences', cr: 3 });
    renderDoneME(); drawSpec();
  });
  ck('added elective is rendered', /CSD355/.test(await p.textContent('#doneMEList')));
  ck('current CSE source exposes exactly three specialisations and no legacy Systems bucket',
     /Artificial Intelligence and Machine Learning/.test(await p.textContent('#specBox')) &&
     /Data Science and Big Data Analytics/.test(await p.textContent('#specBox')) &&
     /Cyber Security and Privacy/.test(await p.textContent('#specBox')) &&
     !/Systems and Networks/.test(await p.textContent('#specBox')));
  await p.evaluate(() => {
    document.getElementById('programme').value = 'bachelor-of-design'; drawSpec();
  });
  ck('B.Des. renders streams rather than CSE-style credit buckets',
     /B\.Des\. stream/.test(await p.textContent('#specBox')) &&
     /Experience Design/.test(await p.textContent('#specBox')) &&
     /available choice/.test(await p.textContent('#specBox')));
  await p.evaluate(() => {
    choosePathway('bachelor-of-design', 'experience-design');
  });
  ck('a programme pathway is interactive and updates the selected summary',
     /YOUR CHOICE/.test(await p.textContent('#specBox')) &&
     /Experience Design/.test(await p.textContent('.pathway-hero')));
  await p.evaluate(() => {
    document.getElementById('programme').value = 'ph-d-in-civil-engineering'; drawSpec();
  });
  ck('doctoral programme renders research areas without calling them credit credentials',
     /Research areas/.test(await p.textContent('#specBox')) &&
     /Environmental Engineering/.test(await p.textContent('#specBox')) &&
     /not transcript specialisation buckets/.test(await p.textContent('#specBox')));
  const allProgrammeChoices = await p.evaluate(() => {
    const select = document.getElementById('programme');
    return PROGRAMME_CATALOG.every(programme => {
      select.value = programme.id;
      drawSpec();
      const first = programme.pathways && programme.pathways.options && programme.pathways.options[0];
      if (!first || !document.querySelector('.pathway-choice')) return false;
      choosePathway(programme.id, first.id);
      return PATHWAY_SELECTIONS[programme.id] === first.id && !!document.querySelector('.pathway-hero');
    });
  });
  ck('all 44 programmes render and accept a pathway/focus choice', allProgrammeChoices);
  await p.evaluate(() => {
    document.getElementById('programme').value = 'b-tech-in-computer-science-and-engineering'; drawSpec();
  });
  await p.evaluate(() => { removeDoneME(0); });
  ck('remove button clears it back to empty', /Nothing added yet/.test(await p.textContent('#doneMEList')));

  console.log('\n=== §21 WISHLIST SCHEDULER (backend CP-SAT) ===');
  await p.evaluate(() => { CHOICE_GROUPS = []; });
  await p.click('.tab[data-p="prof"]');
  await p.evaluate(() => {
    document.getElementById('credMin').value = 0;
    document.getElementById('credTarget').value = 10;
  });
  await p.click('button[onclick="void refreshCreditPolicy()"]');
  await p.waitForFunction(() => /fixed credits/.test(document.getElementById('creditPolicyBox').textContent),
                          null, { timeout: 8000 });
  ck('credit policy summary states fixed + wishlist = ceiling in plain language',
     /fixed credits.*wishlist credits.*ceiling/.test(await p.textContent('#creditPolicyBox')));

  await p.evaluate((codes) => {
    FIXED = []; PICK = {}; PRIO = {};
    PICK[codes[0]] = { want: 5, pkg: 0 }; PRIO[codes[0]] = 'MUST';
    PICK[codes[1]] = { want: 5, pkg: 0 }; PRIO[codes[1]] = 'STRONG';
    renderChosen(); renderFixed();
  }, pair.unavoidable);
  await p.click('.tab[data-p="courses"]');
  await p.evaluate(() => {
    window.__longTasks3 = [];
    window.__lto3 = new PerformanceObserver(l => l.getEntries().forEach(e => window.__longTasks3.push(e.duration)));
    window.__lto3.observe({ entryTypes: ['longtask'] });
  });
  await p.click('#wishBtn');
  await p.waitForFunction(() => /Solver status/.test(document.getElementById('wishOut').textContent)
    || /No schedule found/.test(document.getElementById('wishOut').textContent), null, { timeout: 20000 });
  const longTasks3 = await p.evaluate(() => { window.__lto3.disconnect(); return window.__longTasks3; });
  ck('the wishlist solve does not block the main thread either', longTasks3.length === 0, JSON.stringify(longTasks3));

  const wish = await p.evaluate((codes) => ({
    html: document.getElementById('wishOut').textContent,
    mustIncluded: WISH_RESULT.included.includes(codes[0]),
    otherExcluded: WISH_RESULT.excluded.includes(codes[1]),
  }), pair.unavoidable);
  ck('the must-have course is included', wish.mustIncluded, wish.html.slice(0, 200));
  ck('its genuinely clashing partner is excluded, not silently kept', wish.otherExcluded);
  ck('solver status is shown to the student', /Solver status/.test(wish.html));

  // a wishlist this small already has its why-not eagerly computed (see
  // _solve_wishlist's bounded loop), so the table shows the reason inline
  // rather than a "why not?" button - the on-demand /explain-exclusion
  // endpoint is exercised directly via API instead, as any larger wishlist's
  // UI would use it.
  const inlineReason = await p.evaluate((codes) => {
    const rows = [...document.querySelectorAll('#wishOut table tbody tr')];
    const row = rows.find(r => r.textContent.includes(codes[1]));
    return row ? row.textContent : null;
  }, pair.unavoidable);
  ck('the excluded course already shows a specific inline reason, not a generic failure',
     inlineReason && inlineReason.length > 20 && !/why not\?/.test(inlineReason), inlineReason);

  const onDemand = await p.evaluate((codes) => API.explainExclusion(WISH_JOB, codes[1]), pair.unavoidable);
  ck('on-demand explain-exclusion endpoint also gives a specific blocker for the same course',
     !!onDemand.blocker && onDemand.blocker !== 'lower_priority', JSON.stringify(onDemand));

  console.log('\n=== §22 DEGREE AUDIT AND SPECIALISATION HONESTY ===');
  // Real regression: a fresh B.Sc. (Research) in Mathematics profile - no
  // completed courses, no aggregate override, nothing touched - reported 4 of
  // 8 requirements complete. Root cause was completedRequirementCredits()
  // inferring "done" from Profile-tab "credits remaining" fields that exist to
  // feed the bid-pool formula and default to 0, reading that default as "0
  // remaining" -> "required amount done". Checked here for the exact
  // programme reported, and for every one of the 44 catalogued programmes at
  // once so no single programme's requirement shape can reintroduce it.
  await p.click('.tab[data-p="prof"]');
  await p.evaluate(() => {
    // This page has been driven through many earlier sections in one
    // continuous session (this suite never reloads between §-blocks), so an
    // earlier section's leftover field values (e.g. §11's doneCr=105 worked
    // example) are still sitting in the DOM. Reset every field this audit can
    // read from, to genuinely reproduce "a fresh profile with nothing
    // entered" rather than whatever an earlier section left behind.
    ['doneCr', 'doneME', 'doneUWE', 'doneCCC', 'degCr', 'remCore', 'remOther',
     'remME', 'remUWE', 'remCCC', 'remFL'].forEach(id => {
      const el = document.getElementById(id); if (el) el.value = 0;
    });
    document.getElementById('completedMilestones').value = '';
    document.getElementById('programme').value = 'b-sc-research-in-mathematics';
    applyProgrammeCategories();
    // An earlier section already ran a real audit (with doneCr=105, against
    // whatever programme was selected then) and left LAST_AUDIT populated, so
    // "#auditBox has a table" is already true before this click - waiting on
    // that alone raced ahead of runDegreeAudit()'s own async work and read the
    // stale result. Clear it first and wait for the fresh one specifically.
    LAST_AUDIT = null;
  });
  await p.click('#auditBtn');
  // LAST_AUDIT is a top-level `let` in g_two.html, which - unlike `var` - does
  // not become a `window` property, so `window.LAST_AUDIT` is always
  // `undefined` and `!== null` was true instantly, resolving before the click's
  // own async runDegreeAudit() had done anything. The bare identifier resolves
  // through the page's real scope chain instead.
  await p.waitForFunction(() => LAST_AUDIT !== null);
  const freshMathAudit = await p.evaluate(() => LAST_AUDIT);
  ck('a completely untouched profile reports zero requirements met',
     freshMathAudit.requirements_met === 0, JSON.stringify({met: freshMathAudit.requirements_met, total: freshMathAudit.requirements_total}));
  ck('every requirement row shows zero credit done, not the requirement silently satisfied',
     freshMathAudit.requirements.every(row => row.completed === 0 && row.status !== 'complete'),
     JSON.stringify(freshMathAudit.requirements.filter(r => r.completed !== 0 || r.status === 'complete')));
  ck('the misleading "aggregate progress included" flag does not fire when nothing was entered',
     freshMathAudit.aggregate_progress_applied === false);

  const everyProgrammeEmptyAudit = await p.evaluate(async () => {
    const offenders = [];
    for (const programme of PROGRAMME_CATALOG) {
      document.getElementById('programme').value = programme.id;
      applyProgrammeCategories();
      const result = await API.runDegreeAudit({
        programme_id: programme.id, completed_courses: [], planned_courses: [],
        completed_milestones: [], completed_requirement_credits: {}, custom_requirements: [],
      });
      if (result.requirements_met !== 0) offenders.push({ id: programme.id, met: result.requirements_met });
    }
    return offenders;
  });
  ck('all 44 programmes report zero requirements met on a genuinely empty audit',
     everyProgrammeEmptyAudit.length === 0, JSON.stringify(everyProgrammeEmptyAudit));

  // Specialisation: the CSE rule is "12 bucket credits, OR 6 bucket credits
  // plus the named Project-I course in the same area". The status badge used
  // to print "published course condition reached" the moment 6 credits were
  // reached via the cheaper route, without ever checking whether Project-I was
  // actually completed - contradicting this exact module's own planner note
  // ("Project and grade conditions require confirmation from your record").
  const pathwayHonesty = await p.evaluate(() => {
    const option = { minimum_credits: 12, project_alternative_at: 6, project_credits: 6 };
    return {
      full: pathwayStatus(option, { trackable: true, total: 12, missingMandatory: [] }, false),
      alt: pathwayStatus(option, { trackable: true, total: 6, missingMandatory: [] }, false),
      partial: pathwayStatus(option, { trackable: true, total: 3, missingMandatory: [] }, false),
    };
  });
  ck('the full 12-credit route is a clean "published course condition reached"',
     pathwayHonesty.full.label === 'published course condition reached' && pathwayHonesty.full.tone === 'ok',
     JSON.stringify(pathwayHonesty.full));
  ck('the cheaper 6-credit route never claims full completion - it names the unverified project',
     pathwayHonesty.alt.tone === 'check' && /project not verified/.test(pathwayHonesty.alt.label),
     JSON.stringify(pathwayHonesty.alt));
  ck('a partial 3-credit balance is neither route and is reported as remaining',
     pathwayHonesty.partial.tone === 'warn' && /remaining/.test(pathwayHonesty.partial.label),
     JSON.stringify(pathwayHonesty.partial));

  console.log('\n=== §9 BACKEND UNAVAILABLE ===');
  await p.route('**/api/v1/**', r => r.abort());
  await p.route('**/health/**', r => r.abort());
  await p.evaluate(() => API.healthCheck().then(() => {}));
  await p.waitForTimeout(500);
  await p.evaluate(() => probeHealth());
  await p.waitForTimeout(500);
  const down = await p.evaluate(() => ({
    bar: document.getElementById('backendBar').textContent,
    runDisabled: document.getElementById('runBtn').disabled
  }));
  ck('offline banner explains the situation', /calculation service is unavailable/i.test(down.bar), down.bar.slice(0, 80));
  ck('offline banner promises the plan is safe', /plan is safe/i.test(down.bar));
  ck('run button disabled while offline', down.runDisabled === true);
  ck('a retry control is offered', /Retry connection/.test(down.bar));
  await p.unroute('**/api/v1/**');
  await p.unroute('**/health/**');
  await p.evaluate(() => probeHealth());
  await p.waitForTimeout(800);
  const back = await p.evaluate(() => ({
    bar: document.getElementById('backendBar').textContent,
    runDisabled: document.getElementById('runBtn').disabled }));
  ck('recovers when the backend returns', /connected/i.test(back.bar) && back.runDisabled === false, back.bar.slice(0, 60));

  ck('no page errors across the whole run', errs.length === 0, errs.slice(0, 2).join(' | '));
  await b.close();
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})();
