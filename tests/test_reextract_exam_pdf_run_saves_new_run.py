"""The PDF re-extract script saves a new exam timetable and rewrites no saved one.

``scripts/reextract_exam_pdf_run.py`` used to rewrite saved run #305 in place
(``update_or_create(id=305)``) on whichever database it ran against. A saved exam
timetable is department work: the script now only ever adds a run.
"""

from __future__ import annotations

import ast
import importlib
import json
import sys
import types
from pathlib import Path

import pytest

from core.models import ExamTimetableRun

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "reextract_exam_pdf_run.py"


def _stored() -> list[tuple]:
    return list(ExamTimetableRun.objects.order_by("id").values_list("id", "label", "result_json"))


@pytest.mark.django_db
def test_the_reextract_saves_a_new_run_and_leaves_every_saved_run_as_it_was(monkeypatch):
    # PyMuPDF is only needed to read the PDF; CI does not install it.
    monkeypatch.setitem(sys.modules, "fitz", types.ModuleType("fitz"))
    script = importlib.import_module("scripts.reextract_exam_pdf_run")
    # The run it used to rewrite, and another saved under the very label it saves.
    ExamTimetableRun.objects.create(id=305, label="Saved by the department", result_json="{}")
    ExamTimetableRun.objects.create(label=script.LABEL, result_json='{"status": "ok"}')
    before = _stored()

    run = script.save_as_new_run({"status": "ok", "schedule": []})

    after = _stored()
    assert after[: len(before)] == before
    assert len(after) == len(before) + 1
    assert run.id not in {row[0] for row in before}
    assert (run.label, json.loads(run.result_json)) == (
        script.LABEL,
        {"status": "ok", "schedule": []},
    )


def test_the_reextract_writes_runs_only_through_the_new_run_save():
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    manager_calls = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "objects"
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "ExamTimetableRun"
    }
    assert manager_calls == {"create"}
    # No instance is saved or deleted behind the manager's back.
    assert not {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr in {"save", "delete"}
    }
    main = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "save_as_new_run"
        for node in ast.walk(main)
    )
