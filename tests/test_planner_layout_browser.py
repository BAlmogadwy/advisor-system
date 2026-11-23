"""The staff planner (/planner/) shows everything it loads, whole and the right way round.

Five defects, one test each, measured in a real browser on a loaded student:

F1  a phone: no panel is wider than the screen (a `1fr` grid track cannot go
    below its widest item), the suggestions table and both week grids fit;
F2  the Sections list: both columns of cards sit inside their panel at laptop
    widths and no row's text is cut or overlapped;
F3  Arabic: a time range in a section row is painted start first (an Arabic
    letter before the digits makes the bidi algorithm swap the two times);
F4  the two week grids: a course code and its time wrap instead of being cut
    (the ellipsis removed the START of "ENG101 M45" in Arabic);
F5  switching theme redraws the week grids (their block colours are inline,
    chosen when drawn, so dark text sat on a light block).

The planner's three data requests are answered from a synthetic student made up
below (no database rows, no real data); it holds the cases that caused the
defects: a section of eight meetings, one linked to seven programmes, sections
that clash with the registered week and whose capacity is unknown (the longest
status label), nine course cards, a week of seventeen meetings on five days, a
plan of ten terms. Every measurement is repeated in a wide font (Verdana, DejaVu
Sans) as a CI machine without the design's fonts renders the page. Nothing
leaves the machine (tests/browser_isolation.py).

Each test measures every variant before failing, so one run names every misfit:
which element, at which width, in which language.
"""

from __future__ import annotations

import os

# Playwright's synchronous API runs through a greenlet; fixture creation is
# synchronous ORM work while Django serves the page on its own thread.
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import Client

from tests.test_advisor_bidi import VISUAL_ORDER_JS
from tests.test_exam_rosters_browser import WIDE_FONT

playwright_api = pytest.importorskip("playwright.sync_api")
sync_playwright = playwright_api.sync_playwright

LANGUAGES = ("en", "ar")
FONTS = ("app", "wide")

# ── The synthetic student ────────────────────────────────────────────────────

STUDENT_ID = "4400000"
SEVEN_PROGRAMS = ["AI2", "COE2", "CS2", "CYP2", "DS", "DS2", "IS2"]


def _meeting(day: str, start: str, end: str, room: str = "172FB004") -> dict:
    return {
        "day": day,
        "start_time": start,
        "end_time": end,
        "room": room,
        "instructor": "",
    }


def _baseline_row(code: str, name: str, section: str, credits: int, meeting: dict, tsid: int):
    return {
        "course_code": code,
        "course_key": code,
        "course_name": name,
        "course_number": "",
        "section": section,
        "registered_count": None,
        "credits": credits,
        **meeting,
        "term_section_id": tsid,
        "source": "registration_plan_1448_t1",
    }


# Seventeen meetings on five days, 75 and 100 minutes, no two overlapping.
_BASELINE_MEETINGS = [
    ("CS111", "PROGRAMMING I", "M29", 4, 1, _meeting("MON", "14:30", "15:45", "172FB010")),
    ("CS111", "PROGRAMMING I", "M29", 4, 1, _meeting("THU", "14:30", "15:45", "172FB010")),
    ("CS111", "PROGRAMMING I", "M29", 4, 1, _meeting("WED", "14:45", "16:25", "172FB010")),
    ("ENG101", "ENGLISH LANGUAGE SKILLS I", "M45", 4, 2, _meeting("MON", "08:00", "09:40")),
    ("ENG101", "ENGLISH LANGUAGE SKILLS I", "M45", 4, 2, _meeting("MON", "09:50", "11:30")),
    ("ENG101", "ENGLISH LANGUAGE SKILLS I", "M45", 4, 2, _meeting("SUN", "08:00", "09:40")),
    ("ENG101", "ENGLISH LANGUAGE SKILLS I", "M45", 4, 2, _meeting("SUN", "09:50", "11:30")),
    ("ENG101", "ENGLISH LANGUAGE SKILLS I", "M45", 4, 2, _meeting("THU", "08:00", "09:40")),
    ("ENG101", "ENGLISH LANGUAGE SKILLS I", "M45", 4, 2, _meeting("THU", "09:50", "11:30")),
    ("ENG101", "ENGLISH LANGUAGE SKILLS I", "M45", 4, 2, _meeting("WED", "08:00", "09:40")),
    ("ENG101", "ENGLISH LANGUAGE SKILLS I", "M45", 4, 2, _meeting("WED", "09:50", "11:30")),
    ("GS111", "ISLAMIC CULTURE", "M67", 2, 3, _meeting("THU", "17:40", "19:20")),
    ("MATH105", "CALCULUS I", "M41", 3, 4, _meeting("SUN", "12:45", "14:25")),
    ("MATH105", "CALCULUS I", "M41", 3, 4, _meeting("TUE", "12:45", "14:25")),
    ("PHYS103", "GENERAL PHYSICS I", "M60", 3, 5, _meeting("MON", "13:00", "14:15")),
    ("PHYS103", "GENERAL PHYSICS I", "M60", 3, 5, _meeting("TUE", "16:40", "18:20")),
    ("PHYS103", "GENERAL PHYSICS I", "M60", 3, 5, _meeting("WED", "13:00", "14:15")),
]
BASELINE = [
    _baseline_row(code, name, section, credits, meeting, 23000 + tsid)
    for code, name, section, credits, tsid, meeting in _BASELINE_MEETINGS
]

RECOMMENDATIONS = [
    {
        "course_code": code,
        "course_name": name,
        "credits": credits,
        "priority": priority,
        "score": score,
        "status": status,
        "missing_prerequisites": missing,
        "reason_tags": ["recommended"],
    }
    for code, name, credits, priority, score, status, missing in [
        ("CS111", "PROGRAMMING I", 4, "High", 97, "Eligible", []),
        ("ENG101", "ENGLISH LANGUAGE SKILLS I", 4, "High", 94, "Eligible", []),
        ("GS101", "ISLAMIC CULTURE I", 2, "Med", 80, "Eligible", []),
        ("MATH105", "CALCULUS I", 3, "Med", 76, "Eligible", []),
        ("CS112", "PROGRAMMING II", 4, "Low", 40, "Not eligible", ["CS111"]),
    ]
]

CONTEXT = {
    "ok": True,
    "student": {
        "student_id": int(STUDENT_ID),
        "name": "Student Name",
        "program": "AI2",
        "cohort_section": "M",
        "advisor_id": "",
        "gpa": 1.0,
        "registered_credits": 27,
        "credit_cap": 18,
        "regulatory_max_credits": 19,
    },
    "baseline": BASELINE,
    "baseline_totals": {"courses": 5, "credits": 17},
    "recommendations": RECOMMENDATIONS,
    "eligibility_mode": "strict",
    "strict_passed_only": True,
    "year": "1448",
    "term": "1",
    "warning": None,
}

# (code, name, status, can_register) by term: ten terms, five courses in the
# first. The not-taken courses that can be registered are the nine course cards.
_PLAN = {
    1: [
        ("CS111", "PROGRAMMING I", "not_taken", True),
        ("ENG101", "ENGLISH LANGUAGE SKILLS I", "not_taken", True),
        ("GS101", "ISLAMIC CULTURE I", "not_taken", True),
        ("MATH105", "CALCULUS I", "not_taken", True),
        ("PHYS103", "GENERAL PHYSICS I", "not_taken", True),
    ],
    2: [
        ("CS112", "PROGRAMMING II AND DATA STRUCTURES FOUNDATIONS", "not_taken", True),
        ("ENG102", "ENGLISH LANGUAGE SKILLS II", "not_taken", True),
        ("GS104", "ISLAMIC CULTURE II", "not_taken", True),
        ("CS163", "DISCRETE MATHEMATICS", "failed", True),
        ("STAT103", "STATISTICS", "failed", True),
    ],
    3: [
        ("AI113", "FUNDAMENTALS OF AI", "not_taken", False),
        ("CS113", "OBJECT ORIENTED PROGRAMMING", "not_taken", False),
        ("ENGL103", "TECHNICAL ENGLISH", "not_taken", False),
        ("GS111", "ISLAMIC CULTURE III", "not_taken", True),
        ("MATH106", "CALCULUS II", "not_taken", False),
    ],
    4: [("AI124", "DATA MINING", "not_taken", False), ("CS114", "ALGORITHMS", "not_taken", False)],
    5: [
        ("AI225", "MACHINE LEARNING", "not_taken", False),
        ("CS285", "DATABASES", "not_taken", False),
    ],
    6: [("AI236", "NEURAL NETWORKS", "not_taken", False), ("IS124", "SYSTEMS", "not_taken", False)],
    7: [("AI337", "NLP", "not_taken", False), ("IS275", "SECURITY", "not_taken", False)],
    8: [("AI348", "VISION", "not_taken", False), ("AI398", "PROJECT I", "not_taken", False)],
    9: [("AI479", "ETHICS", "not_taken", False), ("AI499", "PROJECT II", "not_taken", False)],
    10: [("AI492", "CO-OP", "not_taken", False)],
}
STUDENT_PLAN = {
    "student_id": int(STUDENT_ID),
    "program": "AI2",
    "eligibility_mode": "strict",
    "strict_passed_only": True,
    "summary": {
        "passed": 0,
        "studying": 0,
        "failed": 2,
        "not_taken_can_register": 9,
        "not_taken_locked": 20,
        "failed_can_register": 2,
        "failed_locked": 0,
    },
    "blocker_hints": [{"course_code": "CS112", "blocks": 4, "unlock_score": 7.87}],
    "terms": [
        {
            "term": term,
            "courses": [
                {
                    "course_code": code,
                    "course_name": name,
                    "type": "Mandatory",
                    "programme_term": term,
                    "credit_hours": 4 if code.startswith("CS") else 3,
                    "status": status,
                    "can_register": can,
                    "prerequisites": [] if can else ["CS111"],
                    "missing_prereqs": [] if can else ["CS111"],
                    "importance_score": round(9 - term * 0.7 - index * 0.4, 3),
                }
                for index, (code, name, status, can) in enumerate(courses)
            ],
        }
        for term, courses in _PLAN.items()
    ],
}


def _section(
    tsid: int,
    code: str,
    number: str,
    section: str,
    meetings: list[dict],
    *,
    programs: list[str],
    registered: int | None,
    capacity: int | None,
) -> dict:
    return {
        "term_section_id": tsid,
        "course_key": code,
        "course_code": "".join(c for c in code if c.isalpha()),
        "course_number": number,
        "section": section,
        "course_name": code,
        "available_capacity": capacity,
        "registered_count": registered,
        "programs": programs,
        "meetings": meetings,
    }


def _days(days: list[str], *slots: tuple[str, str]) -> list[dict]:
    return [_meeting(day, start, end) for day in days for start, end in slots]


EIGHT = ["SUN", "MON", "WED", "THU"]
SECTIONS = [
    # Clashes with the registered week, capacity unknown: "Conflict · capacity unknown".
    _section(
        1,
        "CS111",
        "111",
        "M29",
        _days(["MON", "THU"], ("14:30", "15:45")),
        programs=["AI2", "DS2"],
        registered=None,
        capacity=25,
    ),
    # One meeting pair, open, two programmes: a row that fits on one line.
    _section(
        2,
        "CS111",
        "111",
        "M30",
        _days(["SUN", "TUE"], ("10:00", "11:15")),
        programs=["AI2", "DS2"],
        registered=12,
        capacity=30,
    ),
    _section(
        3,
        "CS111",
        "111",
        "M31",
        _days(["MON"], ("16:00", "17:15")),
        programs=["AI2"],
        registered=30,
        capacity=30,
    ),
    # Eight meetings (four days, two slots), seven programmes, clashing, capacity unknown.
    _section(
        4,
        "ENG101",
        "101",
        "M45",
        _days(EIGHT, ("08:00", "09:40"), ("09:50", "11:30")),
        programs=SEVEN_PROGRAMS,
        registered=None,
        capacity=None,
    ),
    # Eight meetings, seven programmes, no clash, open.
    _section(
        5,
        "ENG101",
        "101",
        "M46",
        _days(["SUN", "MON", "TUE", "WED"], ("18:30", "19:20"), ("19:30", "20:20")),
        programs=SEVEN_PROGRAMS,
        registered=38,
        capacity=40,
    ),
    _section(
        6,
        "ENG101",
        "101",
        "M47",
        _days(["SUN"], ("08:00", "09:40")),
        programs=["AI2"],
        registered=40,
        capacity=40,
    ),
    _section(
        7,
        "GS101",
        "101",
        "M12",
        _days(["TUE", "THU"], ("10:00", "11:15")),
        programs=["AI2", "COE2", "CS2"],
        registered=None,
        capacity=None,
    ),
    _section(8, "GS101", "101", "M13", [], programs=[], registered=None, capacity=None),
    _section(
        9,
        "MATH105",
        "105",
        "M41",
        _days(["SUN", "TUE"], ("12:45", "14:25")),
        programs=["AI2"],
        registered=20,
        capacity=35,
    ),
    _section(
        10,
        "MATH105",
        "105",
        "M42",
        _days(["MON", "WED"], ("09:50", "11:30")),
        programs=["AI2", "CS2"],
        registered=None,
        capacity=None,
    ),
    _section(
        11,
        "PHYS103",
        "103",
        "M60",
        _days(["MON", "WED"], ("13:00", "14:15")),
        programs=["AI2"],
        registered=None,
        capacity=None,
    ),
    _section(
        12,
        "CS112",
        "112",
        "M20",
        _days(["SUN", "TUE"], ("15:00", "16:15")),
        programs=SEVEN_PROGRAMS,
        registered=10,
        capacity=40,
    ),
    _section(
        13,
        "ENG102",
        "102",
        "M21",
        _days(["THU"], ("12:00", "13:40")),
        programs=["AI2", "DS2"],
        registered=None,
        capacity=None,
    ),
    _section(
        14,
        "GS104",
        "104",
        "M22",
        _days(["TUE"], ("09:00", "10:40")),
        programs=["AI2"],
        registered=5,
        capacity=40,
    ),
    _section(
        15,
        "GS111",
        "111",
        "M23",
        _days(["THU"], ("17:40", "19:20")),
        programs=["AI2", "IS2"],
        registered=None,
        capacity=None,
    ),
]
SECTIONS_CATALOG = {
    "ok": True,
    "sections": SECTIONS,
    "count": len(SECTIONS),
    "program_sections_only": True,
}
# The one row that fits on a single line on a wide screen: open, one meeting
# pair, two programmes, a known capacity (section M30).
ONE_LINE_ROW_ID = 2
COURSE_CARDS = 9

# ── What is measured ─────────────────────────────────────────────────────────

# Collect findings as "kind: N (first: example)" so a run with hundreds of
# offenders still reads as a short list that names the first of each kind.
COLLECT = """
const found = new Map();
const note = (kind, example) => {
  const entry = found.get(kind) || { n: 0, example };
  entry.n += 1;
  found.set(kind, entry);
};
const report = () => [...found].map(([kind, e]) => `${kind}: ${e.n} (first: ${e.example})`);
const R = el => el.getBoundingClientRect();
const px = n => Math.round(n * 10) / 10;
const sketch = el => {
  const cls = [...el.classList].slice(0, 2).map(c => '.' + c).join('');
  const text = (el.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 24);
  return `${el.tagName.toLowerCase()}${el.id ? '#' + el.id : ''}${cls}${text ? ' "' + text + '"' : ''}`;
};
"""

# F1: nothing in the panels reaches past the screen, except inside a frame that
# scrolls sideways by itself (and that frame is on screen); the suggestions
# table fits without scrolling; both week grids fit.
PHONE_FIT = (
    "(strictRec) => {"
    + COLLECT
    + r"""
  const grid = document.querySelector('.planner-grid');
  if (!grid) return ['no .planner-grid'];
  const W = innerWidth;
  const outside = r => r.left < -0.5 || r.right > W + 0.5;
  const tiny = r => r.width <= 2 && r.height <= 2;
  for (const panel of grid.querySelectorAll('.planner-panel')) {
    const r = R(panel);
    if (r.width > 0 && outside(r)) note('panel wider than the screen', `#${panel.id} ${px(r.left)}..${px(r.right)} of ${W}`);
  }
  for (const el of grid.querySelectorAll('*')) {
    const r = R(el);
    if (!el.getClientRects().length || tiny(r) || getComputedStyle(el).visibility === 'hidden') continue;
    let frame = null, hidden = false;
    for (let a = el.parentElement; a && a !== grid; a = a.parentElement) {
      if (tiny(R(a))) { hidden = true; break; }
      if (!frame && ['auto', 'scroll'].includes(getComputedStyle(a).overflowX)) frame = a;
    }
    if (hidden) continue;
    const where = (el.closest('.planner-panel') || {}).id || '?';
    if (frame) {
      const fr = R(frame);
      if (outside(fr)) note('scroll frame past the screen', `${sketch(frame)} in #${where} ${px(fr.left)}..${px(fr.right)} of ${W}`);
    } else if (outside(r)) {
      note('past the screen, no scroll frame', `${sketch(el)} in #${where} ${px(r.left)}..${px(r.right)} of ${W}`);
    }
  }
  // The suggestions table: no sideways scrolling, every Add button on screen.
  const rec = document.querySelector('#recCard .table-responsive');
  if (!rec) note('no #recCard .table-responsive', '');
  else if (strictRec && rec.scrollWidth > rec.clientWidth + 1)
    note('#recCard table scrolls sideways', `scrollWidth ${rec.scrollWidth} > clientWidth ${rec.clientWidth}`);
  const adds = [...document.querySelectorAll('#recTable button')];
  if (adds.length < 5) note('suggestions rows missing', `${adds.length} buttons`);
  for (const button of adds) {
    const r = R(button);
    if (strictRec && outside(r)) note('Add button off screen', `${sketch(button)} ${px(r.left)}..${px(r.right)} of ${W}`);
  }
  // Text cut by a box that does not scroll has no way to be reached.
  for (const el of grid.querySelectorAll('*')) {
    if (!el.getClientRects().length || el.closest('.sr-only')) continue;
    const cs = getComputedStyle(el);
    if (!['hidden', 'clip'].includes(cs.overflowX) || el.clientWidth < 3) continue;
    if (el.scrollWidth > el.clientWidth + 1 && !el.closest('.wg-blocks'))
      note('clipped, not scrollable', `${sketch(el)} scrollWidth ${el.scrollWidth} > ${el.clientWidth}`);
  }
  // Both week grids: all five day columns on screen, nothing scrolling.
  for (const id of ['baselineWeekly', 'visualGrid']) {
    const host = document.getElementById(id);
    const blocks = [...host.querySelectorAll('.wg-blocks')];
    if (!blocks.length) { note('no week grid', '#' + id); continue; }
    for (const block of blocks) {
      const r = R(block);
      if (outside(r)) note('week grid past the screen', `#${id} ${px(r.left)}..${px(r.right)} of ${W}`);
      if (block.scrollWidth > block.clientWidth + 1)
        note('week grid scrolls sideways', `#${id} scrollWidth ${block.scrollWidth} > ${block.clientWidth}`);
    }
    const heads = [...host.querySelectorAll('.wg-h')].filter(h => h.textContent.trim());
    const off = heads.filter(h => outside(R(h)));
    if (heads.length < 5 || off.length)
      note('day columns off screen', `#${id}: ${off.length} of ${heads.length} headers (${off.map(h => h.textContent.trim()).join(', ')})`);
    if (host.scrollWidth > host.clientWidth + 1)
      note('week grid frame scrolls sideways', `#${id} scrollWidth ${host.scrollWidth} > ${host.clientWidth}`);
  }
  return report();
}"""
)

# F2: both columns of section cards inside the panel; no row text cut or
# overlapped.
SECTIONS_FIT = (
    "(args) => {"
    + COLLECT
    + r"""
  const panel = document.getElementById('availableCard');
  const grid = document.getElementById('availGrid');
  const gp = R(panel), gg = R(grid);
  if (gg.left < gp.left - 0.5 || gg.right > gp.right + 0.5)
    note('#availGrid outside #availableCard', `${px(gg.left)}..${px(gg.right)} in ${px(gp.left)}..${px(gp.right)}`);
  if (grid.scrollWidth > grid.clientWidth + 1)
    note('#availGrid scrolls sideways', `scrollWidth ${grid.scrollWidth} > clientWidth ${grid.clientWidth}`);
  const cards = [...grid.querySelectorAll('.avail-course-card')];
  if (cards.length < __CARDS__) note('course cards missing', `${cards.length}`);
  for (const card of cards) {
    const r = R(card);
    if (r.left < gg.left - 0.5 || r.right > gg.right + 0.5)
      note('card outside #availGrid', `${card.dataset.code} ${px(r.left)}..${px(r.right)} in ${px(gg.left)}..${px(gg.right)}`);
    if (r.left < gp.left - 0.5 || r.right > gp.right + 0.5)
      note('card outside #availableCard', `${card.dataset.code} ${px(r.left)}..${px(r.right)} in ${px(gp.left)}..${px(gp.right)}`);
    for (const row of card.querySelectorAll('.avail-sec-row')) {
      const rr = R(row);
      const label = `${card.dataset.code} ${(row.querySelector('.sec-id') || {}).textContent}`;
      if (rr.left < r.left - 0.5 || rr.right > r.right + 0.5)
        note('row outside its card', `${label} ${px(rr.left)}..${px(rr.right)} in ${px(r.left)}..${px(r.right)}`);
      // Same information in the same order: the row's six parts, and every
      // programme of the section still named.
      const order = [...row.children].map(k => k.classList[0]).join(' ');
      if (order !== 'sec-dot sec-id sec-time sec-program sec-capacity sec-status')
        note('row parts changed', `${label}: ${order}`);
      const named = (args.programs[row.dataset.tsid] || []);
      const programText = (row.querySelector('.sec-program') || {}).textContent || '';
      for (const p of named)
        if (!programText.includes(p)) note('programme missing from a row', `${label}: ${p}`);
      const kids = [...row.children].filter(k => R(k).width > 0);
      for (const kid of kids) {
        if (kid.scrollWidth > kid.clientWidth + 1)
          note('row text clipped', `${label} .${kid.className.split(' ')[0]} scrollWidth ${kid.scrollWidth} > ${kid.clientWidth}`);
        const kr = R(kid);
        if (kr.left < rr.left - 0.5 || kr.right > rr.right + 0.5)
          note('row text outside its row', `${label} .${kid.className.split(' ')[0]} ${px(kr.left)}..${px(kr.right)} in ${px(rr.left)}..${px(rr.right)}`);
      }
      for (let i = 0; i < kids.length; i++) for (let j = i + 1; j < kids.length; j++) {
        const a = R(kids[i]), b = R(kids[j]);
        if (Math.min(a.right, b.right) - Math.max(a.left, b.left) > 1 && Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 1)
          note('row texts overlap', `${label} .${kids[i].className.split(' ')[0]} over .${kids[j].className.split(' ')[0]}`);
      }
    }
  }
  // A row that fits on one line today still does (a wide screen, the design's
  // font): its texts share one line.
  if (args.oneLine) {
    const row = grid.querySelector(`.avail-sec-row[data-tsid="${args.oneLine}"]`);
    const mids = [...row.children].filter(k => R(k).width > 0).map(k => (R(k).top + R(k).bottom) / 2);
    if (Math.max(...mids) - Math.min(...mids) > 6)
      note('a one-line row wrapped', `data-tsid ${args.oneLine} spans ${px(Math.max(...mids) - Math.min(...mids))}px of height`);
  }
  return report();
}"""
).replace("__CARDS__", str(COURSE_CARDS))

# F4: in a week grid every block shows its code and time whole.
BLOCKS_FIT = (
    "(args) => {"
    + COLLECT
    + """
  const visual = """
    + VISUAL_ORDER_JS.strip()
    + r"""
  ;
  // The painted lines of an element: its characters grouped by height, each
  // line read left to right.
  const lines = el => {
    const rows = new Map();
    const walk = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    for (let t = walk.nextNode(); t; t = walk.nextNode()) {
      for (let i = 0; i < t.data.length; i++) {
        const range = document.createRange();
        range.setStart(t, i); range.setEnd(t, i + 1);
        const g = range.getBoundingClientRect();
        if (!g.width) continue;
        const key = Math.round(g.top / 3);
        if (!rows.has(key)) rows.set(key, []);
        rows.get(key).push({ left: g.left, ch: t.data[i] });
      }
    }
    return [...rows].sort((a, b) => a[0] - b[0])
      .map(([, chars]) => chars.sort((a, b) => a.left - b.left).map(c => c.ch).join('').trim());
  };
  const W = innerWidth;
  let tag = 0, total = 0;
  for (const id of ['baselineWeekly', 'visualGrid']) {
    const host = document.getElementById(id);
    for (const block of host.querySelectorAll('.wg-blocks')) {
      const r = R(block);
      if (r.left < -0.5 || r.right > W + 0.5)
        note('week grid past the screen', `#${id} ${px(r.left)}..${px(r.right)} of ${W}`);
    }
    const filled = [...host.querySelectorAll('.wg-filled')];
    if (filled.length < 17) note('blocks missing', `#${id}: ${filled.length}`);
    for (const cell of filled) {
      total += 1;
      const cid = cell.querySelector('.wg-cid'), meta = cell.querySelector('.wg-meta');
      const label = `#${id} ${cid.textContent.trim()} ${meta.textContent.trim()}`;
      if (cid.scrollWidth > cid.clientWidth + 1)
        note('code clipped', `${label}: scrollWidth ${cid.scrollWidth} > clientWidth ${cid.clientWidth}`);
      if (meta.scrollWidth > meta.clientWidth + 1)
        note('time clipped', `${label}: scrollWidth ${meta.scrollWidth} > clientWidth ${meta.clientWidth}`);
      if (cell.scrollHeight > cell.clientHeight + 1)
        note('block clipped vertically', `${label}: scrollHeight ${cell.scrollHeight} > clientHeight ${cell.clientHeight}`);
      // The code's first character is painted inside its block, and a visual
      // read of the code starts with it.
      const first = cid.firstChild;
      const range = document.createRange();
      range.setStart(first, 0); range.setEnd(first, 1);
      const g = range.getBoundingClientRect(), b = R(cell);
      if (g.left < b.left - 0.5 || g.right > b.right + 0.5)
        note('first letter of the code outside its block', `${label}: letter ${px(g.left)}..${px(g.right)} in ${px(b.left)}..${px(b.right)}`);
      // Where the text breaks: the code only at its space, the time only after
      // its hyphen, and the time reads start first (also in the Arabic UI).
      // The one place a time may wrap is offered by the markup itself, after the
      // hyphen (there is no break opportunity between a hyphen and a digit).
      const wbr = meta.querySelector('wbr');
      if (!wbr || !(wbr.previousSibling && wbr.previousSibling.data.endsWith('-')))
        note('no break offered after the hyphen', label);
      const code = lines(cid), time = lines(meta);
      const tokens = cid.textContent.trim().split(/\s+/).sort().join('|');
      const shownTokens = code.flatMap(l => l.split(/\s+/)).sort().join('|');
      // A six-letter code (ENG101) sits whole in a 390px phone's ~60px column; a
      // seven-letter one (MATH105) or a 360px column may break inside the word,
      // which is the point of wrapping rather than cutting.
      const longest = Math.max(...cid.textContent.trim().split(/\s+/).map(t => t.length));
      const mustFit = W >= 1366 || (W >= 390 && longest <= 6);
      if (args.strictCode && mustFit && tokens !== shownTokens)
        note('code broken inside a word', `${label}: lines ${code.join(' / ')}`);
      const one = time.length === 1 && /^\d\d:\d\d-\d\d:\d\d$/.test(time[0]);
      const two = time.length === 2 && /^\d\d:\d\d-$/.test(time[0]) && /^\d\d:\d\d$/.test(time[1]);
      if (args.strictTime && !one && !two)
        note('time not whole or not start first', `${label}: lines ${time.join(' / ')}`);
      // A code and a time are Latin data: the Arabic UI must not enlarge them
      // (the Arabic type steps are for Arabic letters), so every language shows
      // the same sizes and a six-letter code sits whole in a phone's column.
      const sizeOf = el => getComputedStyle(el).fontSize;
      if (sizeOf(cid) !== args.codeSize || sizeOf(meta) !== args.timeSize)
        note('code or time at the wrong size', `${label}: code ${sizeOf(cid)}, time ${sizeOf(meta)}`);
      cid.setAttribute('data-qa-cid', String(tag));
      const seen = (visual(`[data-qa-cid="${tag}"]`) || '').trim();
      tag += 1;
      if (seen[0] !== cid.textContent.trim()[0])
        note('code does not read from its first letter', `${label}: shows "${seen.replace(/\n/g, '|')}"`);
    }
  }
  return report();
}"""
)

# F5: the colour of each block's code against its block.
BLOCK_CONTRAST = r"""() => {
  const cv = document.createElement('canvas'); cv.width = cv.height = 1;
  const cx = cv.getContext('2d', { willReadFrequently: true });
  const rgba = v => { cx.clearRect(0, 0, 1, 1); cx.fillStyle = 'rgba(0,0,0,0)'; cx.fillStyle = v; cx.fillRect(0, 0, 1, 1);
    const d = cx.getImageData(0, 0, 1, 1).data; return [d[0], d[1], d[2], d[3] / 255]; };
  const lum = c => c.slice(0, 3).map(x => { x /= 255; return x <= 0.03928 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4; })
    .reduce((s, x, i) => s + x * [0.2126, 0.7152, 0.0722][i], 0);
  const out = [];
  for (const id of ['baselineWeekly', 'visualGrid'])
    for (const cell of document.querySelectorAll(`#${id} .wg-filled`)) {
      const bg = rgba(getComputedStyle(cell).backgroundColor);
      for (const part of ['.wg-cid', '.wg-meta']) {
        const fgCss = getComputedStyle(cell.querySelector(part)).color;
        const fg = rgba(fgCss);
        const a = lum(fg), b = lum(bg);
        out.push({ id, label: cell.querySelector('.wg-cid').textContent.trim() + ' ' + part,
                   ratio: +((Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05)).toFixed(2),
                   bg: getComputedStyle(cell).backgroundColor, fg: fgCss });
      }
    }
  return out;
}"""

# The plan palette is its own scroll frame, and a frame clips a button's focus ring
# at its edge: focus the buttons at the frame's four edges and measure the ring.
PALETTE_RING = r"""() => {
  const frame = document.getElementById('planPalette');
  const buttons = [...frame.querySelectorAll('button:not([disabled])')]
    // a closed <details> keeps laid-out but unpainted content: not a button anyone sees
    .filter(b => b.getClientRects().length && !b.closest('details:not([open])'));
  if (buttons.length < 3) return [`only ${buttons.length} enabled buttons in the palette`];
  const edge = (key, better) => buttons.reduce((a, b) =>
    (better(b.getBoundingClientRect()[key], a.getBoundingClientRect()[key]) ? b : a));
  const picks = {
    top: edge('top', (x, y) => x < y), bottom: edge('bottom', (x, y) => x > y),
    left: edge('left', (x, y) => x < y), right: edge('right', (x, y) => x > y),
  };
  const out = [];
  for (const [side, button] of Object.entries(picks)) {
    // the frame scrolled as far as it goes towards that edge (scrollIntoView
    // would stop with the button flush against the edge, whatever room is left)
    const rtl = getComputedStyle(frame).direction === 'rtl';
    frame.scrollLeft = side === 'left' ? (rtl ? -1e6 : 0) : side === 'right' ? (rtl ? 0 : 1e6) : frame.scrollLeft;
    frame.scrollTop = side === 'top' ? 0 : side === 'bottom' ? 1e6 : frame.scrollTop;
    button.focus({ preventScroll: true });
    const shadow = getComputedStyle(button).boxShadow;
    const spreads = [...shadow.matchAll(/(\d+)px \d+px (?:\d+px )?(\d+)px/g)].map(m => +m[2]);
    const ring = Math.max(4, ...spreads);
    const b = button.getBoundingClientRect(), f = frame.getBoundingClientRect();
    const cut = { top: f.top - (b.top - ring), bottom: (b.bottom + ring) - f.bottom,
                  left: f.left - (b.left - ring), right: (b.right + ring) - f.right }[side];
    if (cut > 0.5) out.push(`the focus ring of the ${side}-most button is cut by ${Math.round(cut * 10) / 10}px`);
  }
  return out;
}"""

READY = (
    "() => document.querySelectorAll('.avail-course-card').length >= __CARDS__"
    " && document.querySelector('#baselineWeekly .wg-filled')"
    " && document.querySelector('#visualGrid .wg-filled')"
    " && document.querySelectorAll('#recTable button').length >= 5"
    " && document.querySelector('#planPalette table')"
    " && !document.querySelector('#availGrid > span.text-secondary')"
).replace("__CARDS__", str(COURSE_CARDS))
NO_MOTION = "*, *::before, *::after { transition: none !important; animation: none !important; }"


class PlannerLayoutBrowserTests(StaticLiveServerTestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._pw = sync_playwright().start()
        cls.browser = cls._pw.chromium.launch()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls._pw.stop()
        super().tearDownClass()

    def setUp(self) -> None:
        user = get_user_model().objects.create_superuser(
            username="planner-layout", email="planner-layout@example.test", password="unused"
        )
        client = Client()
        client.force_login(user)
        self.session = client.cookies[settings.SESSION_COOKIE_NAME].value
        self.page_errors: list[str] = []
        self.addCleanup(self._no_page_errors)

    def _no_page_errors(self) -> None:
        self.assertEqual(self.page_errors, [], "the planner threw while loading")

    def _open(self, language: str, width: int, height: int, font: str = "app"):
        """The planner with the made-up student loaded, as the page loads it."""
        context = self.browser.new_context(
            locale="ar" if language == "ar" else "en-US",
            extra_http_headers={"Accept-Language": language},
            viewport={"width": width, "height": height},
        )
        self.addCleanup(context.close)
        context.add_cookies(
            [
                {
                    "name": settings.SESSION_COOKIE_NAME,
                    "value": self.session,
                    "url": self.live_server_url,
                },
                {
                    "name": settings.LANGUAGE_COOKIE_NAME,
                    "value": language,
                    "url": self.live_server_url,
                },
            ]
        )
        context.add_init_script("try { localStorage.setItem('theme', 'light'); } catch (e) {}")
        if font == "wide":
            context.add_init_script(WIDE_FONT)
        # The three data requests, answered from the made-up student.
        context.route(
            "**/ops/planner/context/",
            lambda route: route.fulfill(json=CONTEXT),
        )
        context.route(
            "**/report/student-plan/**",
            lambda route: route.fulfill(json=STUDENT_PLAN),
        )
        context.route(
            "**/ops/planner/sections-catalog/",
            lambda route: route.fulfill(json=SECTIONS_CATALOG),
        )
        page = context.new_page()
        page.on("pageerror", lambda error: self.page_errors.append(str(error)))
        sent: list[str] = []
        page.on(
            "request",
            lambda r: sent.append(f"{r.method} {r.url}")
            if "/ops/planner/" in r.url or "/report/" in r.url
            else None,
        )
        page.goto(f"{self.live_server_url}/planner/?student={STUDENT_ID}")
        page.add_style_tag(content=NO_MOTION)
        self.assertEqual(page.evaluate("document.documentElement.lang"), language)
        # The page fetches the student by itself; wait for everything it draws.
        page.wait_for_function(READY, timeout=20000)
        page.evaluate("document.fonts.ready.then(() => true)")
        page.evaluate(
            "new Promise(r => requestAnimationFrame(() => requestAnimationFrame(() => r(true))))"
        )
        return page, sent

    def _variants(self, widths: dict[int, int]):
        for language in LANGUAGES:
            for font in FONTS:
                for width, height in widths.items():
                    yield language, font, width, height

    def _assert_no_findings(self, problems: dict[str, list[str]]) -> None:
        if problems:
            self.fail(
                "\n"
                + "\n".join(
                    f"{where}:\n    " + "\n    ".join(found) for where, found in problems.items()
                )
            )

    # ── F1 ───────────────────────────────────────────────────────────────────
    def test_f1_phone_panels_fit_the_screen(self) -> None:
        """At 390 and 360px no panel is wider than the screen and nothing is out of reach."""
        problems: dict[str, list[str]] = {}
        for language, font, width, height in self._variants({390: 844, 360: 800}):
            page, _sent = self._open(language, width, height, font)
            found = page.evaluate(PHONE_FIT, True)
            if found:
                problems[f"{language} {width}px {font} fonts"] = found
            page.context.close()
        self._assert_no_findings(problems)

    def test_f1_mid_widths_panels_stay_on_screen_and_equal(self) -> None:
        """Tablet and small-laptop widths: nothing past the screen, two equal columns above 992px."""
        problems: dict[str, list[str]] = {}
        sizes = {769: 1024, 1024: 768, 1100: 820}
        for language, font, width, height in self._variants(sizes):
            page, _sent = self._open(language, width, height, font)
            # Two half panels as narrow as a phone: the suggestions table still
            # shows every Add button without a sideways scroll.
            found = page.evaluate(PHONE_FIT, True)
            tracks = page.eval_on_selector(
                ".planner-grid", "n => getComputedStyle(n).gridTemplateColumns.split(' ')"
            )
            if width > 992 and (len(tracks) != 2 or tracks[0] != tracks[1]):
                found.append(f"the two panel columns are not equal: {tracks}")
            if found:
                problems[f"{language} {width}px {font} fonts"] = found
            page.context.close()
        self._assert_no_findings(problems)

    # ── F2 ───────────────────────────────────────────────────────────────────
    def test_f2_sections_list_fits_its_panel_at_laptop_widths(self) -> None:
        """Both columns of section cards sit inside the panel; no row text is cut."""
        problems: dict[str, list[str]] = {}
        sizes = {1366: 768, 1440: 900, 1920: 945, 390: 844, 360: 800}
        programs = {str(s["term_section_id"]): s["programs"] for s in SECTIONS}
        for language, font, width, height in self._variants(sizes):
            page, _sent = self._open(language, width, height, font)
            one_line = ONE_LINE_ROW_ID if (width, language, font) == (1920, "en", "app") else None
            found = page.evaluate(SECTIONS_FIT, {"oneLine": one_line, "programs": programs})
            if found:
                problems[f"{language} {width}px {font} fonts"] = found
            page.context.close()
        self._assert_no_findings(problems)

    # ── F3 ───────────────────────────────────────────────────────────────────
    def test_f3_arabic_time_ranges_read_start_first(self) -> None:
        """«اثن 10:15-11:55» was painted «11:55-10:15»; each range is one unit read left to right."""
        problems: dict[str, list[str]] = {}
        for language, font in ((lang, fnt) for lang in LANGUAGES for fnt in FONTS):
            page, _sent = self._open(language, 1440, 900, font)
            found: list[str] = []
            for section in SECTIONS:
                if not section["meetings"]:
                    continue
                selector = f'.avail-sec-row[data-tsid="{section["term_section_id"]}"] .sec-time'
                # The characters sorted by where they were painted, never textContent
                # (which passes on the broken page).
                seen = page.evaluate(VISUAL_ORDER_JS, selector)
                if seen is None:
                    found.append(f"{section['section']}: no .sec-time")
                    continue
                name = f"{section['course_key']} {section['section']}"
                shown = seen.replace(chr(10), " | ")
                # The days that share a time are listed once beside it (slice 4,
                # tests/test_planner_lists_browser.py), so each distinct range is
                # read once.
                ranges = [(m["start_time"], m["end_time"]) for m in section["meetings"]]
                for start, end in sorted(set(ranges)):
                    expected = 1
                    right = seen.count(f"{start}-{end}")
                    swapped = seen.count(f"{end}-{start}") if start != end else 0
                    if swapped:
                        found.append(
                            f"{name}: {start}-{end} is painted end first, as {end}-{start}, "
                            f"{swapped} time(s) (whole row: {shown!r})"
                        )
                    elif right != expected:
                        found.append(
                            f"{name}: {start}-{end} is read left to right {right} of "
                            f"{expected} times (whole row: {shown!r})"
                        )
                # The words survived: a visual scan names digits only, and a deleted
                # day name has no position to measure, so read the text itself.
                words = page.eval_on_selector(selector, "n => n.innerText")
                day_word = "أحد" if language == "ar" else "Sun"
                if section["section"] in ("M30", "M45") and day_word not in words:
                    found.append(
                        f"{section['section']}: the day name {day_word!r} is gone: {words!r}"
                    )
            if found:
                problems[f"{language} 1440px {font} fonts"] = found
            page.context.close()
        self._assert_no_findings(problems)

    # ── F4 ───────────────────────────────────────────────────────────────────
    def test_f4_week_grid_blocks_show_code_and_time_whole(self) -> None:
        """A block wraps its code and time; it never cuts them (no ellipsis, no clipped start)."""
        problems: dict[str, list[str]] = {}
        sizes = {1366: 768, 1440: 900, 390: 844, 360: 800}
        for language, font, width, height in self._variants(sizes):
            page, _sent = self._open(language, width, height, font)
            found = page.evaluate(
                BLOCKS_FIT,
                {
                    # A token wider than its cell may break anywhere (that is the
                    # point of not clipping), so word and wrap-point checks apply
                    # only where a whole code or half-time fits its cell.
                    "strictCode": font == "app",
                    "codeSize": "12px",
                    "timeSize": "11px",
                    "strictTime": font == "app" or width >= 390,
                },
            )
            if found:
                problems[f"{language} {width}px {font} fonts"] = found
            page.context.close()
        self._assert_no_findings(problems)

    def test_plan_palette_scroll_frame_does_not_cut_a_focus_ring(self) -> None:
        """#planPalette scrolls sideways, so it clips vertically too: the ring must fit."""
        problems: dict[str, list[str]] = {}
        for language in LANGUAGES:
            for width, height in ((390, 844), (1440, 900)):
                page, _sent = self._open(language, width, height)
                found = page.evaluate(PALETTE_RING)
                if found:
                    problems[f"{language} {width}px"] = found
                page.context.close()
        self._assert_no_findings(problems)

    # ── F5 ───────────────────────────────────────────────────────────────────
    def test_f5_switching_theme_redraws_the_week_grids(self) -> None:
        """Light, dark, light: every block keeps a readable code, and nothing is requested."""
        problems: dict[str, list[str]] = {}
        for language in LANGUAGES:
            page, sent = self._open(language, 1440, 900)
            found: list[str] = []
            self.assertEqual(page.evaluate("document.documentElement.dataset.theme"), "light")
            shortlist = page.eval_on_selector("#shortlist", "n => n.innerHTML")
            requests_before = list(sent)
            untouched = {
                sel: page.eval_on_selector(sel, "n => n.innerHTML")
                for sel in ("#availGrid", "#recTable", "#planPalette")
            }
            for theme in ("light", "dark", "light"):
                if theme != page.evaluate("document.documentElement.dataset.theme"):
                    page.click("#themeToggle")
                    page.wait_for_function(
                        "t => document.documentElement.dataset.theme === t", arg=theme
                    )
                # Let a redraw triggered by the change finish.
                page.evaluate(
                    "new Promise(r => requestAnimationFrame(() => requestAnimationFrame(() => r(true))))"
                )
                for sel, html in untouched.items():
                    if page.eval_on_selector(sel, "n => n.innerHTML") != html:
                        found.append(f"{theme}: {sel} was redrawn")
                blocks = page.evaluate(BLOCK_CONTRAST)
                if len(blocks) < 68:
                    found.append(f"{theme}: only {len(blocks)} texts (code, time) in the two grids")
                low = [b for b in blocks if b["ratio"] < 4.5]
                if low:
                    first = low[0]
                    found.append(
                        f"{theme}: {len(low)} of {len(blocks)} blocks have a code below 4.5:1 "
                        f"(first: #{first['id']} {first['label']} {first['ratio']}:1, "
                        f"text {first['fg']} on {first['bg']})"
                    )
                if len(sent) != len(requests_before):
                    found.append(
                        f"{theme}: {len(sent) - len(requests_before)} request(s) sent: "
                        f"{sent[len(requests_before) :]}"
                    )
                if page.eval_on_selector("#shortlist", "n => n.innerHTML") != shortlist:
                    found.append(f"{theme}: the shortlist was redrawn")
            if found:
                problems[f"{language} 1440px"] = found
            page.context.close()
        self._assert_no_findings(problems)
