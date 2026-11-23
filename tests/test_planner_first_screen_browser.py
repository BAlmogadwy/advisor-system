"""The staff planner (/planner/): the first screen, measured in a real browser.

Slice 2 of the planner's premium pass. The top of the page was the last glass
card in the product (a boxed command bar repeating the page title, a status
dot, a quality strip, a hidden-on-phone field bar) over a student summary of
seven "Label: value" lines. It is now a flat session section (who and when on
one line, the settings under it), ONE status line, a flat stepper row and a stat
strip, and the page shows ONE placeholder until a student is loaded.

One test per part of the specification (numbers 1-9 in each docstring). The
planner's data requests are answered from the synthetic student of
tests/test_planner_layout_browser.py (no database rows, no real data), with a
small build response defined below. Every fit assertion also runs in a wide
font (Verdana, DejaVu Sans), as a CI machine without the design's fonts renders
the page. Nothing leaves the machine (tests/browser_isolation.py).
"""

from __future__ import annotations

import copy
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
from tests.test_planner_layout_browser import (
    CONTEXT,
    NO_MOTION,
    READY,
    SECTIONS_CATALOG,
    STUDENT_ID,
    STUDENT_PLAN,
)

playwright_api = pytest.importorskip("playwright.sync_api")
sync_playwright = playwright_api.sync_playwright

LANGUAGES = ("en", "ar")
FONTS = ("app", "wide")
# hh:mm:ss, written for a JavaScript string literal that builds a RegExp
TIME = r"\\d\\d:\\d\\d:\\d\\d"

# A made-up build answer: two options, the first with two mapped sections.
BUILD = {
    "ok": True,
    "summary": {
        "scheduled": 4,
        "target": 5,
        "conflicts": 0,
        "swaps_required": 0,
        "best_feasible": True,
    },
    "options": [
        {
            "name": "A1",
            "method": "A",
            "scheduled": 4,
            "target": 5,
            "mappings": [
                {
                    "course_code": "CS",
                    "course_number": "111",
                    "course_key": "CS111",
                    "section": "M30",
                    "term_section_id": 2,
                    "meetings": [
                        {"day": "SUN", "start_time": "10:00", "end_time": "11:15", "room": "R1"},
                        {"day": "TUE", "start_time": "10:00", "end_time": "11:15", "room": "R1"},
                    ],
                },
                {
                    "course_code": "MATH",
                    "course_number": "105",
                    "course_key": "MATH105",
                    "section": "M41",
                    "term_section_id": 9,
                    "meetings": [
                        {"day": "SUN", "start_time": "12:45", "end_time": "14:25", "room": "R2"},
                    ],
                },
            ],
            "unscheduled": [{"course_code": "PHYS103", "reason": "no seat"}],
        },
        {
            "name": "B1",
            "method": "B",
            "scheduled": 3,
            "target": 5,
            "mappings": [],
            "unscheduled": [],
        },
    ],
    "unscheduled": [],
    "swap_suggestions": [],
}

# What each setting reads as, per language.
SUCCESS = {"en": "Student context loaded successfully.", "ar": "تم تحميل بيانات الطالب بنجاح."}
FETCHED = {"en": "Fetched", "ar": "تم الجلب"}
ERROR = {"en": "Error", "ar": "خطأ"}
NO_STUDENT = {"en": "No student loaded", "ar": "لم يتم تحميل بيانات طالب"}
BUILD_TITLE = {"en": "Build options", "ar": "بناء الخيارات"}
SHORTLIST_TITLE = {"en": "Courses to schedule", "ar": "المقررات المراد جدولتها"}
SWAPS_LABEL = {"en": "Suggest swaps", "ar": "اقتراح تبديلات"}
QUALITY = {
    "en": "Mapping quality: partial (2 fallback)",
    "ar": "جودة الربط: جزئي (2 احتياطي)",
}
LAST_BUILD = {"en": "Last build", "ar": "آخر بناء"}

# A calm line has no box; a line that needs attention is tinted.
STYLE = """(sel) => {
  const s = getComputedStyle(document.querySelector(sel));
  return { bg: s.backgroundColor, border: s.borderTopWidth, shadow: s.boxShadow };
}"""

# `allowGround`: the step bar is sticky (slice 5), so it carries the page's own ground colour
# (opaque, so content never shows through) and nothing else: no glass, no shadow, no gradient.
FLAT = r"""(args) => {
  const [sel, allowRuleBelow, allowGround] = args;
  const el = document.querySelector(sel);
  if (!el) return [`${sel}: missing`];
  const s = getComputedStyle(el);
  const bad = [];
  const blur = s.backdropFilter || s.webkitBackdropFilter || 'none';
  if (blur !== 'none') bad.push(`${sel}: backdrop-filter ${blur}`);
  if (s.boxShadow !== 'none') bad.push(`${sel}: box-shadow ${s.boxShadow}`);
  let ground = 'rgba(0, 0, 0, 0)';
  if (allowGround) {
    const probe = document.createElement('i'); document.body.append(probe);
    probe.style.color = getComputedStyle(document.documentElement).getPropertyValue('--bg').trim();
    ground = getComputedStyle(probe).color; probe.remove();
  }
  if (s.backgroundColor !== 'rgba(0, 0, 0, 0)' && s.backgroundColor !== ground)
    bad.push(`${sel}: background ${s.backgroundColor}`);
  if (s.backgroundImage !== 'none') bad.push(`${sel}: background-image`);
  for (const side of ['Top', 'Left', 'Right', 'Bottom']) {
    if (side === 'Bottom' && allowRuleBelow) continue;
    if (parseFloat(s['border' + side + 'Width']) > 0 && s['border' + side + 'Style'] !== 'none')
      bad.push(`${sel}: border-${side.toLowerCase()} ${s['border' + side + 'Width']}`);
  }
  if (parseFloat(s.borderTopLeftRadius) > 0) bad.push(`${sel}: radius ${s.borderTopLeftRadius}`);
  return bad;
}"""

# Centres and boxes of the session's controls, by id.
BOXES = """(ids) => Object.fromEntries(ids.map(id => {
  const el = document.getElementById(id);
  const r = el.getBoundingClientRect();
  return [id, { top: r.top, bottom: r.bottom, left: r.left, right: r.right,
                mid: (r.top + r.bottom) / 2, w: r.width, h: r.height }];
}))"""

# The resolved --text-md in px, so a title is compared with the token itself.
TOKEN_PX = """(name) => {
  const probe = document.createElement('div');
  probe.style.cssText = `position:absolute;visibility:hidden;font-size:var(${name})`;
  document.body.append(probe);
  const px = getComputedStyle(probe).fontSize;
  probe.remove();
  return px;
}"""

# What a heading looks like next to a plain element carrying the same class:
# the heading's own element rules (an h2's margins, face or weight) must not leak.
LIKE_CLASS = """(args) => {
  const [sel, cls] = args;
  const probe = document.createElement('div');
  probe.className = cls;
  document.body.append(probe);
  const want = getComputedStyle(probe);
  const out = [];
  for (const el of document.querySelectorAll(sel)) {
    const got = getComputedStyle(el);
    for (const p of ['fontFamily', 'color', 'marginTop', 'marginBottom', 'fontWeight', 'lineHeight'])
      if (p === 'lineHeight' ? false : got[p] !== want[p])
        out.push(`${el.textContent.trim().slice(0, 20)}: ${p} ${got[p]} != ${want[p]}`);
  }
  probe.remove();
  return out;
}"""


class PlannerFirstScreenBrowserTests(StaticLiveServerTestCase):
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
            username="planner-first-screen",
            email="planner-first-screen@example.test",
            password="unused",
        )
        client = Client()
        client.force_login(user)
        self.session = client.cookies[settings.SESSION_COOKIE_NAME].value
        self.page_errors: list[str] = []
        self.addCleanup(self._no_page_errors)

    def _no_page_errors(self) -> None:
        self.assertEqual(self.page_errors, [], "the planner threw")

    def _open(
        self,
        language: str,
        width: int,
        height: int = 900,
        font: str = "app",
        *,
        student: bool = False,
        context: dict | None = None,
        state: dict | None = None,
    ):
        """The planner as it loads. With `student` the deep link fetches the made-up
        student (`context` replaces it; `state["fail"]` makes the answer an error)."""
        browser_context = self.browser.new_context(
            locale="ar" if language == "ar" else "en-US",
            extra_http_headers={"Accept-Language": language},
            viewport={"width": width, "height": height},
        )
        self.addCleanup(browser_context.close)
        browser_context.add_cookies(
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
        browser_context.add_init_script(
            "try { localStorage.setItem('theme', 'light'); } catch (e) {}"
        )
        if font == "wide":
            browser_context.add_init_script(WIDE_FONT)
        answer = context if context is not None else CONTEXT
        state = state if state is not None else {}

        def serve_context(route):
            if state.get("fail"):
                route.fulfill(json={"error": "Student not found"})
            else:
                route.fulfill(json=answer)

        browser_context.route("**/ops/planner/context/", serve_context)
        browser_context.route(
            "**/report/student-plan/**", lambda route: route.fulfill(json=STUDENT_PLAN)
        )
        browser_context.route(
            "**/ops/planner/sections-catalog/", lambda route: route.fulfill(json=SECTIONS_CATALOG)
        )
        browser_context.route("**/ops/planner/build/", lambda route: route.fulfill(json=BUILD))
        page = browser_context.new_page()
        page.on("pageerror", lambda error: self.page_errors.append(str(error)))
        page.goto(
            f"{self.live_server_url}/planner/" + (f"?student={STUDENT_ID}" if student else "")
        )
        page.add_style_tag(content=NO_MOTION)
        self.assertEqual(page.evaluate("document.documentElement.lang"), language)
        if student:
            if state.get("fail"):
                page.wait_for_function(
                    "() => /Error|خطأ/.test(document.getElementById('fetchState').textContent)",
                    timeout=20000,
                )
            else:
                page.wait_for_function(READY, timeout=20000)
        page.evaluate("document.fonts.ready.then(() => true)")
        page.evaluate(
            "new Promise(r => requestAnimationFrame(() => requestAnimationFrame(() => r(true))))"
        )
        return page

    def _loaded(self, language: str, width: int = 1440, **kwargs):
        return self._open(language, width, student=True, **kwargs)

    def _assert_no_findings(self, problems: dict[str, list[str]]) -> None:
        if problems:
            self.fail(
                "\n"
                + "\n".join(
                    f"{where}:\n    " + "\n    ".join(found) for where, found in problems.items()
                )
            )

    # ── 1. a flat session section and a flat stepper ─────────────────────────
    def test_1_the_session_and_the_stepper_are_flat(self) -> None:
        problems: dict[str, list[str]] = {}
        for language in LANGUAGES:
            page = self._open(language, 1440)
            found = page.evaluate(FLAT, [".planner-session", False])
            found += page.evaluate(FLAT, [".wf-stepper", False])
            # the stepper row itself may carry a rule under it, nothing around it
            found += page.evaluate(FLAT, [".planner-steps", True, True])
            for gone in (".command-bar", ".cb-header", ".sticky-head", "#trustStrip", "#fetchDot"):
                if page.locator(gone).count():
                    found.append(f"{gone} still exists")
            chips = " ".join(page.locator(".ph-chip").all_text_contents())
            if "Steps A" in chips or "خطوات" in chips:
                found.append(f"the Steps A to C chip is still there: {chips!r}")
            if page.locator("#plannerContextChip").count() != 1:
                found.append("#plannerContextChip is gone")
            title = (
                page.locator(".planner-session").inner_text()
                if page.locator(".planner-session").count()
                else ""
            )
            if "Timetable Builder" in title or "منشئ الجدول" in title:
                found.append("the page title is repeated inside the session")
            if found:
                problems[language] = found
            page.context.close()
        self._assert_no_findings(problems)

    # ── 2. desktop: who and when on one line, the settings under it ──────────
    def test_2_desktop_row_one_is_one_line_and_the_settings_sit_below(self) -> None:
        problems: dict[str, list[str]] = {}
        for language in LANGUAGES:
            for font in FONTS:
                for width in (1440, 1366):
                    page = self._open(language, width, 800, font)
                    boxes = page.evaluate(
                        BOXES,
                        [
                            "studentId",
                            "year",
                            "term",
                            "fetchBtn",
                            "mode",
                            "eligibilityMode",
                            "swap",
                            "eligibilityModeHelp",
                        ],
                    )
                    found = []
                    first = ["studentId", "year", "term", "fetchBtn"]
                    mids = [boxes[i]["mid"] for i in first]
                    if max(mids) - min(mids) > 2:
                        found.append(f"row one is not one line: {[round(m, 1) for m in mids]}")
                    row_bottom = max(boxes[i]["bottom"] for i in first)
                    for below in ("mode", "eligibilityMode", "swap"):
                        if boxes[below]["top"] < row_bottom - 0.5:
                            found.append(f"#{below} is not below row one")
                    ids = list(boxes)
                    for a in range(len(ids)):
                        for b in range(a + 1, len(ids)):
                            x, y = boxes[ids[a]], boxes[ids[b]]
                            if (
                                min(x["right"], y["right"]) - max(x["left"], y["left"]) > 1
                                and min(x["bottom"], y["bottom"]) - max(x["top"], y["top"]) > 1
                            ):
                                found.append(f"#{ids[a]} overlaps #{ids[b]}")
                    # The phone's toggle does not exist here, and the settings stay.
                    if page.locator(".planner-options-toggle").is_visible():
                        found.append("the Options toggle shows on a desktop")
                    if not page.locator("#mode").is_visible():
                        found.append("#mode is hidden on a desktop")
                    if found:
                        problems[f"{language} {width}px {font} fonts"] = found
                    page.context.close()
        self._assert_no_findings(problems)

    # ── 3. phone: row one is always on screen, only the settings fold ────────
    def test_3_phone_row_one_is_on_screen_and_the_options_fold(self) -> None:
        problems: dict[str, list[str]] = {}
        for language in LANGUAGES:
            for font in FONTS:
                for width, height in ((390, 844), (360, 800)):
                    page = self._open(language, width, height, font)
                    found = []
                    boxes = page.evaluate(BOXES, ["studentId", "year", "term", "fetchBtn"])
                    for name in ("studentId", "fetchBtn"):
                        box = boxes[name]
                        if not page.locator(f"#{name}").is_visible():
                            found.append(f"#{name} is not visible on first load")
                        if (
                            box["left"] < -0.5
                            or box["right"] > width + 0.5
                            or box["bottom"] > height
                        ):
                            found.append(f"#{name} is off screen: {box}")
                    fetch = boxes["fetchBtn"]
                    mates = [
                        n
                        for n in ("studentId", "year", "term")
                        if abs(boxes[n]["mid"] - fetch["mid"]) < 8
                    ]
                    if not mates:
                        found.append("Fetch is alone on its line")
                    if page.locator("#mode").is_visible():
                        found.append("#mode shows before the Options toggle is pressed")
                    toggle = page.locator(".planner-options-toggle")
                    if not toggle.is_visible():
                        found.append("no Options toggle on a phone")
                    else:
                        control_h = page.evaluate(
                            "() => parseFloat(getComputedStyle(document.documentElement)"
                            ".getPropertyValue('--control-h')) * (/rem/.test(getComputedStyle("
                            "document.documentElement).getPropertyValue('--control-h')) ? 16 : 1)"
                        )
                        if toggle.bounding_box()["height"] < control_h - 0.5:
                            found.append(f"the toggle is {toggle.bounding_box()['height']}px high")
                        if toggle.get_attribute("aria-expanded") != "false":
                            found.append("the toggle starts expanded")
                        if "▼" in toggle.inner_text():
                            found.append("a text arrow in the toggle")
                        toggle.click()
                        for field in ("#mode", "#eligibilityMode", "#swap"):
                            if not page.locator(field).is_visible():
                                found.append(f"{field} did not open")
                        if toggle.get_attribute("aria-expanded") != "true":
                            found.append("aria-expanded did not follow the opening")
                        toggle.click()
                        for field in ("#mode", "#eligibilityMode", "#swap"):
                            if page.locator(field).is_visible():
                                found.append(f"{field} did not close")
                        if toggle.get_attribute("aria-expanded") != "false":
                            found.append("aria-expanded did not follow the closing")
                        # left closed on a phone, a desktop still shows the settings
                        page.set_viewport_size({"width": 1440, "height": 800})
                        if not page.locator("#mode").is_visible():
                            found.append("closed on a phone, hidden on a desktop")
                    if found:
                        problems[f"{language} {width}px {font} fonts"] = found
                    page.context.close()
        self._assert_no_findings(problems)

    # ── 4. one status line ───────────────────────────────────────────────────
    def test_4_one_status_line_calm_when_calm_tinted_when_not(self) -> None:
        problems: dict[str, list[str]] = {}
        for language in LANGUAGES:
            found = []
            page = self._loaded(language)
            # the one status line lives in the sticky step bar (slice 5), not in the session
            count = page.evaluate(
                "document.querySelectorAll('.planner-steps [role=status]').length"
            )
            if count != 1:
                found.append(f"{count} role=status elements in the step bar")
            if page.evaluate("document.querySelectorAll('.planner-session [role=status]').length"):
                found.append("a role=status element is still in the session")
            banner = page.locator("#statusBanner").inner_text()
            if SUCCESS[language] not in banner:
                found.append(f"status line reads {banner!r}")
            state = page.locator("#fetchState").inner_text()
            if "@" in state:
                found.append(f"fetch state still has an @: {state!r}")
            if FETCHED[language] not in state or not page.locator("#fetchState bdi").count():
                found.append(f"fetch state reads {state!r}, time not in a <bdi>")
            if not page.locator("#fetchState").evaluate(
                f"n => new RegExp('{TIME}').test(n.textContent)"
            ):
                found.append(f"no time in the fetch state: {state!r}")
            calm = page.evaluate(STYLE, "#statusBanner")
            if calm["bg"] != "rgba(0, 0, 0, 0)" or calm["border"] != "0px":
                found.append(f"a calm state has a box: {calm}")
            ordered = page.evaluate(VISUAL_ORDER_JS, "#fetchState") or ""
            if not page.evaluate(f"s => new RegExp('{TIME}').test(s)", ordered):
                found.append(f"the time is painted backwards: {ordered!r}")
            page.context.close()

            failing = self._open(language, 1440, student=True, state={"fail": True})
            err = failing.evaluate(STYLE, "#statusBanner")
            if err["bg"] == "rgba(0, 0, 0, 0)":
                found.append("an error has no tint")
            if ERROR[language] not in failing.locator("#fetchState").inner_text():
                found.append("the fetch state does not say Error")
            if found:
                problems[language] = found
            failing.context.close()
        self._assert_no_findings(problems)

    # ── 5. mapping quality only when it is not good; the last build time ─────
    def test_5_quality_shows_only_when_partial_and_the_build_time_is_beside_results(self) -> None:
        partial = copy.deepcopy(CONTEXT)
        for row in partial["baseline"]:
            if row["term_section_id"] in (23001, 23002):
                row["source"] = "registration_plan_fallback"
        problems: dict[str, list[str]] = {}
        for language in LANGUAGES:
            found = []
            page = self._loaded(language, context=partial)
            quality = page.locator("#baselineQuality")
            if not quality.count() or not quality.is_visible():
                found.append("no #baselineQuality for a partial mapping")
            else:
                if QUALITY[language] not in quality.inner_text():
                    found.append(f"quality reads {quality.inner_text()!r}")
                if "planner-banner-warn" not in (quality.get_attribute("class") or ""):
                    found.append("quality is not a warning line")
            if "Unresolved" in page.content() or "trustStrip" in page.content():
                found.append("the old quality strip is still on the page")
            page.context.close()

            page = self._loaded(language)
            normal = page.locator("#baselineQuality")
            if normal.count() and normal.inner_text().strip():
                found.append(f"quality shown for a good mapping: {normal.inner_text()!r}")
            if (
                page.locator("#lastBuildAt").count()
                and page.locator("#lastBuildAt").inner_text().strip()
            ):
                found.append("a last-build time before any build")
            page.click("#nextStep")  # Build is on the second step (slice 5)
            page.click("#runBuilder")
            page.wait_for_function(
                f"() => new RegExp('{TIME}').test(document.getElementById('lastBuildAt').textContent)",
                timeout=10000,
            )
            built = page.locator("#lastBuildAt")
            if LAST_BUILD[language] not in built.inner_text() or not built.locator("bdi").count():
                found.append(f"last build reads {built.inner_text()!r}")
            if not page.evaluate(
                "() => document.getElementById('builderSummary').nextElementSibling.id === 'lastBuildAt'"
            ):
                found.append("#lastBuildAt is not directly under #builderSummary")
            # a changed setting invalidates the results, and the time with them
            page.locator("#swap").evaluate("n => n.click()")
            if page.locator("#lastBuildAt").inner_text().strip():
                found.append("the build time survived invalidation")
            if found:
                problems[language] = found
            page.context.close()
        self._assert_no_findings(problems)

    # ── 6. the student summary is a stat strip ───────────────────────────────
    def test_6_the_student_summary_is_a_stat_strip_and_is_escaped(self) -> None:
        problems: dict[str, list[str]] = {}
        for language in LANGUAGES:
            found = []
            rich = copy.deepcopy(CONTEXT)
            rich["student"]["advisor_id"] = "A123"
            page = self._loaded(language, context=rich)
            stats = page.locator("#studentSummary .ux-stat")
            if stats.count() != 3:
                found.append(f"{stats.count()} stats")
            else:
                values = [stats.nth(i).locator("dd").inner_text() for i in range(3)]
                if values[0] != "1.00":
                    found.append(f"GPA {values[0]!r}")
                if values[1] != "27":
                    found.append(f"registered credits {values[1]!r}")
                if not values[2].startswith("18") or "19" not in values[2]:
                    found.append(f"credit cap {values[2]!r}")
                if not stats.nth(2).locator(".ux-stat-unit").count():
                    found.append("no unit beside a cap below the regulatory limit")
                elif language == "ar" and "النظامي" not in values[2]:
                    found.append(f"the unit is not Arabic: {values[2]!r}")
                elif language == "en" and "advisory" not in values[2]:
                    found.append(f"the unit reads {values[2]!r}")
            if not page.locator("#studentSummary section.ux-stat-strip[aria-label]").count():
                found.append("the stats are not a labelled .ux-stat-strip")
            who = page.locator("#studentSummary .planner-student-meta").inner_text()
            for part in (STUDENT_ID, "AI2", "A123"):
                if part not in who:
                    found.append(f"{part!r} missing from {who!r}")
            if page.locator("#studentSummary .planner-student-name").inner_text() != "Student Name":
                found.append("the name is not in .planner-student-name")
            page.context.close()

            # no unit when the regulatory limit is not higher; no advisor when none
            plain = copy.deepcopy(CONTEXT)
            plain["student"]["regulatory_max_credits"] = 18
            plain["student"]["gpa"] = None
            page = self._loaded(language, context=plain)
            if page.locator("#studentSummary .ux-stat-unit").count():
                found.append("a unit with no higher regulatory limit")
            if page.locator("#studentSummary .ux-stat dd").first.inner_text() != "—":
                found.append("a missing GPA is not a dash")
            meta = page.locator("#studentSummary .planner-student-meta").inner_text()
            if "Advisor" in meta or "المرشد" in meta:
                found.append(f"an advisor shown when there is none: {meta!r}")
            page.context.close()

            hostile = copy.deepcopy(CONTEXT)
            name = '<img src=x onerror="window.__xss=1">'
            hostile["student"]["name"] = name
            hostile["student"]["program"] = "<b>AI2</b>"
            page = self._loaded(language, context=hostile)
            page.wait_for_timeout(300)
            if page.evaluate("window.__xss") is not None:
                found.append("the student's name ran as markup")
            if page.locator("#studentSummary .planner-student-name").inner_text() != name:
                found.append("the name is not shown as text")
            if page.locator("#studentSummary img, #studentSummary b").count():
                found.append("markup from the student's data reached the page")
            if found:
                problems[language] = found
            page.context.close()
        self._assert_no_findings(problems)

    # ── 7. one placeholder before a student is loaded ────────────────────────
    def test_7_one_placeholder_until_a_student_is_loaded(self) -> None:
        problems: dict[str, list[str]] = {}
        for language in LANGUAGES:
            for width in (1440, 390):
                found = []
                state: dict = {}
                page = self._open(language, width, 900, state=state)
                empties = page.locator("main .empty-state:visible")
                if empties.count() != 1:
                    found.append(f"{empties.count()} placeholders before a fetch")
                elif NO_STUDENT[language] not in empties.inner_text():
                    found.append(f"placeholder reads {empties.inner_text()!r}")
                if page.locator(".planner-panel:visible").count():
                    found.append("panels show before a student is loaded")
                if page.locator("[data-loaded]").first.get_attribute("data-loaded") != "false":
                    found.append("data-loaded is not false at first")
                page.fill("#studentId", STUDENT_ID)
                page.click("#fetchBtn")
                page.wait_for_function(READY, timeout=20000)
                if page.locator("main .empty-state:visible").count():
                    found.append("the placeholder stays after a fetch")
                if not page.locator(".planner-panel:visible").count():
                    found.append("no panel after a fetch")
                if page.locator("[data-loaded]").first.get_attribute("data-loaded") != "true":
                    found.append("data-loaded is not true after a fetch")
                state["fail"] = True
                page.click("#fetchBtn")
                page.wait_for_function(
                    "() => /Error|خطأ/.test(document.getElementById('fetchState').textContent)"
                )
                if not page.locator(".planner-panel:visible").count():
                    found.append("a failed later fetch hid the panels")
                if page.locator("main .empty-state:visible").count():
                    found.append("a failed later fetch brought the placeholder back")
                if found:
                    problems[f"{language} {width}px"] = found
                page.context.close()
        self._assert_no_findings(problems)

    # ── 8. three plain names, and no raw source key ──────────────────────────
    def test_8_plain_names_and_a_totals_line_without_the_source_key(self) -> None:
        problems: dict[str, list[str]] = {}
        totals = {"en": "Courses: 5 | Credits: 17", "ar": "المقررات: 5 | الساعات: 17"}
        for language in LANGUAGES:
            found = []
            page = self._loaded(language)
            for sel, want in (
                ("#builderCard .pane-title", BUILD_TITLE[language]),
                ("#shortlistCard .pane-title", SHORTLIST_TITLE[language]),
                ("label:has(#swap)", SWAPS_LABEL[language]),
            ):
                got = page.locator(sel).first.text_content().strip()
                if got != want:
                    found.append(f"{sel} reads {got!r}, not {want!r}")
            line = page.locator("#baselineTotals").inner_text().strip()
            if line != totals[language]:
                found.append(f"totals read {line!r}")
            for raw in ("Source", "registration_plan", "المصدر"):
                if raw in line:
                    found.append(f"the totals line still says {raw!r}")
            if found:
                problems[language] = found
            page.context.close()
        self._assert_no_findings(problems)

    # ── 9. panel titles: a level between the page title and the body ─────────
    def test_9_panel_titles_are_h2_at_the_medium_step(self) -> None:
        problems: dict[str, list[str]] = {}
        for language in LANGUAGES:
            found = []
            page = self._loaded(language)
            md = page.evaluate(TOKEN_PX, "--text-md")
            titles = page.evaluate(
                """() => [...document.querySelectorAll('.planner-grid .pane-title')].map(n => {
                  const s = getComputedStyle(n);
                  return [n.tagName, s.fontSize, s.fontWeight, n.textContent.trim().slice(0, 24)];
                })"""
            )
            if len(titles) < 6:  # the student strip lost its title and the two timetables merged
                found.append(f"only {len(titles)} panel titles")
            for tag, size, weight, text in titles:
                if tag != "H2":
                    found.append(f"{text!r} is a {tag}")
                if size != md:
                    found.append(f"{text!r} is {size}, not {md}")
                if weight != "700":
                    found.append(f"{text!r} has weight {weight}")
            found += page.evaluate(
                LIKE_CLASS, [".planner-grid > :not(#timetableCard) h2.pane-title", "pane-title"]
            )
            found += page.evaluate(
                LIKE_CLASS, ["#advancedDiag .pl-diag-heading", "pl-diag-heading"]
            )
            if page.locator("#advancedDiag h3.pl-diag-heading").count() != 1:
                found.append("the diagnostics heading is not an h3")
            if page.locator("h1").count() != 1:
                found.append(f"{page.locator('h1').count()} h1 on the page")
            if found:
                problems[language] = found
            page.context.close()
        self._assert_no_findings(problems)
