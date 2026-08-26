import pytest
from pytest import MonkeyPatch

from core.models import Prerequisite, ProgrammeRequirement
from core.report_views import _program_importance_scores
from core.services.course_priority import (
    build_program_dependency_graph,
    compute_downstream_importance_scores,
    program_downstream_importance_scores,
)
from core.services.high_priority_missing import (
    _build_unlock_graph_for_program,
    _compute_priority_scores,
    _matches_term_parity,
)

pytestmark = pytest.mark.django_db


def test_high_priority_report_parity_flag_matches_plan_term_groups() -> None:
    assert _matches_term_parity(1, 0) is True
    assert _matches_term_parity(3, 0) is True
    assert _matches_term_parity(2, 0) is False
    assert _matches_term_parity(2, 1) is True
    assert _matches_term_parity(4, 1) is True
    assert _matches_term_parity(1, 1) is False


def test_program_graph_seeds_plan_leaves_and_normalizes_prerequisite_edges() -> None:
    for code in ("CS101", "CS201", "CS299"):
        ProgrammeRequirement.objects.create(program="AI", course_code=code)
    ProgrammeRequirement.objects.create(program="DS", course_code="DS101")

    Prerequisite.objects.create(
        program="AI",
        course_code=" cs201 ",
        prerequisite_course_code=" cs101, ext100 ",
    )
    Prerequisite.objects.create(
        program="AI",
        course_code="OUT500",
        prerequisite_course_code="CS201",
    )

    assert build_program_dependency_graph(" ai ") == {
        "CS101": {"CS201"},
        "CS201": {"OUT500"},
        "CS299": set(),
        "EXT100": {"CS201"},
        "OUT500": set(),
    }


def test_downstream_scores_support_all_legacy_discount_modes() -> None:
    graph = {
        "A": {"B"},
        "B": {"C"},
        "C": {"D"},
        "D": set(),
    }

    inverse_distance = compute_downstream_importance_scores(graph)
    assert inverse_distance == pytest.approx(
        {"A": 1.0 + 0.5 + (1.0 / 3.0), "B": 1.5, "C": 1.0, "D": 0.0}
    )
    assert compute_downstream_importance_scores(graph, discount="none") == {
        "A": 3.0,
        "B": 2.0,
        "C": 1.0,
        "D": 0.0,
    }
    assert compute_downstream_importance_scores(graph, discount="half_power_d") == pytest.approx(
        {"A": 1.75, "B": 1.5, "C": 1.0, "D": 0.0}
    )
    assert compute_downstream_importance_scores(graph, discount="legacy-unknown") == pytest.approx(
        inverse_distance
    )


def test_program_scores_include_a_real_zero_for_an_isolated_plan_course() -> None:
    ProgrammeRequirement.objects.create(program="AI", course_code="AI100")

    assert program_downstream_importance_scores("AI") == {"AI100": 0.0}


def test_high_priority_private_wrappers_delegate_without_rounding(
    monkeypatch: MonkeyPatch,
) -> None:
    graph = {"AI100": {"AI200"}, "AI200": set()}
    monkeypatch.setattr(
        "core.services.high_priority_missing.course_priority.build_program_dependency_graph",
        lambda program: graph if program == "AI" else {},
    )
    monkeypatch.setattr(
        "core.services.high_priority_missing.course_priority.compute_downstream_importance_scores",
        lambda received_graph, discount="1_over_d": {
            "AI100": 1.123456789 if received_graph is graph and discount == "none" else -1.0
        },
    )

    assert _build_unlock_graph_for_program("AI") is graph
    assert _compute_priority_scores(graph, discount="none") == {"AI100": 1.123456789}


def test_report_private_wrapper_preserves_six_decimal_rounding(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "core.report_views.program_downstream_importance_scores",
        lambda program: {"AI100": 1.123456789, "AI200": 0.0} if program == "AI" else {},
    )

    assert _program_importance_scores("AI") == {"AI100": 1.123457, "AI200": 0.0}
