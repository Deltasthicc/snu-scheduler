/* =====================================================================
   SCHEDULE SCREENSHOT

   Renders the current weekly timetable to an off-screen <canvas> and
   exports it as a PNG - a one-click "picture of my schedule" that avoids
   pulling in a DOM-to-image library (html2canvas and friends are large,
   and this project's build is a single self-contained bundle with no
   external script dependencies at all).

   Colours and fonts are read live via getComputedStyle from the actual
   page - not a second, hand-copied palette - so the image always matches
   whatever theme (light/dark/system) the student is currently using and
   never drifts from a_head.html's real CSS the way a hardcoded copy
   eventually would.

   Pure rendering/export of data already on the page, not scheduling or
   clash math - the clash check below is the same mOv()/tOv() pairwise
   comparison c_core.html already runs for the live grid, so a screenshot
   shows the exact same overlaps the Timetable tab shows. Kept off the
   backend per CLAUDE.md design decision §5.4, same as roomLocation().
   ===================================================================== */
(function (factory) {
  var g = (typeof globalThis !== 'undefined' && globalThis)
       || (typeof self !== 'undefined' && self)
       || (typeof window !== 'undefined' && window)
       || this;
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else g.SNAPSHOT = factory();
}(function () {

  const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

  function readTheme() {
    const root = getComputedStyle(document.documentElement);
    const v = (name, fallback) => (root.getPropertyValue(name) || '').trim() || fallback;

    const probe = document.createElement('div');
    probe.style.cssText = 'position:absolute;visibility:hidden;pointer-events:none;left:-9999px;top:-9999px';
    document.body.appendChild(probe);
    const evColors = {};
    ['fix', 'me', 'uwe', 'ccc', 'nb'].forEach(kind => {
      probe.className = 'ev ' + kind;
      const s = getComputedStyle(probe);
      evColors[kind] = { bg: s.backgroundColor, fg: s.color, accent: s.borderLeftColor };
    });
    probe.remove();

    const monoEl = document.querySelector('.tiny, .mono') || document.body;
    const displayEl = document.querySelector('h1, h2') || document.body;

    return {
      page: v('--s1', '#12181D'), panel: v('--s2', '#171F26'), line: v('--line', 'rgba(148,164,177,.25)'),
      text: v('--tx', '#E7EAED'), dim: v('--dim', '#94A4B1'), faint: v('--faint', '#93A1AD'),
      bad: v('--bad', '#E08A85'),
      ev: evColors,
      monoFont: getComputedStyle(monoEl).fontFamily || 'monospace',
      displayFont: getComputedStyle(displayEl).fontFamily || 'sans-serif'
    };
  }

  function roundRect(ctx, x, y, w, h, r) {
    r = Math.min(r, w / 2, h / 2);
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  function fmtHm(m) { return (typeof hm === 'function') ? hm(m) : String(m); }
  function locOf(room) { return (typeof roomLocation === 'function') ? roomLocation(room) : { short: '', label: '' }; }

  // canvas has no CSS text-overflow:ellipsis - narrow side-by-side blocks
  // (two clashing meetings sharing a slot) are often too tight for a full
  // time range, and a raw ctx.clip() alone just chops the text off mid-
  // character ("PRAC 9:00A"), which reads as broken rather than truncated.
  function fitText(ctx, text, maxWidth) {
    if (ctx.measureText(text).width <= maxWidth) return text;
    const ell = '…';
    let lo = 0, hi = text.length;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (ctx.measureText(text.slice(0, mid) + ell).width <= maxWidth) lo = mid; else hi = mid - 1;
    }
    return lo ? text.slice(0, lo) + ell : ell;
  }

  /* events: array of {m:[day,startMin,endMin,component,section,room], term, code, title, kind}
     opts: { title, manual: [{name,cr}] } */
  function renderCanvas(events, opts) {
    opts = opts || {};
    const rows = (events || []).filter(e => e && e.m);
    if (!rows.length) return null;
    const theme = readTheme();

    const badKey = new Set();
    for (let i = 0; i < rows.length; i++) for (let j = i + 1; j < rows.length; j++) {
      const a = rows[i], b = rows[j];
      if (a.code !== b.code && typeof mOv === 'function' && mOv(a.m, b.m, a.term, b.term)) { badKey.add(i); badKey.add(j); }
    }

    let lo = Math.min(...rows.map(e => e.m[1])), hi = Math.max(...rows.map(e => e.m[2]));
    lo = Math.floor(lo / 60) * 60; hi = Math.ceil(hi / 60) * 60;

    const PAD = 26, TIME_COL = 60, DAY_W = 196, HEAD_H = 96, MIN_PX = 1.3;
    const manual = (opts.manual || []).filter(Boolean);
    const gridW = TIME_COL + DAY_W * 6;
    const gridH = Math.max(240, (hi - lo) * MIN_PX);
    const legendH = 40;
    const manualH = manual.length ? 22 + manual.length * 16 : 0;
    const W = PAD * 2 + gridW;
    const H = PAD * 2 + HEAD_H + gridH + legendH + manualH + 16;

    const dpr = (typeof window !== 'undefined' && window.devicePixelRatio) || 1;
    const SCALE = Math.max(2, Math.min(4, dpr * 2)); // "high fidelity" per the feature request - never below 2x
    const canvas = document.createElement('canvas');
    canvas.width = Math.round(W * SCALE);
    canvas.height = Math.round(H * SCALE);
    const ctx = canvas.getContext('2d');
    ctx.scale(SCALE, SCALE);
    ctx.textBaseline = 'alphabetic';

    ctx.fillStyle = theme.page; ctx.fillRect(0, 0, W, H);

    ctx.fillStyle = theme.text;
    ctx.font = '700 21px ' + theme.displayFont;
    ctx.fillText(opts.title || 'Weekly schedule', PAD, PAD + 22);
    ctx.font = '500 11px ' + theme.monoFont;
    ctx.fillStyle = theme.faint;
    ctx.fillText('Generated ' + new Date().toLocaleString(), PAD, PAD + 40);

    const gx = PAD, gy = PAD + HEAD_H;

    ctx.textAlign = 'center';
    DAYS.forEach((d, i) => {
      const cx = gx + TIME_COL + i * DAY_W + DAY_W / 2;
      ctx.fillStyle = theme.panel; ctx.fillRect(gx + TIME_COL + i * DAY_W + 2, gy - 28, DAY_W - 4, 22);
      ctx.fillStyle = theme.dim; ctx.font = '600 11px ' + theme.monoFont;
      ctx.fillText(d, cx, gy - 12);
    });
    ctx.textAlign = 'left';

    ctx.strokeStyle = theme.line; ctx.lineWidth = 1;
    ctx.fillStyle = theme.faint; ctx.font = '500 10px ' + theme.monoFont;
    for (let t = lo; t <= hi; t += 60) {
      const y = gy + (t - lo) * MIN_PX;
      ctx.beginPath(); ctx.moveTo(gx + TIME_COL, y); ctx.lineTo(gx + TIME_COL + DAY_W * 6, y); ctx.stroke();
      ctx.fillText(fmtHm(t).replace(':00', ''), gx, y + 4);
    }
    for (let i = 0; i <= 6; i++) {
      const x = gx + TIME_COL + i * DAY_W;
      ctx.beginPath(); ctx.moveTo(x, gy); ctx.lineTo(x, gy + gridH); ctx.strokeStyle = theme.line; ctx.stroke();
    }

    DAYS.forEach((d, di) => {
      const dayRows = rows.map((e, i) => ({ e, i })).filter(x => x.e.m[0] === di).sort((a, b) => a.e.m[1] - b.e.m[1]);
      const lanes = [];
      dayRows.forEach(x => { let L = 0; while (lanes[L] !== undefined && lanes[L] > x.e.m[1]) L++; lanes[L] = x.e.m[2]; x.lane = L; });
      const nl = Math.max(1, lanes.length);
      const colX = gx + TIME_COL + di * DAY_W;
      dayRows.forEach(x => {
        const e = x.e;
        const top = gy + (e.m[1] - lo) * MIN_PX;
        const h = Math.max(26, (e.m[2] - e.m[1]) * MIN_PX - 2);
        const w = (DAY_W - 4) / nl;
        const left = colX + 2 + x.lane * w;
        const c = theme.ev[e.kind === 'nb' ? 'fix' : e.kind] || theme.ev.fix;

        ctx.fillStyle = c.bg;
        roundRect(ctx, left, top, w - 2, h, 5); ctx.fill();
        if (badKey.has(x.i)) {
          ctx.lineWidth = 2; ctx.strokeStyle = theme.bad;
          roundRect(ctx, left + 1, top + 1, w - 4, h - 2, 5); ctx.stroke();
        } else {
          ctx.fillStyle = c.accent; ctx.fillRect(left, top, 3, Math.min(h, gy + gridH - top));
        }

        const loc = locOf(e.m[5]);
        const textW = w - 10;
        ctx.save();
        ctx.beginPath(); ctx.rect(left + 3, top, w - 7, h); ctx.clip();
        ctx.fillStyle = c.fg;
        ctx.font = '700 10.5px ' + theme.monoFont;
        ctx.fillText(fitText(ctx, String(e.code).split('/')[0], textW), left + 6, top + 13);
        ctx.font = '500 9.5px ' + theme.monoFont;
        const whenText = `${e.m[3] || ''} ${fmtHm(e.m[1]).replace(' ', '')}–${fmtHm(e.m[2]).replace(' ', '')}`;
        ctx.fillText(fitText(ctx, whenText, textW), left + 6, top + 25);
        if (loc.short && h > 36) ctx.fillText(fitText(ctx, loc.short, textW), left + 6, top + 37);
        ctx.restore();
      });
    });

    const legendY = gy + gridH + 26;
    const items = [['Fixed', theme.ev.fix.bg], ['ME', theme.ev.me.bg], ['UWE', theme.ev.uwe.bg],
                   ['CCC', theme.ev.ccc.bg], ['Clash', theme.bad]];
    let lx = gx;
    ctx.font = '600 10.5px ' + theme.monoFont;
    items.forEach(([label, color]) => {
      ctx.fillStyle = color; ctx.fillRect(lx, legendY - 9, 12, 12);
      ctx.fillStyle = theme.dim; ctx.fillText(label, lx + 17, legendY);
      lx += 17 + ctx.measureText(label).width + 22;
    });

    if (manual.length) {
      let my = legendY + 22;
      ctx.fillStyle = theme.faint; ctx.font = '600 10px ' + theme.monoFont;
      ctx.fillText('ALSO ENROLLED (OFF-TIMETABLE)', gx, my);
      ctx.fillStyle = theme.dim; ctx.font = '500 10.5px ' + theme.monoFont;
      manual.forEach(m => { my += 16; ctx.fillText(`${m.name} · ${m.cr} cr`, gx, my); });
    }

    return canvas;
  }

  function downloadPng(canvas, filename) {
    return new Promise(resolve => {
      canvas.toBlob(blob => {
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = filename || 'schedule.png';
        document.body.appendChild(a); a.click(); a.remove();
        setTimeout(() => URL.revokeObjectURL(a.href), 4000);
        resolve(blob);
      }, 'image/png');
    });
  }

  async function captureAndDownload(events, opts) {
    if (typeof document !== 'undefined' && document.fonts && document.fonts.ready) {
      try { await document.fonts.ready; } catch (e) { /* proceed with whatever is loaded */ }
    }
    const canvas = renderCanvas(events, opts || {});
    if (!canvas) return null;
    return downloadPng(canvas, (opts && opts.filename) || 'schedule.png');
  }

  return { renderCanvas, downloadPng, captureAndDownload };
}));
