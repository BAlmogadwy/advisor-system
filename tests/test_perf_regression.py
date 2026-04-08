"""
Performance regression tests.

Ensures batch recommender stays wired into all report paths.
If someone accidentally reverts to per-student loops, these tests
catch it by asserting query counts stay below thresholds.

Thresholds are generous (10-50x above current) to avoid flaky failures
while still catching N+1 regressions (which would be 1000x above).
"""

from __future__ import annotations

import pytest
from django.db import connection, reset_queries
from django.test.utils import override_settings

from core.models import (
    Course,
    Prerequisite,
    ProgrammeRequirement,
    Student,
    StudentCourse,
)

pytestmark = pytest.mark.django_db

# ── How many queries per student is acceptable ──────────────────
# Batch recommender: ~3-7 queries TOTAL regardless of student count.
# Per-student loop: ~15-25 queries PER student.
# Threshold: 100 queries max for any report path with 20 students.
# If N+1 returns, 20 students × 15 queries = 300+ → test fails.
MAX_QUERIES_PER_REPORT = 100


@pytest.fixture()
def perf_students():
    """Create 20 test students with programme data for perf testing."""
    program = "PERF"

    # Create courses
    courses = []
    for i in range(1, 7):
        c, _ = Course.objects.get_or_create(
            course_code=f"PF{i:03d}",
            defaults={"credit_hours": 3, "department": "PERF", "description": f"Perf Course {i}"},
        )
        courses.append(c)

    # Programme requirements (terms 1-3, 2 courses each)
    for i, c in enumerate(courses):
        ProgrammeRequirement.objects.get_or_create(
            program=program,
            course_code=c.course_code,
            defaults={"programme_term": (i // 2) + 1, "credit_hours": 3},
        )

    # Prerequisites: PF002 requires PF001, PF004 requires PF003
    Prerequisite.objects.get_or_create(
        program=program, course_code="PF002", prerequisite_course_code="PF001",
    )
    Prerequisite.objects.get_or_create(
        program=program, course_code="PF004", prerequisite_course_code="PF003",
    )

    # Create 20 students (join year 47 → real_term ~2 for year 1448)
    students = []
    for i in range(20):
        sid = 4700001 + i
        s, _ = Student.objects.get_or_create(
            student_id=sid,
            defaults={"program": program, "section": "M", "name": f"Perf Student {i}"},
        )
        students.append(s)

        # Each student has passed PF001 (term 1), studying nothing
        StudentCourse.objects.get_or_create(
            student=s,
            course=courses[0],
            defaults={"status": "passed", "programme_term": 1},
        )

    return program, students


class TestBatchRecommenderWired:
    """Verify batch recommender is used (not per-student loop) in all report paths."""

    @override_settings(DEBUG=True)
    def test_aggregate_query_count(self, perf_students):
        program, students = perf_students
        from core.services.reporting import _aggregate_cache, build_aggregate_counts

        _aggregate_cache.clear()
        reset_queries()
        count, agg = build_aggregate_counts(1448, 1, program=program)
        queries = len(connection.queries)

        assert count == 20
        assert queries < MAX_QUERIES_PER_REPORT, (
            f"Aggregate used {queries} queries for 20 students — "
            f"likely N+1 regression (batch should use <10)"
        )

    @override_settings(DEBUG=True)
    def test_aggregate_all_programs_query_count(self, perf_students):
        program, students = perf_students
        from core.services.reporting import _aggregate_cache, build_aggregate_counts

        _aggregate_cache.clear()
        reset_queries()
        count, agg = build_aggregate_counts(1448, 1)  # no program filter
        queries = len(connection.queries)

        assert queries < MAX_QUERIES_PER_REPORT, (
            f"All-program aggregate used {queries} queries — "
            f"likely N+1 regression (grouped batch should use <30)"
        )

    @override_settings(DEBUG=True)
    def test_debug_report_query_count(self, perf_students):
        program, students = perf_students
        from core.services.debug_reporting import build_recommendation_debug_report

        reset_queries()
        result = build_recommendation_debug_report(1448, 1, program=program, limit=20)
        queries = len(connection.queries)

        assert result["count"] == 20
        assert queries < MAX_QUERIES_PER_REPORT, (
            f"Debug report used {queries} queries for 20 students — "
            f"likely N+1 regression (batch should use <15)"
        )

    @override_settings(DEBUG=True)
    def test_conflict_matrix_query_count(self, perf_students):
        program, students = perf_students
        from core.services.conflict_matrix import build_conflict_matrix_report

        reset_queries()
        result = build_conflict_matrix_report(1448, 1, program=program, limit=20)
        queries = len(connection.queries)

        assert queries < MAX_QUERIES_PER_REPORT, (
            f"Conflict matrix used {queries} queries for 20 students — "
            f"likely N+1 regression (batch should use <10)"
        )

    @override_settings(DEBUG=True)
    def test_batch_recommender_identical_to_original(self, perf_students):
        """Ensure batch recommender produces same results as original."""
        program, students = perf_students
        from core.services.recommender import recommend_next_courses
        from core.services.recommender_batch import batch_recommend

        sids = [s.student_id for s in students]

        # Original per-student
        original = {}
        for sid in sids:
            recs = recommend_next_courses(sid, 1448, 1)
            if recs:
                original[sid] = recs

        # Batch
        batch = batch_recommend(sids, program, 1448, 1)

        for sid in sids:
            assert original.get(sid, []) == batch.get(sid, []), (
                f"Student {sid}: original={original.get(sid, [])} batch={batch.get(sid, [])}"
            )
