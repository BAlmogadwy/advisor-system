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

    def test_every_key_in_the_table_reaches_the_page(self) -> None:
        """No key may be shadowed by a shorter one that merely prefixes it."""
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
            stray = [
                run
                for run in re.findall(r"[A-Za-z][A-Za-z0-9.+_-]*", text)
                if run not in ALLOWED_LATIN
                # A course or programme code: CS101, AI2, COE2.
                and not re.fullmatch(r"[A-Z]{2,}[0-9]*", run)
                # A dotted path, a snake_case column name, or a lone symbol
                # such as the K in "Top K": code and notation, not prose.
                and "." not in run
                and "_" not in run
                and len(run) > 1
            ]
            if stray:
                offenders.append((text, stray))

        assert offenders == [], "half-translated labels on the page:\n" + "\n".join(
            f"  {text!r} -- stray {stray}" for text, stray in offenders
        )

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
