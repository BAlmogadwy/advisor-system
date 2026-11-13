"""Migration 0080 fills the request key of points written under 0079.

0079 already allowed a point to name a requested section or course; 0080 adds
the key and then a check that the key matches those fields. Without the fill in
between, a single such row would stop the migration.
"""

from __future__ import annotations

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

pytestmark = pytest.mark.django_db(transaction=True)

BEFORE = [("core", "0079_timetable_feedback")]
AFTER = [("core", "0080_timetable_feedback_point_request")]


def _migrate(target):
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(target)
    return executor.loader.project_state(target).apps


def _latest():
    _migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())


def _point(apps, case, **fields):
    point_model = apps.get_model("core", "TimetableFeedbackPoint")
    base = {
        "case": case,
        "anchor_kind": "TIMETABLE",
        "anchor_key": "TIMETABLE",
        "reason_code": "LOAD_TOO_HIGH",
        "seen_fingerprint": "a" * 64,
        "seen_snapshot": {},
    }
    base.update(fields)
    return point_model.objects.create(**base)


def _case(apps):
    case_model = apps.get_model("core", "TimetableFeedback")
    # The historical model has no save() override, so the number is given here.
    return case_model.objects.create(
        student_id=4992001, academic_year="1448", term="1", reference="TTF-2026-90001"
    )


def test_points_that_already_name_a_request_get_their_key():
    try:
        apps = _migrate(BEFORE)
        case = _case(apps)
        _point(apps, case)
        _point(apps, case, reason_code="MISSING_COURSE", requested_course_key="SD301")
        _point(
            apps,
            case,
            anchor_kind="SECTION",
            term_section_id=11,
            section_label="M1",
            anchor_key="SECTION|11",
            reason_code="PREFER_OTHER_SECTION",
            requested_term_section_id=12,
        )

        apps = _migrate(AFTER)

        keys = apps.get_model("core", "TimetableFeedbackPoint").objects.values_list(
            "request_key", flat=True
        )
        assert sorted(keys) == ["", "COURSE|SD301", "SECTION|12"]
    finally:
        # Empty the tables at 0079 first: if 0080 had failed on these rows, going
        # straight to the latest schema would fail on them again and leave every
        # later test at 0079.
        apps = _migrate(BEFORE)
        apps.get_model("core", "TimetableFeedbackPoint").objects.all().delete()
        apps.get_model("core", "TimetableFeedback").objects.all().delete()
        _latest()


def test_a_point_naming_both_a_section_and_a_course_stops_the_migration_by_name():
    try:
        apps = _migrate(BEFORE)
        point = _point(
            apps,
            _case(apps),
            anchor_kind="SECTION",
            term_section_id=11,
            section_label="M1",
            anchor_key="SECTION|11",
            reason_code="TIME_CLASH",
            requested_term_section_id=12,
            requested_course_key="SD301",
        )

        with pytest.raises(RuntimeError, match=f"point {point.pk} names both"):
            _migrate(AFTER)
    finally:
        apps = _migrate(BEFORE)
        apps.get_model("core", "TimetableFeedbackPoint").objects.all().delete()
        apps.get_model("core", "TimetableFeedback").objects.all().delete()
        _latest()
