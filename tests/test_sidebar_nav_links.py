"""Which links the sidebar draws for which role, pinned.

The nav was restructured so each of its seven sections is a `.nav-grp` with a
`<button class="nav-lbl">` header and a `.nav-grp-items` container, which meant
wrapping groups around `{% if can_* %}` blocks that used to sit flat in the
list. That is the one place in that change where a link can quietly go missing
for a role, or reach one that never had it, and nothing else would notice: the
browser suite loads the sidebar as a super admin.

It caught one. The Insights header's own condition left `can_graduation_planning`
out, which cost nothing while the flags were flat (only a super admin has it,
and a super admin has `can_db_admin` too) but dropped Graduation Planning as
soon as the links moved inside the group.

These render the partial directly, with no database, so they are cheap enough
to pin every role AND every flag combination the template branches on.
"""

from __future__ import annotations

import itertools
import re
from html.parser import HTMLParser

import pytest
from django.template import Context, Template
from django.test import RequestFactory
from django.utils import translation

PARTIAL = "core/templates/core/partials/sidebar.html"

#: Every flag the staff branch of the partial reads.
STAFF_FLAGS = (
    "can_db_admin",
    "can_admin_advisors",
    "can_view_portfolio",
    "can_graduation_planning",
    "can_section_planning",
    "can_exam_timetable",
)

#: Each link, and the flag that has to be on for it to be drawn. None means the
#: staff branch always draws it. In the order the partial draws them.
STAFF_LINKS = (
    ("/#student", None),
    ("/virtual-advisor/", None),
    ("/ops/timetable-feedback/", "can_timetable_feedback_staff"),
    ("/#batch", "can_db_admin"),
    ("/#debug", "can_db_admin"),
    ("/#plan", None),
    ("/#prereq", None),
    ("/#eligibility", "can_db_admin"),
    ("/planner/", None),
    ("/timetable-workspace/", "can_section_planning"),
    ("/section-planning/", "can_section_planning"),
    ("/group-availability/", "can_section_planning"),
    ("/instructor-management/", "can_section_planning"),
    ("/timetable-workspace/graph/", "can_section_planning"),
    ("/#conflictmatrix", "can_db_admin"),
    ("/exam-timetable/", "can_exam_timetable"),
    ("/advisor-portfolio/", "can_view_portfolio"),
    ("/graduation-planning/", "can_graduation_planning"),
    ("/#highpriority", "can_db_admin"),
    ("/#exports", "can_db_admin"),
    ("/audit-explorer/", "can_db_admin"),
    ("/#scrape", "can_db_admin"),
    ("/ops/sections-import/", "can_db_admin"),
    ("/user-management/", "can_admin_advisors"),
    ("/#advisoradmin", "can_admin_advisors"),
    ("/db-admin/", "can_db_admin"),
)

STUDENT_LINKS = (
    ("/student/", None),
    ("/student/courses/", None),
    ("/student/graduation/", None),
    ("/student/timetable/", None),
    ("/student/timetable-feedback/", "can_timetable_feedback"),
    ("/student/advisor/", None),
)

ANCHOR = re.compile(r'<a[^>]*\bclass="nav-a[^"]*"[^>]*\bhref="([^"]*)"')
#: HTMLParser reports these without an end tag, so they never go on the stack.
VOID = frozenset("area base br col embed hr img input link meta param source track wbr".split())


class _Nesting(HTMLParser):
    """Where each nav link actually sits, by real nesting.

    A regex cannot answer this: `</div>` says nothing about which element it
    closed, so a link moved out of its group still reads as inside one. A
    mutant that did exactly that survived the regex this replaced.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, frozenset, str | None]] = []
        #: one (group name, is inside that group's items) per link, in order
        self.links: list[tuple[str | None, bool]] = []
        self.groups: dict[str, list[str]] = {}

    def handle_starttag(self, tag, attrs) -> None:
        pairs = dict(attrs)
        classes = frozenset((pairs.get("class") or "").split())
        if "nav-grp" in classes and pairs.get("data-group"):
            self.groups.setdefault(pairs["data-group"], [])
        if "nav-a" in classes:
            group = None
            in_items = False
            for depth, (_, ancestor, name) in enumerate(self.stack):
                if "nav-grp" in ancestor and name:
                    group = name
                if "nav-grp-items" in ancestor and depth:
                    in_items = "nav-grp" in self.stack[depth - 1][1]
            self.links.append((group, in_items))
            if group is not None:
                self.groups.setdefault(group, []).append(pairs.get("href", ""))
        if tag not in VOID:
            self.stack.append((tag, classes, pairs.get("data-group")))

    def handle_startendtag(self, tag, attrs) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in VOID and self.stack:
            self.stack.pop()

    def handle_endtag(self, tag) -> None:
        for at in range(len(self.stack) - 1, -1, -1):
            if self.stack[at][0] == tag:
                del self.stack[at:]
                return


def _nesting(html: str) -> _Nesting:
    parser = _Nesting()
    parser.feed(html)
    return parser


@pytest.fixture(scope="module")
def partial() -> Template:
    with open(PARTIAL, encoding="utf-8") as handle:
        return Template(handle.read())


class _User:
    is_authenticated = True
    username = "sidebar-links"


def _render(partial: Template, **flags) -> str:
    request = RequestFactory().get("/planner/")
    request.user = _User()
    context = {
        "request": request,
        "user": request.user,
        "timetable_feedback_unread": 0,
        "timetable_feedback_staff_count": 0,
    }
    context.update({flag: False for flag in STAFF_FLAGS})
    context["can_timetable_feedback"] = False
    context["can_timetable_feedback_staff"] = False
    context.update(flags)
    return partial.render(Context(context))


def _hrefs(html: str) -> list[str]:
    return ANCHOR.findall(html)


def _expected(links, flags) -> list[str]:
    return [href for href, flag in links if flag is None or flags.get(flag)]


@pytest.mark.parametrize("role", ["SUPER_ADMIN", "GENERAL_ACADEMIC_ADVISOR", "ADVISOR"])
@pytest.mark.parametrize("feedback", [False, True])
def test_a_staff_role_sees_exactly_the_links_its_flags_allow(partial, role, feedback) -> None:
    """Every combination of the six flags the staff branch branches on — not
    only the ones core/sidebar_context.py produces today, so granting a flag to
    another role later cannot silently drop a link."""
    for bits in itertools.product((False, True), repeat=len(STAFF_FLAGS)):
        flags = dict(zip(STAFF_FLAGS, bits, strict=True))
        flags["can_timetable_feedback_staff"] = feedback
        html = _render(partial, role=role, **flags)
        assert _hrefs(html) == _expected(STAFF_LINKS, flags), (role, flags)


def test_graduation_planning_shows_on_its_own_flag(partial) -> None:
    # The regression the restructure introduced and this file caught: the group
    # wrapper has to be the union of its links' conditions, not the header's
    # old, narrower one.
    html = _render(partial, role="ADVISOR", can_graduation_planning=True)
    assert "/graduation-planning/" in _hrefs(html)
    assert 'data-group="insights"' in html


@pytest.mark.parametrize("feedback", [False, True])
def test_a_student_sees_only_the_student_portal(partial, feedback) -> None:
    # Every staff flag on: a student still gets the student branch alone.
    flags = {flag: True for flag in STAFF_FLAGS}
    html = _render(partial, role="STUDENT", can_timetable_feedback=feedback, **flags)
    assert _hrefs(html) == _expected(STUDENT_LINKS, {"can_timetable_feedback": feedback})


def test_the_exam_committee_sees_only_the_exam_timetable(partial) -> None:
    flags = {flag: True for flag in STAFF_FLAGS}
    html = _render(partial, role="EXAM_COMMITTEE", can_timetable_feedback_staff=True, **flags)
    assert _hrefs(html) == ["/exam-timetable/"]


@pytest.mark.parametrize(
    "role", ["SUPER_ADMIN", "GENERAL_ACADEMIC_ADVISOR", "ADVISOR", "STUDENT", "EXAM_COMMITTEE"]
)
def test_every_link_sits_in_a_named_group(partial, role) -> None:
    """The palette reads each destination's section from its group's header, and
    the rail separates the groups: a link outside one would have no section and
    no separator."""
    flags = {flag: True for flag in STAFF_FLAGS}
    html = _render(
        partial, role=role, can_timetable_feedback=True, can_timetable_feedback_staff=True, **flags
    )
    seen = _nesting(html)
    assert len(seen.links) == len(_hrefs(html)) > 0
    stray = [place for place in seen.links if place[0] is None or not place[1]]
    assert stray == [], stray
    for group, hrefs in seen.groups.items():
        assert hrefs, f"{group} has a header and no links"
        assert 'id="nav-lbl-' + group + '"' in html, group
        assert 'aria-labelledby="nav-lbl-' + group + '"' in html, group


@pytest.mark.parametrize("language", ["en", "ar"])
def test_the_partial_leaves_no_template_syntax_in_the_page(partial, language) -> None:
    # Django's `{# … #}` comment is one line only; a two-line one renders.
    flags = {flag: True for flag in STAFF_FLAGS}
    with translation.override(language):
        html = _render(
            partial,
            role="SUPER_ADMIN",
            can_timetable_feedback=True,
            can_timetable_feedback_staff=True,
            **flags,
        )
    for token in ("{%", "%}", "{{", "}}", "{#", "#}"):
        assert token not in html, token
