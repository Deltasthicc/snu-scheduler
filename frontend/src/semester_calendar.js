/* =====================================================================
   SEMESTER CALENDAR

   Real, sourced semester date ranges from SNU's own published Academic
   Calendar PDFs - never guessed, never derived from a formula. Every
   entry below was read directly off the University's own calendar grid
   and cross-checked against independent weekday arithmetic (e.g. the
   PDF says "Start of classes" lands on a Monday - the date entered here
   was verified to actually fall on a Monday before being trusted).

   Only semesters this project has actually read a real calendar for are
   listed. detectSemester() returns null rather than inventing a date
   range for a semester nobody has looked up yet - see
   https://snu.edu.in/home/mandatory-disclosure/academic-calendar-all/
   for the University's full list when a new one needs adding.
   ===================================================================== */
(function (factory) {
  var g = (typeof globalThis !== 'undefined' && globalThis)
       || (typeof self !== 'undefined' && self)
       || (typeof window !== 'undefined' && window)
       || this;
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else g.SEMCAL = factory();
}(function () {

  function toDate(iso) {
    const [y, m, d] = iso.split('-').map(Number);
    return new Date(y, m - 1, d);
  }

  const RAW_SEMESTERS = [
    {
      id: 'monsoon-2026',
      label: 'Monsoon 2026',
      classesStart: '2026-08-17',       // "Start of classes for all students" (Monday)
      firstHalfEnd: '2026-09-30',       // "First Half Finishes" (Wednesday)
      secondHalfStart: '2026-10-12',    // "2nd half begins" (Monday)
      classesEnd: '2026-12-01',         // "Last Teaching Day as per Friday schedule" (Tuesday) -
                                         // the later of two end-of-term dates; see note.
      source: "SNU Academic Calendar for Monsoon 2026 (Aug to Dec 2026), official University PDF",
      note: 'Two schedule-swap make-up days right at the end of term are not modelled: '
          + 'Mon 30 Nov 2026 runs on the Tuesday timetable and Tue 1 Dec 2026 runs on the '
          + "Friday timetable. This export always places a course on its own normal weekday, "
          + 'so those two specific calendar days may be slightly off from the University’s own swap.'
    },
    {
      id: 'spring-2027',
      label: 'Spring 2027',
      classesStart: '2027-01-11',       // "Start of classes for all students" (Monday)
      firstHalfEnd: '2027-02-26',       // "First Half finishes" (Friday)
      secondHalfStart: '2027-03-11',    // "Second Half Begins" (Thursday)
      classesEnd: '2027-04-29',         // "Last Teaching Day" (Thursday)
      source: "SNU Academic Calendar for Spring 2027 (January to May 2027), official University PDF"
    }
  ];

  const SEMESTERS = RAW_SEMESTERS.map(s => Object.assign({}, s, {
    _start: toDate(s.classesStart),
    _end: toDate(s.classesEnd)
  }));

  /* Which known semester is "current" as of `now`:
     - inside a semester's own teaching window (classesStart..classesEnd inclusive) -> that one
     - otherwise, in a gap before some semester's own start -> the nearest upcoming one
       (most useful: a student opening this before term starts, or during a between-
       terms break, wants the next real semester's dates, not a stale finished one)
     - past every known semester's end with nothing newer on file -> null, so the
       caller falls back to asking rather than fabricating a date range */
  function detectSemester(now) {
    now = now || new Date();
    const sorted = SEMESTERS.slice().sort((a, b) => a._start - b._start);
    const inside = sorted.find(s => now >= s._start && now <= s._end);
    if (inside) return inside;
    const upcoming = sorted.find(s => now < s._start);
    if (upcoming) return upcoming;
    return null;
  }

  return { SEMESTERS, detectSemester };
}));
