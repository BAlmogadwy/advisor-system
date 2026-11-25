"""Several allowed sections for one course in the planner build ("only these").

A shortlist item's ``pinned_sections`` may hold one to six distinct sections; the
build places the course in exactly one of them. ONE entry is the historical pin
and must give byte-for-byte what it always gave: the equivalence tests below
compare the whole reply against an expectation recorded before the builder was
changed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from core.models import Student, TermSection, TermSectionMeeting
from core.services.planner_builder import build_plans

# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


def _section(
    course_key: str,
    section: str,
    *,
    day: str = "SUN",
    start: str = "09:00",
    end: str = "10:00",
) -> TermSection:
    row = TermSection.objects.create(
        course_code=course_key,
        course_number="",
        course_key=course_key,
        course_name=f"{course_key} Builder Test",
        section=section,
        available_capacity=30,
        registered_count=0,
    )
    TermSectionMeeting.objects.create(term_section=row, day=day, start_time=start, end_time=end)
    return row


def _base(code: str, credits: int = 3, **extra: Any) -> dict[str, Any]:
    return {"course_code": code, "credits": credits, "status": "Eligible", **extra}


def _pins(*rows: TermSection) -> list[dict[str, Any]]:
    return [{"term_section_id": r.id, "section": r.section} for r in rows]


def _build(shortlist: list[dict[str, Any]], baseline: list[dict[str, Any]] | None = None) -> dict:
    return build_plans(
        "1448",
        "1",
        shortlist,
        baseline or [],
        bool(baseline),
        max_credits=18,
        gender="M",
        program=None,
    )


def _ids(option: dict) -> dict[str, int]:
    return {str(m["course_code"]): int(m["term_section_id"]) for m in option["mappings"]}


def _baseline_row(day: str, start: str, end: str) -> dict[str, Any]:
    return {
        "course_key": "BASE101",
        "section": "M1",
        "day": day,
        "start_time": start,
        "end_time": end,
    }


def _normalise(value: Any, names: dict[int, str], key: str = "") -> Any:
    """Replace database ids by stable labels so a reply can be recorded.

    Only the id-bearing keys are touched (a count that happens to equal an id
    must stay a count), and the mappings of one option are put in course order
    because the builder lists them in database-id order.
    """
    if isinstance(value, dict):
        return {k: _normalise(v, names, k) for k, v in value.items()}
    if isinstance(value, list):
        rows = [_normalise(v, names, key) for v in value]
        if key == "mappings":
            rows.sort(key=lambda m: str(m["course_code"]))
        return rows
    if key in ("term_section_id", "term_section_ids") and isinstance(value, int):
        return names.get(value, value)
    return value


def _labelled(*rows: TermSection) -> dict[int, str]:
    return {r.id: f"id:{r.course_key}/{r.section}" for r in rows}


# --------------------------------------------------------------------------
# single-pin equivalence: replies recorded against the pre-change builder
# --------------------------------------------------------------------------


def _single_pin_success() -> dict:
    _section("ENGL214", "M1", day="SUN")
    pinned = _section("ENGL214", "M2", day="MON")
    _section("ENGL214", "M3", day="TUE")
    fe_a = _section("FE1", "M1", day="MON", start="09:30", end="10:30")
    fe_b = _section("FE1", "M2", day="WED")
    other = _section("AI301", "M1", day="THU")
    rows = list(TermSection.objects.all())
    result = _build(
        [
            _base("ENGL214", must_take=True, pinned_sections=_pins(pinned)),
            _base("FE1", credits=2),
            _base("AI301", credits=4),
        ]
    )
    assert fe_a and fe_b and other
    return _normalise(result, _labelled(*rows))


def _single_pin_must_take_clashing_with_baseline() -> dict:
    pinned = _section("ENGL214", "M2", day="MON")
    _section("ENGL214", "M1", day="SUN")
    _section("FE1", "M1", day="TUE")
    rows = list(TermSection.objects.all())
    result = _build(
        [
            _base("ENGL214", must_take=True, pinned_sections=_pins(pinned)),
            _base("FE1", credits=2),
        ],
        baseline=[_baseline_row("MON", "09:30", "10:30")],
    )
    return _normalise(result, _labelled(*rows))


def _single_pin_optional_clashing_with_baseline() -> dict:
    pinned = _section("AI331", "M2", day="MON")
    _section("AI331", "M1", day="SUN")
    _section("REQ101", "M1", day="TUE")
    rows = list(TermSection.objects.all())
    result = _build(
        [
            _base("REQ101", must_take=True),
            _base("AI331", credits=4, pinned_sections=_pins(pinned)),
        ],
        baseline=[_baseline_row("MON", "09:30", "10:30")],
    )
    return _normalise(result, _labelled(*rows))


#: Recorded from the builder BEFORE it learned about several allowed sections.
_RECORDED = json.loads(
    (Path(__file__).parent / "fixtures" / "planner_single_pin_reply.json").read_text(
        encoding="utf-8"
    )
)
EXPECTED_SINGLE_PIN_SUCCESS: dict = _RECORDED["SUCCESS"]
EXPECTED_SINGLE_PIN_MUST_TAKE_CLASH: dict = _RECORDED["MUST_TAKE_CLASH"]
EXPECTED_SINGLE_PIN_OPTIONAL_CLASH: dict = _RECORDED["OPTIONAL_CLASH"]


@pytest.mark.django_db
def test_single_pin_reply_is_unchanged_when_the_pin_is_usable() -> None:
    assert _single_pin_success() == EXPECTED_SINGLE_PIN_SUCCESS


@pytest.mark.django_db
def test_single_pin_reply_is_unchanged_for_a_must_take_that_cannot_be_placed() -> None:
    assert _single_pin_must_take_clashing_with_baseline() == EXPECTED_SINGLE_PIN_MUST_TAKE_CLASH


@pytest.mark.django_db
def test_single_pin_reply_is_unchanged_for_an_optional_pin_that_cannot_be_placed() -> None:
    assert _single_pin_optional_clashing_with_baseline() == EXPECTED_SINGLE_PIN_OPTIONAL_CLASH


# --------------------------------------------------------------------------
# validation in the build view
# --------------------------------------------------------------------------

_ABSENT = object()


@pytest.fixture(autouse=True)
def _fresh_rate_limit_counters() -> None:
    """The build view is throttled per user pk in a process-wide dict; other files share it."""
    from core import authz

    authz._rate_buckets.clear()


def _post_pins(monkeypatch: pytest.MonkeyPatch, pins: object) -> tuple[Any, list]:
    from tests.test_term_section_programmes import _staff_planner_client

    student, _ = Student.objects.get_or_create(
        student_id=910321, defaults={"name": "Only These", "program": "AI", "section": "M"}
    )
    seen: list[Any] = []

    def fake_build_plans(*args: Any, **kwargs: Any) -> dict[str, object]:
        seen.append(args[2])
        return {"summary": {}, "options": [], "swap_suggestions": []}

    monkeypatch.setattr("core.planner_views.build_plans", fake_build_plans)
    item: dict[str, Any] = {"course_code": "ENGL214", "credits": 3, "status": "Eligible"}
    if pins is not _ABSENT:
        item["pinned_sections"] = pins
    response = _staff_planner_client("only-these-validation").post(
        "/ops/planner/build/",
        data=json.dumps(
            {
                "student_id": student.student_id,
                "academic_year": "1448",
                "term": "1",
                "mode": "keep",
                "program_sections_only": False,
                "shortlist": [item],
                "baseline": [],
            }
        ),
        content_type="application/json",
    )
    return response, seen


def _entries(n: int) -> list[dict[str, Any]]:
    return [{"term_section_id": 500 + i, "section": f"M{i}"} for i in range(n)]


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("pins", "kept"),
    [
        (_ABSENT, 0),
        ([], 0),
        (_entries(1), 1),
        (_entries(2), 2),
        (_entries(6), 6),
    ],
)
def test_view_accepts_zero_to_six_distinct_allowed_sections(
    monkeypatch: pytest.MonkeyPatch, pins: object, kept: int
) -> None:
    response, seen = _post_pins(monkeypatch, pins)
    assert response.status_code == 200
    assert [len(item["pinned_sections"]) for item in seen[0]] == [kept]
    if kept:
        assert [p["term_section_id"] for p in seen[0][0]["pinned_sections"]] == [
            500 + i for i in range(kept)
        ]


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("pins", "code"),
    [
        (_entries(7), "VALIDATION_PINNED_SECTION_COUNT"),
        (_entries(12), "VALIDATION_PINNED_SECTION_COUNT"),
        (
            [{"term_section_id": 500, "section": "M1"}, {"term_section_id": "500", "section": "x"}],
            "VALIDATION_PINNED_SECTION_DUPLICATE",
        ),
        (
            [{"term_section_id": 500}, {"term_section_id": 500}],
            "VALIDATION_PINNED_SECTION_DUPLICATE",
        ),
        ([{"section": "M1"}], "VALIDATION_PINNED_SECTION"),
        (["x"], "VALIDATION_PINNED_SECTION"),
        ([{"term_section_id": "abc"}], "VALIDATION_PINNED_SECTION"),
        ([{"term_section_id": -3}], "VALIDATION_PINNED_SECTION"),
        ({"term_section_id": 5}, "VALIDATION_PINNED_SECTIONS"),
    ],
)
def test_view_refuses_bad_allowed_section_lists(
    monkeypatch: pytest.MonkeyPatch, pins: object, code: str
) -> None:
    response, seen = _post_pins(monkeypatch, pins)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == code
    assert seen == []


@pytest.mark.django_db
def test_view_count_message_states_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    response, _ = _post_pins(monkeypatch, _entries(7))
    assert "6" in response.json()["error"]["message"]


# --------------------------------------------------------------------------
# builder: several allowed sections
# --------------------------------------------------------------------------


def _failures_for(result: dict, code: str) -> list[dict]:
    return [
        f for f in result["summary"]["hard_constraint_failures"] if f.get("course_code") == code
    ]


@pytest.mark.django_db
def test_second_allowed_section_is_used_when_the_first_clashes_with_a_registered_class() -> None:
    first = _section("ENGL214", "M1", day="MON", start="09:00", end="10:00")
    second = _section("ENGL214", "M2", day="TUE")
    third = _section("ENGL214", "M3", day="WED")
    result = _build(
        [_base("ENGL214", must_take=True, pinned_sections=_pins(first, second))],
        baseline=[_baseline_row("MON", "09:30", "10:30")],
    )
    assert result["options"]
    assert {_ids(o)["ENGL214"] for o in result["options"]} == {second.id}
    assert third.id not in {_ids(o)["ENGL214"] for o in result["options"]}


@pytest.mark.django_db
def test_first_allowed_section_is_used_when_the_second_clashes_with_a_registered_class() -> None:
    first = _section("ENGL214", "M1", day="TUE")
    second = _section("ENGL214", "M2", day="MON", start="09:00", end="10:00")
    result = _build(
        [_base("ENGL214", must_take=True, pinned_sections=_pins(first, second))],
        baseline=[_baseline_row("MON", "09:30", "10:30")],
    )
    assert result["options"]
    assert {_ids(o)["ENGL214"] for o in result["options"]} == {first.id}


@pytest.mark.django_db
def test_allowed_section_clashing_with_a_must_take_courses_only_section_is_skipped() -> None:
    clash = _section("ENGL214", "M1", day="MON", start="09:00", end="10:00")
    free = _section("ENGL214", "M2", day="TUE")
    _section("REQ101", "M1", day="MON", start="09:30", end="10:30")
    result = _build(
        [
            _base("REQ101", must_take=True),
            _base("ENGL214", must_take=True, pinned_sections=_pins(clash, free)),
        ]
    )
    assert result["options"]
    assert all(_ids(o)["ENGL214"] == free.id for o in result["options"])


@pytest.mark.django_db
def test_no_allowed_section_usable_is_reported_as_one_unusable_pin_is() -> None:
    a = _section("ENGL214", "M1", day="MON", start="09:00", end="10:00")
    b = _section("ENGL214", "M2", day="MON", start="09:15", end="10:15")
    _section("ENGL214", "M3", day="TUE")  # exists but is not allowed
    baseline = [_baseline_row("MON", "09:30", "10:30")]
    single = _build([_base("ENGL214", must_take=True, pinned_sections=_pins(a))], baseline)
    several = _build([_base("ENGL214", must_take=True, pinned_sections=_pins(a, b))], baseline)
    assert several["options"] == [] and single["options"] == []
    assert several["summary"] == single["summary"]
    assert _failures_for(several, "ENGL214")


@pytest.mark.django_db
def test_optional_course_with_no_usable_allowed_section_is_omitted_not_swapped() -> None:
    a = _section("AI331", "M1", day="MON", start="09:00", end="10:00")
    b = _section("AI331", "M2", day="MON", start="09:15", end="10:15")
    _section("AI331", "M3", day="TUE")
    _section("REQ101", "M1", day="WED")
    result = _build(
        [
            _base("REQ101", must_take=True),
            _base("AI331", credits=4, pinned_sections=_pins(a, b)),
        ],
        baseline=[_baseline_row("MON", "09:30", "10:30")],
    )
    assert result["options"]
    assert all("AI331" not in _ids(o) for o in result["options"])


@pytest.mark.django_db
def test_both_allowed_sections_usable_and_different_options_use_different_ones() -> None:
    first = _section("ENGL214", "M1", day="MON")
    second = _section("ENGL214", "M2", day="TUE")
    third = _section("ENGL214", "M3", day="WED")
    result = _build([_base("ENGL214", must_take=True, pinned_sections=_pins(first, second))])
    used = {_ids(o)["ENGL214"] for o in result["options"]}
    assert used == {first.id, second.id}
    assert third.id not in used
    assert all(len(_ids(o)) == 1 for o in result["options"])


@pytest.mark.django_db
def test_a_listed_section_of_another_course_never_places_that_course() -> None:
    mine = _section("ENGL214", "M1", day="MON")
    _section("ENGL214", "M2", day="TUE")
    foreign = _section("FE1", "M1", day="WED")
    result = _build(
        [
            _base("ENGL214", must_take=True, pinned_sections=_pins(mine, foreign)),
            _base("FE1", credits=2),
        ]
    )
    assert result["options"]
    for option in result["options"]:
        assert _ids(option)["ENGL214"] == mine.id
        assert _ids(option).get("FE1", foreign.id) == foreign.id
