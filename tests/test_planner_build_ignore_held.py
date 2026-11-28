"""Ignore on the staff board: the registered courses are rows of the plan the adviser can edit (S7).

Converting the registered courses puts each one in the shortlist as a Required row pinned to the
section the student holds; a registered course with no section, or one whose section has no
usable meetings, is held as load only (`registered_load_only`). The server never takes the
client's word for what the student holds: the sections, their hours and the load-only courses are
read from the database, as Keep reads them.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from django.contrib.auth.models import Group, User
from django.test import Client

from core.models import (
    Course,
    ElectiveCourse,
    ElectiveTermMapping,
    ProgrammeRequirement,
    Student,
    StudentCourse,
    StudentTermSection,
    TermSection,
    TermSectionMeeting,
    TermSectionProgram,
)
from core.services import planner_builder
from core.services.planner_builder import build_plans
from core.services.rbac import ROLE_SUPER_ADMIN, ensure_role_groups

pytestmark = pytest.mark.django_db

PROG = "HLD"
YEAR, TERM = "1448", "1"
SID = 4_690_777
DAYS = ["SUN", "MON", "TUE", "WED", "THU"]


@pytest.fixture(autouse=True)
def _fresh_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    # The build allows a few calls a minute per user; these tests build more often than that.
    monkeypatch.setattr("core.authz._rate_buckets", {})


def _student() -> None:
    Student.objects.create(
        student_id=SID,
        registration_no=str(SID),
        name="Sample Student",
        program=PROG,
        section="M",
        status="ACTIVE",
    )


def _section(
    code: str,
    day: str,
    start: str,
    end: str,
    *,
    label: str = "M1",
    hours: int = 3,
    in_plan: bool = True,
    programmes: tuple[str, ...] = (PROG,),
    capacity: int = 30,
    taken: int = 1,
    key: str | None = None,
    once: bool = False,
) -> TermSection:
    """One section of ``code`` meeting on ``day`` (and two days later, unless ``once``)."""
    if in_plan and not ProgrammeRequirement.objects.filter(program=PROG, course_code=code).exists():
        ProgrammeRequirement.objects.create(
            program=PROG,
            course_code=code,
            course_name=f"Course {code}",
            programme_term=1,
            credit_hours=hours,
            type="Mandatory",
        )
    section = TermSection.objects.create(
        course_key=key or code,
        course_code=code,
        course_number=code if key else "",
        course_name=f"Course {code}",
        section=label,
        available_capacity=capacity,
        registered_count=taken,
    )
    for programme in programmes:
        TermSectionProgram.objects.create(term_section=section, program=programme)
    first = DAYS.index(day)
    for d in (DAYS[first],) if once else (DAYS[first], DAYS[(first + 2) % 5]):
        TermSectionMeeting.objects.create(
            term_section=section, day=d, start_time=start, end_time=end
        )
    return section


def _register(section: TermSection) -> TermSection:
    StudentTermSection.objects.create(
        student_id=SID,
        academic_year=YEAR,
        term=TERM,
        term_section=section,
        source="scraper_timetable",
    )
    return section


def _studying(code: str, hours: int, *, in_plan: bool = True) -> None:
    """A course the student studies with no section on file (the page holds it as load only)."""
    if in_plan:
        ProgrammeRequirement.objects.create(
            program=PROG,
            course_code=code,
            course_name=f"Course {code}",
            programme_term=1,
            credit_hours=hours,
            type="Mandatory",
        )
    course = Course.objects.create(
        course_code=code, description=f"Course {code}", credit_hours=hours
    )
    StudentCourse.objects.create(student_id=SID, course=course, status="studying")


def _row(code: str, section: TermSection | None = None, *, must: bool = True, credits: int = 3):
    """A shortlist row; with a section it is a registered row (pinned, Required)."""
    item = {
        "course_code": code,
        "credits": credits,
        "priority": "Med",
        "score": 50,
        "status": "Eligible",
        "missing_prerequisites": [],
        "must_take": must,
    }
    if section is not None:
        item["pinned_sections"] = [{"term_section_id": section.id, "section": section.section}]
    return item


def _client() -> Client:
    ensure_role_groups()
    user = User.objects.create(username="held-courses")
    user.groups.add(Group.objects.get(name=ROLE_SUPER_ADMIN))
    client = Client(SERVER_NAME="localhost")
    client.force_login(user)
    return client


def _post_build(client: Client, shortlist, *, cap=18, mode="ignore", **extra):
    return client.post(
        "/ops/planner/build/",
        data={
            "student_id": str(SID),
            "academic_year": YEAR,
            "term": TERM,
            "mode": mode,
            "eligibility_mode": "relaxed",
            "swap": False,
            "program_sections_only": True,
            "allow_full_sections": False,
            "max_credits": cap,
            "shortlist": shortlist,
            **extra,
        },
        content_type="application/json",
    )


def _build(client: Client, shortlist, **kwargs) -> dict:
    response = _post_build(client, shortlist, **kwargs)
    assert response.status_code == 200, response.content
    return response.json()


def _placed(option: dict) -> dict[str, int]:
    return {m["course_key"]: m["term_section_id"] for m in option["mappings"]}


def _apply(client: Client, ids, **extra):
    return client.post(
        "/ops/planner/save-student-sections/",
        data={
            "student_id": str(SID),
            "academic_year": YEAR,
            "term": TERM,
            "term_section_ids": ids,
            "confirm_replace": True,
            **extra,
        },
        content_type="application/json",
    )


# ── The sections the student holds ─────────────────────────────────────────────────────────────
def test_a_held_full_section_is_used_and_a_full_section_not_held_is_not() -> None:
    _student()
    held = _register(_section("HF100", "SUN", "08:00", "09:15", capacity=30, taken=30))
    _section("HF200", "MON", "08:00", "09:15", capacity=30, taken=30)  # full, not held

    result = _build(_client(), [_row("HF100", held), _row("HF200", must=False)])

    assert result["options"], result
    for option in result["options"]:
        assert _placed(option) == {"HF100": held.id}
    assert {u["course_code"] for u in result["unscheduled"]} == {"HF200"}


def test_a_held_section_outside_the_programme_is_built_listed_and_applied() -> None:
    _student()
    held = _register(_section("HO100", "SUN", "08:00", "09:15", programmes=("OTHER",)))
    stranger = _section("HO200", "MON", "08:00", "09:15", programmes=("OTHER",))
    client = _client()

    result = _build(client, [_row("HO100", held)])
    assert result["options"], result
    assert all(_placed(option) == {"HO100": held.id} for option in result["options"])

    def listed(**extra) -> set[int]:
        reply = client.post(
            "/ops/planner/sections-catalog/",
            data={
                "student_id": str(SID),
                "academic_year": YEAR,
                "term": TERM,
                "course_codes": ["HO100", "HO200"],
                "program_sections_only": True,
                **extra,
            },
            content_type="application/json",
        ).json()
        return {section["term_section_id"] for section in reply["sections"]}

    assert listed() == set()
    assert listed(include_registered=True) == {held.id}  # never a stranger's section

    assert _apply(client, [held.id], mode="ignore").status_code == 200
    refused = _apply(client, [stranger.id], mode="ignore")
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "SECTION_NOT_AVAILABLE_TO_STUDENT"


# ── The limit is never below the load the student holds (decision 2) ──────────────────────────
def test_21_of_18_swaps_a_course_for_one_of_the_same_hours_and_finds_no_room_for_one_more() -> None:
    _student()
    held = [
        _register(
            _section(
                f"HC1{i:02d}",
                DAYS[i % 5],
                f"{8 + 2 * (i // 5):02d}:00",
                f"{9 + 2 * (i // 5):02d}:15",
                once=True,
            )
        )
        for i in range(7)
    ]
    _section("HN100", "THU", "14:00", "15:15", once=True)
    rows = [_row(s.course_key, s) for s in held]
    client = _client()

    swap = _build(client, [*rows[:6], _row("HN100", must=False)])
    assert swap["options"], swap
    assert swap["constraints"]["cap_raised_to"] == 21
    assert swap["summary"]["scheduled"] == 7
    # Every option keeps the six (Required); the best one adds HN100 in the room the seventh left.
    for option in swap["options"]:
        assert (
            {s.course_key for s in held[:6]}
            <= set(_placed(option))
            <= ({s.course_key for s in held[:6]} | {"HN100"})
        )

    more = _build(client, [*rows, _row("HN100", must=False)])
    assert more["options"], more
    assert more["constraints"]["credit_room"] == 0
    for option in more["options"]:
        assert set(_placed(option)) == {s.course_key for s in held}
        (left,) = option["unscheduled"]
        assert left["course_code"] == "HN100"
        assert left["reason"] == "No room left under the 18-credit limit: 21 credits are registered"
        assert left["details"] == [
            {"course_credits": 3, "credit_limit": 18, "registered_credits": 21}
        ]


# ── Load-only courses ─────────────────────────────────────────────────────────────────────────
def _load_only_student() -> list[TermSection]:
    """Three timed registered courses (9 cr) and LO100 (4 cr) studied with no section: 13."""
    _student()
    held = [
        _register(_section(f"HL1{i:02d}", DAYS[i], "08:00", "09:15", once=True)) for i in range(3)
    ]
    _studying("LO100", 4)
    _section("HN200", "SUN", "10:00", "11:15")
    _section("HN300", "MON", "10:00", "11:15")
    return held


def test_load_only_hours_come_off_the_room() -> None:
    held = _load_only_student()
    client = _client()
    rows = [_row(s.course_key, s) for s in held] + [
        _row("HN200", must=False),
        _row("HN300", must=False),
    ]

    # 9 + 3 + 3 = 15 under 18: both new courses fit while LO100 is left out of the plan...
    without = _build(client, rows)
    assert max(len(option["mappings"]) for option in without["options"]) == 5
    # ...and with its 4 hours counted only one does (9 + 4 + 3 = 16; one more is 19).
    counted = _build(client, rows, registered_load_only=["LO100"])
    assert counted["options"], counted
    assert max(len(option["mappings"]) for option in counted["options"]) == 4
    assert counted["constraints"]["registered_credits"] == 13
    assert counted["constraints"]["credit_room"] == 5


def test_load_only_is_refused_for_anything_but_the_students_own_untimed_course() -> None:
    held = _load_only_student()
    # A slot the student studies whose hours a registered elective carries: 0 credits.
    ProgrammeRequirement.objects.create(
        program=PROG,
        course_code="HLD1",
        course_name="Program Elective I",
        programme_term=1,
        credit_hours=3,
        type="Program Elective",
    )
    elective = ElectiveCourse.objects.create(
        programme=PROG, course_code="HE463", course_name="Data Mining", credit_hours=3
    )
    ElectiveTermMapping.objects.create(
        academic_year=YEAR,
        term=int(TERM),
        programme=PROG,
        placeholder_code="HLD1",
        elective=elective,
    )
    slot = Course.objects.create(
        course_code="HLD1", description="Program Elective I", credit_hours=3
    )
    StudentCourse.objects.create(student_id=SID, course=slot, status="studying")
    _register(_section("HE463", "THU", "08:00", "09:15", in_plan=False))
    client = _client()
    rows = [_row(s.course_key, s) for s in held]

    for sent, code in (
        (["NOPE100"], "VALIDATION_LOAD_ONLY"),  # not the student's
        ([held[0].course_key], "VALIDATION_LOAD_ONLY"),  # a timed section is a row, not load
        (["HLD1"], "VALIDATION_LOAD_ONLY"),  # a slot carried at 0 credits is not a registration
        (["LO100", "LO100"], "VALIDATION_LOAD_ONLY_DUPLICATE"),
        ("LO100", "VALIDATION_LOAD_ONLY"),  # not a list
    ):
        response = _post_build(client, rows, registered_load_only=sent)
        assert response.status_code == 400, (sent, response.content)
        assert response.json()["error"]["code"] == code, sent

    twice = _post_build(client, [*rows, _row("LO100", must=False)], registered_load_only=["LO100"])
    assert twice.status_code == 400
    assert twice.json()["error"]["code"] == "VALIDATION_LOAD_ONLY_DUPLICATE"
    # Keep never reads it: the registered load is counted the Keep way.
    assert _post_build(client, [], mode="keep", registered_load_only=["NOPE100"]).status_code == 200


def test_a_registered_row_keeps_its_registered_hours_whatever_the_client_sends() -> None:
    _student()
    held = _register(_section("HR100", "SUN", "08:00", "09:15", hours=4))
    _section("HN400", "MON", "08:00", "09:15")
    client = _client()
    shortlist = [_row("HR100", held, credits=1), _row("HN400", must=False)]

    # Sent as 1 credit the two would fit under 6 (1 + 3). Its registered 4 + 3 do not.
    result = _build(client, shortlist, cap=6)
    assert result["options"], result
    assert all(_placed(option) == {"HR100": held.id} for option in result["options"])


# ── The search ────────────────────────────────────────────────────────────────────────────────
def test_the_only_section_of_a_must_take_course_is_never_left_out_to_look_for_alternatives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _student()
    held = _section("HS100", "SUN", "08:00", "09:15")
    _section("HS200", "MON", "08:00", "09:15", label="M1")
    _section("HS200", "MON", "10:00", "11:15", label="M2")
    seen: list[dict] = []
    for name in ("_cp_build_option", "_bitmask_build_option_b", "_bitmask_build_option_c"):
        real = getattr(planner_builder, name)

        def spy(shortlist, catalog, *args, _real=real, **kwargs):
            seen.append({code: len(options) for code, options in catalog.items()})
            return _real(shortlist, catalog, *args, **kwargs)

        monkeypatch.setattr(planner_builder, name, spy)

    result = build_plans(
        YEAR,
        TERM,
        [_row("HS100", held), _row("HS200", must=False)],
        [],
        keep_registered=False,
        gender="M",
        program=PROG,
    )

    assert result["options"], result
    assert seen and all(counts["HS100"] == 1 for counts in seen), seen
    # The other course is still varied: its second section is an alternative.
    assert any(counts["HS200"] == 1 for counts in seen), seen


# ── The forecast token ────────────────────────────────────────────────────────────────────────
def test_the_forecast_of_an_ignore_build_is_refused_once_the_registration_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _student()
    held = _register(_section("HT100", "SUN", "08:00", "09:15"))
    monkeypatch.setattr(
        "core.services.planner_graduation.compare_planner_graduation",
        Mock(return_value={"options": {}}),
    )
    client = _client()
    token = _build(client, [_row("HT100", held)])["graduation_projection_token"]

    def project() -> int:
        return client.post(
            "/ops/planner/graduation-projection/",
            data={"token": token},
            content_type="application/json",
        ).status_code

    assert project() == 200
    _register(_section("HT200", "MON", "08:00", "09:15"))
    assert project() == 409


# ── Apply ─────────────────────────────────────────────────────────────────────────────────────
def test_apply_in_ignore_sets_aside_the_registrar_rows_of_courses_the_plan_places_or_drops() -> (
    None
):
    _student()
    _register(_section("MV100", "SUN", "08:00", "09:15", label="M1"))
    moved = _section("MV100", "SUN", "08:30", "09:45", label="M3")
    _register(_section("DR100", "MON", "08:00", "09:15"))
    into_dropped = _section("IN100", "MON", "08:00", "09:15")
    _register(_section("KZ100", "TUE", "12:00", "13:15"))
    over_kept = _section("OV100", "TUE", "12:00", "13:15")
    client = _client()
    ids = [moved.id, into_dropped.id, over_kept.id]

    def clashes(**extra) -> list[str]:
        response = _apply(client, ids, **extra)
        assert response.status_code == 200, response.content
        return sorted(
            {
                f"{c['course_code']}>{c['clashes_with']}"
                for c in response.json()["clashes_with_registered"]
            }
        )

    assert clashes(mode="ignore", dropped_registered=["DR100"]) == ["OV100>KZ100"]
    # Keep is unchanged: every registrar row is checked.
    assert clashes(mode="keep") == ["IN100>DR100", "MV100>MV100", "OV100>KZ100"]
    refused = _apply(client, ids, mode="ignore", dropped_registered=["NOPE100"])
    assert refused.status_code == 400
    assert refused.json()["error"]["code"] == "VALIDATION_DROPPED_REGISTERED"


# ── Identity, and the promise of an unedited conversion ───────────────────────────────────────
def test_a_course_named_by_code_and_name_converts_and_builds() -> None:
    _student()
    ProgrammeRequirement.objects.create(
        program=PROG,
        course_code="CS112",
        course_name="Programming I",
        programme_term=1,
        credit_hours=3,
        type="Mandatory",
    )
    held = _register(
        _section("CS112", "SUN", "08:00", "09:15", key="CS112::PROGRAMMING_I", in_plan=False)
    )
    _section("CS112", "MON", "08:00", "09:15", key="CS112::PROGRAMMING_II", in_plan=False)
    _section("HN500", "THU", "08:00", "09:15", hours=3, once=True)
    client = _client()

    result = _build(
        client,
        [_row("CS112::PROGRAMMING_I", held, credits=0), _row("HN500", must=False)],
        cap=3,
    )

    assert result["options"], result
    for option in result["options"]:
        assert _placed(option) == {"CS112::PROGRAMMING_I": held.id}
    # Its registered 3 credits were counted (sent as 0), so the limit of 3 has no room left.
    assert result["constraints"]["registered_credits"] == 3


def test_an_unedited_conversion_places_every_course_where_keep_does() -> None:
    _student()
    held = [
        _register(_section("HQ100", "SUN", "08:00", "09:15")),
        _register(_section("HQ200", "MON", "10:00", "11:15")),
    ]
    _section("HW100", "SUN", "08:00", "09:15", label="M1")  # clashes with HQ100
    free_one = _section("HW100", "THU", "08:00", "09:15", label="M2")
    free_two = _section("HW200", "SUN", "12:00", "13:15", label="M1")
    _section("HW200", "MON", "10:00", "11:15", label="M2")  # clashes with HQ200
    client = _client()
    wanted = [_row("HW100", must=False), _row("HW200", must=False)]

    # 12 credits in all under a limit of 12: exactly the room either way.
    keep = _build(client, wanted, mode="keep", cap=12)
    ignore = _build(client, [*[_row(s.course_key, s) for s in held], *wanted], cap=12)

    # The timetables that place every course (alternatives may leave one out, in both modes).
    def whole(result: dict, size: int) -> set[frozenset]:
        return {
            frozenset(_placed(option).items())
            for option in result["options"]
            if len(option["mappings"]) == size
        }

    expected = {"HW100": free_one.id, "HW200": free_two.id}
    registered = {"HQ100": held[0].id, "HQ200": held[1].id}
    assert whole(keep, 2) == {frozenset(expected.items())}
    assert whole(ignore, 4) == {frozenset({**expected, **registered}.items())}
