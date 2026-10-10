"""Shared physical-section ownership boundaries used by every board engine."""

from core.models import SectionPlacement


def external_shared_section_ids(scenario_id: int) -> set[int]:
    """Physical sections referenced by this scenario and another scenario.

    Include owned sections even without a local placement: rebuilds must not
    cascade-delete their externally owned copies. Ownership is independent of
    the optional UI-lock feature.
    """
    from django.db.models import Q

    local_ids = SectionPlacement.objects.filter(board__scenario_id=scenario_id).values_list(
        "term_section_id", flat=True
    )
    return set(
        SectionPlacement.objects.exclude(board__scenario_id=scenario_id)
        .filter(Q(term_section_id__in=local_ids) | Q(term_section__scenario_id=scenario_id))
        .values_list("term_section_id", flat=True)
    )


def assert_rebuild_ownership(scenario_id: int) -> None:
    if external_shared_section_ids(scenario_id):
        raise ValueError(
            "Cannot rebuild a scenario with physical section copies on another scenario; "
            "resolve shared section ownership first."
        )
