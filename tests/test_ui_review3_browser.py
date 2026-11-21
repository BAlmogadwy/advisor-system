"""What the third UI review (2026-11, PR #253) changed in shared behaviour.

A wide table on a touch screen fades its far edge while there is more to
scroll, but never hides its own keyboard focus ring. A phone's user table keeps
the columns an admin acts on. A phone's toasts span the page between its
gutters. Each is a contract of the global stylesheet, so each is read from
computed styles on a real page with the markup injected, in Chrome.
"""

from __future__ import annotations

import os

os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

import pytest
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import Client
from django.urls import reverse

from core.models import Student
from core.services.rbac import ensure_role_groups

playwright_api = pytest.importorskip("playwright.sync_api")
sync_playwright = playwright_api.sync_playwright

STUDENT = 7003301

WIDE_TABLE = """() => {
  const wrap = document.createElement('div');
  wrap.className = 'table-wrap';
  wrap.tabIndex = 0;
  wrap.id = 'reviewWide';
  wrap.style.cssText = 'max-width: 280px; overflow-x: auto;';
  wrap.innerHTML = '<table style="width: 900px"><tr><td>wide</td></tr></table>';
  document.querySelector('main, body').prepend(wrap);
  return wrap.scrollWidth > wrap.clientWidth;
}"""

FADE = """async () => {
  // a scroll timeline drives the fade: give it two frames to start
  await new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)));
  const style = getComputedStyle(document.getElementById('reviewWide'));
  return { mask: style.maskImage, far: style.getPropertyValue('--tbl-fade-e').trim(),
           outline: style.outlineStyle };
}"""


class UIReview3BrowserTests(StaticLiveServerTestCase):
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

    def _page(
        self, *, width: int = 1440, height: int = 900, touch: bool = False, language: str = "en"
    ):
        from core.services import student_otp

        ensure_role_groups()
        Student.objects.get_or_create(
            student_id=STUDENT, defaults={"name": "S", "program": "AI", "section": "M"}
        )
        client = Client()
        client.force_login(student_otp.provision_student_user(STUDENT))
        context = self.browser.new_context(
            viewport={"width": width, "height": height},
            is_mobile=touch,
            has_touch=touch,
        )
        context.add_cookies(
            [
                {
                    "name": "sessionid",
                    "value": client.cookies["sessionid"].value,
                    "url": self.live_server_url,
                },
                {"name": "advisor_language", "value": language, "url": self.live_server_url},
            ]
        )
        self.addCleanup(context.close)
        page = context.new_page()
        page.goto(f"{self.live_server_url}{reverse('student_advisor')}")
        page.wait_for_load_state("networkidle")
        self.assertEqual(page.evaluate("document.documentElement.lang"), language)
        return page

    def test_a_wide_table_fades_its_far_edge_on_a_touch_screen_and_keeps_its_focus_ring(
        self,
    ) -> None:
        for language, direction in (("en", "to right"), ("ar", "to left")):
            with self.subTest(language=language):
                page = self._page(width=390, height=844, touch=True, language=language)
                self.assertTrue(page.evaluate(WIDE_TABLE), "the probe table does not overflow")
                seen = page.evaluate(FADE)
                # the far edge fades while there is more that way, in the
                # reading direction
                self.assertIn(direction, seen["mask"], seen)
                self.assertEqual(seen["far"], "28px", seen)
                # keyboard focus: no mask, so the ring drawn outside the box shows
                reached = False
                for _ in range(40):
                    page.keyboard.press("Tab")
                    if page.evaluate("() => document.activeElement.id === 'reviewWide'"):
                        reached = True
                        break
                self.assertTrue(reached, "the probe table was never reached by keyboard")
                focused = page.evaluate(FADE)
                self.assertEqual(focused["mask"], "none", focused)
                self.assertNotEqual(focused["outline"], "none", focused)
        # a mouse desktop draws the scrollbar itself, and a mask would fade it
        page = self._page()
        page.evaluate(WIDE_TABLE)
        self.assertEqual(page.evaluate(FADE)["mask"], "none")

    def test_a_phone_user_table_keeps_the_columns_an_admin_acts_on(self) -> None:
        names = [
            "check",
            "user",
            "role",
            "advisor",
            "depts",
            "status",
            "login",
            "created",
            "actions",
        ]
        build = """names => {
          const table = document.createElement('table');
          table.id = 'umTable';
          table.innerHTML = '<thead><tr>' + names.map(n => '<th>' + n + '</th>').join('') + '</tr></thead>'
            + '<tbody><tr>' + names.map(n => '<td>' + n + '</td>').join('') + '</tr></tbody>';
          document.querySelector('main, body').prepend(table);
          const shown = cell => getComputedStyle(cell).display !== 'none';
          return {
            head: [...table.querySelectorAll('th')].filter(shown).map(cell => cell.textContent),
            body: [...table.querySelectorAll('td')].filter(shown).map(cell => cell.textContent),
          };
        }"""
        phone = self._page(width=390, height=844, touch=True).evaluate(build, names)
        kept = ["check", "user", "role", "status", "actions"]
        self.assertEqual(phone, {"head": kept, "body": kept})
        desktop = self._page().evaluate(build, names)
        self.assertEqual(desktop, {"head": names, "body": names})

    def test_phone_toasts_span_the_page_between_its_gutters(self) -> None:
        show = """async () => {
          notify.success('saved');
          const toast = document.querySelector('#toast-container .ux-toast');
          // measured at rest: the entry animation scales the toast while it lands
          await Promise.all(toast.getAnimations().map(animation => animation.finished));
          const r = toast.getBoundingClientRect();
          const style = getComputedStyle(toast);
          const resolve = token => { const probe = document.createElement('div'); probe.style.background = `var(${token})`;
            document.body.appendChild(probe); const value = getComputedStyle(probe).backgroundColor; probe.remove(); return value; };
          return { start: Math.round(r.left), end: Math.round(innerWidth - r.right), width: Math.round(r.width),
                   background: style.backgroundColor, page: resolve('--bg'), card: resolve('--card'),
                   edge: style.borderTopWidth };
        }"""
        phone = self._page(width=390, height=844, touch=True).evaluate(show)
        self.assertEqual(phone["start"], phone["end"], phone)
        self.assertLess(phone["start"], 20, phone)
        desktop = self._page().evaluate(show)
        self.assertLessEqual(desktop["width"], 340, desktop)
        # a card with an edge, not the page's own colour with none
        self.assertNotEqual(desktop["page"], desktop["card"], desktop)
        self.assertEqual(desktop["background"], desktop["card"], desktop)
        self.assertEqual(desktop["edge"], "1px", desktop)
