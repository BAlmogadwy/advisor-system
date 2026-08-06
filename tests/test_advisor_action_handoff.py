"""A route is the server's decision, not something the model paraphrases.

Written from a live failure. A student asked for a timetable ignoring their
current registration; `build_my_timetable` refused correctly and returned
`REBUILD_REQUIRES_PLANNER_CONFIRMATION` / `OPEN_STUDENT_PLANNER` — "available,
through the planner, after a confirmation". The loop handed that to the model,
which answered «لا يمكن للنظام بناء جدول يتجاهل تسجيلك الحالي» and told the
student to delete their courses in the university portal.

The dangerous part is the last clause. The rebuild produces a PLANNING DRAFT and
never touches the registration record; a student following that advice loses
real seats to obtain a draft. So the assertions below are not about wording —
they are that the model is never asked.

THREE MORE ROUTES ARRIVE FROM THE QUESTION, NOT FROM A TOOL

Viewing alternatives, preferring one, and editing a draft are MUTATIONS of a
planner draft or reads of one, and the capability registry accepts nothing that
is not read-only. There is no tool whose refusal could carry them, so the
question is where they are recognised — `advisor_intent.classify_intent` — and
the same rule applies: the server decides, the answer is a constant, the provider
is never contacted. `zero provider calls` is therefore asserted for each, not
inferred from the payload looking right.

The rebuild keeps its executor-driven path. A test below proves the question
alone does NOT route it, because two implementations of one refusal is how the
audited one stops running.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from core.models import FinalDisposition, Student
from core.services import virtual_advisor as va
from core.services import virtual_advisor_capabilities as caps
from core.services.advisor_actions import (
    HANDOFFS,
    INTENT_EDIT_DRAFT,
    INTENT_REBUILD_WITHOUT_CURRENT_SECTIONS,
    INTENT_SELECT_PREFERRED_ALTERNATIVE,
    INTENT_VIEW_ALTERNATIVES,
    MAX_ALTERNATIVES,
    OPEN_STUDENT_PLANNER,
    ROUTED_INTENTS,
    ActionHandoff,
    alternative_ref_in,
    handoff_for,
    handoff_for_question,
)
from core.services.advisor_intent import IntentFamily
from core.services.advisor_outcome import derive_outcome
from core.services.advisor_principal import AdvisorPrincipal
from core.services.rbac import ROLE_ADVISOR, ROLE_STUDENT
from core.services.timetable_provenance import TIMETABLE_FACT_KEYS
from tests.test_llm_remote_execution_boundary import ExecutionSpy, ScriptedClient, _call

pytestmark = pytest.mark.django_db

MINE = 7301001

BATCH = Path(__file__).resolve().parents[1] / "evals" / "advisor" / "planner_priority_batch.yaml"

#: Every handoff this repository can emit, so the safety assertions below run over
#: the SET rather than over the one that motivated them. A fifth route added
#: without its own wording reviewed lands in these tests without anyone listing it
#: here — which is the only arrangement under which "every referral says the
#: registration is untouched" stays true after the next one is written.
ALL_HANDOFFS: tuple[ActionHandoff, ...] = (*HANDOFFS.values(), *ROUTED_INTENTS.values())

REFUSAL = {
    "tool": "build_my_timetable",
    "ok": False,
    "reason": "REBUILD_REQUIRES_PLANNER_CONFIRMATION",
    "error": "Rebuilding without the student's current sections requires confirmation.",
    "action": "OPEN_STUDENT_PLANNER",
}


@pytest.fixture(autouse=True)
def _student() -> None:
    Student.objects.get_or_create(
        student_id=MINE, defaults={"name": "طالب", "program": "AI", "section": "M"}
    )


def _principal() -> AdvisorPrincipal:
    return AdvisorPrincipal(role=ROLE_STUDENT, student_id=MINE)


def _ask(client, question: str = "ابني لي جدول وتجاهل المسجل حاليا") -> dict:
    return va.answer_virtual_advisor(
        question=question, principal=_principal(), academic_year=1448, term=1, client=client
    )


def test_the_model_never_sees_the_route_and_never_writes_the_answer(monkeypatch) -> None:
    """THE test. The provider is not asked a second time, and the answer is a
    constant from this repository rather than anything a model produced."""
    ExecutionSpy(monkeypatch, result=REFUSAL)
    client = ScriptedClient(
        [_call("build_my_timetable", {"keep_current_sections": False})],
        final="لا يمكن للنظام بناء جدول يتجاهل تسجيلك الحالي.",  # what it said live
    )
    payload = _ask(client)

    # ONE provider call: the turn that requested the tool. No continuation.
    assert len(client.requests) == 1, "the model was asked to interpret the route"
    assert payload["answer"] != client.final
    assert payload["agent"]["action_handoff"] == OPEN_STUDENT_PLANNER


def test_the_answer_says_the_feature_exists_and_registration_is_untouched() -> None:
    """The three failures of the live answer, asserted as their opposites."""
    handoff = handoff_for(REFUSAL)
    assert handoff is not None
    arabic = handoff.answer("Arabic")

    # 1. it is available — a confirmation requirement, not a denial.
    assert "لا يمكن" not in arabic
    assert "تأكيد" in arabic and "المخطط الدراسي" in arabic
    # 2. the student is routed INTO the planner, not out to the portal…
    assert "افتح المخطط الدراسي" in arabic
    # 3. …and told plainly that nothing was changed. This is the sentence that
    #    matters most: the live answer's advice would have cost real seats.
    assert "لن يحذف أو يغيّر تسجيلك الرسمي" in arabic
    assert handoff.registration_modified is False


def test_the_interface_gets_a_structured_action_not_prose(monkeypatch) -> None:
    """A button cannot be rendered from a sentence, and a sentence is what the
    UI would have had to parse."""
    ExecutionSpy(monkeypatch, result=REFUSAL)
    payload = _ask(ScriptedClient([_call("build_my_timetable", {"keep_current_sections": False})]))

    assert payload["action"] == {
        "type": "OPEN_STUDENT_PLANNER",
        "intent": "REBUILD_WITHOUT_CURRENT_SECTIONS",
        "requires_confirmation": True,
        "registration_modified": False,
    }


def test_every_response_carries_the_action_key(monkeypatch) -> None:
    """`None`, not absent — so a consumer tests one key rather than two shapes."""
    ExecutionSpy(monkeypatch, result={"tool": "my_progress", "ok": True})
    payload = _ask(ScriptedClient([_call("my_progress", {})]), question="كيف حالي؟")
    assert payload["action"] is None


def test_an_english_question_gets_the_english_handoff(monkeypatch) -> None:
    ExecutionSpy(monkeypatch, result=REFUSAL)
    payload = _ask(
        ScriptedClient([_call("build_my_timetable", {"keep_current_sections": False})]),
        question="Build me a timetable ignoring what I am registered in",
    )
    assert "study planner" in payload["answer"]
    assert "does not delete or change your official registration" in payload["answer"]


def test_an_ordinary_result_is_not_intercepted() -> None:
    """The loop is unchanged for the tools that return data — which is all of
    them but one. A router that fires on anything unexpected would replace real
    answers with a referral."""
    for result in (
        {"tool": "my_progress", "ok": True, "reason": "SOMETHING_ELSE"},
        {"tool": "build_my_timetable", "ok": True, "new_sections": []},
        {"ok": False, "error": "boom"},
        "not a dict",
        None,
    ):
        assert handoff_for(result) is None


def test_a_chat_rebuild_request_is_routed_before_any_work_happens() -> None:
    """The guard is the FIRST thing in the executor, and it has to be.

    It used to sit after an early return for "nothing to schedule", so a student
    with an empty plan asking for a rebuild got an ordinary empty result and no
    route. That was harmless while the model was told never to call — and became
    the likely path the moment the description told it to. Whether a rebuild is
    refused must not depend on how much the recommender happened to find first.

    This student has no courses at all, which is exactly the shape that used to
    slip past.
    """
    result = caps.get_default_registry().execute(
        "build_my_timetable",
        {"keep_current_sections": False},
        scope={"role": ROLE_STUDENT, "student_id": MINE},
        ctx={"academic_year": 1448, "term": 1},
    )
    # Against the WHOLE fact set, not against one key. The old assertion named
    # `placed`, which no longer exists on any path — so it passed by asking a
    # question about a key nobody writes, which is the quietest way for a guard test
    # to stop guarding. A refused rebuild must carry no timetable at all.
    assert not (set(result) & TIMETABLE_FACT_KEYS), (
        "chat rebuilt a timetable without the current sections"
    )
    assert result["ok"] is False
    assert result["reason"] == "REBUILD_REQUIRES_PLANNER_CONFIRMATION"
    assert result["action"] == "OPEN_STUDENT_PLANNER"


def test_the_handoff_text_names_no_student_and_no_identifier() -> None:
    """It is server-authored and skips the output contract, so its safety is a
    property of the constant rather than of a check that runs over it.

    Over every handoff, including the three routed from the question. The
    no-digit rule is what keeps «الخيار الثاني» out of the prose: the ordinal the
    student named travels in `alternative_ref`, where a UI can re-resolve it
    against the list it is actually showing, rather than in a sentence that would
    be a false statement about their own request if the extraction were wrong.
    """
    for handoff in ALL_HANDOFFS:
        assert handoff is not None
        body = json.dumps([handoff.answer("Arabic"), handoff.answer("English")], ensure_ascii=False)
        assert not any(ch.isdigit() for ch in body), "a digit in a fixed referral is suspect"
        assert "STUDENT_REF" not in body
        assert "REDACTED" not in body


def test_the_tool_description_tells_the_model_to_call_not_to_answer() -> None:
    """The live failure, at its actual root.

    The description read "Discarding those sections and rebuilding the whole week
    is not available here at all: if the student asks for that, tell them to open
    the planner". The model obeyed it exactly — it never called, so the server
    never saw the request, and the model wrote the routing prose itself. The
    deterministic handoff was downstream of a call the description forbade.

    Two things must therefore stay true: the model is told to CALL, and the
    parameter that expresses the intent is actually advertised. Without the
    second, the intent is inexpressible and the route is unreachable.
    """
    capability = caps.get_default_registry().capabilities["build_my_timetable"]
    description = capability.description

    assert "keep_current_sections=false" in description, "the model is not told to call"
    assert "Do not answer that request yourself" in description
    assert "not available here at all" not in description, "the denial wording is back"

    properties = capability.parameters.get("properties") or {}
    assert "keep_current_sections" in properties, "the intent cannot be expressed"
    assert properties["keep_current_sections"]["type"] == "boolean"


# ==========================================================================
# The three routes decided from the QUESTION.
# ==========================================================================

VIEW = "سوِّ لي أكثر من خيار للجدول، مو خيار واحد بس."
PREFER = "احفظ الخيار الثاني كجدولي المفضل."
EDIT = "عدّلت قائمة المقررات؛ أعد بناء البدائل بناءً على التعديل الجديد."
REBUILD = "ابنِ لي جدولًا جديدًا من الصفر وتجاهل كل الشعب المسجلة عندي."


@pytest.mark.parametrize(
    ("question", "intent"),
    (
        (VIEW, INTENT_VIEW_ALTERNATIVES),
        (PREFER, INTENT_SELECT_PREFERRED_ALTERNATIVE),
        (EDIT, INTENT_EDIT_DRAFT),
    ),
)
def test_a_planner_route_costs_no_provider_call_and_no_capability_execution(
    monkeypatch, question: str, intent: str
) -> None:
    """THE test for the three new routes, and the reason they exist.

    Selecting an alternative writes `selected_alternative` on a draft row. The
    registry refuses to hold anything that is not read-only, so this can never be
    a tool — which leaves exactly two designs: the server routes it, or the model
    improvises prose about it. The second is what produced «لا يمكن للنظام» plus
    advice to delete real registrations.

    `client.requests` is empty, so no provider saw the question. `spy.count` is
    zero, so nothing was executed and — because the server-side policy prefetch
    runs through the registry too — no retrieval happened either. Both are
    asserted rather than one inferred from the other.
    """
    spy = ExecutionSpy(monkeypatch)
    client = ScriptedClient([], final="أستطيع حفظ الخيار الثاني نيابة عنك.")
    payload = _ask(client, question=question)

    assert client.requests == [], "the model was asked about a decision the server made"
    assert spy.count == 0, "a route ran a capability"
    assert payload["answer"] != client.final
    assert payload["action"]["type"] == OPEN_STUDENT_PLANNER
    assert payload["action"]["intent"] == intent
    assert payload["action"]["registration_modified"] is False
    assert payload["agent"]["action_handoff"] == OPEN_STUDENT_PLANNER
    assert payload["agent"]["intent_route"] == intent


def test_the_chosen_alternative_travels_as_a_reference_the_planner_can_resolve() -> None:
    """«الخيار الثاني» is a POSITION, and the planner's own key is a hash.

    `build_student_options` keys each alternative on
    `sha256("-".join(section ids))[:16]`, regenerated whenever the draft version
    changes. A sentence cannot name one. So the ordinal travels as an ordinal for
    the planner to resolve against what it is currently displaying, and
    `select_alternative` still refuses anything not among the offered keys — the
    ref is a hint into that list, never an authorisation to write to it.
    """
    handoff = handoff_for_question(PREFER)
    assert handoff is not None
    assert handoff.as_payload() == {
        "type": "OPEN_STUDENT_PLANNER",
        "intent": "SELECT_PREFERRED_ALTERNATIVE",
        "requires_confirmation": True,
        "registration_modified": False,
        "alternative_ref": "ALT_2",
    }


def test_a_preference_with_no_ordinal_omits_the_key_rather_than_defaulting_to_the_first() -> None:
    """An absent ordinal must be an absent KEY, not an empty string.

    «احفظ جدولي المفضل» names no position. Emitting `"alternative_ref": ""` would
    make a client test the value as well as the key, and the value that reads
    most naturally as "nothing chosen" is the one a careless client would treat
    as the first alternative.
    """
    handoff = handoff_for_question("احفظ جدولي المفضل.")
    assert handoff is not None
    assert handoff.intent == INTENT_SELECT_PREFERRED_ALTERNATIVE
    assert "alternative_ref" not in handoff.as_payload()


ORDINALS: tuple[tuple[str, str], ...] = (
    ("احفظ الخيار الثاني كجدولي المفضل.", "ALT_2"),
    ("احفظ الخيار الأول كجدولي المفضل.", "ALT_1"),
    ("خلّ البديل رقم ٣ هو المفضل عندي.", "ALT_3"),
    # Pins the fold on the TABLE side. «بدائل» carries a ya-hamza, which
    # `normalise` folds to «بدايل» — so the spellings are folded at definition
    # the way `arabic_text.STOPWORDS` is. Stored raw, this entry would be
    # compared against an already-folded question and never match, which is the
    # bug that module's comment records for four of the commonest Arabic words.
    ("من البدائل الثاني هو المفضل عندي", "ALT_2"),
    ("save option 3 as my preferred timetable", "ALT_3"),
    ("make the second option my preferred schedule", "ALT_2"),
    ("I want option #2 saved as preferred", "ALT_2"),
    # The ordinal names a term, not an alternative. It is two tokens away from
    # «الخيار» and belongs to «الفصل», so adjacency is what drops it — a
    # sentence-wide search would answer a different question with a real payload.
    ("ابنِ الخيار الذي يناسب الفصل الثاني واحفظه كجدولي المفضل", ""),
    # Nine is what `build_plans` can produce: three methods x `_top_k_method(k=3)`,
    # de-duplicated. A tenth was never on offer, so there is nothing to point at.
    ("احفظ الخيار العاشر كجدولي المفضل", ""),
    ("save option 12 as preferred", ""),
    # No option word at all: an ordinal on its own names nothing.
    ("احفظ الثاني", ""),
    ("", ""),
)


@pytest.mark.parametrize(("question", "expected"), ORDINALS)
def test_the_ordinal_is_read_beside_the_option_word_or_not_at_all(
    question: str, expected: str
) -> None:
    assert alternative_ref_in(question) == expected


def test_the_ceiling_matches_what_the_generator_can_actually_produce() -> None:
    """Nine is not a round number chosen for safety.

    `planner_builder.build_plans` loops `for method in ("A", "B", "C")` over
    `_top_k_method(method, k=3)`, and `build_student_options` then drops duplicate
    signatures. Nine is the ceiling; anything above it names a timetable the
    student cannot have been shown.
    """
    assert MAX_ALTERNATIVES == 9
    assert alternative_ref_in(f"save option {MAX_ALTERNATIVES} as preferred") == "ALT_9"
    assert alternative_ref_in(f"save option {MAX_ALTERNATIVES + 1} as preferred") == ""


# ==========================================================================
# What every referral must say, whichever route it is.
# ==========================================================================


@pytest.mark.parametrize("handoff", ALL_HANDOFFS, ids=lambda h: h.intent)
def test_every_referral_says_it_exists_routes_into_the_planner_and_leaves_registration_alone(
    handoff: ActionHandoff,
) -> None:
    """The three failures of the live answer, asserted as their opposites, for
    every route rather than for the one that produced them.

    The last pair is the one that matters. The live answer told a student to
    delete real registrations to obtain a planning draft; «لن يحذف أو يغيّر
    تسجيلك الرسمي» and its English twin are the sentences that stop the next
    version of that advice, and they are asserted in both languages because an
    English-speaking student reads only the second.
    """
    arabic = handoff.answer("Arabic")
    english = handoff.answer("English")

    # 1. it is available. Not «لا يمكن», which is what the model said live.
    assert "لا يمكن" not in arabic
    assert "cannot" not in english.lower()
    assert "not available" not in english.lower()
    # 2. the student is routed INTO the planner, not out to the portal.
    assert "افتح المخطط الدراسي" in arabic
    assert "Open the study planner" in english
    # 3. and told plainly that nothing was changed, in both languages.
    assert "لن يحذف أو يغيّر تسجيلك الرسمي" in arabic
    assert "يبقى تسجيلك الفعلي كما هو" in arabic
    assert "does not delete or change your official registration" in english
    assert "your actual registration stays exactly as it is" in english
    assert handoff.registration_modified is False


#: Phrases that report a change as already made. Written out because the failure
#: mode is not "the answer is rude" — a student who reads «تم حفظ الخيار الثاني»
#: stops, believing the planner holds a preference it does not, and «سجّلتك» is
#: the same sentence the live answer's advice would have made true by hand.
DONE_AR = ("تم حفظ", "تم اختيار", "تم التعديل", "تم تسجيلك", "سجّلتك", "حفظت لك", "تم إعادة")
DONE_EN = (
    "i have saved",
    "i saved",
    "has been saved",
    "i have selected",
    "has been selected",
    "has been updated",
    "you are now registered",
    "i have registered",
    "i have rebuilt",
)


@pytest.mark.parametrize("handoff", ALL_HANDOFFS, ids=lambda h: h.intent)
def test_no_referral_reports_a_change_that_has_not_happened(handoff: ActionHandoff) -> None:
    arabic = handoff.answer("Arabic")
    english = handoff.answer("English").lower()
    for phrase in DONE_AR:
        assert phrase not in arabic, f"{handoff.intent} claims «{phrase}»"
    for phrase in DONE_EN:
        assert phrase not in english, f"{handoff.intent} claims '{phrase}'"


@pytest.mark.parametrize("handoff", ALL_HANDOFFS, ids=lambda h: h.intent)
def test_every_referral_is_genuinely_bilingual(handoff: ActionHandoff) -> None:
    """Not "has two fields" — has two DIFFERENT non-empty texts.

    A default that returns the Arabic for both is invisible to any assertion made
    in one language, and this repository has shipped exactly that shape before:
    an Arabic-only screen passed 14 of 14 tests because no assertion named an
    Arabic string.
    """
    arabic = handoff.answer("Arabic")
    english = handoff.answer("English")
    assert arabic.strip() and english.strip()
    assert arabic != english
    assert any("؀" <= ch <= "ۿ" for ch in arabic), "the Arabic text is not Arabic"
    assert not any("؀" <= ch <= "ۿ" for ch in english), "Arabic leaked into the English"


def test_an_english_planner_question_is_answered_in_english(monkeypatch) -> None:
    ExecutionSpy(monkeypatch)
    payload = _ask(ScriptedClient([]), question="Save the second option as my preferred schedule")
    assert payload["action"]["alternative_ref"] == "ALT_2"
    assert "Open the study planner" in payload["answer"]
    assert "does not delete or change your official registration" in payload["answer"]


# ==========================================================================
# What must NOT be routed.
# ==========================================================================


def test_a_rebuild_is_not_routed_from_the_question(monkeypatch) -> None:
    """It keeps the executor path, and that is the point.

    `build_my_timetable` refuses a rebuild as its first statement, where the
    arguments are known and the refusal is auditable against them. Recognising
    the same request from the question as well would give one rule two
    implementations — and the audited one is the one that would quietly stop
    running, because the router would answer first every time.
    """
    assert handoff_for_question(REBUILD) is None

    spy = ExecutionSpy(monkeypatch, result=REFUSAL)
    payload = _ask(
        ScriptedClient([_call("build_my_timetable", {"keep_current_sections": False})]),
        question=REBUILD,
    )
    # Two executions, and both matter: the server-side policy prefetch runs
    # through the same registry, which is why `spy.count == 0` on a routed turn
    # proves retrieval was skipped as well as the tool.
    assert [name for name, _ in spy.calls] == ["policy_lookup", "build_my_timetable"], (
        "the rebuild bypassed the executor that refuses it"
    )
    assert payload["action"]["intent"] == INTENT_REBUILD_WITHOUT_CURRENT_SECTIONS


def test_an_adviser_is_not_sent_to_a_screen_that_will_refuse_them(monkeypatch) -> None:
    """Every planner-draft view builds its principal with
    `AdvisorPrincipal.for_student` and answers 403 to anything else.

    So «افتح المخطط الدراسي» offered to an adviser names a door that is locked
    against them — the same defect as denying a feature that exists, pointed the
    other way. Staff fall through to the ordinary loop instead.
    """
    ExecutionSpy(monkeypatch)
    client = ScriptedClient([], final="جواب المرشد.")
    payload = va.answer_virtual_advisor(
        question=PREFER,
        principal=AdvisorPrincipal(role=ROLE_ADVISOR, student_id=MINE),
        academic_year=1448,
        term=1,
        client=client,
    )
    assert payload["action"] is None
    assert client.requests, "the adviser's question was never answered by anything"


def test_an_ordinary_question_is_not_routed() -> None:
    """The router abstains by default, and the default has to survive.

    A route replaces a real answer with a referral, so a family that fires on
    anything unexpected is strictly worse than no router at all.
    """
    for question in (
        "وش جدولي المسجل حاليًا؟",
        "ليش AI491 مقفل؟",
        "كم الحد الأعلى للساعات؟",
        "كيف حالي؟",
        "",
    ):
        assert handoff_for_question(question) is None


# ==========================================================================
# Fail closed.
# ==========================================================================


def test_an_unknown_action_cannot_be_constructed() -> None:
    """A route the interface has no screen for is a button that goes nowhere."""
    with pytest.raises(ValueError, match="unknown action"):
        ActionHandoff(
            action="OPEN_REGISTRATION_PORTAL",  # the very place the live answer sent them
            intent=INTENT_VIEW_ALTERNATIVES,
            answer_ar="…",
            answer_en="…",
        )


def test_an_unknown_intent_cannot_be_constructed() -> None:
    with pytest.raises(ValueError, match="unknown intent"):
        ActionHandoff(
            action=OPEN_STUDENT_PLANNER,
            intent="DELETE_REGISTRATION",
            answer_ar="…",
            answer_en="…",
        )


def test_a_reference_cannot_be_attached_to_a_route_that_offers_no_list() -> None:
    """«الخيار الثاني» means nothing to a rebuild: there is no list yet to be
    second in. Refused on the type rather than checked at each call site."""
    rebuild = HANDOFFS["REBUILD_REQUIRES_PLANNER_CONFIRMATION"]
    assert rebuild.accepts_alternative_ref is False
    with pytest.raises(ValueError, match="carries no alternative reference"):
        rebuild.with_alternative_ref("ALT_2")


def test_the_rebuild_family_is_deliberately_absent_from_the_question_router() -> None:
    assert set(ROUTED_INTENTS) == {
        IntentFamily.PLANNER_VIEW_ALTERNATIVES,
        IntentFamily.PLANNER_SELECT_PREFERRED,
        IntentFamily.PLANNER_EDIT_DRAFT,
    }
    assert IntentFamily.PLANNER_REBUILD not in ROUTED_INTENTS


# ==========================================================================
# The stored turn.
# ==========================================================================


def test_a_routed_turn_is_stored_as_an_answer_and_not_as_an_abstention(monkeypatch) -> None:
    """`derive_outcome` reads the payload, and ABSTAIN sets `STATUS_ABSTAINED`.

    This is why the route returns BEFORE the server-side policy prefetch rather
    than after it, and the reason is measured rather than assumed. Against the
    loaded store the three routed questions retrieve:

        TT02  «سوِّ لي أكثر من خيار للجدول»  retrieved     8 policies, 2 citable
        TT24  «احفظ الخيار الثاني…»          none_matched  0 policies
        TT26  «عدّلت قائمة المقررات…»         retrieved     8 policies, 2 citable

    TT24 is the one that would abstain: `policy_required` defaults True for a
    payload that does not set it, `none_matched` becomes POLICY_NOT_FOUND, and
    POLICY_NOT_FOUND with no citations is ABSTAIN — a route the student can act
    on, stored as a question the adviser declined to answer. The other two are
    the second half of the same argument: eight policy records would ride along
    in `tool_results`, which the UI renders as the evidence behind the answer,
    beside a referral that cites none of them.
    """
    ExecutionSpy(monkeypatch)
    payload = _ask(ScriptedClient([]), question=PREFER)
    assert derive_outcome(payload).disposition == FinalDisposition.PASS


def test_a_routed_turn_attributes_the_text_to_no_model(monkeypatch) -> None:
    """`_persist_answer` stores `result["model"]` on the turn. Naming the
    configured model would record a provider that never saw the question as the
    author of a constant in this repository."""
    ExecutionSpy(monkeypatch)
    payload = _ask(ScriptedClient([]), question=VIEW)
    assert payload["model"] == ""
    assert payload["tool_results"] == []
    assert payload["citations"] == []
    assert payload["cited_policy_ids"] == []


# ==========================================================================
# The owner's batch labels, read from the batch.
# ==========================================================================


def _batch_rows() -> list[dict]:
    return yaml.safe_load(BATCH.read_text(encoding="utf-8"))["questions"]


def _labelled_intents() -> list[tuple[str, str, str]]:
    """(id, question, intent) for every batch row whose label names an intent.

    Read from the file rather than copied, so that adding a labelled route to the
    batch and forgetting to implement it is a failure here rather than a silent
    divergence discovered on the next live run.
    """
    out: list[tuple[str, str, str]] = []
    for row in _batch_rows():
        for fact in row.get("expected_facts") or []:
            text = str(fact).strip()
            if text.startswith("intent is "):
                out.append((row["id"], row["ar"], text[len("intent is ") :].strip()))
    return out


@pytest.mark.parametrize(("qid", "question", "intent"), _labelled_intents(), ids=lambda v: str(v))
def test_the_batch_labels_that_name_an_intent_get_that_intent(
    qid: str, question: str, intent: str
) -> None:
    handoff = handoff_for_question(question)
    assert handoff is not None, f"{qid} is labelled {intent} and was not routed"
    assert handoff.intent == intent
    assert handoff.action == OPEN_STUDENT_PLANNER


def test_the_labelled_rows_are_the_ones_this_test_thinks_they_are() -> None:
    """Guards the harness, not the code.

    `_labelled_intents()` parses free text out of the batch. If the label wording
    changes, the parametrisation silently empties and every assertion above it
    passes by having nothing to check — the shape of vacuous test this repository
    has shipped before.
    """
    assert {qid for qid, _, _ in _labelled_intents()} == {"TT02", "TT24", "TT26"}


def test_the_third_routed_row_is_labelled_like_the_other_two() -> None:
    """TT26's label used to disagree with the route, and the disagreement was pinned.

    It read `expected_action: null`, `required_tools: [build_my_timetable]` — a label
    describing a system that cannot answer «عدّلت قائمة المقررات؛ أعد بناء البدائل
    بناءً على التعديل الجديد». The edit happened in the planner, on a draft;
    `_exec_build_my_timetable` builds from `must_include` plus
    `recommend_next_courses` and has no access to a draft, so a build here returns
    alternatives from the SYSTEM's course list and presents them as "based on your
    edit" — a fabrication with a tool call behind it.

    The label is now corrected, so this row is an ordinary member of the
    parametrisation above. What remains worth pinning is that the tool it used to
    REQUIRE is now merely allowed: requiring it would score the fabrication as
    correct behaviour.
    """
    row = next(r for r in _batch_rows() if r["id"] == "TT26")
    assert row["expected_action"] == OPEN_STUDENT_PLANNER
    assert row["required_tools"] == []
    assert row["allowed_tools"] == ["build_my_timetable"]

    handoff = handoff_for_question(row["ar"])
    assert handoff is not None
    assert handoff.intent == INTENT_EDIT_DRAFT


def test_a_routed_question_never_reaches_a_provider(monkeypatch) -> None:
    """The hand-off is decided before generation, so no provider is contacted.

    `ScriptedClient([])` raises if asked for a completion, so a route that fell
    through to the model fails here rather than costing a paid call and returning
    prose where a structured action belongs.
    """
    # Filtered on the FAMILY, not on `expected_action`. TT27-TT29 also expect
    # OPEN_STUDENT_PLANNER, and they reach it the other way — through
    # `build_my_timetable` refusing, which needs a model call to request the tool.
    # Selecting on the action alone would assert "no provider" about the three
    # cases whose route depends on one.
    routed = {str(f) for f in ROUTED_INTENTS}
    for row in _batch_rows():
        if row["expected_family"] not in routed:
            continue
        assert row.get("expected_action"), f"{row['id']} is routed but labels no action"
        ExecutionSpy(monkeypatch)
        payload = _ask(ScriptedClient([]), question=row["ar"])
        assert payload["action"], row["id"]
        assert payload["action"]["type"] == row["expected_action"], row["id"]
        assert payload["model"] == "", f"{row['id']} named a provider that never saw it"
        assert payload["usage"] == {}, f"{row['id']} recorded provider usage"
