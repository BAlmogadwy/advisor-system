"""The adviser portfolio in a real browser.

One defect, and it is the kind only a browser finds. The page decided whether to
load an adviser's own students with::

    const USER_ROLE = 'userRole';
    if (USER_ROLE === 'ADVISOR' && USER_ADVISOR_ID) { loadStudents(...) }

The identifiers as string literals, not the values the template writes two lines
above. `'userRole' === 'ADVISOR'` is false for everyone, so the branch was dead.

And the fall-through was not a working alternative: `advisor_portfolio.html` puts
`d-none` on the adviser bar for exactly `role == 'ADVISOR'`, so an adviser landed
on an empty table inviting them to "choose an advisor above" — from a control
CSS had hidden. No API test can see any of it; the endpoint was always correct.
"""

from __future__ import annotations

import os

# See tests/test_advisor_browser.py: Playwright's sync API runs a greenlet loop,
# which Django mistakes for an async context and blocks every ORM call.
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

import pytest
from django.contrib.auth.models import User
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import Client
from django.urls import reverse

from core.models import Student
from core.services.rbac import ensure_role_groups, set_user_scope

playwright = pytest.importorskip("playwright.sync_api")
sync_playwright = playwright.sync_playwright

ADVISER_ID = "A77"
OTHER_ADVISER_ID = "A88"


class PortfolioBrowserTests(StaticLiveServerTestCase):
    """One browser for the class, one fresh page per test."""

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

    # ── fixtures ────────────────────────────────────────────────
    def _students(self) -> None:
        """Three mine, one someone else's — so 'loaded something' is not enough."""
        ensure_role_groups()
        for sid, advisor in (
            (990001, ADVISER_ID),
            (990002, ADVISER_ID),
            (990003, ADVISER_ID),
            (990004, OTHER_ADVISER_ID),
        ):
            Student.objects.update_or_create(
                student_id=sid,
                defaults={
                    "name": f"STUDENT {sid}",
                    "program": "AI",
                    "section": "M",
                    "advisor_id": advisor,
                    "status": "ACTIVE",
                },
            )

    def _staff_cookie(self, role_advisor_id: str) -> dict:
        user = User.objects.create_user(
            username=f"adv{role_advisor_id}", password="x", is_staff=True
        )
        set_user_scope(user.id, advisor_id=role_advisor_id)
        client = Client()
        client.force_login(user)
        return {
            "name": "sessionid",
            "value": client.cookies["sessionid"].value,
            "url": self.live_server_url,
        }

    def _page(self, role_advisor_id: str = ADVISER_ID):
        context = self.browser.new_context()
        context.add_cookies([self._staff_cookie(role_advisor_id)])
        self.addCleanup(context.close)
        return context.new_page()

    def _open(self, page) -> None:
        page.goto(f"{self.live_server_url}{reverse('advisor_portfolio_page')}")
        page.wait_for_load_state("networkidle")

    def _rows(self, page) -> list[str]:
        return page.eval_on_selector_all(
            "#apTable tbody tr[data-sid]",
            "rows => rows.map(r => r.dataset.sid)",
        )

    # ── the defect ──────────────────────────────────────────────
    def test_an_adviser_lands_on_their_own_students_without_touching_the_picker(self):
        """The whole point of the role branch, asserted through the rendered table.

        Not through the endpoint — `/report/students-by-advisor/` was always
        correct. What was broken is that nothing called it on load.
        """
        self._students()
        page = self._page()
        self._open(page)
        page.wait_for_selector("#apTable tbody tr[data-sid]", timeout=15_000)

        loaded = set(self._rows(page))
        assert loaded == {"990001", "990002", "990003"}, loaded
        assert "990004" not in loaded, "another adviser's student is on the screen"

    def test_the_role_constants_carry_values_not_their_own_names(self):
        """Directly, because the branch above can also be reached by accident.

        If `USER_ROLE` were ever a literal again the table test would still fail,
        but it would fail as 'no rows' — a symptom with several causes. This names
        the cause.
        """
        self._students()
        page = self._page()
        self._open(page)
        assert page.evaluate("() => USER_ROLE") == "ADVISOR"
        assert page.evaluate("() => USER_ADVISOR_ID") == ADVISER_ID

    def test_a_super_admin_gets_a_populated_picker_and_no_auto_load(self):
        """The other branch, so the fix cannot become 'always load own students'.

        The first version of this asserted only that no rows appeared, and a
        reviewer killed it: stubbing `loadAdvisors` to a no-op left both
        assertions holding. Absence of rows is the weaker half — the branch's
        actual job is to OFFER the picker, so the option has to be there.

        That needs a real `AcademicAdvisor` row. `_students()` writes
        `Student.advisor_id` strings only, and `/report/advisors/` lists
        `AcademicAdvisor`, so without one the select is empty by construction and
        the assertion would pass for the wrong reason again.
        """
        from django.contrib.auth.models import Group

        from core.models import AcademicAdvisor
        from core.services.rbac import ROLE_SUPER_ADMIN

        self._students()
        AcademicAdvisor.objects.update_or_create(
            advisor_id=ADVISER_ID,
            defaults={
                "full_name": "ADVISER SEVENTY SEVEN",
                "email": f"{ADVISER_ID}@example.edu",
                "department": "AI",
            },
        )
        ensure_role_groups()
        user = User.objects.create_user(username="boss", password="x", is_staff=True)
        user.groups.add(Group.objects.get(name=ROLE_SUPER_ADMIN))
        set_user_scope(user.id, advisor_id="")
        client = Client()
        client.force_login(user)
        context = self.browser.new_context()
        context.add_cookies(
            [
                {
                    "name": "sessionid",
                    "value": client.cookies["sessionid"].value,
                    "url": self.live_server_url,
                }
            ]
        )
        self.addCleanup(context.close)
        page = context.new_page()
        self._open(page)

        assert page.evaluate("() => USER_ROLE") == ROLE_SUPER_ADMIN
        assert self._rows(page) == [], "a super admin auto-loaded somebody's portfolio"

        page.wait_for_function(
            "() => document.querySelectorAll('#apAdvisorSelect option').length > 1",
            timeout=15_000,
        )
        options = page.eval_on_selector_all("#apAdvisorSelect option", "os => os.map(o => o.value)")
        assert ADVISER_ID in options, options
        assert "d-none" not in (page.get_attribute("#apAdvisorBar", "class") or ""), (
            "the picker a super admin depends on is hidden"
        )

    def test_a_missing_template_block_degrades_to_english_defaults_not_a_dead_page(self):
        """The `typeof` guard, tested where it can be: at the wire.

        My first attempt evaluated `typeof userRole` inside `new Function`, which
        runs in global scope where the value IS defined — it asserted nothing and
        passed for the wrong reason. The fix is not to give up on it: intercept
        the HTML and strip the inline block, which is exactly the condition the
        guard exists for.

        The claim under test is not "USER_ROLE becomes empty" — it is "the rest
        of this file still runs". A bare reference would throw a ReferenceError at
        the top level and take sorting, filtering, export and the drawer with it.
        """
        errors: list[str] = []
        page = self._page()
        page.on("pageerror", lambda e: errors.append(str(e)))

        def strip_the_block(route):
            response = route.fetch()
            body = response.text()
            start = body.index("const userRole")
            end = body.index("</script>", start)
            route.fulfill(
                response=response, body=body[:start] + body[end:], headers=response.headers
            )

        page.route(f"**{reverse('advisor_portfolio_page')}", strip_the_block)
        self._open(page)

        assert page.evaluate("() => USER_ROLE") == ""
        assert page.evaluate("() => USER_ADVISOR_ID") == ""
        assert page.evaluate("() => typeof loadStudents") == "function", (
            "the file stopped executing — the guard did not do its job"
        )
        assert errors == [], errors
