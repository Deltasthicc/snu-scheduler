/* =====================================================================
   CALENDAR EXPORT

   Turns the currently chosen schedule into a standard .ics (iCalendar)
   file - the one format Google Calendar, Apple Calendar, Outlook, and
   Samsung Calendar (which syncs through the Android calendar provider)
   all import natively, so one export covers "whatever you choose"
   without a separate integration per provider.

   Pure formatting of data already sitting in the browser (course code,
   meeting time, room), not scheduling or clash math, so it stays on the
   client side per this project's own compute-authority rule (see
   CLAUDE.md design decision §5.4) - same category as the CSV/JSON export
   already in plans.js.

   No semester start/end date is hardcoded anywhere in this codebase
   (checked: neither rules.py nor docs/RULES_REFERENCE.md carries one,
   because the University has never published one to this project). This
   module never invents one either - the caller must supply real dates,
   which the UI collects directly from the student rather than guessing.
   ===================================================================== */
(function (factory) {
  var g = (typeof globalThis !== 'undefined' && globalThis)
       || (typeof self !== 'undefined' && self)
       || (typeof window !== 'undefined' && window)
       || this;
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else g.CAL = factory();
}(function () {

  const DOW = ['MO', 'TU', 'WE', 'TH', 'FR', 'SA', 'SU']; // index 0=Mon, matching m[0]
  const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

  function pad(n, l) { l = l || 2; return String(n).padStart(l, '0'); }

  function parseISODate(s) {
    // local-midnight Date, not UTC - avoids the classic "one day early in
    // some timezones" bug that new Date("YYYY-MM-DD") alone is prone to.
    const [y, m, d] = s.split('-').map(Number);
    return new Date(y, m - 1, d);
  }

  function requireDate(s, label) {
    if (!s || !ISO_DATE.test(s)) throw new Error(label + ' must be set to a valid date first.');
    const d = parseISODate(s);
    if (isNaN(d.getTime())) throw new Error(label + ' is not a valid calendar date.');
    return d;
  }

  function addDays(d, n) { const r = new Date(d); r.setDate(r.getDate() + n); return r; }

  // first date on/after `from` that falls on weekday `dow` (0=Mon..6=Sun)
  function firstOnOrAfter(from, dow) {
    const fromDow = (from.getDay() + 6) % 7; // JS getDay(): 0=Sun -> rotate to 0=Mon
    const delta = (dow - fromDow + 7) % 7;
    return addDays(from, delta);
  }

  function dateOnly(d) { return pad(d.getFullYear(), 4) + pad(d.getMonth() + 1) + pad(d.getDate()); }

  function icsUtcStamp(d) {
    return pad(d.getUTCFullYear(), 4) + pad(d.getUTCMonth() + 1) + pad(d.getUTCDate()) + 'T'
      + pad(d.getUTCHours()) + pad(d.getUTCMinutes()) + pad(d.getUTCSeconds()) + 'Z';
  }

  function icsLocalTime(minutes) {
    const hh = Math.floor(minutes / 60), mm = minutes % 60;
    return pad(hh) + pad(mm) + '00';
  }

  // RFC 5545 line folding at 75 octets. Every field this app emits is short
  // ASCII (course codes, times, room labels), so a plain length check is
  // enough - no need for the octet-boundary-aware folding a general library
  // would require for multi-byte UTF-8 text.
  function foldLine(line) {
    if (line.length <= 75) return line;
    const out = [];
    let rest = line;
    while (rest.length > 75) { out.push(rest.slice(0, 75)); rest = ' ' + rest.slice(75); }
    out.push(rest);
    return out.join('\r\n');
  }

  function escText(s) {
    return String(s == null ? '' : s)
      .replace(/\\/g, '\\\\').replace(/;/g, '\\;').replace(/,/g, '\\,').replace(/\n/g, '\\n');
  }

  const HALF_TERMS = { 'First half': 'start', 'Second half': 'end' };

  /* events: array of {m:[day,startMin,endMin,component,section,room], term, code, title}
     opts: { semesterStart, semesterEnd, midpoint? } - all "YYYY-MM-DD" strings.
     midpoint is only required if the schedule actually has a First/Second-half
     course; when omitted for a full-semester-only schedule it is simply unused. */
  function buildIcs(events, opts) {
    opts = opts || {};
    const start = requireDate(opts.semesterStart, 'Semester start date');
    const end = requireDate(opts.semesterEnd, 'Semester end date');
    if (!(start < end)) throw new Error('Semester end date must be after the semester start date.');
    const rows = (events || []).filter(e => e && e.m);
    const needsMidpoint = rows.some(e => HALF_TERMS[e.term]);
    let mid = null;
    if (opts.midpoint) {
      mid = requireDate(opts.midpoint, 'Half-semester boundary date');
      if (!(start < mid && mid < end)) {
        throw new Error('Half-semester boundary date must fall between the semester start and end dates.');
      }
    } else if (needsMidpoint) {
      throw new Error('This schedule has a half-semester course - set the half-semester boundary date too.');
    }

    const lines = [
      'BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//SNU Scheduler//Timetable Export//EN',
      'CALSCALE:GREGORIAN', 'METHOD:PUBLISH',
      'X-WR-CALNAME:' + escText(opts.calendarName || 'My schedule'),
      'X-WR-TIMEZONE:Asia/Kolkata',
      // Asia/Kolkata has no daylight-saving transitions, so a single fixed
      // offset covers every date this app will ever export - no RRULE-driven
      // transition rules needed, unlike most other timezones.
      'BEGIN:VTIMEZONE', 'TZID:Asia/Kolkata', 'BEGIN:STANDARD',
      'DTSTART:19700101T000000', 'TZOFFSETFROM:+0530', 'TZOFFSETTO:+0530', 'TZNAME:IST',
      'END:STANDARD', 'END:VTIMEZONE'
    ];

    const now = new Date();
    const seen = new Set();
    rows.forEach(e => {
      const key = [e.code, e.m[0], e.m[1], e.m[2], e.m[3], e.m[4]].join('|');
      if (seen.has(key)) return; // the same package can list a meeting twice if two components share a slot
      seen.add(key);

      let rangeStart = start, rangeEnd = end;
      const half = HALF_TERMS[e.term];
      if (half === 'start') rangeEnd = mid;
      else if (half === 'end') rangeStart = mid;
      if (!(rangeStart < rangeEnd)) return; // degenerate range - nothing to export for this meeting

      const dow = e.m[0]; // 0=Mon..5=Sat already, matches DOW[]
      const first = firstOnOrAfter(rangeStart, dow);
      if (first > rangeEnd) return; // this weekday never actually falls within the range

      const dOnly = dateOnly(first);
      const untilDate = dateOnly(rangeEnd);
      const roomCode = e.m[5];
      const loc = (typeof roomLocation === 'function') ? roomLocation(roomCode) : { label: '' };
      const uid = 'snu-' + key.replace(/[^A-Za-z0-9]/g, '') + '@snu-scheduler.local';

      lines.push('BEGIN:VEVENT');
      lines.push('UID:' + uid);
      lines.push('DTSTAMP:' + icsUtcStamp(now));
      lines.push('DTSTART;TZID=Asia/Kolkata:' + dOnly + 'T' + icsLocalTime(e.m[1]));
      lines.push('DTEND;TZID=Asia/Kolkata:' + dOnly + 'T' + icsLocalTime(e.m[2]));
      lines.push(foldLine('RRULE:FREQ=WEEKLY;BYDAY=' + DOW[dow] + ';UNTIL=' + untilDate + 'T235959'));
      lines.push(foldLine('SUMMARY:' + escText(e.code.split('/')[0] + ' ' + (e.m[3] || ''))));
      const desc = [e.title, e.m[4], e.term].filter(Boolean).join(' \u00b7 ');
      lines.push(foldLine('DESCRIPTION:' + escText(desc)));
      if (loc.label) lines.push(foldLine('LOCATION:' + escText(loc.label)));
      lines.push('END:VEVENT');
    });

    lines.push('END:VCALENDAR');
    return lines.join('\r\n') + '\r\n';
  }

  return { buildIcs };
}));
