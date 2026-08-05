/* §10/§11 plan store: CRUD, migrations, corruption recovery, hostile imports. */
let pass = 0, fail = 0;
const ck = (n, c, x) => { console.log((c ? '  PASS  ' : '  FAIL  ') + n + (x ? '   [' + x + ']' : '')); c ? pass++ : fail++; };

function fresh(seed) {
  const store = Object.assign({}, seed || {});
  globalThis.localStorage = {
    getItem: k => store[k] ?? null,
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: k => { delete store[k]; },
    _dump: () => store
  };
  delete require.cache[require.resolve('../src/plans.js')];
  return { P: require('../src/plans.js'), store };
}

console.log('=== §10 SAVED PLANS ===');
{
  const { P } = fresh();
  const a = P.save('Plan A', { pools: { ME: 297, UWE: 215, CCC: 492 }, courses: [{ code: 'CSD358', credits: 3 }] });
  ck('save returns a record with an id', !!(a && a.id));
  ck('active plan is set on save', P.activeId() === a.id);
  const b = P.save('Plan B', { pools: { ME: 1, UWE: 1, CCC: 1 }, courses: [] });
  ck('two plans coexist', P.list().length === 2);
  ck('list is newest-first', P.list()[0].id === b.id);
  ck('rename works', P.rename(a.id, 'Renamed') && P.get(a.id).name === 'Renamed');
  const d = P.duplicate(a.id, 'Copy of A');
  ck('duplicate creates a new id with the same payload',
     d.id !== a.id && JSON.stringify(d.payload) === JSON.stringify(P.get(a.id).payload));
  ck('delete removes only the target', P.remove(d.id) && P.list().length === 2 && !!P.get(a.id));
  ck('round-trip payload is intact', P.get(a.id).payload.courses[0].code === 'CSD358');
  ck('timestamps present', typeof P.get(a.id).created === 'number' && typeof P.get(a.id).updated === 'number');
}
{
  const { P } = fresh();
  const rec = P.save('X', { pools: { ME: 1, UWE: 1, CCC: 1 }, courses: [] });
  P.setActive(rec.id);
  const { P: P2 } = fresh(globalThis.localStorage._dump());
  ck('active plan survives a reload', P2.activeId() === rec.id && !!P2.get(rec.id));
}
{
  // corrupted storage must be quarantined, not silently wiped
  const { P, store } = fresh({ 'snu.plans.v1': '{not json' });
  const l = P.list();
  ck('corrupted storage recovers to an empty list', Array.isArray(l) && l.length === 0);
  const quarantined = Object.keys(globalThis.localStorage._dump()).some(k => k.includes('.corrupt.'));
  ck('the corrupted blob is quarantined for inspection', quarantined);
}
{
  // migrations
  const v1 = JSON.stringify({ schema: 1, plans: { p1: { id: 'p1', name: 'old', created: 1, updated: 1,
    payload: { pools: { ME: 1, UWE: 1, CCC: 1 }, courses: ['CSD358', 'CSD361'] } } } });
  const { P } = fresh({ 'snu.plans.v1': v1 });
  const p = P.get('p1');
  ck('v1 course-code array migrates to objects',
     Array.isArray(p.payload.courses) && typeof p.payload.courses[0] === 'object'
     && p.payload.courses[0].code === 'CSD358');
  ck('v3 adds an assumptions block', !!p.payload.assumptions && p.payload.assumptions.budgetMode === 'SHARED_LIVE');
  ck('v4 adds degree-progress profile fields with safe defaults',
     p.payload.profile.doneCr === 0 && p.payload.profile.degCr === 160);
  ck('v4 adds empty fixed/manual arrays', Array.isArray(p.payload.fixed) && Array.isArray(p.payload.manual));
  ck('v5 adds an empty doneElectives array', Array.isArray(p.payload.doneElectives) && p.payload.doneElectives.length === 0);
  ck('v6 adds an empty choiceGroups array', Array.isArray(p.payload.choiceGroups) && p.payload.choiceGroups.length === 0);
  ck('v6 adds a neutral creditPolicy block',
     !!p.payload.creditPolicy && p.payload.creditPolicy.overloadOn === false && p.payload.creditPolicy.min === 0);
}
{
  const { P } = fresh();
  // NOTE: `'__proto__' in obj` is always true via Object.prototype, so the correct
  // assertion is that it is not an OWN property and that Object.prototype is clean.
  const hasOwn = (o, k) => Object.prototype.hasOwnProperty.call(o, k);
  const raw = JSON.parse('{"__proto__":{"polluted":1},"ok":2}');
  ck('JSON.parse really does create __proto__ as an own property', hasOwn(raw, '__proto__'));
  const bad = P.sanitise(raw);
  ck('sanitise strips prototype-pollution keys',
     bad.ok === 2 && !hasOwn(bad, '__proto__') && ({}).polluted === undefined);
  const nested = P.sanitise(JSON.parse('{"a":{"constructor":{"x":1},"b":3}}'));
  ck('sanitise strips dangerous keys at every depth',
     nested.a.b === 3 && !hasOwn(nested.a, 'constructor'));
}

console.log('\n=== §11 IMPORT / EXPORT ===');
{
  const { P } = fresh();
  const payload = { pools: { ME: 297, UWE: 215, CCC: 492 },
                    courses: [{ code: 'CSD358', credits: 3, seats: 120, priority: 'MUST' }],
                    assumptions: { headlineMode: 'HIGH', budgetMode: 'SHARED_LIVE' } };
  const json = P.exportJson(payload, { ruleVersion: '2026.M.3', modelVersion: 'competition-v3' });
  const v = P.validateImport(json);
  ck('exported JSON validates', v.ok, (v.errors || []).join('; '));
  ck('export records the rule version', JSON.parse(json).ruleVersion === '2026.M.3');
  ck('round-trip equality', JSON.stringify(v.payload) === JSON.stringify(P.sanitise(payload)));
  const r = P.importAsNewPlan(json, 'From file');
  ck('import as new plan succeeds', r.ok && P.list().length === 1);
  const before = P.list().length;
  const r2 = P.importReplacingActive(json);
  ck('import replacing active does not add a plan', r2.ok && P.list().length === before);
}
{
  const { P } = fresh();
  const cases = [
    ['corrupted JSON', '{not json'],
    ['array at top level', '[1,2,3]'],
    ['missing schemaVersion', JSON.stringify({ payload: { courses: [] } })],
    ['newer schema', JSON.stringify({ schemaVersion: 99, payload: { courses: [] } })],
    ['missing payload', JSON.stringify({ schemaVersion: 3 })],
    ['negative pool', JSON.stringify({ schemaVersion: 3, payload: { pools: { ME: -5, UWE: 1, CCC: 1 } } })],
    ['implausible pool', JSON.stringify({ schemaVersion: 3, payload: { pools: { ME: 1e9, UWE: 1, CCC: 1 } } })],
    ['courses not an array', JSON.stringify({ schemaVersion: 3, payload: { courses: {} } })],
    ['invalid credits', JSON.stringify({ schemaVersion: 3, payload: { courses: [{ code: 'A', credits: 99 }] } })],
    ['impossible seats', JSON.stringify({ schemaVersion: 3, payload: { courses: [{ code: 'A', seats: -3 }] } })],
    ['fractional seats', JSON.stringify({ schemaVersion: 3, payload: { courses: [{ code: 'A', seats: 2.5 }] } })],
    ['duplicate course ids', JSON.stringify({ schemaVersion: 3, payload: { courses: [{ code: 'A' }, { code: 'A' }] } })],
    ['malformed meeting', JSON.stringify({ schemaVersion: 3, payload: { courses: [{ code: 'A', meetings: [{ start: 600, end: 500 }] }] } })],
    ['negative live bidders', JSON.stringify({ schemaVersion: 3, payload: { courses: [{ code: 'A', liveBidders: -2 }] } })],
    // built as a raw string: `{__proto__: ...}` in JS literal syntax sets the prototype
    // rather than creating an own property, so JSON.stringify would silently drop it.
    ['prototype pollution key', '{"schemaVersion":3,"payload":{"__proto__":{"x":1},"courses":[]}}'],
    ['constructor pollution key', '{"schemaVersion":3,"payload":{"constructor":{"x":1},"courses":[]}}'],
    ['oversized file', JSON.stringify({ schemaVersion: 3, payload: { note: 'x'.repeat(2.2 * 1024 * 1024) } })]
  ];
  let rejected = 0;
  cases.forEach(([name, text]) => {
    const v = P.validateImport(text);
    if (!v.ok) rejected++;
    else console.log('        NOT REJECTED: ' + name);
  });
  ck(`all ${cases.length} hostile imports rejected with reasons`, rejected === cases.length,
     rejected + '/' + cases.length);
  ck('nothing was written to storage by a failed import', P.list().length === 0);
}
{
  const { P } = fresh();
  const older = JSON.stringify({ schemaVersion: 2, payload: { pools: { ME: 1, UWE: 1, CCC: 1 }, courses: [] } });
  const v = P.validateImport(older);
  ck('an older schema imports with a migration warning', v.ok && v.warnings.length > 0, (v.warnings || []).join(';'));
}
{
  const { P } = fresh();
  const recs = [{ code: 'CSD358', category: 'ME', priority: 'MUST', credits: 3, cap: 75, bid: 75,
                  worst_tested: 0.445, expected_charge: 70, target_met: false,
                  demand: { source: 'stress-default', expected_rivals: 348, seats: 120 } }];
  const csv = P.exportCsv(recs);
  ck('CSV has a header and one data row', csv.split('\n').length === 2);
  ck('CSV includes the demand provenance', /stress-default/.test(csv));
  const tricky = P.exportCsv([{ code: 'A,B', category: 'ME', priority: 'x"y', cap: 1, bid: 1 }]);
  ck('CSV quotes commas and escapes quotes', tricky.includes('"A,B"') && tricky.includes('"x""y"'));
  const summary = P.printableSummary({ pools: { ME: 297, UWE: 215, CCC: 492 } },
                                     { recommendations: recs, rule_version: 'r', model_version: 'm',
                                       trials: 8000, seed: 1, budget_mode: 'SHARED_LIVE' });
  ck('printable summary includes the not-a-guarantee caveat', /not guarantees/.test(summary));
  ck('printable summary flags the unconfirmed budget rule', /NOT officially confirmed/.test(summary));
}
console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
