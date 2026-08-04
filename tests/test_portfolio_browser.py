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


# ── the three defects the revived branch exposed ─────────────────
#
# None was caused by fixing `USER_ROLE`. All three sat behind a branch that had
# never executed, so nothing had ever reached them.


def _blank_advisor_client(self):
    """An ADVISOR with no advisor_id — the state the template and the JS disagreed
    about. Reachable in production through the user-admin create and demote paths,
    through `rbac_seed --advisor-id ''`, and by simply having no UserScope row."""
    user = User.objects.create_user(username="blankadv", password="x", is_staff=True)
    set_user_scope(user.id, advisor_id="")
    client = Client()
    client.force_login(user)
    return {
        "name": "sessionid",
        "value": client.cookies["sessionid"].value,
        "url": self.live_server_url,
    }


class PortfolioDeadEndTests(PortfolioBrowserTests):
    """Same harness, different failures."""

    def _blank_page(self):
        context = self.browser.new_context()
        context.add_cookies([_blank_advisor_client(self)])
        self.addCleanup(context.close)
        return context.new_page()

    # ── 1. blank advisor_id ──────────────────────────────────────

    def test_an_adviser_with_no_id_is_told_rather_than_left_staring(self):
        """It used to show an empty table saying "choose an advisor above" while
        CSS hid the control it named, with no toast and no console error.

        Either the picker is usable or the problem is stated. Silence is the one
        outcome that is not allowed.
        """
        self._students()
        page = self._blank_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        self._open(page)

        bar_hidden = "d-none" in (page.get_attribute("#apAdvisorBar", "class") or "")
        body = page.inner_text("#apTable tbody")

        assert not bar_hidden or body.strip(), "hidden picker AND an empty explanation"
        if bar_hidden:
            raise AssertionError("the picker is hidden for a user who has no portfolio")
        assert errors == [], errors

    def test_the_403_from_the_advisor_list_is_surfaced(self):
        """`if (!res.ok || !Array.isArray(data.items)) return;` — a bare return.

        `loadStudents`, twenty lines below, already renders its failures into the
        tbody and raises a toast. This one did not.
        """
        self._students()
        page = self._blank_page()
        self._open(page)
        page.wait_for_selector("#apTable tbody", timeout=15_000)
        body = page.inner_text("#apTable tbody")
        assert body.strip(), "the table is blank and nothing said why"
        assert "choose an advisor" not in body.lower(), (
            "still inviting the user to use a control they cannot see or use"
        )

    def test_the_program_filter_survives_for_a_healthy_adviser(self):
        """`#apProgramFilter` and `#apLoadedLabel` were children of the hidden bar,
        so EVERY adviser lost them — including one whose portfolio loads fine."""
        self._students()
        page = self._page()
        self._open(page)
        page.wait_for_selector("#apTable tbody tr[data-sid]", timeout=15_000)
        assert page.is_visible("#apProgramFilter"), "the program filter went with the picker"

    # ── 2. truncation ────────────────────────────────────────────

    def _many(self, n=60):
        ensure_role_groups()
        for i in range(n):
            Student.objects.update_or_create(
                student_id=960000 + i,
                defaults={
                    "name": f"STUDENT {960000 + i}",
                    "program": "AI",
                    "section": "M",
                    "advisor_id": ADVISER_ID,
                    "status": "ACTIVE",
                },
            )

    def test_a_roster_over_fifty_is_fully_reachable(self):
        """The client asked for no page size, took the server default of 50, and its
        own PAGE_SIZE is also 50 — so its pager computed one page and hid itself.
        Ten of sixty advisees had no control that could reach them."""
        self._many(60)
        page = self._page()
        self._open(page)
        page.wait_for_selector("#apTable tbody tr[data-sid]", timeout=20_000)

        assert page.evaluate("() => allStudents.length") == 60, page.evaluate(
            "() => allStudents.length"
        )
        assert page.inner_html("#apPagination").strip(), "no pager for a 60-row roster"

    def test_the_totals_agree_with_each_other(self):
        """It read "50 students" beside an attention count of 60 — the page
        contradicting itself, because one number came from the slice and the other
        from the server's summary over the whole set."""
        self._many(60)
        page = self._page()
        self._open(page)
        page.wait_for_selector("#apTable tbody tr[data-sid]", timeout=20_000)

        chip = page.inner_text("#apCountChip")
        students = page.inner_text("#mStudents")
        assert "60" in chip, chip
        assert students.strip() == "60", students
        assert "50" not in page.inner_text("#apShowing").split("of")[-1], page.inner_text(
            "#apShowing"
        )

    # ── 3. the CSV export ────────────────────────────────────────

    def test_an_adviser_can_export_the_rows_they_can_already_read(self):
        """The JSON was ROLE_ADVISOR and the CSV was ROLE_GENERAL_ADVISOR, so an
        adviser saw every row on screen and was refused the same rows as a file.
        `#apCsvLink` is an <a href>, so the click navigated the whole tab to a raw
        403 JSON blob."""
        self._students()
        page = self._page()
        self._open(page)
        page.wait_for_selector("#apTable tbody tr[data-sid]", timeout=15_000)

        href = page.get_attribute("#apCsvLink", "href")
        assert href and href != "#", href
        if href.startswith("/"):
            href = self.live_server_url + href
        response = page.request.get(href)
        assert response.status == 200, f"{response.status}: {response.text()[:200]}"
        assert "text/csv" in (response.headers.get("content-type") or "")
        assert "990001" in response.text()

    def test_the_export_still_refuses_another_advisers_roster(self):
        """Relaxing the guard is only safe because the scope resolver refuses a
        mismatch. Before it, this view answered one with 200 and a header-only
        file — a refusal that downloads looking like an empty portfolio."""
        self._students()
        page = self._page()
        self._open(page)
        response = page.request.get(
            f"{self.live_server_url}/export/students-by-advisor.csv?advisor_id={OTHER_ADVISER_ID}"
        )
        assert response.status == 403, f"{response.status}: {response.text()[:200]}"

    def test_a_roster_past_the_request_ceiling_reports_the_true_total(self):
        """The only case where the two numbers differ.

        Below the ceiling `rosterTotal` and `allStudents.length` are equal, so a
        mutant reverting the chips to the slice length is EQUIVALENT and survives —
        measured, not assumed. This builds a roster past `SERVER_PAGE_SIZE` so the
        distinction becomes observable, and pins the honest behaviour: report the
        server's count, and say plainly that not every row is loaded.
        """
        ensure_role_groups()
        Student.objects.bulk_create(
            [
                Student(
                    student_id=950000 + i,
                    name=f"BULK {i}",
                    program="AI",
                    section="M",
                    advisor_id=ADVISER_ID,
                    status="ACTIVE",
                )
                for i in range(505)
            ],
            ignore_conflicts=True,
        )
        page = self._page()
        self._open(page)
        page.wait_for_selector("#apTable tbody tr[data-sid]", timeout=60_000)

        loaded = page.evaluate("() => allStudents.length")
        assert loaded == 500, loaded

        assert "505" in page.inner_text("#apCountChip"), page.inner_text("#apCountChip")
        assert page.inner_text("#mStudents").strip() == "505", page.inner_text("#mStudents")
        assert page.is_visible("#apTruncatedNote"), "a truncated roster said nothing about it"
        note = page.inner_text("#apTruncatedNote")
        assert "500" in note and "505" in note, note
        # And it must point somewhere. Rows 501-505 are NOT reachable in this
        # table — that is the acknowledged boundary, not full pagination — so the
        # note has to name the export, which passes no page_size and returns the
        # complete roster.
        assert "CSV" in note.upper(), note
        assert page.get_attribute("#apCsvLink", "href") not in (None, "#")
