/* Calendar export (.ics) tests — run in Node, no DOM needed since buildIcs()
   is pure text formatting. Real-browser wiring (the actual download, and
   roomLocation() feeding LOCATION lines) is covered by the Playwright suite. */
let pass = 0, fail = 0;
const ck = (n, c, x) => { console.log((c ? '  PASS  ' : '  FAIL  ') + n + (x ? '   [' + x + ']' : '')); c ? pass++ : fail++; };

delete require.cache[require.resolve('../src/calendar_export.js')];
const CAL = require('../src/calendar_export.js');

console.log('=== CALENDAR EXPORT (.ics) ===');

const fullSemMon = { m: [0, 575, 655, 'LEC', 'LEC1', 'A204'], term: 'Full semester', code: 'CSD211', title: 'Computer Organisation' };

{
  try { CAL.buildIcs([fullSemMon], { semesterEnd: '2026-12-15' }); ck('missing start date throws', false); }
  catch (e) { ck('missing start date throws', /start/i.test(e.message), e.message); }
}
{
  try { CAL.buildIcs([fullSemMon], { semesterStart: '2026-12-15', semesterEnd: '2026-08-25' }); ck('end before start throws', false); }
  catch (e) { ck('end before start throws', /after/i.test(e.message), e.message); }
}
{
  const half = { ...fullSemMon, term: 'First half' };
  try { CAL.buildIcs([half], { semesterStart: '2026-08-25', semesterEnd: '2026-12-15' }); ck('half-term course without a boundary date throws', false); }
  catch (e) { ck('half-term course without a boundary date throws', /half-semester boundary/i.test(e.message), e.message); }
}
{
  try {
    CAL.buildIcs([fullSemMon], { semesterStart: '2026-08-25', semesterEnd: '2026-12-15', midpoint: '2027-01-01' });
    ck('boundary date outside the semester range throws', false);
  } catch (e) { ck('boundary date outside the semester range throws', /between/i.test(e.message), e.message); }
}
{
  const text = CAL.buildIcs([fullSemMon], { semesterStart: '2026-08-25', semesterEnd: '2026-12-15' });
  ck('output is a well-formed VCALENDAR', text.startsWith('BEGIN:VCALENDAR') && text.trim().endsWith('END:VCALENDAR'));
  ck('output uses CRLF line endings', text.includes('\r\n'));
  ck('carries a VTIMEZONE for Asia/Kolkata (no DST, single fixed +0530 offset)',
     text.includes('TZID:Asia/Kolkata') && text.includes('TZOFFSETTO:+0530'));
  ck('a Monday meeting recurs weekly on MO', /RRULE:FREQ=WEEKLY;BYDAY=MO/.test(text), text.match(/RRULE:[^\r\n]+/)?.[0]);
  ck('recurrence runs until the semester end date', text.includes('UNTIL=20261215T235959'));
  ck('first occurrence lands on the first Monday on/after the semester start (2026-08-31)',
     /DTSTART;TZID=Asia\/Kolkata:20260831T093500/.test(text), text.match(/DTSTART;TZID[^\r\n]+/)?.[0]);
  ck('summary uses the bare course code, not the cross-listed pair', text.includes('SUMMARY:CSD211 LEC'));
}
{
  // 2026-08-25 is itself a Tuesday; a Monday-anchored range should still start
  // on 2026-08-31, not silently skip a week or land mid-range.
  const tue = { ...fullSemMon, m: [1, 575, 655, 'LEC', 'LEC1', 'A204'] };
  const text = CAL.buildIcs([tue], { semesterStart: '2026-08-25', semesterEnd: '2026-12-15' });
  ck('a Tuesday meeting starts on the semester\'s own first Tuesday (2026-08-25)',
     text.includes('DTSTART;TZID=Asia/Kolkata:20260825T093500'));
}
{
  const h1 = { ...fullSemMon, code: 'CCC101', title: 'First half course', term: 'First half' };
  const h2 = { ...fullSemMon, code: 'CCC102', title: 'Second half course', term: 'Second half' };
  const text = CAL.buildIcs([h1, h2], { semesterStart: '2026-08-25', semesterEnd: '2026-12-15', midpoint: '2026-10-15' });
  const h1Block = text.split('BEGIN:VEVENT').find(b => b.includes('CCC101'));
  const h2Block = text.split('BEGIN:VEVENT').find(b => b.includes('CCC102'));
  ck('a First-half course\'s recurrence ends at the boundary date, not the semester end',
     h1Block.includes('UNTIL=20261015T235959'), h1Block.match(/RRULE:[^\r\n]+/)?.[0]);
  ck('a Second-half course starts on/after the boundary date, not the semester start',
     new RegExp('DTSTART;TZID=Asia/Kolkata:2026(10(1[5-9]|2\\d|3[01])|11)').test(h2Block.match(/DTSTART[^\r\n]+/)[0]),
     h2Block.match(/DTSTART[^\r\n]+/)[0]);
}
{
  const dup = [fullSemMon, { ...fullSemMon }];
  const text = CAL.buildIcs(dup, { semesterStart: '2026-08-25', semesterEnd: '2026-12-15' });
  const count = (text.match(/BEGIN:VEVENT/g) || []).length;
  ck('an identical meeting listed twice produces only one VEVENT', count === 1, count);
}
{
  const commaTitle = { ...fullSemMon, title: 'Topics in AI, ML & Data; Science' };
  const text = CAL.buildIcs([commaTitle], { semesterStart: '2026-08-25', semesterEnd: '2026-12-15' });
  ck('commas and semicolons in the description are escaped per RFC 5545',
     text.includes('Topics in AI\\, ML & Data\\; Science'), text.match(/DESCRIPTION:[^\r\n]+/)?.[0]);
}
{
  global.roomLocation = code => ({ label: 'A Block, 2nd Floor, Room 204' });
  delete require.cache[require.resolve('../src/calendar_export.js')];
  const CAL2 = require('../src/calendar_export.js');
  const text = CAL2.buildIcs([fullSemMon], { semesterStart: '2026-08-25', semesterEnd: '2026-12-15' });
  ck('a resolvable room code becomes a LOCATION line', text.includes('LOCATION:A Block\\, 2nd Floor\\, Room 204'));
  delete global.roomLocation;
}
{
  const noRoom = { ...fullSemMon, m: [0, 575, 655, 'LEC', 'LEC1', ''] };
  delete require.cache[require.resolve('../src/calendar_export.js')];
  const CAL3 = require('../src/calendar_export.js');
  const text = CAL3.buildIcs([noRoom], { semesterStart: '2026-08-25', semesterEnd: '2026-12-15' });
  ck('no roomLocation() available in this environment - LOCATION is simply omitted, not fabricated',
     !text.includes('LOCATION:'));
}
{
  const manual = { name: 'Project-1', cr: 6 }; // off-timetable items have no `m` at all
  const text = CAL.buildIcs([manual, fullSemMon], { semesterStart: '2026-08-25', semesterEnd: '2026-12-15' });
  const count = (text.match(/BEGIN:VEVENT/g) || []).length;
  ck('an off-timetable item with no meeting time is skipped, not crashed on', count === 1, count);
}

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
