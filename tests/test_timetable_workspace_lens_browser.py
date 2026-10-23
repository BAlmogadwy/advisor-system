"""The Timetable Workspace's programme lens in a real browser.

docs/TIMETABLE-WORKSPACE-UI.md §7.2 item 6, 6d. The plan lens comes with the
quality read (`quality/?lens=1`, one seating on the server), never from
/plan-lens/, and a programme in the board's filter dims what that
programme's per-plan file leaves out, by the server's roles
(`programme_view`): the page holds no copy of the rule.

The scenario (`build_lens_scenario`): AI150 is in both plans, its S1 marked
for AI2 and its S2 for DS; an AI2 student who also takes AI160, whose only
section meets at AI150 S1's time, is seated in S2. So AI2's file shows
AI150 S1 (its own), AI160 S1 (its own) and AI150 S2 ("other": it seats one
AI2 student, and is mainly DS's); DS170, in the DS plan alone, is in no AI2
file.

LensTests (1366×650 and 1440×790, light and dark, English and Arabic, the
wide font): typing "AI2" dims DS170 (the DS-only card) and not AI150 S2 (the
"other" card); the export reads "Programme AI2 only"; the page made exactly
one `quality/?lens=1` read (on open) and none of /plan-lens/; the dim and the
legend's programme line read at 4.5:1 and are not faded; the frame keeps its
acceptance.
"""

from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")

from django.contrib.staticfiles.testing import StaticLiveServerTestCase  # noqa: E402
from django.test import override_settings  # noqa: E402

from core.models import ScenarioStudentCourseRequest, Student  # noqa: E402
from tests import one_teacher_support as fx  # noqa: E402
from tests.test_timetable_workspace_frame_browser import CONTRAST, FRAME  # noqa: E402
from tests.timetable_workspace_browser_support import WorkspaceBrowserMixin  # noqa: E402

#: The students: who they are and what they want.
AI2_BOTH, AI2_ONE, DS_A, DS_B = 990001, 990002, 990101, 990102


def build_lens_scenario():
    """``(scenario, sections)``: the board described in the module's docstring,
    ``sections`` by "CODE LABEL"."""
    sc, board = fx.scenario("Programme lens", programs=("AI2", "DS"))
    shared = fx.requirement("AI2", "AI150", "Shared course")
    fx.requirement("DS", "AI150", "Shared course")
    ai2_only = fx.requirement("AI2", "AI160", "AI2 course")
    ds_only = fx.requirement("DS", "DS170", "DS course")
    fx.budget(sc, shared, 2)
    fx.budget(sc, ai2_only, 1)
    fx.budget(sc, ds_only, 1)
    sections = {}
    for key, label, mark, day in (
        (shared, "S1", "AI2", "SUN"),
        (shared, "S2", "DS", "MON"),
        (ai2_only, "S1", "", "SUN"),
        (ds_only, "S1", "", "TUE"),
    ):
        ts = fx.section(sc, key, label)
        ts.preferred_programme = mark
        ts.save(update_fields=["preferred_programme"])
        fx.place(board, ts, [(day, "09:00", "10:15")])
        sections[f"{ts.course_code} {label}"] = ts
    wants = {
        AI2_BOTH: ("AI2", (shared, ai2_only)),
        AI2_ONE: ("AI2", (shared,)),
        DS_A: ("DS", (shared, ds_only)),
        DS_B: ("DS", (shared, ds_only)),
    }
    for sid, (programme, keys) in wants.items():
        Student.objects.create(
            student_id=sid,
            registration_no=str(sid),
            name=f"Student {sid}",
            program=programme,
            section="M",
            status="active",
        )
        for key in keys:
            ScenarioStudentCourseRequest.objects.create(
                scenario=sc,
                student_id=sid,
                course_key=key,
                course_code=key.split("::", 1)[0],
                primary_term=2,
                status=ScenarioStudentCourseRequest.STATUS_REQUESTED,
                priority=ScenarioStudentCourseRequest.PRIORITY_NORMAL,
                source="test",
            )
    return sc, sections


# What is wrong with the dims, as short findings.
LENS = r"""() => {
  const bad = [];
  const strip = text => String(text || '').replace(/[\u2066\u2069]/g, '');
  const cards = [...document.querySelectorAll('#twGrid .tw-card')];
  const byCode = code => cards.filter(card => card.querySelector('.tw-card-code')?.textContent.trim() === code);
  const dim = card => card.getAttribute('data-dim') || '';
  const ds = byCode('DS170');
  if (ds.length !== 1 || dim(ds[0]) !== 'lens') bad.push(`DS170: ${ds.map(dim)}`);
  for (const code of ['AI150', 'AI160']) {
    for (const card of byCode(code)) if (dim(card)) bad.push(`${code} ${card.dataset.placementId} dimmed`);
  }
  if (byCode('AI150').length !== 2) bad.push(`${byCode('AI150').length} AI150 cards`);
  const other = byCode('AI150').find(card => /seats 1 of 3|فيها 1 من 3/.test(card.title));
  if (!other) bad.push(`no "other" AI150: ${byCode('AI150').map(card => strip(card.title))}`);
  const scope = strip(document.getElementById('twExportScope').textContent);
  if (scope !== 'Programme AI2 only' && scope !== 'برنامج AI2 فقط') bad.push(`export scope: ${scope}`);
  const legend = document.getElementById('twLegendDimLens');
  if (!legend || legend.hidden || !legend.getBoundingClientRect().width) bad.push('no legend line for the programme');
  else {
    let opacity = 1;
    for (let e = legend; e; e = e.parentElement) opacity *= parseFloat(getComputedStyle(e).opacity);
    if (opacity < 1) bad.push(`legend line at opacity ${opacity}`);
  }
  if (!document.getElementById('twLegendDim').hidden) bad.push('the text filter’s legend line, with no text');
  return bad;
}"""


@override_settings(TIMETABLE_SECTION_PROGRAMME_PREFERENCE=True)
class LensTests(WorkspaceBrowserMixin, StaticLiveServerTestCase):
    maxDiff = None

    def setUp(self) -> None:
        super().setUp()
        self.scenario, self.sections = build_lens_scenario()
        self.path = f"/timetable-workspace/?scenario={self.scenario.id}"

    def test_a_programme_dims_what_its_file_leaves_out_by_the_servers_roles(self) -> None:
        problems: dict[str, list] = {}
        for language in ("en", "ar"):
            for theme in ("light", "dark"):
                for width, height in ((1366, 650), (1440, 790)):
                    page, sent, errors = self._load(language, theme, width, height)
                    page.fill("#twSearch", "AI2")
                    page.wait_for_function(
                        "document.querySelectorAll('#twGrid .tw-card[data-dim]').length === 1"
                    )
                    found = page.evaluate(LENS)
                    found += page.evaluate(
                        CONTRAST,
                        [
                            ".tw-card[data-dim] .tw-card-code",
                            ".tw-card[data-dim] .tw-card-room",
                            "#twLegendDimLens",
                        ],
                    )
                    found += page.evaluate(FRAME, 1440)
                    reads = [
                        url for url in self.requested if "/quality/" in url or "/plan-lens/" in url
                    ]
                    lens_reads = [url for url in reads if url.endswith("/quality/?lens=1")]
                    if len(lens_reads) != 1 or any("/plan-lens/" in url for url in reads):
                        found.append(f"reads: {reads}")
                    if sent or errors:
                        found += [*sent, *errors]
                    if found:
                        problems[f"{language} {theme} {width}x{height}"] = found
                    page.context.close()
        self.assertEqual(problems, {})
