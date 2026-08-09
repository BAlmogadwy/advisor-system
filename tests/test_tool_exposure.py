"""Which tools a routed question advertises — asserted on NAMES, not on a count.

A count passes while the wrong single tool is exposed, and the defect this commit
removes is exactly that shape: twelve tools on every question, so the model picked
`course_prerequisites` — the reverse-direction tool — for a forward-unlock question
because it was there to pick.

The negative assertions matter as much as the positive ones. An accidental
fallback-to-all-tools satisfies every "contains" check ever written.
"""

from __future__ import annotations

import pytest

from core.models import Student
from core.services.advisor_intent import (
    CompositionKind,
    IntentFamily,
    capabilities_for_route,
    route_intent,
)
from core.services.rbac import ROLE_STUDENT
from core.services.virtual_advisor import _withheld_for
from core.services.virtual_advisor_capabilities import get_default_registry

pytestmark = pytest.mark.django_db

SID = 7301001


@pytest.fixture(autouse=True)
def _student() -> None:
    Student.objects.get_or_create(
        student_id=SID, defaults={"name": "طالب", "program": "AI", "section": "M"}
    )


def _scope() -> dict:
    return {"role": ROLE_STUDENT, "student_id": SID}


def schemas_for(question: str) -> set[str]:
    """The tool NAMES this question would actually advertise to the model.

    Built the way the loop builds them — registry filtered by scope, minus the
    withheld set — rather than by reading the mapping function, so a change that
    narrows the map but never reaches the schemas fails here.
    """
    withheld = _withheld_for(route_intent(question), _scope())
    return {
        (s.get("function") or {}).get("name")
        for s in get_default_registry().tool_schemas_for_scope(_scope())
    } - withheld


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("وش جدولي؟", {"my_timetable"}),
        ("وش الشعب اللي ما تتعارض مع جدولي لـ AI352؟", {"my_clash_free_sections"}),
        ("وش أهم مقرر عندي؟", {"my_progress"}),
        ("وش يفتح AI331؟", {"why_course_locked"}),
        ("ليش AI491 مقفل؟", {"why_course_locked"}),
        ("ابنِ لي جدول", {"build_my_timetable"}),
    ],
)
def test_a_single_capability_route_advertises_exactly_one_tool(question, expected) -> None:
    assert schemas_for(question) == expected


def test_the_cross_plan_ranking_does_not_get_the_single_course_tool() -> None:
    """CP02, the regression 6A.2 fixed and this commit must not undo.

    «أي مقرر عندي يفتح أكبر عدد من المقررات مباشرة؟» asks which course wins across
    the plan. `why_course_locked` analyses ONE named course and cannot rank, and it
    fires on the same sentence — so leaving it advertised is leaving the wrong answer
    within reach.
    """
    exposed = schemas_for("أي مقرر عندي يفتح أكبر عدد من المقررات مباشرة؟")
    assert exposed == {"my_progress"}
    assert "why_course_locked" not in exposed


def test_a_multi_capability_route_advertises_its_ordered_union() -> None:
    """TT20's two halves, from the ROUTE's own secondaries rather than an exception
    list. Order is asserted because tool order changes what a model reaches for."""
    question = "ليش ما ضفت AI491؟ هل المشكلة في المتطلب السابق أو في وقت الشعبة؟"
    route = route_intent(question)
    assert route.composition is CompositionKind.MULTI_CAPABILITY
    assert capabilities_for_route(route) == ("build_my_timetable", "why_course_locked")
    assert schemas_for(question) == {"build_my_timetable", "why_course_locked"}


def test_a_data_plus_policy_route_never_advertises_the_policy_lookup() -> None:
    """TT08. Retrieval already ran, server-side, before generation. Advertising the
    lookup to "complete" the composition would hand the model back the decision this
    branch took away from it — and any records it fetched would not be in the
    contract computed before the answer was written."""
    exposed = schemas_for("أريد تسجيل 19 ساعة، هل تستطيع بناء جدول كامل بهذا الحد؟")
    assert exposed == {"build_my_timetable"}
    assert "policy_lookup" not in exposed


@pytest.mark.parametrize(
    "question",
    [
        "احفظ الخيار الثاني كجدولي المفضل.",
        "سوِّ لي أكثر من خيار للجدول، مو خيار واحد بس.",
        "عدّلت قائمة المقررات؛ أعد بناء البدائل بناءً على التعديل الجديد.",
    ],
)
def test_a_question_routed_handoff_advertises_no_tools_at_all(question: str) -> None:
    """These are decided from the QUESTION and never reach the loop. Zero is a
    decision, not a gap."""
    assert capabilities_for_route(route_intent(question)) == ()
    assert schemas_for(question) == set()


def test_a_recognised_rebuild_is_decided_before_the_model_is_asked() -> None:
    """Zero tools, and the refusal still comes from the one place that owns it.

    Exposing `build_my_timetable` and hoping the model called it was the first
    attempt at this, and it left a hole: with `tool_choice` free, a model that simply
    did not call it produced «سأبني لك جدولًا جديدًا يتجاهل تسجيلك الحالي» with
    `action: None` — a promise to discard a registration the system would not have
    touched. `answer_virtual_advisor` now executes that capability itself when the
    route says rebuild, so the rule is still written once and the model cannot skip
    it. With the turn decided before generation, zero tools is the honest surface.
    """
    question = "ابنِ لي جدولًا جديدًا من الصفر وتجاهل كل الشعب المسجلة عندي."
    assert route_intent(question).primary_family is IntentFamily.PLANNER_REBUILD
    assert capabilities_for_route(route_intent(question)) == ()
    assert schemas_for(question) == set()


def test_an_unrecognised_rebuild_phrasing_keeps_the_tool_that_refuses_it() -> None:
    """The second path, and the reason the first is not enough.

    «Build me a timetable ignoring what I am registered in» is not recognised as a
    rebuild — «ignoring» is not the ignore verb — so it classifies PLANNER_BUILD and
    the short-circuit does not fire. It still reaches `build_my_timetable`, which
    refuses on the arguments and emits the same typed route. The two paths cover each
    other: the router catches the phrasings it knows, the executor catches the rest.
    """
    question = "Build me a timetable ignoring what I am registered in"
    assert route_intent(question).primary_family is IntentFamily.PLANNER_BUILD
    assert schemas_for(question) == {"build_my_timetable"}


def test_an_unrouted_question_keeps_the_permitted_registry() -> None:
    """The escape hatch, and the regression this commit must not become.

    GENERAL_AGENT means the router was not certain. Narrowing an unfamiliar question
    to nothing would be a worse failure than a wide surface — it would leave the
    model to answer from memory with no way to check anything.
    """
    assert capabilities_for_route(route_intent("وين مبنى كلية الحاسبات؟")) is None
    exposed = schemas_for("وين مبنى كلية الحاسبات؟")
    assert len(exposed) > 1
    assert "policy_lookup" not in exposed, "retrieval is server-side on every path"


def test_narrowing_can_only_remove_and_never_widen() -> None:
    """RBAC is not bypassed by routing.

    The withheld set is subtracted from the registry's ROLE-FILTERED list, so a route
    naming a tool the principal may not use does not gain it. Asserted over every
    family rather than for one, because the property is what keeps narrowing safe.
    """
    permitted = {
        (s.get("function") or {}).get("name")
        for s in get_default_registry().tool_schemas_for_scope(_scope())
    }
    for question in (
        "وش جدولي؟",
        "ابنِ لي جدول",
        "وش أهم مقرر عندي؟",
        "وين مبنى كلية الحاسبات؟",
    ):
        assert schemas_for(question) <= permitted, question


def test_the_family_map_names_no_tool_the_registry_lacks() -> None:
    """A mapping to a tool that does not exist narrows a turn to nothing at all."""
    from core.services.advisor_intent import CAPABILITY_FOR_FAMILY

    registered = set(get_default_registry().capabilities)
    for family, tools in CAPABILITY_FOR_FAMILY.items():
        for tool in tools:
            assert tool in registered, f"{family} names {tool}, which is not registered"


def test_the_handoff_families_and_the_map_do_not_overlap() -> None:
    """A hand-off family with a tool would advertise one and then never use it."""
    from core.services.advisor_intent import CAPABILITY_FOR_FAMILY

    # PLANNER_REBUILD is absent from this list on purpose — see
    # `test_the_rebuild_keeps_the_tool_that_produces_its_refusal`. The other three
    # are decided from the question and never reach the loop.
    for family in (
        IntentFamily.PLANNER_VIEW_ALTERNATIVES,
        IntentFamily.PLANNER_EDIT_DRAFT,
        IntentFamily.PLANNER_SELECT_PREFERRED,
    ):
        assert family not in CAPABILITY_FOR_FAMILY


def test_the_union_keeps_route_order_rather_than_alphabetical_order() -> None:
    """Tool ORDER changes what a model reaches for, so it is part of the contract.

    TT20's own union happens to be alphabetical, so a sort would pass every
    end-to-end assertion — this builds a route where the two orders differ and
    asserts the primary still comes first. Two traces of the same route must be
    comparable, and a set-derived list is not.
    """
    from core.services.advisor_intent import AdvisorRoute

    route = AdvisorRoute(
        primary_family=IntentFamily.COURSE_PRIORITY,
        secondary_families=(IntentFamily.PLANNER_BUILD,),
        composition=CompositionKind.MULTI_CAPABILITY,
    )
    assert capabilities_for_route(route) == ("my_progress", "build_my_timetable")


def test_the_data_plus_policy_route_does_not_name_the_lookup_at_all() -> None:
    """Asserted on the MAPPING, not only on the schemas.

    `_withheld_for` withholds `policy_lookup` on every path, so a route that named it
    would still be filtered out and every end-to-end test would pass. That belt is
    worth having and it hides this: the route must not ask for the lookup in the
    first place, because retrieval already ran and the records it would fetch were
    not in the contract computed before generation.
    """
    route = route_intent("أريد تسجيل 19 ساعة، هل تستطيع بناء جدول كامل بهذا الحد؟")
    assert route.composition is CompositionKind.DATA_PLUS_POLICY
    assert capabilities_for_route(route) == ("build_my_timetable",)


def test_the_rebuild_is_a_declared_handoff_family() -> None:
    """Asserted on the MEMBERSHIP, not only on the empty result.

    `capabilities_for_route` returns `()` for PLANNER_REBUILD two ways — because it
    is a hand-off family, and because the capability map does not name it — so the
    behaviour is the same with either removed. Today that makes them interchangeable;
    the day a tool is added back to the map, the membership is the only thing still
    holding the surface at zero.
    """
    from core.services.advisor_intent import _HANDOFF_FAMILIES, CAPABILITY_FOR_FAMILY

    assert IntentFamily.PLANNER_REBUILD in _HANDOFF_FAMILIES
    assert IntentFamily.PLANNER_REBUILD not in CAPABILITY_FOR_FAMILY
