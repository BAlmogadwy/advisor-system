"""A manual move into a time the teacher's rules forbid, in a real browser (slice C).

Nothing is refused: the move is saved and the page SHOWS the problem as a
warning (the Move dialog's line, the toast, the card, the Issues row), in
English and in Arabic. Dr A has a "no mornings" rule (nothing before 13:00);
AI111 S1 meets Sunday and Tuesday 13:00, so Sunday 09:00 is forbidden.
"""

from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")

from django.contrib.staticfiles.testing import StaticLiveServerTestCase  # noqa: E402
from django.test import override_settings  # noqa: E402

from tests._instructor_availability_fixture import AVAILABILITY_FLAGS  # noqa: E402
from tests.live_server_idle import live_server_paused  # noqa: E402
from tests.test_tw_teacher_rules_display import build_ruled  # noqa: E402
from tests.timetable_workspace_browser_support import WorkspaceBrowserMixin  # noqa: E402

WORDS = {
    "en": {
        "kind": "Teacher not available at this time",
        "line": "This time is outside Dr A’s teaching times",
    },
    "ar": {
        "kind": "المدرّس غير متاح في هذا الوقت",
        "line": "هذا الوقت خارج أوقات تدريس Dr A",
    },
}
ISOLATES = str.maketrans("", "", chr(0x2066) + chr(0x2069))


@override_settings(**AVAILABILITY_FLAGS)
class TeacherRulesBrowserTests(WorkspaceBrowserMixin, StaticLiveServerTestCase):
    maxDiff = None

    def setUp(self) -> None:
        super().setUp()  # the superuser, its session, and a default scenario
        from core.models import ProgrammeRequirement, SectionPlacement

        # The mixin's own scenario is replaced by the ruled one.
        self.scenario.delete()
        ProgrammeRequirement.objects.all().delete()
        self.scenario, _board, _key, self.s1, _s2, _a, _b = build_ruled()
        self.path = f"/timetable-workspace/?scenario={self.scenario.id}"
        self.sunday = SectionPlacement.objects.get(term_section=self.s1, day="SUN")
        self.card = f'#twGrid [data-focus-key="placement:{self.sunday.id}"]'

    def test_a_manual_move_into_a_forbidden_time_is_saved_and_shown(self) -> None:
        from core.models import SectionPlacement

        for language in ("en", "ar"):
            words = WORDS[language]
            page, sent, errors = self._load(language, "light", 1440, 790, cards=2)
            page.click(self.card)
            page.click('#twRightBody [data-tw-action="move"]')
            page.wait_for_function("document.activeElement?.name === 'twMoveChoice'")
            # The forbidden time is offered, in the warning tone, with its sentence.
            line = page.evaluate(
                """() => {
                  const radio = document.querySelector('input[name="twMoveChoice"][value="SUN|09:00"]');
                  const row = radio.closest('.ux-choice');
                  const said = radio.getAttribute('aria-describedby').split(' ')
                    .map(id => document.getElementById(id).textContent);
                  return { tone: row.dataset.tone, disabled: radio.disabled, said };
                }"""
            )
            self.assertEqual(line["tone"], "warning", language)
            self.assertFalse(line["disabled"], language)
            self.assertTrue(
                any(
                    said.translate(ISOLATES) == f"{words['kind']}: {words['line']}"
                    for said in line["said"]
                ),
                (language, line["said"]),
            )
            page.click('#twMoveChoices label:has(input[value="SUN|09:00"])')
            page.click("#twMoveCommit")
            # Saved: the meeting is at 09:00 and the page says so as a warning.
            page.wait_for_function(
                """id => !document.getElementById('twMoveDialog').open
                  && TW.placements.some(p => p.id === id && p.start_time === '09:00')
                  && document.querySelector('.tw-workspace').dataset.busy === 'false'""",
                arg=self.sunday.id,
            )
            self.assertEqual(
                SectionPlacement.objects.get(pk=self.sunday.pk).start_time, "09:00", language
            )
            page.wait_for_function(
                """kind => [...document.querySelectorAll('#toast-container .ux-toast.warning .ux-toast-title')]
                  .some(title => title.textContent.includes(kind))""",
                arg=words["kind"],
            )
            page.wait_for_function(
                f"document.querySelector('{self.card}').classList.contains('warning')"
            )
            page.click("#twTabIssues")
            page.wait_for_function(
                """([kind, subject]) => [...document.querySelectorAll('#twRightBody .tw-issue-row[data-tone="warning"]')]
                  .some(row => row.querySelector('.tw-issue-kind').textContent === kind
                    && row.querySelector('.tw-issue-subject').textContent.replace(/[\\u2066\\u2069]/g, '') === subject)""",
                arg=[words["kind"], "Dr A — AI111 S1"],
            )
            self.assertEqual(
                [m for m in sent if "/move/" not in m and not m.endswith("/move")], [], language
            )
            self.assertEqual(errors, [], language)
            page.context.close()
            # The next language starts from the same Sunday 13:00, once the
            # server has read what the move scheduled (its readiness check).
            with live_server_paused(self.server_thread):
                SectionPlacement.objects.filter(pk=self.sunday.pk).update(
                    start_time="13:00", end_time="14:15"
                )
