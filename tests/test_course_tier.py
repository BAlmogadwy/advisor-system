"""Course-tier classifier + per-scenario tier-map builder."""

from __future__ import annotations

import pytest

from core.services.timetable_course_tier import (
    classify_course_tier,
    clear_program_count_cache,
    program_count_by_code,
)


@pytest.fixture(autouse=True)
def _clear_tier_cache():
    # program_count_by_code is a process-lifetime lru_cache; the test DB rolls
    # back per test but the Python cache does not. Clear before AND after so no
    # test poisons another's count map.
    clear_program_count_cache()
    yield
    clear_program_count_cache()


# ── Pure classifier ──────────────────────────────────────────────────────


def test_prefix_beats_count_math_stat_are_t2() -> None:
    # MATH471 sits in only 2 plans (count rule alone => T1) but the prefix wins.
    assert classify_course_tier("MATH471", 2) == "T2"
    assert classify_course_tier("STAT301", 6) == "T2"
    assert classify_course_tier("MATH101", 1) == "T2"


def test_gen_ed_prefixes_are_t3() -> None:
    for code in ("ENGL103", "GS104", "GSE1", "GSE3", "FE1", "FE2"):
        assert classify_course_tier(code, 12) == "T3", code


def test_plan_count_boundary_two_vs_three() -> None:
    # Specialised (<=2 plans) => T1; shared (>2) => T2.
    assert classify_course_tier("CS211", 2) == "T1"
    assert classify_course_tier("CS211", 3) == "T2"
    assert classify_course_tier("AI113", 1) == "T1"


def test_orphan_defaults_to_t1() -> None:
    assert classify_course_tier("AI463", 0) == "T1"
    assert classify_course_tier("DS487", 0) == "T1"
    # explicit override honoured
    assert classify_course_tier("AI463", 0, default="T2") == "T2"


def test_normalisation_case_and_spaces() -> None:
    # normalize_code upper-cases and strips spaces before prefix matching.
    assert classify_course_tier(" gse 1 ", 12) == "T3"
    assert classify_course_tier("math101", 6) == "T2"


# ── DB-backed count map + builder ────────────────────────────────────────


@pytest.mark.django_db
def test_program_count_by_code_counts_distinct_programmes() -> None:
    from core.models import ProgrammeRequirement

    ProgrammeRequirement.objects.create(program="AI", course_code="CS111")
    ProgrammeRequirement.objects.create(program="AI2", course_code="CS111")
    ProgrammeRequirement.objects.create(program="DS", course_code="CS111")
    ProgrammeRequirement.objects.create(program="AI", course_code="AI300")
    clear_program_count_cache()

    counts = program_count_by_code()
    assert counts["CS111"] == 3
    assert counts["AI300"] == 1


@pytest.mark.django_db
def test_manual_cache_clear_refreshes() -> None:
    from core.models import ProgrammeRequirement
    from core.services import timetable_course_tier as tct

    ProgrammeRequirement.objects.create(program="AI", course_code="CS111")
    clear_program_count_cache()
    assert program_count_by_code()["CS111"] == 1

    # Simulate a bulk_create (which does NOT fire post_save signals) by inserting
    # then confirming a manual clear picks up the new row.
    ProgrammeRequirement.objects.bulk_create(
        [ProgrammeRequirement(program="DS", course_code="CS111")]
    )
    assert program_count_by_code()["CS111"] == 1  # bulk_create bypassed the signal
    tct.clear_program_count_cache()
    assert program_count_by_code()["CS111"] == 2


@pytest.mark.django_db
def test_post_save_signal_auto_invalidates_cache() -> None:
    from core.models import ProgrammeRequirement

    ProgrammeRequirement.objects.create(program="AI", course_code="CS111")
    assert program_count_by_code()["CS111"] == 1
    # A plain .create() fires post_save -> cache cleared automatically.
    ProgrammeRequirement.objects.create(program="DS", course_code="CS111")
    assert program_count_by_code()["CS111"] == 2


@pytest.mark.django_db
def test_build_course_tier_map_keys_by_course_identity() -> None:
    from core.models import ProgrammeRequirement, TermSection, TimetableScenario
    from core.services.timetable_optimizer_v2 import build_course_tier_map_for_scenario

    scenario = TimetableScenario.objects.create(name="TierMap", gender="M", programs=["AI"])
    # CS111 shared by 3 plans => T2; AI300 by 1 => T1; GSE1 by prefix => T3;
    # AI463 absent from ProgrammeRequirement => orphan default T1.
    for prog in ("AI", "AI2", "DS"):
        ProgrammeRequirement.objects.create(program=prog, course_code="CS111")
    ProgrammeRequirement.objects.create(program="AI", course_code="AI300")
    ProgrammeRequirement.objects.create(program="AI", course_code="GSE1")
    clear_program_count_cache()

    TermSection.objects.create(
        scenario=scenario, course_code="CS111", course_key="CS111::INTRO_PROGRAMMING", section="M"
    )
    TermSection.objects.create(
        scenario=scenario, course_code="AI300", course_key="AI300::MACHINE_LEARNING", section="M"
    )
    TermSection.objects.create(
        scenario=scenario, course_code="GSE1", course_key="GSE1::GEN_ELECTIVE", section="M"
    )
    TermSection.objects.create(scenario=scenario, course_code="AI463", course_key="", section="M")

    tier_map = build_course_tier_map_for_scenario(scenario.id)
    # keyed by the SectionState.course_code identity (course_key or course_code)
    assert tier_map["CS111::INTRO_PROGRAMMING"] == "T2"
    assert tier_map["AI300::MACHINE_LEARNING"] == "T1"
    assert tier_map["GSE1::GEN_ELECTIVE"] == "T3"
    assert tier_map["AI463"] == "T1"  # orphan default, bare code key
    # bare-code fallback keys present too
    assert tier_map["CS111"] == "T2"
