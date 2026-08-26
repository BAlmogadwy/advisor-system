from __future__ import annotations

import pytest

from core.models import TermSection, TermSectionMeeting
from core.services import planner_builder
from core.services.planner_builder import (
    Meeting,
    _known_full_section,
    _overlap,
    _remaining_capacity,
    _to_minutes,
    build_plans,
)


def test_to_minutes_handles_malformed_times() -> None:
    """Regression: a dirty free-text time must not raise (it 500'd the build)."""
    assert _to_minutes("08:30") == 510
    assert _to_minutes("00:00") == 0
    for bad in ("8:00 AM", "0800", "", "8.30", "x:y"):
        assert _to_minutes(bad) == -1
    assert _to_minutes(None) == -1  # type: ignore[arg-type]


def test_overlap_treats_malformed_meeting_as_non_conflicting() -> None:
    good = Meeting(day="MON", start="08:00", end="10:00")
    overlapping = Meeting(day="MON", start="09:00", end="11:00")
    malformed = Meeting(day="MON", start="8:00 AM", end="10:00")
    assert _overlap(good, overlapping) is True
    assert _overlap(good, malformed) is False  # bad data -> no false conflict, no crash


@pytest.mark.django_db
def test_build_plans_survives_malformed_baseline_time() -> None:
    """One malformed time anywhere must not 500 the whole plan build."""
    baseline = [
        {
            "course_key": "CS101",
            "section": "M1",
            "day": "MON",
            "start_time": "8:00 AM",  # malformed
            "end_time": "10:00",
            "term_section_id": 1,
        }
    ]
    result = build_plans("1448", "1", [], baseline, True)
    assert "options" in result
    assert "summary" in result


@pytest.mark.parametrize(
    ("maximum", "registered", "remaining", "full"),
    [
        (30, 29, 1, False),
        (30, 30, 0, True),
        (30, 31, 0, True),
        (0, 0, 0, True),
        (None, None, None, False),
        (30, None, None, False),
    ],
)
def test_capacity_contract_uses_maximum_minus_registered(
    maximum: int | None,
    registered: int | None,
    remaining: int | None,
    full: bool,
) -> None:
    section = {"available_capacity": maximum, "registered_count": registered}
    assert _remaining_capacity(section) == remaining
    assert _known_full_section(section) is full


#: Distinct slots so the four capacity fixtures are real alternatives rather
#: than interchangeable clones.
_CAPACITY_SLOTS = {
    "M1": ("SUN", "09:00", "09:50"),
    "M2": ("MON", "09:00", "09:50"),
    "M3": ("TUE", "09:00", "09:50"),
    "M4": ("WED", "09:00", "09:50"),
}


def _planner_section(
    section: str,
    *,
    maximum: int | None,
    registered: int | None,
) -> TermSection:
    """A capacity fixture WITH a real meeting.

    These sections used to be created with no meetings at all, which the
    builder then treated as free all week — so this test passed only because
    phantom sections were schedulable. That is the defect the completeness gate
    now closes, so the fixture has to describe a section that could genuinely
    be timetabled; otherwise the test asserts the bug instead of capacity.
    """
    row = TermSection.objects.create(
        course_code="CS",
        course_number="200",
        course_key="CS200",
        course_name="Planner Capacity Test",
        section=section,
        available_capacity=maximum,
        registered_count=registered,
    )
    day, start, end = _CAPACITY_SLOTS.get(section, ("SUN", "09:00", "09:50"))
    TermSectionMeeting.objects.create(term_section=row, day=day, start_time=start, end_time=end)
    return row


def _builder_section(
    course_key: str,
    section: str,
    *,
    day: str = "SUN",
    start: str = "09:00",
    end: str = "10:00",
    maximum: int | None = 30,
    registered: int | None = 0,
) -> TermSection:
    row = TermSection.objects.create(
        course_code=course_key,
        course_number="",
        course_key=course_key,
        course_name=f"{course_key} Builder Test",
        section=section,
        available_capacity=maximum,
        registered_count=registered,
    )
    TermSectionMeeting.objects.create(
        term_section=row,
        day=day,
        start_time=start,
        end_time=end,
    )
    return row


def _option_ids_by_course(option: dict) -> dict[str, int]:
    return {
        str(mapping["course_code"]): int(mapping["term_section_id"])
        for mapping in option.get("mappings", [])
    }


def _assert_every_method_returned(result: dict) -> None:
    methods = {str(option.get("method")) for option in result.get("options", [])}
    assert methods == {"A", "B", "C"}


def _mapped_section_ids(result: dict, method: str) -> set[int]:
    return {
        int(mapping["term_section_id"])
        for option in result.get("options", [])
        if option.get("method") == method
        for mapping in option.get("mappings", [])
    }


@pytest.mark.django_db
def test_capacity_enforcement_excludes_known_full_sections_from_every_method() -> None:
    open_section = _planner_section("M1", maximum=30, registered=29)
    full_section = _planner_section("M2", maximum=30, registered=30)
    overfull_section = _planner_section("M3", maximum=30, registered=31)
    unknown_section = _planner_section("M4", maximum=None, registered=None)

    result = build_plans(
        "1448",
        "1",
        [{"course_code": "CS200", "credits": 3, "status": "Eligible"}],
        [],
        False,
        consider_capacity=True,
        gender="M",
        program=None,
    )

    for method in ("A", "B", "C"):
        mapped = _mapped_section_ids(result, method)
        assert open_section.id in mapped
        assert unknown_section.id in mapped
        assert full_section.id not in mapped
        assert overfull_section.id not in mapped


@pytest.mark.django_db
def test_allow_full_sections_makes_full_section_eligible_in_every_method() -> None:
    full_section = _planner_section("M1", maximum=30, registered=30)

    result = build_plans(
        "1448",
        "1",
        [{"course_code": "CS200", "credits": 3, "status": "Eligible"}],
        [],
        False,
        consider_capacity=False,
        gender="M",
        program=None,
    )

    for method in ("A", "B", "C"):
        assert _mapped_section_ids(result, method) == {full_section.id}


@pytest.mark.django_db
def test_must_take_course_is_present_in_every_top_k_option() -> None:
    required = _builder_section("ENGL214", "M1", day="SUN")
    _builder_section("FE1", "M1", day="MON")
    _builder_section("FE1", "M2", day="TUE")

    result = build_plans(
        "1448",
        "1",
        [
            {
                "course_code": "ENGL214",
                "credits": 3,
                "status": "Eligible",
                "must_take": True,
            },
            {"course_code": "FE1", "credits": 2, "status": "Eligible"},
        ],
        [],
        False,
        max_credits=18,
        gender="M",
        program=None,
    )

    assert result["summary"]["best_feasible"] is True
    _assert_every_method_returned(result)
    assert result["options"]
    assert all(
        _option_ids_by_course(option).get("ENGL214") == required.id for option in result["options"]
    )


@pytest.mark.django_db
def test_must_take_invariant_also_guards_no_ortools_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    required = _builder_section("REQ102", "M1", day="SUN")
    _builder_section("OPT102", "M1", day="MON")
    monkeypatch.setattr(planner_builder, "cp_model", None)

    result = build_plans(
        "1448",
        "1",
        [
            {
                "course_code": "REQ102",
                "credits": 3,
                "status": "Eligible",
                "must_take": True,
            },
            {"course_code": "OPT102", "credits": 3, "status": "Eligible"},
        ],
        [],
        False,
        max_credits=18,
        gender="M",
        program=None,
    )

    assert result["options"]
    assert all(
        _option_ids_by_course(option).get("REQ102") == required.id for option in result["options"]
    )


@pytest.mark.django_db
def test_must_take_with_pin_uses_exact_section_in_every_option() -> None:
    unpinned = _builder_section("ENGL214", "M1", day="SUN")
    pinned = _builder_section("ENGL214", "M2", day="MON")
    _builder_section("FE1", "M1", day="TUE")

    result = build_plans(
        "1448",
        "1",
        [
            {
                "course_code": "ENGL214",
                "credits": 3,
                "status": "Eligible",
                "must_take": True,
                "pinned_sections": [{"term_section_id": pinned.id, "section": "M2"}],
            },
            {"course_code": "FE1", "credits": 2, "status": "Eligible"},
        ],
        [],
        False,
        max_credits=18,
        gender="M",
        program=None,
    )

    _assert_every_method_returned(result)
    assert all(
        _option_ids_by_course(option).get("ENGL214") == pinned.id for option in result["options"]
    )
    assert unpinned.id not in {
        section_id
        for option in result["options"]
        for section_id in _option_ids_by_course(option).values()
    }


@pytest.mark.django_db
def test_optional_pinned_course_can_be_omitted_but_never_uses_another_section() -> None:
    _builder_section("REQ101", "M1", day="SUN")
    unpinned = _builder_section("AI331", "M1", day="MON")
    pinned = _builder_section("AI331", "M2", day="TUE")

    result = build_plans(
        "1448",
        "1",
        [
            {
                "course_code": "REQ101",
                "credits": 3,
                "status": "Eligible",
                "must_take": True,
            },
            {
                "course_code": "AI331",
                "credits": 4,
                "status": "Eligible",
                "pinned_sections": [{"term_section_id": pinned.id, "section": "M2"}],
            },
        ],
        [],
        False,
        max_credits=18,
        gender="M",
        program=None,
    )

    mapped = [_option_ids_by_course(option) for option in result["options"]]
    assert mapped
    assert any("AI331" in option for option in mapped)
    assert all(option.get("AI331", pinned.id) == pinned.id for option in mapped)
    assert all(unpinned.id not in option.values() for option in mapped)


@pytest.mark.django_db
def test_impossible_pinned_must_take_returns_no_partial_options() -> None:
    _builder_section("ENGL214", "M1", day="SUN")
    wrong_course_section = _builder_section("FE1", "M1", day="MON")

    result = build_plans(
        "1448",
        "1",
        [
            {
                "course_code": "ENGL214",
                "credits": 3,
                "status": "Eligible",
                "must_take": True,
                "pinned_sections": [{"term_section_id": wrong_course_section.id, "section": "M1"}],
            },
            {"course_code": "FE1", "credits": 2, "status": "Eligible"},
        ],
        [],
        False,
        max_credits=18,
        gender="M",
        program=None,
    )

    assert result["options"] == []
    assert result["summary"]["best_feasible"] is False
    assert result["summary"]["scheduled"] == 0
    assert result["summary"]["hard_constraint_failures"][0]["course_code"] == "ENGL214"


@pytest.mark.django_db
def test_baseline_clashing_must_take_returns_no_partial_options() -> None:
    _builder_section("ENGL214", "M1", day="SUN", start="09:00", end="10:00")
    _builder_section("FE1", "M1", day="MON")
    baseline = [
        {
            "course_key": "BASE101",
            "section": "M1",
            "day": "SUN",
            "start_time": "09:00",
            "end_time": "10:00",
        }
    ]

    result = build_plans(
        "1448",
        "1",
        [
            {
                "course_code": "ENGL214",
                "credits": 3,
                "status": "Eligible",
                "must_take": True,
            },
            {"course_code": "FE1", "credits": 2, "status": "Eligible"},
        ],
        baseline,
        True,
        max_credits=18,
        gender="M",
        program=None,
    )

    assert result["options"] == []
    assert result["summary"]["best_feasible"] is False


@pytest.mark.django_db
def test_must_take_does_not_override_max_credit_limit() -> None:
    _builder_section("REQ401", "M1", day="SUN")
    _builder_section("OPT201", "M1", day="MON")

    result = build_plans(
        "1448",
        "1",
        [
            {
                "course_code": "REQ401",
                "credits": 4,
                "status": "Eligible",
                "must_take": True,
            },
            {"course_code": "OPT201", "credits": 2, "status": "Eligible"},
        ],
        [],
        False,
        max_credits=3,
        gender="M",
        program=None,
    )

    assert result["options"] == []
    assert result["summary"]["best_feasible"] is False


@pytest.mark.django_db
def test_optional_course_is_dropped_before_must_take_at_credit_limit() -> None:
    required = _builder_section("REQ301", "M1", day="SUN")
    _builder_section("OPT301", "M1", day="MON")

    result = build_plans(
        "1448",
        "1",
        [
            {
                "course_code": "REQ301",
                "credits": 3,
                "status": "Eligible",
                "must_take": True,
            },
            {"course_code": "OPT301", "credits": 3, "status": "Eligible"},
        ],
        [],
        False,
        max_credits=3,
        gender="M",
        program=None,
    )

    _assert_every_method_returned(result)
    assert all(
        _option_ids_by_course(option) == {"REQ301": required.id} for option in result["options"]
    )


@pytest.mark.django_db
def test_full_pinned_must_take_respects_capacity_toggle() -> None:
    pinned = _builder_section("REQ201", "M1", day="SUN", maximum=30, registered=30)
    shortlist = [
        {
            "course_code": "REQ201",
            "credits": 3,
            "status": "Eligible",
            "must_take": True,
            "pinned_sections": [{"term_section_id": pinned.id, "section": "M1"}],
        }
    ]

    blocked = build_plans(
        "1448",
        "1",
        shortlist,
        [],
        False,
        consider_capacity=True,
        max_credits=18,
        gender="M",
        program=None,
    )
    allowed = build_plans(
        "1448",
        "1",
        shortlist,
        [],
        False,
        consider_capacity=False,
        max_credits=18,
        gender="M",
        program=None,
    )

    assert blocked["options"] == []
    assert blocked["summary"]["best_feasible"] is False
    _assert_every_method_returned(allowed)
    assert all(
        _option_ids_by_course(option) == {"REQ201": pinned.id} for option in allowed["options"]
    )
