/* =====================================================================
   CLIENT-SIDE CLASH PREVIEW ONLY  (§2)

   §2 permits the browser to do "lightweight clash previews". This file is
   what remains of the old core/engine.js after the authoritative functions
   were removed:

     REMOVED -> computePools()      now GET/POST /api/v1/pools
     REMOVED -> settleAuction()     now POST    /api/v1/settlement
     REMOVED -> netCeiling(), selectedCredits(), validateClashStructure()
                (moved to the backend contract / validate-plan)
     KEPT    -> pure time-overlap arithmetic, which is display logic

   Nothing here computes a bid, a probability, a pool or a price.
   ===================================================================== */
(function (factory) {
  var g = (typeof globalThis !== 'undefined' && globalThis)
       || (typeof self !== 'undefined' && self)
       || (typeof window !== 'undefined' && window) || this;
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else g.CLASH = factory();
}(function () {
  // "Both half" (a CCC running in both halves at the same weekly slot) occupies
  // that slot all semester, so it overlaps exactly what "Full semester" does.
  // Must stay identical to backend app/services/scheduler.py::_term_overlap.
  function spansBothHalves(t) { return t === 'Full semester' || t === 'Both half'; }
  function termsOverlap(a, b) {
    if (a === b) return true;
    return spansBothHalves(a) || spansBothHalves(b);
  }
  function meetingsOverlap(m1, t1, m2, t2) {
    if (m1.d !== m2.d) return false;
    if (!termsOverlap(t1, t2)) return false;
    return m1.st < m2.en && m2.st < m1.en;
  }
  function findConflicts(setA, setB) {
    const out = [];
    setA.forEach(a => setB.forEach(b => {
      if (a.code === b.code) return;
      if (meetingsOverlap(a.m, a.term, b.m, b.term)) {
        out.push({ a: a.code, b: b.code, day: a.m.d,
                   start: Math.max(a.m.st, b.m.st), end: Math.min(a.m.en, b.m.en) });
      }
    }));
    return out;
  }
  return { termsOverlap, meetingsOverlap, findConflicts };
}));
