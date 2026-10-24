"""Contrast and the type floor, rendered (stage 3, C1; docs/stage3/C1.md).

Seven staff pages in a real browser, light and dark: the Timetable Workspace,
split view, the dashboard, the planner, user management, the profile and the
database admin page; and (after review) the timetable graph, the two sign-in
pages signed out, and the student portal's home and adviser signed in as a
student. What C1 changed is measured as the user sees it, against the
backgrounds it actually sits on (translucent layers composited; a gradient at
each of its stops, the worst counting):

- every text painted in a C1 token passes 4.5:1 (3:1 when large): brand text
  (`--text-brand`), a severity ink (`--sev-*`), and the primary pair
  (`--btn-primary-ink` on `--btn-primary-bg`);
- the skip link passes (it was 4.11:1 light, 3.04:1 dark, on every page);
- every control drawn in the primary pair still passes with :hover and with
  :active forced (the sign-in button's hover was 3.04:1, the graph's Sync 1:1),
  and so do the confirm dialog's primary and the student adviser's avatar;
- no text is painted in the `--teal` fill any more (split view's own sheet too,
  since style check S3);
- the sidebar's text never goes under 11px, at 1440, 1100 and in the phone
  drawer at 400 (it was 8.9, 7.4 and 6.5px);
- the workspace's "Draft" pill (4.17:1), its filter placeholder (2.02:1) and
  its English heading (3.63:1) pass, and the pill lies flat in the toolbar;
- split view's brand text is no lower than it was (its English heading reads
  the shared rule), and split follows the theme: its colour scheme is the
  theme's, light or dark;
- a light theme tells the browser it is light, even when the OS is dark;
- in forced colours the skip link and the pills keep a visible box.

In a wide font (Verdana, DejaVu Sans), as CI's fonts render. Nothing leaves
the machine (tests/browser_isolation.py); the pages are only read.
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

from core.models import SectionPlacement
from core.services.timetable_section_policy import create_policy_section_bundle
from tests import one_teacher_support as fx
from tests.test_exam_rosters_browser import WIDE_FONT

playwright_api = pytest.importorskip("playwright.sync_api")
sync_playwright = playwright_api.sync_playwright

THEMES = ("light", "dark")
#: The workspace-specific checks need the board loaded; the others only the page.
PAGES = {
    "workspace": "/timetable-workspace/?scenario={sc}",
    "split": "/timetable-workspace/split/?scenario={sc}",
    "dashboard": "/",
    "planner": "/planner/",
    "user-management": "/user-management/",
    "profile": "/profile/",
    "db-admin": "/db-admin/",
    "graph": "/timetable-workspace/graph/?scenario={sc}",
}
#: Signed out: the staff and student sign-in pages (their button is the only
#: filled primary a visitor meets, and its hover was white on --teal-bright).
SIGNED_OUT = {"login": "/login/", "student-login": "/student/login/"}
#: Signed in as a student: the portal (light by default, with its own toggle).
STUDENT = {"student-home": "/student/", "student-advisor": "/student/advisor/"}
STUDENT_ID = 4410001

# Shared by every measurement: parse a computed colour, composite a translucent
# one over what is under it, and the WCAG contrast of two colours. The
# background of an element is its own and its ancestors' colours composited
# down to the first opaque one (or the page); a gradient or image on the way
# makes it unmeasurable (null), as in the stage-3 audits.
MEASURE = r"""
const parse = c => { const m = c && c.match(/rgba?\(([^)]+)\)/); if (!m) return null;
  const p = m[1].split(/[ ,\/]+/).filter(Boolean).map(Number); return {r:p[0], g:p[1], b:p[2], a:p.length > 3 ? p[3] : 1}; };
const over = (f, b) => ({r:f.r*f.a + b.r*(1-f.a), g:f.g*f.a + b.g*(1-f.a), b:f.b*f.a + b.b*(1-f.a), a:1});
const lum = c => { const f = v => { v /= 255; return v <= 0.03928 ? v/12.92 : Math.pow((v+0.055)/1.055, 2.4); };
  return 0.2126*f(c.r) + 0.7152*f(c.g) + 0.0722*f(c.b); };
const ratio = (a, b) => { const x = lum(a), y = lum(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); };
const white = {r:255, g:255, b:255, a:1};
// Every opaque colour the element's text may sit on: its own and its
// ancestors' colours composited down to the first opaque one, and a gradient
// on the way at each of its stops, over what is under it (an image: null).
const bgsOf = el => {
  const layers = [];
  let bases = null;
  for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
    const cs = getComputedStyle(n), c = parse(cs.backgroundColor);
    if (cs.backgroundImage !== 'none') {
      if (/url\(/.test(cs.backgroundImage)) return null;
      const under = c && c.a === 1 ? [c]
        : (bgsOf(n.parentElement) || []).map(b => (c && c.a > 0 ? over(c, b) : b));
      const stops = (cs.backgroundImage.match(/rgba?\([^)]+\)/g) || []).map(parse).filter(Boolean);
      bases = under.flatMap(b => (stops.length ? stops.map(s => over(s, b)) : [b]));
      if (!bases.length) return null;
      break;
    }
    if (c && c.a > 0) { if (c.a === 1) { bases = [c]; break; } layers.push(c); }
  }
  return (bases || [white]).map(b => layers.reduceRight((acc, layer) => over(layer, acc), b));
};
// One background, where there is exactly one (no gradient on the way).
const bgOf = el => { const all = bgsOf(el); return all && all.length === 1 ? all[0] : null; };
// The worst contrast of the element's text over everything it may sit on.
const contrastOf = (el, pseudo) => {
  const fg = parse(getComputedStyle(el, pseudo || null).color), bgs = bgsOf(el);
  if (!fg || !bgs) return null;
  return Math.min(...bgs.map(bg => +ratio(fg.a < 1 ? over(fg, bg) : fg, bg).toFixed(2)));
};
const shown = el => { const r = el.getBoundingClientRect(), s = getComputedStyle(el);
  return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none' && +s.opacity > 0.05; };
const ownText = el => [...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim());
// A token's value (a hex) as the browser reports a computed colour.
const probe = document.createElement('i'), seenRgb = new Map();
const rgbOf = value => { if (!seenRgb.has(value)) { document.body.append(probe); probe.style.color = value;
  seenRgb.set(value, getComputedStyle(probe).color); probe.remove(); } return seenRgb.get(value); };
const label = el => (el.id ? '#' + el.id : el.tagName.toLowerCase())
  + (el.className && el.className.toString().trim() ? '.' + el.className.toString().trim().split(/\s+/).slice(0, 2).join('.') : '')
  + ' "' + el.textContent.trim().slice(0, 24) + '"';
"""

# Every visible text painted in a C1 token, measured; and every text still in
# the --teal fill. Tokens are resolved at the element, so a subtree that
# overrides one (split view's dark block) is compared with its own value.
# Named too: the controls C1 moved onto the primary pair, wherever a page shows
# one (the planner's active step and its number sit on that fill in a wash of
# their own, so neither colour test would see them).
NAMED = ", ".join(
    (
        ".wf-step.active",
        ".wf-step.active .wf-num",
        ".wf-nav .wf-next",
        ".pl-btn-primary",
        ".um-create-btn",
        ".um-act-btn.um-act-save",
        ".pf-submit-primary",
        ".audit-view-btn.active",
        ".pg-btn.active",
        ".ap-grad-tab.is-active",
        ".dlg-footer .btn-confirm.primary",
        ".tw-english-heading",
        "#twValidationBadge",
        ".avatar",
        ".neu-btn-text",
        ".va-avatar",
        ".pl-btn-teal",
        ".tg-btn-primary",
    )
)
#: What each page must have measured among the named controls (so a page that
#: stops drawing one cannot pass by measuring nothing). The sidebar's initials
#: sit on a gradient of the primary pair, measured at both of its stops.
MEASURED = {
    "workspace": ("#twValidationBadge", "tw-english-heading", "div.avatar"),
    "planner": ("wf-step.active", "span.wf-num", "pl-btn-primary", "wf-next", "pl-btn-teal"),
    "user-management": ("um-create-btn",),
    "graph": ("#tgSync",),
    "login": ("neu-btn-text",),
    "student-login": ("neu-btn-text",),
}
AUDIT = (
    "() => {"
    + MEASURE
    + r"""
  const INKS = ['--text-brand', '--sev-ok', '--sev-critical', '--sev-warning', '--sev-info'];
  const failed = [], teal = [], named = []; let checked = 0, skipped = 0;
  for (const el of document.querySelectorAll('body *')) {
    if (!ownText(el) || !shown(el) || el.closest('script, style, svg, noscript')) continue;
    const cs = getComputedStyle(el);
    const at = value => value ? rgbOf(value) : null;
    const hit = INKS.filter(t => at(cs.getPropertyValue(t).trim()) === cs.color);
    // The primary pair: its ink on its fill, flat or a gradient of the pair's
    // two fills (white alone is not the pair).
    const fills = bgsOf(el) || [];
    const pair = [at(cs.getPropertyValue('--btn-primary-bg').trim()), at(cs.getPropertyValue('--btn-primary-hover').trim())];
    if (at(cs.getPropertyValue('--btn-primary-ink').trim()) === cs.color && fills.length
        && fills.every(f => pair.includes(`rgb(${Math.round(f.r)}, ${Math.round(f.g)}, ${Math.round(f.b)})`)))
      hit.push('--btn-primary-ink');
    if (at(cs.getPropertyValue('--teal').trim()) === cs.color) teal.push(label(el));
    if (el.matches(NAMED)) hit.push('named');
    if (!hit.length) continue;
    const r = contrastOf(el);
    if (r === null) { skipped++; continue; }
    checked++;
    if (hit.includes('named')) named.push(`${label(el)} ${r}:1`);
    const size = parseFloat(cs.fontSize), large = size >= 24 || (size >= 18.66 && +cs.fontWeight >= 700);
    if (r < (large ? 3 : 4.5)) failed.push(`${label(el)} ${hit[0]} ${r}:1`);
  }
  const skip = document.querySelector('.skip-link');
  return {failed, teal, named, checked, skipped, skip: skip ? contrastOf(skip) : null};
}""".replace("NAMED", repr(NAMED))
)


class ContrastAndTypeFloorBrowserTests(StaticLiveServerTestCase):
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
            username="contrast-audit", email="contrast-audit@example.test", password="unused"
        )
        client = Client()
        client.force_login(user)
        self.session = client.cookies[settings.SESSION_COOKIE_NAME].value
        sc, board = fx.scenario("Contrast audit", programs=("AI",))
        key = fx.requirement("AI", "AI111", "Intro AI")
        fx.budget(sc, key, 1)
        fx.place(
            board,
            fx.section(sc, key, "S1"),
            (("SUN", "09:00", "10:15"), ("TUE", "09:00", "10:15")),
            room="R101",
        )
        # An English section: both pages draw its own grid under a brand heading.
        english = fx.section(sc, "ENG101", "M1")
        create_policy_section_bundle(
            board, english, day="SUN", start_time="09:00", end_time="12:00", room="R102"
        )
        self.cards = SectionPlacement.objects.filter(board=board).count()
        self.scenario = sc

    def _student_session(self) -> str:
        """A signed-in student, provisioned as the OTP sign-in does."""
        from django.utils import timezone

        from core.models import Student
        from core.services import student_otp
        from core.services.rbac import ensure_role_groups

        ensure_role_groups()
        Student.objects.get_or_create(
            student_id=STUDENT_ID, defaults={"name": "Contrast", "program": "AI", "section": "M"}
        )
        client = Client()
        client.force_login(student_otp.provision_student_user(STUDENT_ID))
        session = client.session
        session[student_otp.STUDENT_AUTHENTICATED_AT_SESSION_KEY] = int(timezone.now().timestamp())
        session.save()
        return session.session_key

    def _open(
        self,
        name: str,
        theme: str,
        *,
        width: int = 1440,
        height: int = 900,
        language: str = "en",
        os_scheme: str | None = None,
        forced: bool = False,
    ):
        context = self.browser.new_context(
            locale="ar" if language == "ar" else "en-US",
            extra_http_headers={"Accept-Language": language},
            viewport={"width": width, "height": height},
            color_scheme=os_scheme or theme,
            forced_colors="active" if forced else "none",
        )
        self.addCleanup(context.close)
        cookies = [
            {"name": settings.LANGUAGE_COOKIE_NAME, "value": language, "url": self.live_server_url}
        ]
        session = None if name in SIGNED_OUT else self.session
        if name in STUDENT:
            session = self._student_session()
        if session:
            cookies.append(
                {
                    "name": settings.SESSION_COOKIE_NAME,
                    "value": session,
                    "url": self.live_server_url,
                }
            )
        context.add_cookies(cookies)
        # The student portal keeps its theme under its own key.
        context.add_init_script(
            f"try {{ localStorage.setItem('theme', '{theme}');"
            f" localStorage.setItem('student-theme', '{theme}'); }} catch (e) {{}}"
        )
        context.add_init_script(WIDE_FONT)
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        path = {**PAGES, **SIGNED_OUT, **STUDENT}[name]
        page.goto(self.live_server_url + path.format(sc=self.scenario.id))
        page.add_style_tag(
            content="*, *::before, *::after { transition: none !important; animation: none !important; }"
        )
        self.assertEqual(page.evaluate("document.documentElement.dataset.theme"), theme)
        if name == "workspace":
            page.wait_for_function(
                f"document.querySelectorAll('.tw-card').length === {self.cards}"
                " && !!document.querySelector('.tw-english-heading')"
                " && !document.getElementById('twValidationBadge').hidden"
            )
        elif name == "split":
            page.wait_for_selector(".tw-english-heading")
        else:
            page.wait_for_load_state("load")
        self.addCleanup(lambda: self.assertEqual(errors, [], f"{name} {theme}: page errors"))
        return page

    # ── every page ──────────────────────────────────────────────────────────

    def _audit(self, name: str, page) -> dict:
        seen = page.evaluate(AUDIT)
        self.assertGreaterEqual(seen["skip"], 4.5, "the skip link")
        self.assertEqual(seen["failed"], [])
        self.assertGreater(seen["checked"], 0, "nothing in a C1 token was measured")
        for part in MEASURED.get(name, ()):
            self.assertTrue(any(part in line for line in seen["named"]), f"{part} not measured")
        self.assertEqual(seen["teal"], [])
        self._assert_primaries_keep_the_pair_in_every_state(name, page)
        return seen

    # Every control the page draws in the primary pair, with :hover and then
    # :active forced on all of them: a state rule may repaint its fill or its
    # label, never so that the label stops reading (the graph's Sync went 1:1
    # on hover; the sign-in button 3.04:1).
    PRIMARIES = (
        "() => {"
        + MEASURE
        + r"""
  const ink = rgbOf(getComputedStyle(document.body).getPropertyValue('--btn-primary-ink').trim());
  const pair = ['--btn-primary-bg', '--btn-primary-hover'].map(t => rgbOf(getComputedStyle(document.body).getPropertyValue(t).trim()));
  const key = f => `rgb(${Math.round(f.r)}, ${Math.round(f.g)}, ${Math.round(f.b)})`;
  let n = 0;
  for (const el of document.querySelectorAll('button, a[href], [role=tab], input[type=submit]')) {
    const text = [el, ...el.querySelectorAll('*')].find(x => ownText(x) && shown(x));
    const fills = text && bgsOf(text);
    if (text && fills && fills.length && getComputedStyle(text).color === ink
        && fills.every(f => pair.includes(key(f)))) el.setAttribute('data-c1-primary', String(n++));
  }
  return n;
}"""
    )
    PRIMARY_STATE = (
        "() => {"
        + MEASURE
        + r"""
  document.getAnimations().forEach(a => { try { a.finish(); } catch (e) { /* endless */ } });
  return [...document.querySelectorAll('[data-c1-primary]')].map(el => {
    const text = [el, ...el.querySelectorAll('*')].find(x => ownText(x) && shown(x));
    return [label(el), text ? contrastOf(text) : null];
  });
}"""
    )

    def _assert_primaries_keep_the_pair_in_every_state(self, name: str, page) -> None:
        if not page.evaluate(self.PRIMARIES):
            return
        cdp = page.context.new_cdp_session(page)
        cdp.send("DOM.enable")
        cdp.send("CSS.enable")
        root = cdp.send("DOM.getDocument", {"depth": -1})["root"]["nodeId"]
        nodes = cdp.send("DOM.querySelectorAll", {"nodeId": root, "selector": "[data-c1-primary]"})
        for state in ("hover", "active"):
            for node in nodes["nodeIds"]:
                cdp.send("CSS.forcePseudoState", {"nodeId": node, "forcedPseudoClasses": [state]})
            low = [
                f"{where} {ratio}:1"
                for where, ratio in page.evaluate(self.PRIMARY_STATE)
                if ratio is None or ratio < 4.5
            ]
            self.assertEqual(low, [], f"{name}: primary controls on :{state}")
        for node in nodes["nodeIds"]:
            cdp.send("CSS.forcePseudoState", {"nodeId": node, "forcedPseudoClasses": []})
        cdp.detach()

    def test_every_page_paints_its_tokens_at_4_5_and_no_text_in_the_teal_fill(self) -> None:
        for name in PAGES:
            for theme in THEMES:
                with self.subTest(page=name, theme=theme):
                    self._audit(name, self._open(name, theme))

    # ── states: hover and pressed ───────────────────────────────────────────
    # A state rule that repaints only the fill keeps the ink the element already
    # has: the sign-in button's hover and the confirm dialog's were white on
    # --teal-bright (3.06:1 in light). The state is forced through the
    # inspector protocol, so nothing is clicked and no form is sent.

    def _forced(self, page, selector: str, state: str | None) -> float:
        cdp = page.context.new_cdp_session(page)
        cdp.send("DOM.enable")
        cdp.send("CSS.enable")
        root = cdp.send("DOM.getDocument")["root"]["nodeId"]
        node = cdp.send("DOM.querySelector", {"nodeId": root, "selector": selector})["nodeId"]
        self.assertTrue(node, selector)
        cdp.send(
            "CSS.forcePseudoState",
            {"nodeId": node, "forcedPseudoClasses": [state] if state else []},
        )
        ratio = page.evaluate(
            "sel => {"
            + MEASURE
            + r"""
  // A transition the page keeps (an !important one) ends now, not mid-way.
  document.getAnimations().forEach(a => { try { a.finish(); } catch (e) { /* endless */ } });
  const el = document.querySelector(sel);
  const text = [el, ...el.querySelectorAll('*')].find(n => ownText(n) && shown(n));
  return text ? contrastOf(text) : null;
}""",
            selector,
        )
        cdp.send("CSS.forcePseudoState", {"nodeId": node, "forcedPseudoClasses": []})
        cdp.detach()
        return ratio

    def test_the_sign_in_pages_keep_the_primary_pair_at_rest_on_hover_and_pressed(self) -> None:
        for name in SIGNED_OUT:
            for theme in THEMES:
                page = self._open(name, theme)
                with self.subTest(page=name, theme=theme):
                    self._audit(name, page)
                for state in (None, "hover", "active"):
                    with self.subTest(page=name, theme=theme, state=state):
                        ratio = self._forced(page, "#loginBtn", state)
                        self.assertIsNotNone(ratio, "the sign-in button's text is not shown")
                        self.assertGreaterEqual(ratio, 4.5)

    def test_the_confirm_dialogs_primary_keeps_the_pair_on_hover(self) -> None:
        for theme in THEMES:
            page = self._open("dashboard", theme)
            page.evaluate("() => { dlg.confirm({title: 'Contrast'}); }")
            page.wait_for_selector(".dlg-footer .btn-confirm.primary")
            for state in (None, "hover"):
                with self.subTest(theme=theme, state=state):
                    ratio = self._forced(page, ".dlg-footer .btn-confirm.primary", state)
                    self.assertIsNotNone(ratio)
                    self.assertGreaterEqual(ratio, 4.5)

    # ── the student portal ──────────────────────────────────────────────────

    def test_the_student_portal_paints_its_tokens_at_4_5(self) -> None:
        for name in STUDENT:
            for theme in THEMES:
                with self.subTest(page=name, theme=theme):
                    page = self._open(name, theme)
                    self._audit(name, page)
                    if name != "student-advisor":
                        continue
                    # The adviser's avatar on the conversation (re-painted by
                    # `.sa-main .va-avatar`: white on --brand to --teal, 4.11:1).
                    ratio = page.evaluate(
                        "() => {"
                        + MEASURE
                        + r"""
  const avatar = document.createElement('div');
  avatar.className = 'va-avatar';
  avatar.textContent = 'AI';
  document.querySelector('.sa-main').append(avatar);
  return contrastOf(avatar);
}"""
                    )
                    self.assertGreaterEqual(ratio, 4.5, "the adviser's avatar")

    # ── the sidebar ─────────────────────────────────────────────────────────

    SIDEBAR = (
        "() => [...document.querySelectorAll('.sidebar *')]"
        ".filter(el => { const r = el.getBoundingClientRect(), s = getComputedStyle(el);"
        "  return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'"
        "    && (el.tagName === 'SELECT' || [...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim())); })"
        ".map(el => [(el.className || el.tagName).toString().split(' ')[0], parseFloat(getComputedStyle(el).fontSize)])"
    )

    def test_the_sidebar_text_keeps_the_11px_floor_at_every_width(self) -> None:
        for width, height, language in (
            (1440, 900, "en"),
            (1100, 800, "en"),
            (400, 800, "en"),
            (400, 800, "ar"),
        ):
            with self.subTest(width=width, language=language):
                page = self._open(
                    "dashboard", "light", width=width, height=height, language=language
                )
                if width <= 768:  # a phone: the sidebar is a drawer behind its button
                    page.click(".sidebar-toggle")
                    page.wait_for_selector(".sidebar.sidebar-open")
                sizes = page.evaluate(self.SIDEBAR)
                self.assertGreater(len(sizes), 10, "the sidebar's text is not shown")
                small = [(cls, round(size, 2)) for cls, size in sizes if size < 11]
                self.assertEqual(small, [])

    # ── the workspace ───────────────────────────────────────────────────────

    TOOLBAR = (
        "() => {"
        + MEASURE
        + r"""
  const pill = document.getElementById('twValidationBadge');
  const search = document.getElementById('twSearch');
  const heading = document.querySelector('.tw-english-heading');
  return {
    pill: contrastOf(pill), pillText: pill.textContent.trim(),
    pillShadow: getComputedStyle(pill).boxShadow, pillGloss: getComputedStyle(pill, '::before').content,
    inToolbar: !!pill.closest('.ux-toolbar'),
    placeholder: contrastOf(search, '::placeholder'), hasPlaceholder: !!search.placeholder,
    heading: contrastOf(heading),
  };
}"""
    )

    def test_the_workspace_draft_pill_placeholder_and_english_heading_pass(self) -> None:
        for theme, width in (("light", 1440), ("dark", 1440), ("light", 400)):
            with self.subTest(theme=theme, width=width):
                page = self._open("workspace", theme, width=width, height=900)
                seen = page.evaluate(self.TOOLBAR)
                self.assertEqual(seen["pillText"], "Draft")
                self.assertTrue(seen["inToolbar"])
                self.assertGreaterEqual(seen["pill"], 4.5, "the Draft pill (4.17:1 before)")
                self.assertEqual(seen["pillShadow"], "none", "a toolbar pill is flat")
                self.assertEqual(seen["pillGloss"], "none", "a toolbar pill has no gloss")
                self.assertTrue(seen["hasPlaceholder"])
                self.assertGreaterEqual(seen["placeholder"], 4.5, "the filter placeholder (2.02:1)")
                self.assertGreaterEqual(seen["heading"], 4.5, "the English heading (3.63:1)")

    # ── split view ──────────────────────────────────────────────────────────

    SPLIT = (
        "() => {"
        + MEASURE
        + r"""
  // Split paints its page with a gradient: measure against its own card.
  const card = parse(rgbOf(getComputedStyle(document.body).getPropertyValue('--card').trim()));
  const against = el => { const fg = parse(getComputedStyle(el).color); return +ratio(fg, card).toFixed(2); };
  const heading = document.querySelector('.tw-english-heading');
  const tab = document.querySelector('.tws-bpanel-tab.active');
  return { heading: against(heading), tab: tab ? against(tab) : null,
           scheme: getComputedStyle(document.body).colorScheme };
}"""
    )

    def test_split_views_brand_text_is_not_lower_than_before(self) -> None:
        # Before C1 the shared English heading was --teal: 4.29:1 on split's
        # card in light, 5.81:1 in dark. Split's own teal text is left alone.
        for theme in THEMES:
            with self.subTest(theme=theme):
                seen = self._open("split", theme).evaluate(self.SPLIT)
                self.assertGreaterEqual(seen["heading"], 4.5, "the English heading on split")
                self.assertGreaterEqual(seen["heading"], 5.81 if theme == "dark" else 4.29)
                if seen["tab"] is not None:
                    self.assertGreaterEqual(seen["tab"], 4.29 if theme == "light" else 5.81)
                self.assertEqual(seen["scheme"], theme, "split follows the theme (style check S3)")

    # ── the browser's own colour scheme ─────────────────────────────────────

    def test_a_light_theme_tells_the_browser_it_is_light_even_on_a_dark_os(self) -> None:
        for theme in THEMES:
            with self.subTest(theme=theme):
                page = self._open("dashboard", theme, os_scheme="dark")
                self.assertEqual(
                    page.evaluate("getComputedStyle(document.documentElement).colorScheme"), theme
                )

    # ── forced colours ──────────────────────────────────────────────────────

    BOXES = r"""() => {
  const box = el => { const s = getComputedStyle(el), r = el.getBoundingClientRect();
    return {width: parseFloat(s.borderTopWidth), style: s.borderTopStyle, border: s.borderTopColor,
            back: s.backgroundColor, ink: s.color, top: r.top, focused: document.activeElement === el}; };
  return {skip: box(document.querySelector('.skip-link')), pill: box(document.getElementById('twValidationBadge'))};
}"""

    def test_forced_colours_keep_the_skip_link_and_the_pills_a_visible_box(self) -> None:
        page = self._open("workspace", "light", forced=True)
        page.keyboard.press("Tab")
        seen = page.evaluate(self.BOXES)
        self.assertTrue(seen["skip"]["focused"])
        self.assertGreaterEqual(seen["skip"]["top"], 0, "the focused skip link is on screen")
        for name in ("skip", "pill"):
            box = seen[name]
            with self.subTest(element=name):
                self.assertGreaterEqual(box["width"], 1)
                self.assertEqual(box["style"], "solid")
                self.assertNotEqual(box["border"], box["back"])
                self.assertEqual(box["border"], box["ink"])
