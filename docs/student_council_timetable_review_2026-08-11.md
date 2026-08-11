# Draft mail to the Student Council — Monsoon 2026 draft timetable review

Subject: **Automated check of the 10 Aug draft timetable — 7 first-year core clashes, 1 CCC-slot collision, and a large UWE-access gap between cohorts**

---

Dear Student Council,

Thank you for pushing for the extra review day and for circulating the draft
workbook. I maintain an unofficial planning tool that parses the timetable
programmatically, so I was able to run automated checks across all 327 courses
and 1,182 timetable rows rather than by eye. Sharing the results in case they
are useful for the consolidated review, the final review this evening, or the
School POC process afterwards.

Everything below was produced by machine-checking the draft workbook you
circulated, and every item lists the exact rows so the Academic Office can
verify each one independently.

## 1. What checks out

Against the assurances you listed, on the cohorts that actually enrol through
COMPAS (2nd, 3rd and 4th year):

- **No unavoidable major-core vs major-core clashes.** Zero, across all 2nd,
  3rd and 4th-year cohorts.
- **No core course blocks the CCC slot** (Mon/Wed 11:15–12:45) for those years.

So the core-clash assurance appears to hold for the bidding cohorts. The
problems below are concentrated in first year and in UWE access.

## 2. Seven unavoidable core-vs-core clashes, all in first year

"Unavoidable" means every published section combination of the two courses
overlaps, so no student in that cohort can attend both:

| Cohort | Courses | The overlap |
|---|---|---|
| DES 1st yr | DES102/DES1001 vs DES104/DES1003 | both Fri 13:00–14:55 |
| DES 1st yr | DES102/DES1001 vs ECO108/ECO1009 | Tue 9:00–10:55 vs 9:35–11:00 |
| DES 1st yr | DES103/DES1002 vs DES104/DES1003 | Mon 15:05–17:00 vs 15:10–16:35 |
| IHS 1st yr | ECO108/ECO1009 vs IHS1002 | Thu 9:35–11:00 vs 9:35–10:30 |
| IHS 1st yr | IHS1002 vs IHS1003 | Tue 14:10–15:35 vs 14:20–15:45 |
| PHY 1st yr | CCC704/CCC2302 vs PHY1007 | Wed 11:15–12:40 vs 11:10–13:05 |
| PHY 1st yr | CHY111/CHY1011 vs PHY1001 | Thu 9:35–11:00 vs 9:05–10:00 |

Several are near-misses of 10–35 minutes (IHS1002/IHS1003 overlap by 75
minutes; PHY1001 ends 55 minutes into CHY111's lecture), which suggests small
shifts could resolve them.

## 3. One course sits on the protected CCC slot

**PHY1007** has a single published practical, Wed 11:10–13:05, which covers the
Mon/Wed 11:15–12:45 CCC slot. It has no alternative section, so any student
required to take it cannot take a Wednesday CCC. This is also the cause of the
CCC704/PHY1007 clash above.

## 4. First-year courses lost their batch tagging

In the draft workbook, the **Student Block** column is populated for only
**18 of 332 first-year rows**, against **229 of 277 second-year rows**.

Without it there is no way to tell which lecture/tutorial/practical group a
given first-year student belongs to. As one illustration, PHY1011 has 4
lectures, 10 tutorials and 10 practicals; with no batch tagging that reads as
**372 possible combinations** instead of one allocated group per student. Any
tool — or student — reading this file cannot produce a correct first-year
timetable. The previous version of the data did carry these tags, so this looks
like something dropped in this revision rather than a deliberate change.

## 5. Two smaller data issues

- **`CCC407/CCC2200` has no course title** in any of its rows (blank in every
  instance).
- **One malformed cell**: ECE301 and ECE302 list their student blocks as
  `ECE31 to ECE33, ECE36 ECE310 & ECE311` — the second part uses spaces and an
  ampersand where every other row uses commas. It parses as a single
  meaningless group unless handled specially.

## 6. UWE access is very uneven between cohorts

Since maximising UWE access was an explicit goal, I measured it: for each
cohort, how many of the 187 UWE-flagged courses have at least one section that
does not clash with that cohort's own core courses.

| Cohort | UWEs reachable (of 187) |
|---|---|
| Chemistry 2nd yr | 15 (8%) |
| Design 2nd yr | 15 (8%) |
| Biotech 2nd yr | 25 (13%) |
| Civil 2nd yr | 25 (13%) |
| … | … |
| Civil 4th yr | 165 (88%) |
| Biotech 4th yr | 167 (89%) |

For Chemistry, Design and Civil 2nd year the figure is exact — every one of
their core courses has a single fixed section, so there is no flexibility to
recover access. A 2nd-year Chemistry student can genuinely only reach 8% of the
UWE catalogue, while a 4th year reaches ~88%.

If the Office is able to make only a few changes, moving 2nd-year core sections
off the most contested UWE slots would likely do more for UWE access than
anything else, since those cohorts have the least room and the most degree
requirements left.

## Method and caveats

- Checks were run against the draft workbook exactly as circulated.
- "Unavoidable clash" = every published section combination overlaps. Clashes
  that a student can avoid by choosing a different section are not listed.
- Half-semester handling: courses marked `Both half` (9 CCC courses) were
  treated as occupying the slot in both halves.
- The UWE figures use each cohort's core courses as fixed. Where a core course
  has multiple sections this slightly understates access; I verified the three
  worst cohorts have no such flexibility, so those numbers are exact.
- I am happy to share the scripts, the parsed dataset, or re-run everything
  against the final timetable this evening and send results within minutes.

Thanks again for the work on this.

Best regards,
Shashwat Rajan
