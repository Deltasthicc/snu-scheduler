/* Semester calendar detection tests - run in Node, pure date logic. */
let pass = 0, fail = 0;
const ck = (n, c, x) => { console.log((c ? '  PASS  ' : '  FAIL  ') + n + (x ? '   [' + x + ']' : '')); c ? pass++ : fail++; };

const SEMCAL = require('../src/semester_calendar.js');

console.log('=== SEMESTER CALENDAR DETECTION ===');

const d = (y, m, day) => new Date(y, m - 1, day);

{
  const ids = SEMCAL.SEMESTERS.map(s => s.id);
  ck('at least the two currently-published semesters are on file',
     ids.includes('monsoon-2026') && ids.includes('spring-2027'), JSON.stringify(ids));
}
{
  const sem = SEMCAL.detectSemester(d(2026, 8, 19));
  ck('mid-teaching (19 Aug 2026, inside Monsoon 2026) detects Monsoon 2026', sem && sem.id === 'monsoon-2026', sem && sem.id);
}
{
  const sem = SEMCAL.detectSemester(d(2026, 8, 17));
  ck('the exact first day of classes (17 Aug 2026) detects Monsoon 2026', sem && sem.id === 'monsoon-2026', sem && sem.id);
}
{
  const sem = SEMCAL.detectSemester(d(2026, 12, 1));
  ck('the exact last day of classes (1 Dec 2026) still detects Monsoon 2026, not the next semester',
     sem && sem.id === 'monsoon-2026', sem && sem.id);
}
{
  const sem = SEMCAL.detectSemester(d(2026, 7, 1));
  ck('well before term starts (1 Jul 2026) still resolves to the upcoming Monsoon 2026, not null',
     sem && sem.id === 'monsoon-2026', sem && sem.id);
}
{
  const sem = SEMCAL.detectSemester(d(2026, 12, 20));
  ck('during the winter break between Monsoon 2026 and Spring 2027 resolves to the upcoming Spring 2027',
     sem && sem.id === 'spring-2027', sem && sem.id);
}
{
  const sem = SEMCAL.detectSemester(d(2027, 3, 1));
  ck('mid-teaching (1 Mar 2027, inside Spring 2027) detects Spring 2027', sem && sem.id === 'spring-2027', sem && sem.id);
}
{
  const sem = SEMCAL.detectSemester(d(2027, 4, 29));
  ck('the exact last day of Spring 2027 classes still detects Spring 2027', sem && sem.id === 'spring-2027', sem && sem.id);
}
{
  const sem = SEMCAL.detectSemester(d(2027, 8, 1));
  ck('well past the last known semester with nothing newer on file returns null, not a guess',
     sem === null, sem && sem.id);
}
{
  const mon = SEMCAL.SEMESTERS.find(s => s.id === 'monsoon-2026');
  ck('Monsoon 2026 first-half end falls strictly between classes start and end',
     mon.classesStart < mon.firstHalfEnd && mon.firstHalfEnd < mon.classesEnd);
  ck('Monsoon 2026 second-half start falls strictly between classes start and end',
     mon.classesStart < mon.secondHalfStart && mon.secondHalfStart < mon.classesEnd);
  ck('Monsoon 2026 has a real gap between the two halves (first-half end before second-half start)',
     mon.firstHalfEnd < mon.secondHalfStart, `${mon.firstHalfEnd} < ${mon.secondHalfStart}`);
  ck('Monsoon 2026 carries a source citation, not an unsourced date range', !!mon.source);
}
{
  const spr = SEMCAL.SEMESTERS.find(s => s.id === 'spring-2027');
  ck('Spring 2027 first-half end falls strictly between classes start and end',
     spr.classesStart < spr.firstHalfEnd && spr.firstHalfEnd < spr.classesEnd);
  ck('Spring 2027 second-half start falls strictly between classes start and end',
     spr.classesStart < spr.secondHalfStart && spr.secondHalfStart < spr.classesEnd);
  ck('Spring 2027 carries a source citation, not an unsourced date range', !!spr.source);
}
{
  // detectSemester() must not throw or silently misbehave when called with
  // no argument at all - it should fall back to the real "now".
  let threw = false;
  try { SEMCAL.detectSemester(); } catch (e) { threw = true; }
  ck('detectSemester() with no argument does not throw', !threw);
}

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
