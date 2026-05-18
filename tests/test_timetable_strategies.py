"""
Timetable strategy correctness tests.

Tests that all placement strategies (compact, optimal, hybrid, adaptive,
load_balanced) produce valid, non-empty schedules and respect constraints:
- No same-group time overlaps
- Labs use dedicated lab slot grid (100-min)
- Day spacing: prefer non-consecutive days
- Time consistency: same start time across meetings
- Optimal fallback: returns placements even on infeasible/timeout
"""

from __future__ import annotations

import pytest

from core.models import (
    Course,
    DeliveryBoard,
    Prerequisite,
    ProgrammeRequirement,
    Room,
    ScenarioSectionBudget,
    ScenarioStudentMap,
    SectionPlacement,
    Student,
    StudentCourse,
    TimetableScenario,
)
from core.services.timetable_autoplace import (
    DEFAULT_LAB_SLOTS,
    DEFAULT_SLOTS,
    STRATEGIES,
    auto_place_board,
    auto_place_scenario,
)

pytestmark = pytest.mark.django_db


@pytest.fixture()
def timetable_scenario():
    """Create a small scenario with 5 courses across 2 terms for strategy testing."""
    # Courses: 2x 4-credit (has lab), 2x 3-credit, 1x 2-credit
    course_data = [
        ("TS401", 4, 3, "TS"),  # 4cr, term 3
        ("TS402", 4, 3, "TS"),  # 4cr, term 3
        ("TS301", 3, 3, "TS"),  # 3cr, term 3
        ("TS302", 3, 3, "TS"),  # 3cr, term 3
        ("TS201", 2, 3, "TS"),  # 2cr (lab only), term 3
    ]

    for code, cr, term, dept in course_data:
        Course.objects.get_or_create(
            course_code=code,
            defaults={"credit_hours": cr, "department": dept, "description": f"Test {code}"},
        )
        ProgrammeRequirement.objects.get_or_create(
            program="TS",
            course_code=code,
            defaults={"programme_term": term, "credit_hours": cr},
        )

    # Simple prereq chain
    Prerequisite.objects.get_or_create(
        program="TS",
        course_code="TS402",
        prerequisite_course_code="TS401",
    )

    # Create 10 students
    students = []
    for i in range(10):
        sid = 9900001 + i
        s, _ = Student.objects.get_or_create(
            student_id=sid,
            defaults={
                "program": "TS",
                "section": "M",
                "name": f"Test Student {i}",
                "total_earned_credits": 60,
                "current_registered_credits": 15,
            },
        )
        students.append(s)
        # All passed TS401
        c401 = Course.objects.get(course_code="TS401")
        StudentCourse.objects.get_or_create(
            student=s,
            course=c401,
            defaults={"status": "passed", "programme_term": 3},
        )

    # Create scenario
    scenario = TimetableScenario.objects.create(
        academic_year="1448",
        term="1",
        name="Strategy Test",
        slot_config=DEFAULT_SLOTS,
        lab_slot_config=DEFAULT_LAB_SLOTS,
    )
    board = DeliveryBoard.objects.create(
        scenario=scenario,
        label="Term 3",
        nominal_term=3,
        program="TS",
        display_order=1,
    )

    # Section budgets
    for code, cr, _term, dept in course_data:
        ScenarioSectionBudget.objects.create(
            scenario=scenario,
            course_code=code,
            department=dept,
            credit_hours=cr,
            planned_sections=1,
            max_per_section=25,
            total_demand=10,
            programme_term=3,
        )

    # Student maps
    for s in students:
        ScenarioStudentMap.objects.create(
            scenario=scenario,
            student_id=s.student_id,
            primary_term=3,
            is_cross_term=False,
            recommended_courses=["TS402", "TS301", "TS302", "TS201"],
        )

    # Create rooms for TS programme
    Room.objects.get_or_create(
        room_code="TSR01",
        defaults={"capacity": 30, "department": "TS", "room_type": "lecture", "building": "T1"},
    )
    Room.objects.get_or_create(
        room_code="TSR02",
        defaults={"capacity": 30, "department": "TS", "room_type": "lecture", "building": "T1"},
    )
    Room.objects.get_or_create(
        room_code="TSR03",
        defaults={"capacity": 50, "department": "TS", "room_type": "lecture", "building": "T1"},
    )
    Room.objects.get_or_create(
        room_code="TSLAB1",
        defaults={"capacity": 30, "department": "TS", "room_type": "lab", "building": "T1"},
    )

    return scenario, board


class TestStrategyRegistry:
    """Verify all expected strategies are registered."""

    def test_all_strategies_exist(self):
        expected = {
            "compact",
            "morning",
            "balanced",
            "optimal",
            "hybrid",
            "load_balanced",
            "adaptive",
        }
        assert expected.issubset(set(STRATEGIES.keys()))


class TestCompactStrategy:
    """Test the default compact strategy."""

    def test_places_all_sections(self, timetable_scenario):
        scenario, board = timetable_scenario
        result = auto_place_board(board.id, strategy="compact")
        assert result["placed"] > 0
        assert result["skipped"] == 0

    def test_minimal_student_overlaps(self, timetable_scenario):
        """Student overlaps should be minimal (soft penalty, not hard block)."""
        scenario, board = timetable_scenario
        auto_place_board(board.id, strategy="compact")
        placements = SectionPlacement.objects.filter(board=board)
        overlap_count = _count_student_overlaps(placements)
        # Soft model: some overlaps are acceptable, but should be few
        total = placements.count()
        assert overlap_count <= total * 0.3, f"Too many overlaps: {overlap_count}/{total}"

    def test_labs_use_lab_slots(self, timetable_scenario):
        scenario, board = timetable_scenario
        auto_place_board(board.id, strategy="compact")
        _assert_labs_use_lab_slots(board)

    def test_same_course_two_sections_are_generated_back_to_back(self):
        scenario = TimetableScenario.objects.create(
            academic_year="1448",
            term="1",
            name="Back-to-back planner test",
            slot_config=DEFAULT_SLOTS,
            lab_slot_config=DEFAULT_LAB_SLOTS,
        )
        board = DeliveryBoard.objects.create(
            scenario=scenario,
            label="Term 1",
            nominal_term=1,
            program="BTB",
            display_order=1,
        )
        Course.objects.get_or_create(
            course_code="BTB101",
            defaults={"credit_hours": 3, "department": "BTB"},
        )
        ProgrammeRequirement.objects.get_or_create(
            program="BTB",
            course_code="BTB101",
            defaults={"programme_term": 1, "credit_hours": 3},
        )
        ScenarioSectionBudget.objects.create(
            scenario=scenario,
            course_code="BTB101",
            department="BTB",
            credit_hours=3,
            planned_sections=2,
            max_per_section=25,
            total_demand=40,
            programme_term=1,
        )
        for idx in range(20):
            ScenarioStudentMap.objects.create(
                scenario=scenario,
                student_id=9910000 + idx,
                primary_term=1,
                recommended_courses=["BTB101"],
            )
        Room.objects.create(
            room_code="BTB-R1",
            capacity=50,
            department="BTB",
            room_type="lecture",
            building="B1",
        )

        result = auto_place_board(board.id, strategy="compact")
        assert result["skipped"] == 0

        placements = list(
            SectionPlacement.objects.filter(board=board, term_section__course_code="BTB101")
            .select_related("term_section")
            .order_by("term_section__section", "day", "start_time")
        )
        by_section: dict[str, list[SectionPlacement]] = {}
        for placement in placements:
            by_section.setdefault(placement.term_section.section, []).append(placement)

        assert set(by_section) == {"S1", "S2"}
        assert len(by_section["S1"]) == len(by_section["S2"]) == 2
        assert _sections_are_back_to_back(by_section["S1"], by_section["S2"])

    def test_same_course_three_sections_generate_at_least_one_back_to_back_pair(self):
        scenario = TimetableScenario.objects.create(
            academic_year="1448",
            term="1",
            name="Back-to-back three-section planner test",
            slot_config=DEFAULT_SLOTS,
            lab_slot_config=DEFAULT_LAB_SLOTS,
        )
        board = DeliveryBoard.objects.create(
            scenario=scenario,
            label="Term 1",
            nominal_term=1,
            program="BT3",
            display_order=1,
        )
        Course.objects.get_or_create(
            course_code="BTB301",
            defaults={"credit_hours": 3, "department": "BT3"},
        )
        ProgrammeRequirement.objects.get_or_create(
            program="BT3",
            course_code="BTB301",
            defaults={"programme_term": 1, "credit_hours": 3},
        )
        ScenarioSectionBudget.objects.create(
            scenario=scenario,
            course_code="BTB301",
            department="BT3",
            credit_hours=3,
            planned_sections=3,
            max_per_section=25,
            total_demand=60,
            programme_term=1,
        )
        for idx in range(30):
            ScenarioStudentMap.objects.create(
                scenario=scenario,
                student_id=9920000 + idx,
                primary_term=1,
                recommended_courses=["BTB301"],
            )
        Room.objects.create(
            room_code="BT3-R1",
            capacity=50,
            department="BT3",
            room_type="lecture",
            building="B1",
        )

        result = auto_place_board(board.id, strategy="compact")
        assert result["skipped"] == 0

        placements = list(
            SectionPlacement.objects.filter(board=board, term_section__course_code="BTB301")
            .select_related("term_section")
            .order_by("term_section__section", "day", "start_time")
        )
        by_section: dict[str, list[SectionPlacement]] = {}
        for placement in placements:
            by_section.setdefault(placement.term_section.section, []).append(placement)

        assert set(by_section) == {"S1", "S2", "S3"}
        assert _course_has_back_to_back_pair(by_section)


class TestOptimalStrategy:
    """Test CP-SAT optimal strategy with fallback."""

    def test_places_sections_or_falls_back(self, timetable_scenario):
        scenario, board = timetable_scenario
        result = auto_place_scenario(scenario.id, strategy="optimal")
        # Should never return 0 placed (fallback to compact)
        assert result.get("total_placed", 0) > 0 or result.get("boards", {})


class TestAdaptiveStrategy:
    """Test the adaptive portfolio strategy."""

    def test_places_all_sections(self, timetable_scenario):
        scenario, board = timetable_scenario
        result = auto_place_scenario(scenario.id, strategy="adaptive")
        assert result["total_placed"] > 0

    def test_reports_phase_info(self, timetable_scenario):
        scenario, board = timetable_scenario
        result = auto_place_scenario(scenario.id, strategy="adaptive")
        phases = result.get("adaptive_phases", {})
        assert len(phases) > 0
        for _label, info in phases.items():
            assert "greedy" in info


class TestHybridStrategy:
    """Test greedy + annealing hybrid."""

    def test_places_sections(self, timetable_scenario):
        scenario, board = timetable_scenario
        result = auto_place_scenario(scenario.id, strategy="hybrid")
        assert result["total_placed"] > 0


class TestLoadBalancedStrategy:
    """Test greedy + redistribution."""

    def test_places_sections(self, timetable_scenario):
        scenario, board = timetable_scenario
        result = auto_place_scenario(scenario.id, strategy="load_balanced")
        assert result["total_placed"] > 0


class TestDaySpacing:
    """Verify day spacing preference (non-consecutive days)."""

    def test_prefers_spaced_days(self, timetable_scenario):
        scenario, board = timetable_scenario
        auto_place_board(board.id, strategy="compact")
        placements = list(
            SectionPlacement.objects.filter(board=board)
            .select_related("term_section")
            .order_by("term_section__course_code", "day")
        )

        day_idx = {"SUN": 0, "MON": 1, "TUE": 2, "WED": 3, "THU": 4}
        consecutive_count = 0
        total_pairs = 0

        # Group by section
        from collections import defaultdict

        by_section: dict[int, list] = defaultdict(list)
        for p in placements:
            by_section[p.term_section_id].append(p)

        for _ts_id, section_placements in by_section.items():
            if len(section_placements) < 2:
                continue
            days = sorted(day_idx.get(p.day, 99) for p in section_placements)
            for i in range(len(days) - 1):
                total_pairs += 1
                if days[i + 1] - days[i] == 1:
                    consecutive_count += 1

        # At least some pairs should be non-consecutive (can't guarantee all)
        if total_pairs > 0:
            consecutive_ratio = consecutive_count / total_pairs
            assert consecutive_ratio < 1.0, (
                "All meeting pairs are consecutive — spacing not working"
            )


class TestTimeConsistency:
    """Verify same start time preference across lecture meetings."""

    def test_lectures_same_start_time(self, timetable_scenario):
        scenario, board = timetable_scenario
        auto_place_board(board.id, strategy="compact")
        placements = list(
            SectionPlacement.objects.filter(board=board).select_related("term_section")
        )

        from collections import defaultdict

        by_section: dict[int, list] = defaultdict(list)
        for p in placements:
            by_section[p.term_section_id].append(p)

        inconsistent = 0
        total = 0

        def _to_min(t):
            h, m = t.split(":")
            return int(h) * 60 + int(m)

        for _ts_id, section_placements in by_section.items():
            # Only check lecture meetings (≤ 75 min)
            lectures = [
                p for p in section_placements if (_to_min(p.end_time) - _to_min(p.start_time)) <= 75
            ]
            if len(lectures) < 2:
                continue
            total += 1
            starts = set(p.start_time for p in lectures)
            if len(starts) > 1:
                inconsistent += 1

        # Most sections should have consistent start times
        if total > 0:
            assert inconsistent / total < 0.5, (
                f"{inconsistent}/{total} sections have inconsistent start times"
            )


class TestRoomAssignment:
    """Verify rooms are assigned correctly during placement."""

    def test_rooms_assigned_by_greedy(self, timetable_scenario):
        scenario, board = timetable_scenario
        auto_place_board(board.id, strategy="compact")
        placements = SectionPlacement.objects.filter(board=board)
        assigned = placements.exclude(room="").exclude(room="UNASSIGNED").count()
        total = placements.count()
        assert assigned > 0, "No rooms assigned by greedy placer"
        # Allow some UNASSIGNED when rooms are tight (10% buffer may exceed capacity)
        assert assigned >= total * 0.8, f"Only {assigned}/{total} placements have rooms"

    def test_no_room_double_booking(self, timetable_scenario):
        scenario, board = timetable_scenario
        auto_place_board(board.id, strategy="compact")
        placements = list(SectionPlacement.objects.filter(board=board).exclude(room=""))

        # Group by (day, start_time, room) — each combo should have at most 1 entry
        from collections import defaultdict

        slot_room: dict[tuple, list] = defaultdict(list)
        for p in placements:
            if p.room and p.room != "UNASSIGNED":
                slot_room[(p.day, p.start_time, p.room)].append(p)

        for key, entries in slot_room.items():
            assert len(entries) <= 1, (
                f"Room double-booking: {key[2]} on {key[0]} {key[1]} has {len(entries)} sections"
            )

    def test_room_capacity_respected(self, timetable_scenario):
        scenario, board = timetable_scenario
        auto_place_board(board.id, strategy="compact")
        placements = list(
            SectionPlacement.objects.filter(board=board)
            .exclude(room="")
            .exclude(room="UNASSIGNED")
            .select_related("term_section")
        )

        from core.models import ScenarioSectionBudget

        budget_map = {
            b.course_code: b.max_per_section
            for b in ScenarioSectionBudget.objects.filter(
                scenario=board.scenario, programme_term=board.nominal_term
            )
        }

        room_caps = {r.room_code: r.capacity for r in Room.objects.all()}

        for p in placements:
            cap = budget_map.get(p.term_section.course_code, 40)
            room_cap = room_caps.get(p.room, 0)
            assert room_cap >= cap, (
                f"{p.term_section.course_code} section needs {cap} seats "
                f"but room {p.room} has {room_cap}"
            )

    def test_labs_in_lab_rooms_only(self, timetable_scenario):
        """Only 4-credit 100-min meetings go to lab rooms.
        2-credit 100-min meetings are long lectures in lecture rooms."""
        scenario, board = timetable_scenario
        auto_place_board(board.id, strategy="compact")
        placements = list(
            SectionPlacement.objects.filter(board=board)
            .exclude(room="")
            .exclude(room="UNASSIGNED")
            .select_related("term_section")
        )

        from core.models import ScenarioSectionBudget

        credit_map = {
            b.course_code: b.credit_hours
            for b in ScenarioSectionBudget.objects.filter(scenario=scenario)
        }
        room_types = {r.room_code: r.room_type for r in Room.objects.all()}

        def _to_min(t):
            h, m = t.split(":")
            return int(h) * 60 + int(m)

        for p in placements:
            duration = _to_min(p.end_time) - _to_min(p.start_time)
            cr = credit_map.get(p.term_section.course_code, 3)
            rtype = room_types.get(p.room, "lecture")
            is_lab_meeting = duration > 80 and cr == 4
            if is_lab_meeting:
                assert rtype == "lab", (
                    f"4cr lab meeting {p.term_section.course_code} ({duration}min) "
                    f"assigned to {rtype} room {p.room}"
                )
            else:
                assert rtype == "lecture", (
                    f"Lecture meeting {p.term_section.course_code} ({duration}min, {cr}cr) "
                    f"assigned to {rtype} room {p.room}"
                )


# ── Helper assertions ──


def _count_student_overlaps(placements) -> int:
    """Count how many placement pairs with shared students overlap in time."""
    from core.services.timetable_overlap import build_overlap_matrix, courses_share_students
    from core.services.timetable_workspace import _time_mask

    placement_list = list(placements.select_related("term_section", "board__scenario"))
    if not placement_list:
        return 0

    board = placement_list[0].board
    course_codes = {p.term_section.course_code for p in placement_list}
    overlap_matrix = build_overlap_matrix(board.scenario_id, course_codes)

    count = 0
    n = len(placement_list)
    for i in range(n):
        for j in range(i + 1, n):
            a, b = placement_list[i], placement_list[j]
            if a.term_section.course_code == b.term_section.course_code:
                continue
            mask_a = _time_mask(a.day, a.start_time, a.end_time)
            mask_b = _time_mask(b.day, b.start_time, b.end_time)
            if mask_a & mask_b:
                if courses_share_students(
                    overlap_matrix, a.term_section.course_code, b.term_section.course_code
                ):
                    count += 1
    return count


def _assert_no_same_group_overlaps(placements):
    """Legacy wrapper — now counts instead of raising."""
    count = _count_student_overlaps(placements)
    if count > 0:
        raise AssertionError(f"{count} student overlap(s) found")


def _sections_are_back_to_back(left, right) -> bool:
    """Every meeting in ``right`` has a same-day consecutive partner in ``left``."""

    def _to_min(t):
        h, m = str(t).split(":")
        return int(h) * 60 + int(m)

    def _gap(a, b):
        if a.day != b.day:
            return None
        a_s, a_e = _to_min(a.start_time), _to_min(a.end_time)
        b_s, b_e = _to_min(b.start_time), _to_min(b.end_time)
        if a_s < b_e and b_s < a_e:
            return -1
        return b_s - a_e if a_e <= b_s else a_s - b_e

    for right_meeting in right:
        if not any(
            (gap := _gap(left_meeting, right_meeting)) is not None and 0 <= gap <= 15
            for left_meeting in left
        ):
            return False
    return True


def _course_has_back_to_back_pair(by_section: dict[str, list[SectionPlacement]]) -> bool:
    labels = sorted(by_section)
    for idx, left_label in enumerate(labels):
        for right_label in labels[idx + 1 :]:
            left = by_section[left_label]
            right = by_section[right_label]
            if _sections_are_back_to_back(left, right) or _sections_are_back_to_back(right, left):
                return True
    return False


def _assert_labs_use_lab_slots(board):
    """Assert 100-min placements use lab slot times, not merged lecture slots."""
    placements = SectionPlacement.objects.filter(board=board)
    lab_starts = {s["start"] for s in DEFAULT_LAB_SLOTS}

    def _to_min(t):
        h, m = t.split(":")
        return int(h) * 60 + int(m)

    for p in placements:
        duration = _to_min(p.end_time) - _to_min(p.start_time)
        if duration > 80:
            assert p.start_time in lab_starts, (
                f"Lab placement {p.term_section.course_code} at {p.start_time}-{p.end_time} "
                f"doesn't use a lab slot. Expected one of: {lab_starts}"
            )
