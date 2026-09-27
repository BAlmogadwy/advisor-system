"""Linked exams through the API: Build, multistart, Check, Save, Optimise, Fix and Copy.

In this release links are reachable only through the request's ``linked_exams``
(the page gains its editor next). A run saves its links next to its pins; every
loaded action defaults to the loaded run's links, as it does to its pins; a
board that splits a link is refused with the field that says which; and a
student registered in two linked courses is reported as the clash it is.
"""

import itertools
import random
from copy import deepcopy

import pytest
from django.core.cache import cache
from django.urls import reverse

from core import models
from core.models import (
    Course,
    ExamTimetableRun,
    ProgrammeRequirement,
    Room,
    Student,
)
from core.services.exam_multistart import run_multistart
from core.services.exam_run_schema import load_normalised_run, normalise_exam_run_payload
from core.services.exam_timetable import build_enrolled_sets_with_meta, build_exam_timetable
from core.services.linked_exams import resolve_linked_exams
from tests import exam_linked_parity_corpus as corpus
from tests.exam_source_factory import scraped_exam_registration

pytestmark = pytest.mark.django_db

DAYS = ["Sun", "Mon", "Tue"]
PERIODS = ["08:00-10:00", "13:00-15:00"]
SCOPE = {"programs": ["AI", "CS"], "sections": ["F", "M"]}

#: (program, code, plan name, plan term). PHYS103's two plan names make it two
#: exams, "PHYS103 (1)" and "PHYS103 (2)"; AI212 and AI225 are one course
#: under two codes, carrying different credits.
PLAN = [
    ("AI", "AI212", "Intro to AI", 3),
    ("AI", "CS101", "Programming", 1),
    ("AI", "MATH106", "Calculus", 2),
    ("AI", "PHYS103", "Physics I", 1),
    ("CS", "AI225", "Intro to AI", 3),
    ("CS", "CS101", "Programming", 1),
    ("CS", "MATH106", "Calculus", 2),
    ("CS", "PHYS103", "General Physics", 1),
]
CREDITS = {"AI212": 3, "AI225": 4, "CS101": 3, "MATH106": 4, "PHYS103": 4}


def _populate(*, shared_student: bool = False) -> None:
    for code, credits in CREDITS.items():
        Course.objects.create(course_code=code, description=code, credit_hours=credits)
    for program, code, name, term in PLAN:
        ProgrammeRequirement.objects.create(
            program=program, course_code=code, course_name=name, programme_term=term
        )
    registrations = {
        "AI": {"AI212": range(8), "CS101": range(4), "MATH106": range(4, 8), "PHYS103": range(8)},
        "CS": {"AI225": range(8), "CS101": range(4), "MATH106": range(4, 8), "PHYS103": range(8)},
    }
    for offset, (program, courses) in enumerate(registrations.items()):
        for number in range(8):
            student = Student.objects.create(
                student_id=500 + offset * 100 + number,
                program=program,
                section="F" if number % 2 else "M",
            )
            for code, members in courses.items():
                if number in members:
                    scraped_exam_registration(student, Course.objects.get(course_code=code))
    if shared_student:
        # One AI student also registered in AI225: two papers at the link's one
        # time. The AI plan names AI225 too, or that student would make it a
        # second exam; it shares AI212's plan term, which a link may.
        ProgrammeRequirement.objects.create(
            program="AI", course_code="AI225", course_name="Intro to AI", programme_term=3
        )
        scraped_exam_registration(
            Student.objects.get(student_id=500), Course.objects.get(course_code="AI225")
        )
    for gender in ("F", "M"):
        for index in range(6):
            Room.objects.create(room_code=f"R{gender}{index}", capacity=30, section=gender)


@pytest.fixture
def population():
    _populate()


@pytest.fixture
def client_(client, django_user_model, monkeypatch):
    monkeypatch.setattr("core.authz._rate_buckets", {})
    cache.clear()
    client.force_login(django_user_model.objects.create_superuser(username="links-admin"))
    return client


def _identities() -> dict[str, str]:
    _enrolled, meta = build_enrolled_sets_with_meta(**SCOPE)
    return {code: data["course_identity"] for code, data in meta.items()}


def _physics() -> tuple[str, str]:
    codes = sorted(code for code in _identities() if code.startswith("PHYS103"))
    assert codes == ["PHYS103 (1)", "PHYS103 (2)"]
    return codes[0], codes[1]


def _link(*codes) -> dict:
    identities = _identities()
    return {
        "members": [{"course_identity": identities[code], "course_code": code} for code in codes]
    }


def _links() -> list[dict]:
    return [_link("AI225", "AI212"), _link(*_physics())]


def _post(client, payload, status=200, url="exam_timetable_build"):
    response = client.post(reverse(url), payload, content_type="application/json")
    assert response.status_code == status, response.content
    return response.json()


def _build(client, *, status=200, **changes):
    return _post(
        client,
        {
            "label": "Linked exams",
            "days": DAYS,
            "periods": PERIODS,
            "max_per_day": 2,
            **SCOPE,
            "assign_rooms": True,
            "pinned": [],
            "linked_exams": _links(),
            **changes,
        },
        status=status,
    )


def _loaded(client, built, board, *, mode=None, status=200, **changes):
    payload = {
        "label": "Linked exams",
        "days": DAYS,
        "periods": PERIODS,
        "max_per_day": 2,
        **SCOPE,
        "previous_run_id": built["run_id"],
        "base_schedule": board,
        "pinned": built["pinned"],
        **changes,
    }
    if mode is None:
        return _post(client, payload, status=status, url="exam_timetable_draft_impact")
    return _post(client, {**payload, "mode": mode}, status=status)


def _where(result) -> dict[str, tuple[str, str]]:
    return {entry["course_code"]: (entry["day"], entry["period"]) for entry in result["schedule"]}


def _together(result, *groups) -> bool:
    """One real day and period per group, or all of it in OVERFLOW at any index."""
    where = {
        entry["course_code"]: ("OVERFLOW",)
        if entry["day"] == "OVERFLOW"
        else (entry["day"], entry["period"])
        for entry in result["schedule"]
    }
    return all(len({where[code] for code in group}) == 1 for group in groups)


def _groups():
    return [("AI212", "AI225"), _physics()]


def _move(board, code, day, period):
    moved = deepcopy(board)
    for entry in moved:
        if entry["course_code"] == code:
            entry.update(day=day, period=period)
    return moved


def _free_slot(result):
    """A (day, period) no exam uses."""
    used = set(_where(result).values())
    return next(
        (day, period)
        for day, period in itertools.product(DAYS, PERIODS)
        if (day, period) not in used
    )


# ── Build ────────────────────────────────────────────────────────────────────


def test_a_build_seats_each_link_together_and_saves_it(client_, population):
    built = _build(client_)
    assert _together(built, *_groups())
    identities = _identities()
    first, second = _physics()
    assert built["linked_exams"] == [
        {
            "members": [
                {"course_identity": identities["AI212"], "course_code": "AI212"},
                {"course_identity": identities["AI225"], "course_code": "AI225"},
            ]
        },
        {
            "members": [
                {"course_identity": identities[first], "course_code": first},
                {"course_identity": identities[second], "course_code": second},
            ]
        },
    ]
    assert built["qa"]["linked_exams"] == {
        "links": 2,
        "courses": 4,
        "students_in_two_linked_courses": 0,
        "mixed_credit_links": 1,
        "online_courses": 0,
    }
    assert built["schema_version"] == 6
    saved = load_normalised_run(ExamTimetableRun.objects.get(pk=built["run_id"]))
    assert saved["linked_exams"] == built["linked_exams"]
    detail = client_.get(reverse("exam_timetable_detail", args=[built["run_id"]])).json()
    assert detail["linked_exams"] == built["linked_exams"]


def test_a_build_without_links_saves_none_and_reports_none(client_, population):
    built = _build(client_, linked_exams=[])
    assert built["linked_exams"] == []
    assert "linked_exams" not in built["qa"]


def test_links_leave_every_other_fact_per_real_course(client_, population):
    """Cards, section groups and fingerprints stay one per real course."""
    built = _build(client_)
    codes = {entry["course_code"] for entry in built["schedule"]}
    assert {"AI212", "AI225", *_physics()} <= codes
    assert set(built["section_enrollment"]) == codes
    unlinked = _build(client_, linked_exams=[])
    assert built["section_enrollment"] == unlinked["section_enrollment"]
    assert built["input_fingerprint"] == unlinked["input_fingerprint"], "Links are not an input"


@pytest.mark.parametrize(
    ("links", "code", "field"),
    [
        ("AI212", "linked_exams_invalid", "linked_exams"),
        ([{"members": "AI212"}], "linked_exams_invalid", "linked_exams[0].members"),
        (lambda: [_link("AI212")], "linked_exams_too_few_members", "linked_exams[0].members"),
        (
            lambda: [_link("AI212", "AI225"), _link("CS101", "AI212")],
            "linked_exams_course_repeated",
            "linked_exams[1].members[1].course_identity",
        ),
        (
            lambda: [
                {
                    "members": [
                        _link("AI212")["members"][0],
                        {"course_identity": "EE999::nothing", "course_code": "EE999"},
                    ]
                }
            ],
            "linked_exams_course_not_selected",
            "linked_exams[0].members[1].course_identity",
        ),
    ],
    ids=["not-a-list", "members-not-a-list", "one-member", "course-in-two-links", "unknown"],
)
def test_a_build_refuses_bad_links_by_field_and_saves_nothing(
    client_, population, links, code, field
):
    body = _build(client_, linked_exams=links() if callable(links) else links, status=400)
    assert (body["code"], body["field"]) == (code, field)
    assert not ExamTimetableRun.objects.exists()


def test_a_build_refuses_a_link_to_a_course_it_did_not_select(client_, population):
    selected = sorted(code for code in _identities() if code != "AI225")
    body = _build(client_, selected_courses=selected, status=400)
    # The request names AI225 first; the refusal points at what was sent.
    assert (body["code"], body["field"]) == (
        "linked_exams_course_not_selected",
        "linked_exams[0].members[0].course_identity",
    )


def test_a_build_refuses_linked_exams_pinned_apart(client_, population):
    pins = [
        {"course_code": "AI212", "day": "Sun", "period": PERIODS[0]},
        {"course_code": "AI225", "day": "Mon", "period": PERIODS[0]},
    ]
    body = _build(client_, pinned=pins, status=400)
    assert (body["code"], body["field"]) == ("linked_exams_pins_disagree", "linked_exams[0]")
    assert not ExamTimetableRun.objects.exists()


def test_one_pinned_member_takes_its_link_with_it(client_, population):
    pins = [{"course_code": "AI225", "day": "Tue", "period": PERIODS[1]}]
    built = _build(client_, pinned=pins)
    assert _where(built)["AI212"] == _where(built)["AI225"] == ("Tue", PERIODS[1])


# ── multistart ───────────────────────────────────────────────────────────────


def test_every_multistart_candidate_seats_its_links_together(population):
    report = run_multistart(
        label="Linked multistart",
        days=DAYS,
        periods=PERIODS,
        **SCOPE,
        linked_exams=_links(),
        seeds=[1, 2, 3],
    )
    assert report.candidates_by_role
    for candidate in report.candidates_by_role.values():
        assert _together(candidate.payload, *_groups())
    for run in ExamTimetableRun.objects.all():
        saved = load_normalised_run(run)
        assert _together(saved, *_groups())
        assert len(saved["linked_exams"]) == 2


def test_multistart_refuses_bad_links_by_field_and_saves_nothing(client_, population, settings):
    settings.TIMETABLE_EXAM_MULTISTART_ENABLED = True
    body = _build(client_, multistart=True, n_runs=2, linked_exams=[_link("AI212")], status=400)
    assert (body["code"], body["field"]) == (
        "linked_exams_too_few_members",
        "linked_exams[0].members",
    )
    assert not ExamTimetableRun.objects.exists()


# ── loaded actions ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "mode", [None, "save_loaded_changes", "optimize_loaded", "minimum_change_repair"]
)
def test_a_board_that_splits_a_saved_link_is_refused(client_, population, mode):
    """The page sends no links yet, so every action takes the loaded run's."""
    built = _build(client_)
    day, period = _free_slot(built)
    split = _move(built["schedule"], "AI225", day, period)
    runs = ExamTimetableRun.objects.count()
    body = _loaded(client_, built, split, mode=mode, status=400)
    assert (body["code"], body["field"]) == ("linked_exams_split", "linked_exams[0]")
    assert ExamTimetableRun.objects.count() == runs


def test_links_sent_with_the_request_replace_the_saved_ones(client_, population):
    built = _build(client_)
    day, period = _free_slot(built)
    split = _move(built["schedule"], "AI225", day, period)
    checked = _loaded(client_, built, split, linked_exams=[])
    assert checked["linked_exams"] == []
    saved = _loaded(client_, built, split, mode="save_loaded_changes", linked_exams=[])
    assert (
        load_normalised_run(ExamTimetableRun.objects.get(pk=saved["run_id"]))["linked_exams"] == []
    )


def test_a_link_moved_whole_is_checked_and_saved(client_, population):
    built = _build(client_)
    day, period = _free_slot(built)
    moved = _move(_move(built["schedule"], "AI225", day, period), "AI212", day, period)
    checked = _loaded(client_, built, moved)
    assert checked["linked_exams"] == built["linked_exams"]
    assert checked["qa"]["linked_exams"] == built["qa"]["linked_exams"]
    saved = _loaded(client_, built, moved, mode="save_loaded_changes")
    assert _where(saved)["AI212"] == _where(saved)["AI225"] == (day, period)
    assert (
        load_normalised_run(ExamTimetableRun.objects.get(pk=saved["run_id"]))["linked_exams"]
        == built["linked_exams"]
    )


@pytest.mark.parametrize("numbered", [True, False], ids=["fresh-indices", "no-index"])
def test_a_link_wholly_in_overflow_is_together_at_any_index(client_, population, numbered):
    """Pinning an unrelated exam renumbers every OVERFLOW entry on the page, and
    an entry sent without an index is given the next free one."""
    built = _build(client_)
    board = deepcopy(built["schedule"])
    slot_count = len(DAYS) * len(PERIODS)
    for offset, entry in enumerate(e for e in board if e["course_code"] in ("AI212", "AI225")):
        entry.update(day="OVERFLOW", period=f"Extra-{slot_count + offset * 3}")
        if numbered:
            entry["slot_index"] = slot_count + offset * 3
        else:
            entry.pop("slot_index", None)
    checked = _loaded(client_, built, board)
    assert {
        checked_entry["day"]
        for checked_entry in checked["schedule"]
        if checked_entry["course_code"] in ("AI212", "AI225")
    } == {"OVERFLOW"}
    saved = _loaded(client_, built, board, mode="save_loaded_changes")
    assert saved["linked_exams"] == built["linked_exams"]


def test_optimise_keeps_links_together_and_saves_them(client_, population):
    built = _build(client_)
    optimised = _loaded(client_, built, built["schedule"], mode="optimize_loaded")
    assert _together(optimised, *_groups())
    assert optimised["linked_exams"] == built["linked_exams"]


def test_fix_moves_a_link_whole_when_the_registrar_drags_onto_it(client_, population):
    """CS101 shares students with both AI212 and AI225. Dragged onto the link's
    period it is the registrar's; the link is what steps aside - both courses."""
    built = _build(client_, linked_exams=[_link("AI212", "AI225")])
    day, period = _where(built)["AI212"]
    dragged = _move(built["schedule"], "CS101", day, period)
    repaired = _loaded(client_, built, dragged, mode="minimum_change_repair")
    assert _where(repaired)["CS101"] == (day, period)
    assert _where(repaired)["AI212"] == _where(repaired)["AI225"] != (day, period)
    moved = [move["course_code"] for move in repaired["minimum_change"]["moves"]]
    assert {"AI212", "AI225"} <= set(moved)
    assert repaired["linked_exams"] == built["linked_exams"]
    assert repaired["minimum_change"]["linked_clash_students"] == 0


def test_copy_carries_the_links_and_its_actions_use_them(client_, population):
    built = _build(client_)
    response = client_.post(
        reverse("exam_timetable_copy", args=[built["run_id"]]), {}, content_type="application/json"
    )
    assert response.status_code == 201, response.content
    copied = response.json()
    assert copied["linked_exams"] == built["linked_exams"]
    detail = client_.get(reverse("exam_timetable_detail", args=[copied["run_id"]])).json()
    assert detail["linked_exams"] == built["linked_exams"]
    day, period = _free_slot(built)
    split = _move(built["schedule"], "AI212", day, period)
    body = _loaded(client_, {**built, "run_id": copied["run_id"]}, split, status=400)
    assert body["code"] == "linked_exams_split"


# ── students in two linked courses ───────────────────────────────────────────


def test_a_student_in_two_linked_courses_is_a_linked_exam_clash(client_):
    _populate(shared_student=True)
    built = _build(client_, linked_exams=[_link("AI212", "AI225")])
    rows = [
        row for row in built["qa"]["manual_override_details"] if row["kind"] == "linked_same_slot"
    ]
    assert [(row["student_id"], row["courses"]) for row in rows] == [(500, ["AI212", "AI225"])]
    assert built["qa"]["linked_exams"]["students_in_two_linked_courses"] == 1
    assert "manual_override" in built["status_flags"]
    repaired = _loaded(client_, built, built["schedule"], mode="minimum_change_repair")
    assert repaired["saved"] is False, "Separating linked courses is not a repair"
    assert repaired["minimum_change"]["linked_clash_students"] == 1


# ── the saved shape ──────────────────────────────────────────────────────────


def test_a_run_saved_before_links_existed_has_none():
    migrated = normalise_exam_run_payload(
        {"schema_version": 5, "status": "ok", "schedule": [], "pinned": []}
    )
    assert migrated["schema_version"] == 6
    assert migrated["linked_exams"] == []
    assert normalise_exam_run_payload(migrated) == migrated
    kept = normalise_exam_run_payload(
        {"schema_version": 6, "status": "ok", "linked_exams": [{"members": []}]}
    )
    assert kept["linked_exams"] == [{"members": []}], "A current run's links are its own"


# ── randomised: real builds never split a link ───────────────────────────────


@pytest.mark.parametrize("seed", range(8))
def test_no_real_build_optimise_fix_or_multistart_splits_a_link(seed):
    """The parity population, with random links and pins, through every solver."""
    corpus.create_population(models)
    rng = random.Random(4400 + seed)
    _enrolled, meta = build_enrolled_sets_with_meta(programs=["AI", "CS"])
    codes = sorted(meta)
    picked = rng.sample(codes, 5)
    groups = [tuple(sorted(picked[:2])), tuple(sorted(picked[2:5]))]
    raw = [
        {"members": [{"course_identity": meta[code]["course_identity"]} for code in group]}
        for group in groups
    ]
    slots = list(itertools.product(corpus.POPULATION_DAYS, corpus.POPULATION_PERIODS))
    day, period = rng.choice(slots)
    pins = [{"course_code": rng.choice(groups[rng.randrange(2)]), "day": day, "period": period}]
    common = {
        "days": corpus.POPULATION_DAYS,
        "periods": corpus.POPULATION_PERIODS,
        "max_per_day": 2,
        "programs": ["AI", "CS"],
        "pinned": pins,
        "linked_exams": raw,
        "thin_conflict_threshold": 0,
    }
    links = resolve_linked_exams(raw, meta)
    built = build_exam_timetable(
        "Random links", seed=rng.randint(0, 99), assign_rooms=True, persist=False, **common
    )
    assert links.together(built["schedule"]), (groups, _where(built))
    assert _where(built)[pins[0]["course_code"]] == (day, period)

    from core import exam_views

    loaded = {
        **{
            key: common[key] for key in ("days", "periods", "max_per_day", "pinned", "linked_exams")
        },
        "schedule_raw": built["schedule"],
        "selected_courses": [entry["course_code"] for entry in built["schedule"]],
        "assign_rooms": True,
        "seed": rng.randint(0, 99),
        "thin_conflict_threshold": common["thin_conflict_threshold"],
        "programs": ["AI", "CS"],
        "sections": [],
    }
    optimised = exam_views._optimise_loaded_schedule(
        label="Random optimise", save=lambda _label, _result: 1, **loaded
    )
    assert links.together(optimised["schedule"]), groups
    # Drag an unlinked exam that shares students with a link onto that link, then Fix.
    placed = _where(built)
    # The unpinned link, so the repair is free to move it.
    group = next(g for g in groups if pins[0]["course_code"] not in g)
    neighbours = {
        row["course_a"] if row["course_b"] in group else row["course_b"]
        for row in built["conflicts"]
        if {row["course_a"], row["course_b"]} & set(group)
    }
    loose = sorted(
        code
        for code in neighbours
        if links.unit(code) == code
        and placed[code][0] != "OVERFLOW"
        and code != pins[0]["course_code"]
    )
    assert loose and placed[group[0]][0] != "OVERFLOW", "Every seed must drag"
    if loose:
        dragged = _move(built["schedule"], rng.choice(loose), *placed[group[0]])
        fixed = exam_views._minimum_change_schedule(
            label="Random fix",
            source_placements=placed,
            carried_protection=[],
            save=lambda _label, _result: 1,
            **{**loaded, "schedule_raw": dragged},
        )
        assert "schedule" in fixed, "The drag was meant to need a repair"
        assert links.together(fixed["schedule"]), groups
    report = run_multistart(label="Random multistart", seeds=[1, 2], **common)
    for candidate in report.candidates_by_role.values():
        assert links.together(candidate.payload["schedule"]), groups
