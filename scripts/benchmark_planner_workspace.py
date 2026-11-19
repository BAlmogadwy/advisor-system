"""Time the rebuild workspace on synthetic data in a PostgreSQL pytest database.

With DATABASE_URL pointing to a PostgreSQL service that allows test databases:
    python -m pytest -s -q scripts/benchmark_planner_workspace.py

The first scenario represents the rebuild target. Each larger sample adds
unrelated scenarios. Fixture writes, size checks and solver work are untimed.
"""

from __future__ import annotations

import json
import re
from statistics import median
from time import perf_counter

import pytest
from django.db import connection, transaction

from core.models import (
    BoardSectionVisibility,
    DeliveryBoard,
    Instructor,
    SectionInstructor,
    SectionPlacement,
    StudentTermSection,
    TermSection,
    TermSectionMeeting,
    TermSectionProgram,
    TimetableRepairGlobalPlan,
    TimetableRepairRun,
    TimetableScenario,
)
from core.services.planner_rebuild_workspace import _WORKSPACE_MODELS, rebuild_workspace


@transaction.atomic
def _add_scenario(number, teacher):
    scenario = TimetableScenario.objects.create(
        academic_year="1448", term="1", name=f"Draft {number}"
    )
    board = DeliveryBoard.objects.create(scenario=scenario, label="CS")
    sections = TermSection.objects.bulk_create(
        [
            TermSection(
                scenario=scenario,
                course_code="CS",
                course_number=str(101 + i),
                course_key=f"CS{101 + i}",
                section="S1",
                course_name=f"Computing {i}",
            )
            for i in range(32)
        ]
    )
    SectionInstructor.objects.bulk_create(
        [
            SectionInstructor(
                scenario=scenario, course_key=s.course_key, section=s.section, instructor=teacher
            )
            for s in sections
        ]
    )
    TermSectionProgram.objects.bulk_create(
        [TermSectionProgram(term_section=s, program="CS") for s in sections]
    )
    BoardSectionVisibility.objects.bulk_create(
        [BoardSectionVisibility(board=board, term_section=s) for s in sections]
    )
    StudentTermSection.objects.bulk_create(
        [
            StudentTermSection(
                term_section=s,
                student_id=i * 24 + seat + 1,
                academic_year="1448",
                term="1",
                source="registrar",
            )
            for i, s in enumerate(sections)
            for seat in range(24)
        ]
    )
    slots = [
        dict(term_section=s, day=day, start_time="09:00", end_time="10:00", room=f"R{i}")
        for i, s in enumerate(sections)
        for day in ("SUN", "TUE")
    ]
    TermSectionMeeting.objects.bulk_create(
        [TermSectionMeeting(instructor=teacher.full_name, **slot) for slot in slots]
    )
    placements = SectionPlacement.objects.bulk_create(
        [SectionPlacement(board=board, **slot) for slot in slots]
    )
    run = TimetableRepairRun.objects.create(
        scenario=scenario, target_placement=placements[0], target_section=sections[0]
    )
    candidate = run.candidates.create(
        candidate_id="candidate", day="MON", start_time="09:00", end_time="10:00"
    )
    plan = TimetableRepairGlobalPlan.objects.create(scenario=scenario)
    plan.items.create(sequence=1, repair_run=run, candidate=candidate, placement=placements[0])


def _sample(tables):
    copies = {}

    def time_copy(execute, sql, params, many, context):
        match = re.match(r'INSERT INTO pg_temp\."([^"]+)" SELECT \* FROM ', sql)
        if not match:
            return execute(sql, params, many, context)
        started = perf_counter()
        result = execute(sql, params, many, context)
        table = match.group(1)
        assert table not in copies
        copies[table] = 1000 * (perf_counter() - started)
        return result

    with connection.execute_wrapper(time_copy):
        started = perf_counter()
        with rebuild_workspace():
            pass
        elapsed = 1000 * (perf_counter() - started)
    assert copies.keys() == tables
    return elapsed, copies


@pytest.mark.django_db(transaction=True)
def test_measure_workspace():
    if connection.vendor != "postgresql":
        pytest.skip("Select a PostgreSQL test database to measure the rebuild workspace")
    tables = {model._meta.db_table for model in _WORKSPACE_MODELS}
    teacher = Instructor.objects.create(
        full_name="Benchmark Teacher", normalised_name="benchmark teacher"
    )
    previous = 0
    for count in (1, 32, 128):
        for number in range(previous, count):
            _add_scenario(number, teacher)
        previous = count
        source_counts = {m._meta.db_table: m.objects.count() for m in _WORKSPACE_MODELS}
        # This untimed pass verifies all rows and warms the same copy path.
        with rebuild_workspace(), connection.cursor() as cursor:
            assert {m._meta.db_table: m.objects.count() for m in _WORKSPACE_MODELS} == source_counts
            cursor.execute(
                "SELECT sum(pg_total_relation_size(to_regclass('pg_temp.' || quote_ident(name)))) "
                "FROM unnest(%s::text[]) AS rel(name)",
                [sorted(tables)],
            )
            temp_bytes = int(cursor.fetchone()[0])
        samples = [_sample(tables) for _ in range(9)]
        totals = [total for total, _ in samples]
        per_table = {
            table: round(median(copies[table] for _, copies in samples), 2)
            for table in sorted(tables)
        }
        print(
            json.dumps(
                {
                    "postgres_version": connection.pg_version,
                    "scenarios": count,
                    "rows": sum(source_counts.values()),
                    "rows_by_table": source_counts,
                    "temp_mib": round(temp_bytes / 2**20, 2),
                    "samples": len(samples),
                    "workspace_median_ms": round(median(totals), 2),
                    "workspace_range_ms": [round(min(totals), 2), round(max(totals), 2)],
                    "copy_median_ms": round(
                        median(sum(copies.values()) for _, copies in samples), 2
                    ),
                    "copy_median_ms_by_table": per_table,
                }
            ),
            flush=True,
        )
