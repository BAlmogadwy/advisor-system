"""Locked days and periods on the exam page in Chromium, against the real endpoints.

The jsdom suite (tests/frontend/exam-locks.test.cjs) covers every state with fake
answers. This lays out what locks add - a lock toggle on every day and period,
the locked tint and badge, the lock bar and the builder's list - at 1440px and at
phone width (390px), in English and in Arabic, light and dark, in the design's
font and in a wide one (Verdana), and in forced colours (High Contrast), so a
layout that only fits one machine's fonts fails anywhere. It then works the
toggles with the keyboard alone and saves the locks through the real Check and
Save. Nothing leaves the machine (tests/browser_isolation.py).
"""

from __future__ import annotations

import json
import os

# Playwright's synchronous API runs through a greenlet; fixture creation is
# synchronous ORM work while Django serves the page on its own thread.
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.core.cache import cache
from django.test import Client, override_settings
from django.urls import reverse

from core import authz, models
from core.services.rbac import ROLE_EXAM_COMMITTEE, ensure_role_groups
from tests import exam_linked_parity_corpus as corpus
from tests.test_exam_rosters_browser import WIDE_FONT

playwright_api = pytest.importorskip("playwright.sync_api")
expect = playwright_api.expect
sync_playwright = playwright_api.sync_playwright

DAYS = corpus.POPULATION_DAYS
PERIODS = corpus.POPULATION_PERIODS
SCOPE = {"programs": ["AI", "CS"], "sections": ["F", "M"]}
LOCKS = [{"day": "Sun"}, {"day": "Tue", "period": PERIODS[1]}]

# Every lock control inside its own table cell, never under the exam cards, big
# enough to hit (24px, WCAG 2.5.8), every badge inside its cell, and how far the
# page scrolls sideways.
LAYOUT = """() => {
  const grid = document.getElementById('schedGrid');
  const box = node => node.getBoundingClientRect();
  const inside = (inner, outer) => { const a = box(inner), b = box(outer);
    return a.left >= b.left - 1 && a.right <= b.right + 1 && a.top >= b.top - 1 && a.bottom <= b.bottom + 1; };
  const toggles = [...grid.querySelectorAll('.et-lock-toggle')];
  return {
    toggles: toggles.length,
    outside: toggles.filter(toggle => !inside(toggle, toggle.closest('td, th'))).map(toggle => toggle.getAttribute('aria-label')),
    small: toggles.filter(toggle => box(toggle).width < 24 || box(toggle).height < 24).map(toggle => toggle.getAttribute('aria-label')),
    underCards: [...grid.querySelectorAll('td > .et-cell-lockbar')].filter(bar => {
      const cards = bar.parentElement.querySelector('.et-slot-courses');
      return cards && box(cards).top < box(bar).bottom - 0.5;
    }).length,
    badges: [...grid.querySelectorAll('.et-lock-badge')].map(badge => ({ inCell: inside(badge, badge.closest('td')), text: badge.textContent })),
    locked: grid.querySelectorAll('td[data-locked="true"]').length,
    page: document.documentElement.scrollWidth - window.innerWidth,
  };
}"""
# The badge's words against the surface behind them, as the WCAG contrast ratio.
CONTRAST = """() => {
  const rgb = value => (value.match(/[\\d.]+/g) || []).map(Number);
  const channel = c => { c /= 255; return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4; };
  const luminance = ([r, g, b]) => 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
  const backdrop = node => {
    for (let at = node; at; at = at.parentElement) {
      const c = rgb(getComputedStyle(at).backgroundColor);
      if (c.length === 3 || c[3] === 1) return c;
    }
    return [255, 255, 255];
  };
  return [...document.querySelectorAll('#schedGrid .et-lock-badge, #lockBar, #examLockLine:not([hidden])')].map(node => {
    const a = luminance(rgb(getComputedStyle(node).color)), b = luminance(backdrop(node));
    return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
  });
}"""
# Everything in a section that spills out of it, and every left-to-right label broken over two lines.
FITS = """(id) => {
  const element = document.getElementById(id);
  const box = element.getBoundingClientRect();
  const shown = node => { const r = node.getBoundingClientRect(); return r.width > 1 && r.height > 1; };
  return {
    outside: [...element.querySelectorAll('*')].filter(shown).filter(node => {
      const r = node.getBoundingClientRect(); return r.left < box.left - 1 || r.right > box.right + 1; })
      .map(node => node.id || node.className || node.tagName),
    broken: [...element.querySelectorAll('bdi[dir=ltr]')].filter(shown).filter(node => {
      const range = document.createRange(); range.selectNodeContents(node); return range.getClientRects().length > 1; })
      .map(node => node.textContent),
  };
}"""
# The refusal alert on screen, and its Close button not under anything (the
# page's back-to-top button sits in the same corner on a phone).
REFUSAL_SEEN = """() => {
  const alert = document.getElementById('examLockRefusal');
  const r = alert.getBoundingClientRect();
  const close = document.getElementById('examLockRefusalClose').getBoundingClientRect();
  const hit = document.elementFromPoint(close.left + close.width / 2, close.top + close.height / 2);
  return {
    shown: !alert.hidden && r.height > 0,
    inView: r.bottom > 0 && r.top < innerHeight && r.left >= 0 && r.right <= innerWidth,
    closeFree: Boolean(hit && hit.closest('#examLockRefusalClose')),
  };
}"""
# Where a card is on the board: [day, period].
PLACE = """code => {
  const card = [...document.querySelectorAll('#schedGrid .et-course')].find(item => item.dataset.course === code);
  const cell = card.closest('td');
  return [cell.dataset.day, cell.dataset.period];
}"""
# Resolves once the page has stopped moving for ten frames: the editor scrolls
# itself into view after a load, and a drag measured before that lands elsewhere.
SETTLED = """async () => {
  const at = () => JSON.stringify([scrollX, scrollY, document.documentElement.scrollHeight]);
  let last = at();
  for (let still = 0; still < 10;) {
    await new Promise(resolve => requestAnimationFrame(resolve));
    const now = at();
    still = now === last ? still + 1 : 0;
    last = now;
  }
  return true;
}"""
# Words the page wraps in left-to-right isolates inside Arabic, without them.
ISOLATES = {code: None for code in range(0x2066, 0x206A)}


def _plain(text: str | None) -> str:
    return (text or "").translate(ISOLATES).strip()


@override_settings(EXAM_JOBS_ENABLED=False)
class ExamLocksBrowserTests(StaticLiveServerTestCase):
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
        cache.clear()
        authz._rate_buckets.clear()
        corpus.create_population(models)
        ensure_role_groups()
        admin = get_user_model().objects.create_superuser(username="locks-browser-admin")
        client = Client()
        client.force_login(admin)
        self.client_ = client
        built = self._post(
            "exam_timetable_build",
            {
                "label": "Locks browser",
                "days": DAYS,
                "periods": PERIODS,
                "max_per_day": 2,
                **SCOPE,
                "assign_rooms": True,
                "pinned": [],
                "seed": 7,
            },
        )
        self.open_run = built["run_id"]
        # The same run with a day and a period locked, saved as the page saves it.
        loaded = {
            "label": "Locks browser",
            "days": DAYS,
            "periods": PERIODS,
            "max_per_day": 2,
            **SCOPE,
            "previous_run_id": built["run_id"],
            "base_schedule": built["schedule"],
            "pinned": [],
            "exam_locks": LOCKS,
        }
        checked = self._post("exam_timetable_draft_impact", loaded)
        saved = self._post(
            "exam_timetable_build",
            {
                **loaded,
                "mode": "save_loaded_changes",
                "expected_input_fingerprint": checked["input_fingerprint"],
            },
        )
        assert saved["exam_locks"] == LOCKS, saved.get("exam_locks")
        self.locked_run = saved["run_id"]

    def _post(self, url: str, payload: dict) -> dict:
        response = self.client_.post(reverse(url), payload, content_type="application/json")
        assert response.status_code == 200, response.content
        return response.json()

    def _page(
        self,
        language: str,
        width: int,
        run: int,
        *,
        wide_font=False,
        theme="light",
        forced=False,
        height=900,
    ):
        user = get_user_model().objects.create_user(
            username=f"locks-{language}-{get_user_model().objects.count()}"
        )
        user.groups.add(Group.objects.get(name=ROLE_EXAM_COMMITTEE))
        client = Client()
        client.force_login(user)
        context = self.browser.new_context(
            locale="ar" if language == "ar" else "en-US",
            extra_http_headers={"Accept-Language": language},
            viewport={"width": width, "height": height},
            forced_colors="active" if forced else "none",
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
                    "value": language,
                    "url": self.live_server_url,
                },
            ]
        )
        self.addCleanup(context.close)
        if wide_font:
            context.add_init_script(WIDE_FONT)
        # The theme the page's own switch saves, read before anything paints.
        context.add_init_script(
            f"try {{ localStorage.setItem('theme', {theme!r}); }} catch (error) {{}}"
        )
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(errors, []))
        page.goto(f"{self.live_server_url}/exam-timetable/?run={run}")
        expect(page.locator("#schedGrid .et-grid")).to_be_visible()
        self.assertEqual(page.evaluate("() => document.documentElement.dataset.theme"), theme)
        return page

    def _assert_layout(self, language: str, width: int, **options) -> None:
        where = f"{language} {width}px {options}"
        page = self._page(language, width, self.locked_run, **options)
        page.evaluate("async () => { await document.fonts.ready; return true; }")
        facts = page.evaluate(LAYOUT)
        # A toggle for every day and every period cell: 4 days x 2 periods.
        self.assertEqual(facts["toggles"], len(DAYS) * (len(PERIODS) + 1), where)
        self.assertEqual(facts["outside"], [], where)
        self.assertEqual(facts["small"], [], where)
        self.assertEqual(facts["underCards"], 0, where)
        self.assertEqual(facts["locked"], len(PERIODS) + 1, where)
        self.assertTrue(
            all(badge["inCell"] for badge in facts["badges"]), f"{where}: {facts['badges']}"
        )
        self.assertEqual(
            {badge["text"] for badge in facts["badges"]},
            {"مقفل" if language == "ar" else "Locked"},
            where,
        )
        self.assertLessEqual(facts["page"], 0, f"{where}: the page scrolls sideways")
        if not options.get("forced"):
            for ratio in page.evaluate(CONTRAST):
                self.assertGreaterEqual(ratio, 4.5, where)
        # The lock bar and the builder's list fit, and never break a day or period.
        page.locator("#lockBar").scroll_into_view_if_needed()
        for section in ("lockBar", "examLockEditor"):
            if section == "examLockEditor":
                page.locator("#examSetupSummary").click()
                page.locator("#examLockEditor").scroll_into_view_if_needed()
                expect(page.locator("#examLockRows tr")).to_have_count(2)
            fits = page.evaluate(FITS, section)
            self.assertEqual(fits["outside"], [], f"{where}: {section} spills")
            self.assertEqual(fits["broken"], [], f"{where}: {section} breaks a label")
        if options.get("forced"):
            # High Contrast drops tints: a locked cell is framed instead, and a
            # pressed toggle takes the system highlight.
            style = page.evaluate("""() => {
              const cell = document.querySelector('#schedGrid td[data-locked="true"]');
              const pressed = document.querySelector('#schedGrid .et-lock-toggle[aria-pressed="true"]');
              const c = getComputedStyle(cell), p = getComputedStyle(pressed);
              return { outline: c.outlineStyle, width: c.outlineWidth, adjust: p.forcedColorAdjust, background: p.backgroundColor };
            }""")
            self.assertEqual(style["outline"], "dashed", where)
            self.assertEqual(style["width"], "2px", where)
            self.assertEqual(style["adjust"], "none", where)
            self.assertNotEqual(style["background"], "rgba(0, 0, 0, 0)", where)

    def test_locks_fit_a_laptop_in_english_and_arabic_light_and_dark(self) -> None:
        for language in ("en", "ar"):
            for theme in ("light", "dark"):
                self._assert_layout(language, 1440, theme=theme)

    def test_locks_fit_a_phone_in_english_and_arabic_light_and_dark(self) -> None:
        for language in ("en", "ar"):
            for theme in ("light", "dark"):
                self._assert_layout(language, 390, theme=theme)

    def test_locks_fit_in_a_wide_font_and_in_forced_colours(self) -> None:
        for language in ("en", "ar"):
            for width in (1440, 390):
                self._assert_layout(language, width, wide_font=True)
        self._assert_layout("en", 1440, forced=True)
        self._assert_layout("ar", 390, forced=True)

    def test_locks_work_from_the_keyboard_and_save_through_check_and_save(self) -> None:
        page = self._page("en", 1440, self.open_run)
        # The grid region, then Tab: the first control in the timetable is Sun's lock.
        page.locator("#schedGrid").focus()
        page.keyboard.press("Tab")
        focused = page.evaluate(
            "() => [document.activeElement.getAttribute('aria-label'), document.activeElement.getAttribute('aria-pressed'), getComputedStyle(document.activeElement).outlineStyle]"
        )
        self.assertEqual(focused, ["Lock day Sun", "false", "solid"])
        page.keyboard.press("Space")
        # Drawn again, the toggle keeps the keyboard, pressed now.
        focused = page.evaluate(
            "() => [document.activeElement.getAttribute('aria-label'), document.activeElement.getAttribute('aria-pressed')]"
        )
        self.assertEqual(focused, ["Lock day Sun", "true"])
        expect(page.locator("#examLockLine")).to_have_text(
            "1 lock change not saved — Check changes, then Save Changes."
        )
        page.keyboard.press("Tab")
        self.assertEqual(
            page.evaluate("() => document.activeElement.getAttribute('aria-label')"),
            f"Lock period Sun {PERIODS[0]}",
        )
        # A period of a locked day: Enter unlocks just that period.
        page.keyboard.press("Enter")
        self.assertEqual(
            page.evaluate(
                "() => [document.activeElement.getAttribute('aria-label'), document.activeElement.getAttribute('aria-pressed')]"
            ),
            [f"Lock period Sun {PERIODS[0]}", "false"],
        )
        expect(page.locator('[data-lock-day="Sun"]:not([data-lock-period])')).to_have_attribute(
            "aria-pressed", "false"
        )
        expect(
            page.locator(f'[data-lock-day="Sun"][data-lock-period="{PERIODS[1]}"]')
        ).to_have_attribute("aria-pressed", "true")
        # Check, then Save, by keyboard; the saved run holds the lock.
        page.locator("#checkDraftBtn").focus()
        page.keyboard.press("Enter")
        expect(page.locator("#examCheckStatus")).to_contain_text("Checked")
        page.locator("#saveLoadedBtn").focus()
        page.keyboard.press("Enter")
        expect(page.locator("#examLockLine")).to_be_hidden()
        expect(page.locator("#saveLoadedBtn")).to_be_disabled()
        run = models.ExamTimetableRun.objects.order_by("-id").first()
        self.assertEqual(
            json.loads(run.result_json)["exam_locks"], [{"day": "Sun", "period": PERIODS[1]}]
        )
        page.goto(f"{self.live_server_url}/exam-timetable/?run={run.pk}")
        expect(
            page.locator(f'[data-lock-day="Sun"][data-lock-period="{PERIODS[1]}"]')
        ).to_have_attribute("aria-pressed", "true")
        expect(
            page.locator(f'[data-lock-day="Sun"][data-lock-period="{PERIODS[0]}"]')
        ).to_have_attribute("aria-pressed", "false")

    def test_a_drag_onto_a_locked_cell_is_refused_there_in_words(self) -> None:
        # A browser fires no drop on a cell that refused the drag: the page says
        # why when the drag ends over it, in the page's language.
        for language in ("en", "ar"):
            page = self._page(language, 1440, self.locked_run)
            source = page.locator(
                '#schedGrid td[data-day]:not([data-locked="true"]) .et-course[draggable="true"]'
            ).first
            code = source.get_attribute("data-course")
            home = page.evaluate(PLACE, code)
            source.scroll_into_view_if_needed()
            page.evaluate(SETTLED)
            source.drag_to(
                page.locator(f'#schedGrid td[data-day="Sun"][data-period="{PERIODS[1]}"]')
            )
            self.assertEqual(page.evaluate(PLACE, code), home, language)
            expect(page.locator("#examLockRefusal")).to_be_visible()
            self.assertEqual(
                _plain(page.locator("#examLockRefusalText").text_content()),
                f"Sun {PERIODS[1]} مقفلة — ألغِ قفلها أولاً."
                if language == "ar"
                else f"Sun {PERIODS[1]} is locked — unlock it first.",
            )
            seen = page.evaluate(REFUSAL_SEEN)
            self.assertEqual(seen, {"shown": True, "inView": True, "closeFree": True}, language)

    def test_a_refusal_is_said_in_view_wherever_on_the_board_it_happened(self) -> None:
        # A short window, so the board is taller than it: the toolbar at the top
        # scrolls away while the registrar works on the last day.
        for language, width, wide_font in (("en", 1440, False), ("ar", 390, True)):
            where = f"{language} {width}px"
            page = self._page(language, width, self.locked_run, height=560, wide_font=wide_font)
            last = DAYS[-1]
            mover = page.locator(
                f'#schedGrid td[data-day]:not([data-day="{last}"]):not([data-locked="true"]) '
                '.et-course[draggable="true"]'
            ).first
            code = mover.get_attribute("data-course")
            # Moved into the last day, a cell no longer matches the saved run:
            # its lock is refused, and the reason is on screen.
            mover.locator("[data-exam-move]").click()
            page.locator("#examMoveDay").select_option(last)
            page.locator("#examMovePeriod").select_option(PERIODS[1])
            page.locator("#confirmExamMove").click()
            self.assertEqual(page.evaluate(PLACE, code), [last, PERIODS[1]], where)
            lock = page.locator(
                f'#schedGrid [data-lock-day="{last}"][data-lock-period="{PERIODS[1]}"]'
            )
            lock.scroll_into_view_if_needed()
            lock.click()
            expect(lock).to_have_attribute("aria-pressed", "false")
            expect(page.locator("#examLockRefusal")).to_be_visible()
            self.assertIn(last, _plain(page.locator("#examLockRefusalText").text_content()), where)
            self.assertEqual(
                page.evaluate(REFUSAL_SEEN),
                {"shown": True, "inView": True, "closeFree": True},
                where,
            )
            # Closed from the keyboard: gone, and focus is back on the refused lock.
            page.locator("#examLockRefusalClose").focus()
            page.keyboard.press("Enter")
            expect(page.locator("#examLockRefusal")).to_be_hidden()
            self.assertEqual(
                page.evaluate("() => document.activeElement.dataset.lockPeriod"), PERIODS[1], where
            )
            # Undone, the day matches the saved run and locks; Move on one of its
            # cards is refused, said on screen too.
            page.locator("#undoExamBtn").click()
            day_lock = page.locator(f'#schedGrid [data-lock-day="{last}"]:not([data-lock-period])')
            day_lock.scroll_into_view_if_needed()
            day_lock.click()
            expect(day_lock).to_have_attribute("aria-pressed", "true")
            move = page.locator(
                f'#schedGrid td[data-day="{last}"] .et-course [data-exam-move]'
            ).last
            move.focus()
            page.keyboard.press("Enter")
            expect(page.locator("#examMoveDialog")).not_to_have_attribute("open", "")
            expect(page.locator("#examLockRefusal")).to_be_visible()
            self.assertEqual(
                page.evaluate(REFUSAL_SEEN),
                {"shown": True, "inView": True, "closeFree": True},
                where,
            )
            self.assertEqual(
                page.evaluate(
                    "() => document.getElementById('examEditToolbar').getBoundingClientRect().bottom > 0"
                ),
                False,
                f"{where}: the toolbar scrolled away, so the check above means something",
            )
            # And at the top of the board, far above its foot: said in view there too.
            page.locator("#examLockRefusalClose").click()
            first = page.locator(
                f'#schedGrid td[data-day="{DAYS[0]}"] .et-course [data-exam-move]'
            ).first
            first.focus()
            page.keyboard.press("Enter")
            expect(page.locator("#examLockRefusal")).to_be_visible()
            self.assertEqual(
                page.evaluate(REFUSAL_SEEN),
                {"shown": True, "inView": True, "closeFree": True},
                where,
            )
            self.assertGreater(
                page.evaluate(
                    "() => document.getElementById('examScheduleWorkspace').getBoundingClientRect().bottom - innerHeight"
                ),
                100,
                f"{where}: the board's foot is below the window, so the check above means something",
            )
