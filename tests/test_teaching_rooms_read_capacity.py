"""Teaching keeps ``Room.capacity``; only the exam side reads ``Room.exam_capacity``.

The owner's request (2026-09-28) was for the exam timetable only: lecture
rooming, the planner and section sizing seat by a room's capacity. Two guards:
the lecture room loaders still hand on ``capacity`` when a room's exam seats
differ, and the source itself - no teaching module names ``exam_capacity``,
and no exam module reads the rooms table except through the one inventory
that turns exam seats into the exam engine's ``capacity``.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from core.models import Room
from core.services.timetable_exact_rooming import _load_room_inventory
from core.services.timetable_rooming import get_programme_rooms

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ("core", "scheduler", "scripts", "config")

#: The only modules that may name ``exam_capacity``: the model, and the exam
#: room inventory (migrations define the column and are left out).
READS_EXAM_SEATS = {"core/models.py", "core/services/exam_room_inventory.py"}


def _python_sources():
    for top in SOURCES:
        for path in sorted((ROOT / top).rglob("*.py")):
            relative = path.relative_to(ROOT).as_posix()
            if "/migrations/" not in relative:
                yield relative, path.read_text(encoding="utf-8")


def _names_exam_seats(text: str) -> bool:
    """Code that reads or writes ``exam_capacity``: an attribute, a keyword, a
    field name or lookup in a string - not a comment or a docstring about it."""
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Attribute) and node.attr == "exam_capacity":
            return True
        if isinstance(node, ast.keyword) and node.arg == "exam_capacity":
            return True
        if isinstance(node, ast.Name) and node.id == "exam_capacity":
            return True
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and re.fullmatch(r"-?exam_capacity(__\w+)?", node.value)
        ):
            return True
    return False


def _is_exam_module(relative: str) -> bool:
    return bool(re.search(r"(^|/)(exam_|[a-z_]*exam[a-z_]*\.py$)", relative))


@pytest.mark.django_db
def test_lecture_room_loaders_read_capacity_not_exam_seats():
    Room.objects.create(room_code="T30", capacity=30, exam_capacity=5, department="CS")
    Room.objects.create(room_code="T0", capacity=0, exam_capacity=40, department="CS")
    Room.objects.create(room_code="T50", capacity=50, exam_capacity=0, department="CS")
    teaching = {"T30": 30, "T0": 0, "T50": 50}

    assert {row["room_code"]: row["capacity"] for row in get_programme_rooms(["CS"])} == teaching
    rows_by_code, capacity_by_code = _load_room_inventory()
    assert capacity_by_code == teaching
    assert {code: row["capacity"] for code, row in rows_by_code.items()} == teaching


def test_only_the_model_and_the_exam_inventory_name_exam_seats():
    naming = {relative for relative, text in _python_sources() if _names_exam_seats(text)}
    assert naming == READS_EXAM_SEATS


def test_no_exam_module_reads_the_rooms_table_but_the_inventory():
    exam_modules = [(r, t) for r, t in _python_sources() if _is_exam_module(r)]
    # The scan reaches the exam modules it guards.
    found = {relative for relative, _text in exam_modules}
    assert {
        "core/services/exam_timetable.py",
        "core/services/exam_evaluation.py",
        "core/exam_views.py",
        "core/management/commands/import_exam_timetable_csv.py",
        "scripts/reextract_exam_pdf_run.py",
    } <= found
    reading = {
        relative
        for relative, text in exam_modules
        if re.search(r"\bRoom\.objects\b|import[^\n]*\bRoom\b|^\s+Room,\s*$", text, re.M)
    }
    assert reading == {"core/services/exam_room_inventory.py"}
