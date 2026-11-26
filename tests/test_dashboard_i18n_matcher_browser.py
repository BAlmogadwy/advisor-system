"""Every Arabic string in the dashboard's table must actually reach the page.

The dashboard keeps English in the markup and swaps it in the browser from the
MAP in `_js_i18n.html`. That matcher used to walk `Object.entries(MAP)` and take
the first key the element's text merely *started with*, so a short key declared
earlier beat the exact key written for the same label:

    "Copy" is declared before "Copy Link"  =>  "Copy Link" rendered «نسخ Link»

Nineteen labels rendered half-Arabic that way -- «تشغيل report», «تحميل Plan»,
«إيقافped» -- and 21 keys' Arabic could never be reached at all. Nothing failed;
the page just quietly showed two languages in one label.

These tests drive the SHIPPED matcher, exposed as `window.dashboardI18n`, rather
than a Python copy of it: a copy would keep passing while the real page broke.
"""

from __future__ import annotations

import json
import os
import re

# Playwright's synchronous API uses a greenlet loop. The ORM work below is
# synchronous fixture setup; Django's live server handles requests separately.
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

import pytest  # noqa: E402
from django.conf import settings  # noqa: E402
from django.contrib.auth.models import User  # noqa: E402
from django.contrib.staticfiles.testing import StaticLiveServerTestCase  # noqa: E402
from django.test import Client  # noqa: E402
from django.urls import reverse  # noqa: E402

playwright_api = pytest.importorskip("playwright.sync_api")
sync_playwright = playwright_api.sync_playwright

ARABIC = re.compile(r"[؀-ۿ]")
LATIN = re.compile(r"[A-Za-z]")

#: Latin runs that belong in an Arabic label: file formats, acronyms, and the
#: identifiers the department uses untranslated. Anything else next to Arabic in
#: one label is a half-translation.
ALLOWED_LATIN = {
    "CSV",
    "XLSX",
    "JSON",
    "PDF",
    "GPA",
    "ID",
    "IDs",
    "MRI",
    "LIVE",
    "PID",
    "ESC",
    "HEATMAP",
    "VISUAL",
    "Neo4j",
    "Oracle",
    "AI",
    "AI2",
    "COE",
    "COE2",
    "CS",
    "CS2",
    "CYP",
    "CYP2",
    "DS",
    "DS2",
    "IS",
    "IS2",
}

#: The admin this test signs in as. Its name is interpolated into the greeting,
#: so it is data beside Arabic, not an untranslated label.
ADMIN_USERNAME = "i18n-browser-admin"

# The Arabic the MAP gives the four server-only empty-state captions, spelled
# out here rather than read back from the table so that a wrong edit to either
# one has to disagree with the other.
ARABIC_FOR_EMPTY_STATES = (
    "لم يتم تحميل المتطلبات",
    "لا توجد بيانات تشخيص",
    "لا توجد نتائج أهلية",
    "لم يتم تحميل تقرير",
)


def _lone_letter_after_number(text: str, match: re.Match[str]) -> bool:
    """Is this run a single letter that belongs to the number before it?

    The run pattern starts at a letter, so it finds the "s" inside "5s" and
    the "x" of "3 x 100" as runs of their own. Those are units and
    multipliers, not English prose left untranslated beside Arabic.
    """
    if len(match.group()) != 1:
        return False
    before = text[: match.start()].rstrip()
    return bool(before) and before[-1].isdigit()


def test_a_lone_letter_counts_as_prose_unless_a_number_claims_it() -> None:
    """The one-letter rule the stray-English check below leans on.

    No browser needed, and worth its own test: the rule decides whether a
    half-translated label is reported or waved through, and the page has no
    label of either shape today, so nothing else here would notice it break.
    """
    pattern = re.compile(r"[A-Za-z][A-Za-z0-9.+_-]*")

    def lone_runs(text: str) -> list[tuple[str, bool]]:
        return [(m.group(), _lone_letter_after_number(text, m)) for m in pattern.finditer(text)]

    # Prose: the "a" is the whole tell that this label is half-translated.
    assert lone_runs("تشغيل a scrape to see history here")[0] == ("a", False)
    # Notation: a unit or a multiplier belonging to the number before it.
    assert lone_runs("5s")[0] == ("s", True)
    assert lone_runs("3 x 100")[0] == ("x", True)
    # Nothing before it to claim it, so prose again.
    assert lone_runs("a")[0] == ("a", False)
    # Longer runs are never claimed, however they are spelled.
    assert lone_runs("100 students")[0] == ("students", False)


class DashboardI18nMatcherTests(StaticLiveServerTestCase):
    """Drive the real matcher in a real page."""

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

    def _arabic_dashboard(self):
        user = User.objects.create_superuser(
            username=ADMIN_USERNAME,
            email="i18n-browser@example.test",
            password="unused",
        )
        client = Client()
        client.force_login(user)
        context = self.browser.new_context(
            locale="ar",
            extra_http_headers={"Accept-Language": "ar"},
        )
        # Closed before the flush even when the page never loads (closing twice is harmless).
        self.addCleanup(context.close)
        context.add_cookies(
            [
                {
                    "name": settings.SESSION_COOKIE_NAME,
                    "value": client.cookies[settings.SESSION_COOKIE_NAME].value,
                    "url": self.live_server_url,
                },
                {
                    "name": settings.LANGUAGE_COOKIE_NAME,
                    "value": "ar",
                    "url": self.live_server_url,
                },
            ]
        )
        page = context.new_page()
        page.goto(f"{self.live_server_url}{reverse('dashboard')}")
        page.wait_for_function("() => Boolean(window.dashboardI18n)")
        return context, page

    # -- the invariant that makes the whole table trustworthy ---------------

    def test_every_key_in_the_table_survives_the_matcher(self) -> None:
        """No key may be shadowed by a shorter one that merely prefixes it.

        This is about the matcher, not about coverage: it proves every key
        comes back as its own Arabic, not that the page renders a label
        that key matches. Nothing here would notice a key for a string
        that no template produces.
        """
        context, page = self._arabic_dashboard()
        try:
            unreachable = page.evaluate(
                """() => {
                    const { translate, map } = window.dashboardI18n;
                    const table = map();
                    const bad = [];
                    for (const [en, ar] of Object.entries(table)) {
                        const got = translate(en);
                        if (got !== ar) bad.push({ key: en, want: ar, got });
                    }
                    return bad;
                }"""
            )
        finally:
            context.close()
        assert unreachable == [], (
            "these keys have Arabic that the matcher never returns:\n"
            + "\n".join(
                f"  {row['key']!r}: wanted {row['want']!r}, got {row['got']!r}"
                for row in unreachable
            )
        )

    def test_an_ordinary_key_never_translates_a_longer_label(self) -> None:
        """A label is translated whole or not at all -- never half.

        "Students" is a key, so "Students Scanned" used to come back
        «الطلاب Scanned», and "Stop" turned "Stopped" into «إيقافped». Only a
        "caption:" key may carry a tail, and that tail is a value, not prose.
        """
        context, page = self._arabic_dashboard()
        try:
            results = page.evaluate(
                """() => {
                    const { translate, map } = window.dashboardI18n;
                    const table = map();
                    const bad = [];
                    for (const en of Object.keys(table)) {
                        if (en.endsWith(":")) continue;  // captions carry a value
                        for (const tail of ["ped", " Scanned", "/Semester", "s"]) {
                            const probe = en + tail;
                            if (Object.prototype.hasOwnProperty.call(table, probe)) continue;
                            const got = translate(probe);
                            if (got !== null) bad.push({ probe, got });
                        }
                    }
                    return bad;
                }"""
            )
        finally:
            context.close()
        assert results == [], "the matcher half-translated:\n" + "\n".join(
            f"  {row['probe']!r} -> {row['got']!r}" for row in results
        )

    def test_the_prefix_case_still_works_for_a_filled_in_tail(self) -> None:
        """The feature the prefix branch exists for must survive the fix."""
        context, page = self._arabic_dashboard()
        try:
            got = page.evaluate(
                """() => {
                    const { translate } = window.dashboardI18n;
                    return {
                        role: translate("Role: Super Admin"),
                        scope: translate("Access scope: all"),
                    };
                }"""
            )
        finally:
            context.close()
        assert got["role"] is not None and got["role"].startswith("الدور:")
        assert got["role"].endswith(" Super Admin")
        assert got["scope"] is not None and got["scope"].startswith("نطاق الصلاحيات:")

    def test_a_label_named_after_an_object_builtin_is_left_alone(self) -> None:
        """`MAP["constructor"]` is truthy, so it used to replace the label.

        The result is checked by `typeof` INSIDE the page: a function crossing
        `page.evaluate` arrives as None, so asserting on the returned value let
        the unguarded lookup pass this test.
        """
        context, page = self._arabic_dashboard()
        try:
            got = page.evaluate(
                """() => {
                    const { translate } = window.dashboardI18n;
                    return ["constructor", "toString", "valueOf", "hasOwnProperty"]
                        .map(k => [k, translate(k) === null ? "null" : typeof translate(k)]);
                }"""
            )
        finally:
            context.close()
        assert all(kind == "null" for _, kind in got), got

    def test_the_walker_leaves_builtin_named_text_and_titles_alone(self) -> None:
        """The same guard, through the real walker, on the nodes it touches.

        `translateOptions` and `translateAttributes` look keys up directly, so
        each needed its own guard; driving the shipped walker over an injected
        subtree is what proves all three.
        """
        context, page = self._arabic_dashboard()
        try:
            got = page.evaluate(
                """() => {
                    const host = document.createElement("div");
                    host.innerHTML = `
                      <select title="constructor">
                        <option>constructor</option>
                        <option>toString</option>
                      </select>
                      <div>valueOf</div>
                      <input placeholder="constructor">
                    `;
                    document.body.appendChild(host);
                    window.dashboardI18n.walk(host);
                    const result = {
                        options: [...host.querySelectorAll("option")].map(o => o.textContent),
                        div: host.querySelector("div").textContent,
                        title: host.querySelector("select").getAttribute("title"),
                        placeholder: host.querySelector("input").getAttribute("placeholder"),
                    };
                    host.remove();
                    return result;
                }"""
            )
        finally:
            context.close()
        assert got["options"] == ["constructor", "toString"], got
        assert got["div"] == "valueOf", got
        assert got["title"] == "constructor", got
        assert got["placeholder"] == "constructor", got

    def test_the_walker_does_translate_a_real_key_in_an_injected_subtree(self) -> None:
        """So the test above cannot pass by the walker doing nothing at all."""
        context, page = self._arabic_dashboard()
        try:
            got = page.evaluate(
                """() => {
                    const host = document.createElement("div");
                    host.innerHTML = `
                      <select><option>Download</option></select>
                      <div>Copy Link</div>
                    `;
                    document.body.appendChild(host);
                    window.dashboardI18n.walk(host);
                    const result = {
                        option: host.querySelector("option").textContent,
                        div: host.querySelector("div").textContent,
                    };
                    host.remove();
                    return result;
                }"""
            )
        finally:
            context.close()
        assert got["option"] == "تنزيل", got
        assert got["div"] == "نسخ الرابط", got

    # -- the labels that were visibly broken --------------------------------

    def test_the_labels_that_rendered_half_english_are_whole_now(self) -> None:
        context, page = self._arabic_dashboard()
        try:
            got = page.evaluate(
                """() => {
                    const { translate } = window.dashboardI18n;
                    return {
                        copyLink: translate("Copy Link"),
                        runReport: translate("Run report"),
                        loadPlan: translate("Load Plan"),
                        stopped: translate("Stopped"),
                        yearSemester: translate("Year/Semester"),
                        lockedOnly: translate("Locked only"),
                        // Rendered «تشغيل a scrape to see history here»,
                        // because "Run" is a key and this sentence is not.
                        scrapeHint: translate(
                          "Run a scrape to see history here"
                        ),
                    };
                }"""
            )
        finally:
            context.close()
        for name, value in got.items():
            assert value is not None, f"{name} lost its Arabic"
            assert not LATIN.search(value), f"{name} still carries English: {value!r}"
            assert ARABIC.search(value), f"{name} is not Arabic: {value!r}"

    def test_no_rendered_label_mixes_arabic_with_stray_english(self) -> None:
        """The page itself, not just the matcher: nothing reads half-translated."""
        context, page = self._arabic_dashboard()
        try:
            leaves = page.evaluate(
                """() => {
                    const out = [];
                    document.querySelectorAll(
                        "h1,h2,h3,h4,h5,h6,div,span,p,a,button,label,th,td,summary,li,small,strong,option"
                    ).forEach(el => {
                        if (el.children.length) return;
                        const text = (el.textContent || "").trim();
                        if (text) out.push(text);
                    });
                    return out;
                }"""
            )
        finally:
            context.close()

        offenders = []
        for text in leaves:
            if not (ARABIC.search(text) and LATIN.search(text)):
                continue
            # Values the page interpolates are data beside Arabic, not labels.
            if "@" in text or ADMIN_USERNAME in text:
                continue
            # A file path the scrape status line shows: data, isolated in Latin.
            if re.search(r"[A-Za-z]:\\|/[\w.-]+/", text):
                continue
            # Notation: the d of the decay curves "(1/d)" and "(1/√d)".
            text = re.sub(r"\(1/√?d\)", "", text)
            stray = [
                run
                for run, lone_after_number in (
                    (m.group(), _lone_letter_after_number(text, m))
                    for m in re.finditer(r"[A-Za-z][A-Za-z0-9.+_-]*", text)
                )
                if run not in ALLOWED_LATIN
                # A unit or a multiplier: the s of "5s", the x of "3 x 100".
                and not lone_after_number
                # A course or programme code: CS101, AI2, COE2.
                and not re.fullmatch(r"[A-Z]{2,}[0-9]*", run)
                # A dotted path or a snake_case column name: code, not prose.
                and "." not in run
                and "_" not in run
                # A lone CAPITAL is notation, like the K in "Top K". A lone
                # lowercase letter is English prose: «تشغيل a scrape to see
                # history here» is a real half-translation whose only unlisted
                # run is the "a", and `len(run) > 1` would wave it through. No
                # label on the page has that shape today -- that one is
                # rewritten by page-dashboard.js before it can be read -- so
                # this is a hole closed, not a bug caught.
                and not re.fullmatch(r"[A-Z]", run)
            ]
            if stray:
                offenders.append((text, stray))

        assert offenders == [], "half-translated labels on the page:\n" + "\n".join(
            f"  {text!r} -- stray {stray}" for text, stray in offenders
        )

    def test_no_panel_renders_a_label_wholly_in_english(self) -> None:
        """The test above only sees a label that MIXES Arabic with English.

        A label left wholly in English passed it: in 2026-11 the Arabic
        dashboard still showed "Run preview", "Debug Details", "Download" and
        about a hundred more across nine panels, because the walker cannot
        reach an icon-and-text button or a hint with a <strong>. Text nodes
        are read here, not leaf elements, so the words beside an icon count,
        and so do placeholders, titles, aria-labels and options.
        """
        panels = [
            "batch",
            "debug",
            "prereq",
            "eligibility",
            "highpriority",
            "exports",
            "scrape",
            "advisoradmin",
            "student",
            "conflictmatrix",
            "plan",
        ]
        context, page = self._arabic_dashboard()
        try:
            strings = page.evaluate(
                """(ids) => {
                    const out = [];
                    for (const id of ids) {
                      const root = document.getElementById(id);
                      if (!root) { out.push([id, "MISSING PANEL"]); continue; }
                      const tw = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
                      while (tw.nextNode()) {
                        const p = tw.currentNode.parentElement;
                        if (!p || ["SCRIPT", "STYLE"].includes(p.tagName) || p.closest("svg")) continue;
                        const t = tw.currentNode.nodeValue.replace(/\\s+/g, " ").trim();
                        if (t) out.push([id, t]);
                      }
                      root.querySelectorAll("[placeholder],[title],[aria-label]").forEach(el => {
                        for (const a of ["placeholder", "title", "aria-label"]) {
                          const v = (el.getAttribute(a) || "").trim();
                          if (v) out.push([id, v]);
                        }
                      });
                    }
                    return out;
                }""",
                panels,
            )
        finally:
            context.close()

        offenders = []
        for panel, text in strings:
            if ARABIC.search(text) or "@" in text or ADMIN_USERNAME in text:
                continue
            words = [
                run
                for run in re.findall(r"[A-Za-z][A-Za-z0-9.+_-]*", text)
                if run.rstrip(".") not in ALLOWED_LATIN
                and not re.fullmatch(r"[A-Z]{1,}[0-9]*", run)  # codes, ids: CS101, A001
                and "_" not in run
            ]
            if words:
                offenders.append((panel, text))
        assert offenders == [], "labels still wholly English in Arabic:\n" + "\n".join(
            f"  #{panel}: {text!r}" for panel, text in offenders
        )

    def test_the_panel_empty_states_render_arabic(self) -> None:
        """A key is only worth adding if some label on the page matches it.

        The matcher tests above cannot tell: they drive it over the table, so a
        key for a string no template produces passes them all. These four
        empty-state captions are server-rendered and appear in no JS string
        table, which makes the MAP the only thing that can translate them --
        so asserting on the page is the only way to know the keys are live.
        """
        wanted = [
            "No prerequisites loaded",
            "No debug data",
            "No eligibility results",
            "No report loaded",
        ]
        context, page = self._arabic_dashboard()
        try:
            found = page.evaluate(
                """(english) => {
                    const out = {};
                    const leaves = [...document.querySelectorAll("div,span,td")]
                      .filter(el => !el.children.length)
                      .map(el => (el.textContent || "").trim());
                    for (const en of english) out[en] = leaves.includes(en);
                    return { out, leaves };
                }""",
                wanted,
            )
        finally:
            context.close()

        still_english = [en for en in wanted if found["out"][en]]
        assert still_english == [], (
            "these captions render in English, so their MAP key is not "
            f"reaching them: {still_english}"
        )
        # And the Arabic really is on the page, not merely the English absent
        # because the caption moved or was renamed.
        table = ARABIC_FOR_EMPTY_STATES
        missing = [ar for ar in table if ar not in found["leaves"]]
        assert missing == [], f"Arabic captions not rendered: {missing}"

    def test_the_debug_trace_colours_each_prerequisite_by_its_status(self) -> None:
        """A passed prerequisite is green, one being studied blue, a missing one red.

        The trace's statuses come from debug_reporting.py in upper case
        (PASSED / STUDYING / MISSING). Until 2026-11 the pill compared them with
        lower-case 'passed' / 'studying', so every pill rendered red, even for
        a prerequisite the student had passed.
        """
        trace = [
            {
                "course_code": "CS210",
                "prerequisites": ["CS101", "CS102", "MATH101"],
                "prerequisite_status": [
                    {"prerequisite": "CS101", "status": "PASSED"},
                    {"prerequisite": "CS102", "status": "STUDYING"},
                    {"prerequisite": "MATH101", "status": "MISSING"},
                ],
            }
        ]
        payload = {
            "count": 1,
            "items": [
                {
                    "student_id": "4400001",
                    "program": "CS",
                    "real_term": 3,
                    "next_term": 4,
                    "passed": ["CS101"],
                    "studying": ["CS102"],
                    "recommended_courses": ["CS210"],
                    "recommendation_details": trace,
                }
            ],
        }
        context, page = self._arabic_dashboard()
        try:
            page.route(
                "**/report/recommendation-debug/**",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(payload),
                ),
            )
            page.evaluate("() => { location.hash = 'debug'; }")
            page.fill("#dbgYear", "1447")
            page.fill("#dbgSemester", "1")
            page.click("#dbgRun")
            page.click("#dbgTable .dbg-trace")
            page.wait_for_selector("#debugTraceBody .pill-status")
            pills = page.evaluate(
                """() => [...document.querySelectorAll('#debugTraceBody .pill-status')]
                    .map(el => [el.textContent.split(':')[0].trim(),
                                ['pill-g', 'pill-b', 'pill-r'].filter(c => el.classList.contains(c))])"""
            )
        finally:
            context.close()

        assert pills == [
            ["CS101", ["pill-g"]],
            ["CS102", ["pill-b"]],
            ["MATH101", ["pill-r"]],
        ]

    def test_the_table_has_no_duplicate_keys(self) -> None:
        """Two entries for one key means one Arabic string is dead weight."""
        source = (
            settings.BASE_DIR
            / "core"
            / "templates"
            / "core"
            / "partials"
            / "dashboard"
            / "_js_i18n.html"
        ).read_text(encoding="utf-8")
        body = source[source.index("const MAP = {") :]
        body = body[: body.index("\n    };")]
        keys = re.findall(r'^\s*"((?:[^"\\]|\\.)*)"\s*:', body, re.MULTILINE)
        duplicates = sorted({key for key in keys if keys.count(key) > 1})
        assert duplicates == [], f"duplicated MAP keys: {duplicates}"
