"""What runs, in what order, and what is allowed to leave.

The remote boundary is a sequence, and a sequence can only be tested by watching
where it stops. So most of these tests assert on two counters:

    registry.execute calls   — did anything read the database?
    captured provider calls  — did anything leave the institution?

A refusal that happens one line too late still passes a "the model did not see
it" assertion while having already queried the record. Counting both is the
difference between proving the order and asserting the outcome.

Every request the fake client receives is retained verbatim, so the final check
in most tests is the blunt one: the real student id and the real name appear
NOWHERE in anything that was sent.
"""

from __future__ import annotations

import dataclasses
import json

import pytest
from django.test import override_settings

from core.models import Course, Student, StudentCourse
from core.services import virtual_advisor as va
from core.services import virtual_advisor_capabilities as caps
from core.services.advisor_principal import AdvisorPrincipal
from core.services.advisor_remote_boundary import (
    CachedToolExecution,
    LocalToolBoundary,
    RemoteToolBoundary,
    boundary_for_scope,
)
from core.services.llm_backend import ChatResult, ToolCallRequest, ToolChatResult
from core.services.llm_remote_privacy import (
    PROJECTORS,
    RemoteIdentityMap,
)
from core.services.rbac import ROLE_ADVISOR, ROLE_STUDENT

pytestmark = pytest.mark.django_db

MINE = 4502156
OUTSIDE = 4502157
NAME = "عبدالله محمد"
FIXED = "TESTNONCE0001"

STUDENT_SCOPE = {"role": ROLE_STUDENT, "student_id": MINE}
ADVISER_SCOPE = {"role": ROLE_ADVISOR, "advisor_id": "ADV-1", "departments": []}


# ── fakes ────────────────────────────────────────────────────────


class ScriptedClient:
    """An LLM that does what the test says, and keeps every request it was given.

    `chat_with_tools` pops one scripted turn per call; `chat` returns the final
    text. Both append the exact `messages` list to `requests`, which is what the
    privacy assertions read — not a summary of it, because a summary is another
    place for a leak to hide.
    """

    def __init__(self, turns: list[ToolChatResult], final: str = "الإجابة النهائية.") -> None:
        self.turns = list(turns)
        self.final = final
        self.requests: list[list[dict]] = []
        self.tool_schemas: list[list[dict]] = []

    def resolve_model(self, model=None) -> str:
        return model or "test-model"

    def chat_with_tools(self, messages, *, tools=None, **kwargs) -> ToolChatResult:
        self.requests.append(json.loads(json.dumps(messages, default=str)))
        self.tool_schemas.append(tools or [])
        if self.turns:
            return self.turns.pop(0)
        return _answer_turn(self.final)

    def chat(self, messages, **kwargs) -> ChatResult:
        self.requests.append(json.loads(json.dumps(messages, default=str)))
        return ChatResult(content=self.final, model="test-model", usage={})

    @property
    def sent_text(self) -> str:
        return json.dumps(self.requests, ensure_ascii=False)


def _call(name: str, arguments: dict, call_id: str = "c1") -> ToolChatResult:
    request = ToolCallRequest(
        id=call_id, name=name, arguments=arguments, raw_arguments=json.dumps(arguments)
    )
    return ToolChatResult(
        content="",
        tool_calls=(request,),
        model="test-model",
        usage={},
        assistant_message={
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            ],
        },
    )


def _answer_turn(text: str) -> ToolChatResult:
    return ToolChatResult(
        content=text,
        tool_calls=(),
        model="test-model",
        usage={},
        assistant_message={"role": "assistant", "content": text},
    )


class ExecutionSpy:
    """Counts executor calls and remembers the arguments each one received.

    Patches the CLASS, not the registry instance. `get_default_registry()` is a
    singleton, and `monkeypatch.setattr(instance, "execute", ...)` undoes itself
    by assigning the original bound method back — which leaves a permanent
    instance attribute shadowing the class one. Every later test that patches
    `AdvisorCapabilityRegistry.execute` then silently patches nothing. That
    happened: it turned an "unavailable policy store" test green by making the
    store work.
    """

    def __init__(self, monkeypatch, result=None) -> None:
        self.calls: list[tuple[str, dict]] = []
        original = caps.AdvisorCapabilityRegistry.execute

        def counted(registry_self, name, args, *, scope=None, ctx=None):
            self.calls.append((name, dict(args or {})))
            if result is not None:
                return {**result, "tool": name}
            return original(registry_self, name, args, scope=scope, ctx=ctx)

        monkeypatch.setattr(caps.AdvisorCapabilityRegistry, "execute", counted)

    @property
    def count(self) -> int:
        return len(self.calls)


@pytest.fixture
def roster() -> None:
    Student.objects.create(
        student_id=MINE, name=NAME, program="AI", section="M", advisor_id="ADV-1"
    )
    Student.objects.create(
        student_id=OUTSIDE, name="طالب آخر", program="DS", section="F", advisor_id="ADV-9"
    )


def _remote(scope=STUDENT_SCOPE, **kwargs) -> RemoteToolBoundary:
    kwargs.setdefault("identities", RemoteIdentityMap(nonce=FIXED))
    return RemoteToolBoundary(scope=scope, **kwargs)


def _run_loop(client, boundary, scope=STUDENT_SCOPE, messages=None):
    telemetry = {"tools_called": [], "boundary_refusals": [], "iterations": 0}
    answer, usage, local, provider = va._run_agent_loop(
        llm=client,
        resolved_model="test-model",
        messages=messages if messages is not None else [{"role": "user", "content": "س"}],
        scope=scope,
        ctx={"academic_year": 1448, "term": 1},
        telemetry=telemetry,
        boundary=boundary,
    )
    return answer, local, provider, telemetry


# ── 1. the happy path, and the split it produces ─────────────────


def test_local_result_is_complete_and_provider_result_is_projected(roster, monkeypatch) -> None:
    spy = ExecutionSpy(
        monkeypatch,
        result={
            "ok": True,
            "student_context": {
                "student": {"student_id": MINE, "name": NAME, "program": "AI", "gpa": 3.4},
            },
        },
    )
    client = ScriptedClient([_call("get_student_context", {})])
    _, local, provider, _ = _run_loop(client, _remote())

    assert spy.count == 1
    # The local half keeps everything the evidence panel and the audit record
    # were built to show.
    assert local[0]["student_context"]["student"]["student_id"] == MINE
    assert local[0]["student_context"]["student"]["name"] == NAME
    # The provider half keeps the academic facts and nothing that names anyone.
    assert provider[0]["student_context"]["student"] == {"program": "AI", "gpa": 3.4}
    assert str(MINE) not in client.sent_text
    assert NAME not in client.sent_text


def test_the_two_lists_are_the_same_objects_on_a_local_backend(roster, monkeypatch) -> None:
    """Reversibility, asserted rather than assumed: with `LLM_BACKEND=local` the
    loop hands the same object to both readers, exactly as it did before the
    boundary existed."""
    ExecutionSpy(monkeypatch, result={"ok": True, "student_context": {"student": {"name": NAME}}})
    client = ScriptedClient([_call("get_student_context", {})])
    _, local, provider, _ = _run_loop(client, LocalToolBoundary())

    assert local[0] is provider[0]
    assert NAME in client.sent_text


# ── 2. failure semantics: where each refusal stops ───────────────


def test_a_denied_capability_is_refused_before_any_execution(roster, monkeypatch) -> None:
    spy = ExecutionSpy(monkeypatch)
    client = ScriptedClient([_call("portfolio_triage", {})])
    _, local, provider, telemetry = _run_loop(client, _remote(ADVISER_SCOPE), ADVISER_SCOPE)

    assert spy.count == 0, "a DENY capability must not reach the registry"
    assert local == [] and provider == []
    assert telemetry["boundary_refusals"] == [
        {"name": "portfolio_triage", "stage": "pre_execution"}
    ]
    assert "CAPABILITY_NOT_AVAILABLE_FOR_REMOTE_BACKEND" in client.sent_text


def test_a_denied_capability_is_never_advertised(roster) -> None:
    boundary = _remote(ADVISER_SCOPE)
    schemas = caps.get_default_registry().tool_schemas_for_scope(ADVISER_SCOPE)
    offered = {s["function"]["name"] for s in boundary.tool_schemas(schemas)}
    assert "portfolio_triage" not in offered


@pytest.mark.parametrize(
    "arguments",
    [
        {"student_id": OUTSIDE},
        {"advisor_id": "ADV-9"},
        {"email": "someone@taibahu.edu.sa"},
        {"student_ids": [MINE, OUTSIDE]},
    ],
)
def test_a_forged_identity_argument_is_refused_before_execution(
    roster, monkeypatch, arguments: dict
) -> None:
    spy = ExecutionSpy(monkeypatch)
    client = ScriptedClient([_call("get_student_context", arguments)])
    _, _, _, telemetry = _run_loop(client, _remote())

    assert spy.count == 0
    assert telemetry["boundary_refusals"][0]["stage"] == "pre_execution"
    assert str(OUTSIDE) not in client.sent_text


@pytest.mark.parametrize(
    "reference",
    [
        "STUDENT_REF_TESTNONCE0001_7",  # right nonce, never issued
        "STUDENT_REF_OTHERNONCE99_1",  # wrong nonce
        "STUDENT_1",  # the obvious guess
        "",  # empty
    ],
)
def test_an_unknown_stale_or_guessed_reference_is_refused_before_execution(
    roster, monkeypatch, reference: str
) -> None:
    spy = ExecutionSpy(monkeypatch)
    client = ScriptedClient([_call("get_student_context", {"student_ref": reference})])
    _, _, _, telemetry = _run_loop(client, _remote(ADVISER_SCOPE), ADVISER_SCOPE)

    assert spy.count == 0
    assert telemetry["boundary_refusals"][0]["stage"] == "pre_execution"


def test_resolution_is_not_permission(roster, monkeypatch) -> None:
    """THE test for step four. A reference is issued for a student this adviser
    may not read — which the map allows, because minting is not authorising — and
    the model then uses it. Resolution succeeds; execution must still not happen.

    If `authorise_resolved_arguments` were dropped, this is the test that goes
    red, and nothing else would: the reference is genuine, the nonce is right,
    and the argument reaching the executor would be a real student id.
    """
    identities = RemoteIdentityMap(nonce=FIXED)
    smuggled = identities.reference_for(OUTSIDE)

    spy = ExecutionSpy(monkeypatch)
    client = ScriptedClient([_call("get_student_context", {"student_ref": smuggled})])
    boundary = RemoteToolBoundary(scope=ADVISER_SCOPE, identities=identities)
    _, local, provider, telemetry = _run_loop(client, boundary, ADVISER_SCOPE)

    assert identities.resolve(smuggled) == OUTSIDE, "the reference itself is genuine"
    assert spy.count == 0, "a resolvable reference is not an authorised one"
    assert local == [] and provider == []
    assert telemetry["boundary_refusals"][0]["stage"] == "pre_execution"


def test_a_capability_with_no_projector_is_refused_before_execution(roster, monkeypatch) -> None:
    """A configuration error, detectable in advance — so it stops in advance,
    rather than reading a record it would then have to throw away."""
    monkeypatch.delitem(PROJECTORS, "get_student_context")
    spy = ExecutionSpy(monkeypatch)
    client = ScriptedClient([_call("get_student_context", {})])
    _, _, _, telemetry = _run_loop(client, _remote())

    assert spy.count == 0
    assert telemetry["boundary_refusals"][0]["stage"] == "pre_execution"


def test_a_result_the_projector_rejects_is_never_serialised(roster, monkeypatch, caplog) -> None:
    """Not every bad shape is knowable before execution. This one is not: the
    executor returns something the projector will not accept, so the read has
    already happened. What must still hold is that nothing is SENT — and that the
    result does not survive in the log line or the exception either.
    """
    secret = f"{NAME} {MINE} secret-payload"
    spy = ExecutionSpy(monkeypatch, result={"ok": True, "student_context": secret})

    def refuse(result, identities):
        from core.services.llm_backend import LLMPrivacyError

        raise LLMPrivacyError("unexpected shape")

    monkeypatch.setitem(PROJECTORS, "get_student_context", refuse)
    client = ScriptedClient([_call("get_student_context", {})])
    with caplog.at_level("DEBUG"):
        _, local, provider, telemetry = _run_loop(client, _remote())

    assert spy.count == 1, "this failure is only detectable after execution"
    assert provider == [], "nothing projected means nothing retained for the provider"
    assert local == [], "and a result that cannot be projected is not half-recorded"
    assert telemetry["boundary_refusals"][0]["stage"] == "projection"
    assert secret not in client.sent_text
    assert secret not in caplog.text
    assert str(MINE) not in caplog.text


# ── 3. the duplicate-call cache ──────────────────────────────────


def test_a_duplicate_call_reuses_both_halves_and_reprojects_neither(roster, monkeypatch) -> None:
    spy = ExecutionSpy(
        monkeypatch,
        result={"ok": True, "student_context": {"student": {"student_id": MINE, "name": NAME}}},
    )
    boundary = _remote()
    projections: list[str] = []
    original = boundary.project_tool_result

    def counted(tool_name, result):
        projections.append(tool_name)
        return original(tool_name, result)

    boundary.project_tool_result = counted  # type: ignore[method-assign]

    client = ScriptedClient(
        [_call("get_student_context", {}, "a"), _call("get_student_context", {}, "b")]
    )
    _, local, provider, _ = _run_loop(client, boundary)

    assert spy.count == 1, "the second identical call must not re-query"
    assert projections == ["get_student_context"], "and must not re-project"
    assert len(local) == 1 and len(provider) == 1

    # Both tool messages the provider saw came from the cached PROVIDER half.
    # Read from the LAST request, which carries the whole accumulated
    # conversation; earlier requests are prefixes of it.
    tool_messages = [m for m in client.requests[-1] if m.get("role") == "tool"]
    assert len(tool_messages) == 2
    for message in tool_messages:
        assert str(MINE) not in message["content"]
        assert NAME not in message["content"]
    assert "duplicate call" in tool_messages[-1]["content"]


def test_two_references_to_the_same_student_share_one_cache_entry(roster, monkeypatch) -> None:
    """The key is built from the RESOLVED arguments, so the cache is about the
    student rather than about the string the model happened to use."""
    identities = RemoteIdentityMap(nonce=FIXED)
    reference = identities.reference_for(MINE)
    spy = ExecutionSpy(monkeypatch, result={"ok": True, "student_context": {"student": {}}})
    client = ScriptedClient(
        [
            _call("get_student_context", {"student_ref": reference}, "a"),
            _call("get_student_context", {"student_ref": reference}, "b"),
        ]
    )
    _run_loop(
        client,
        RemoteToolBoundary(scope=ADVISER_SCOPE, identities=identities),
        ADVISER_SCOPE,
    )
    assert spy.count == 1


def test_the_boundary_keeps_the_identity_map_it_was_given() -> None:
    """The second of two independent guards against the `or` bug. A map that has
    issued nothing is the NORMAL state at construction, and `identities or
    RemoteIdentityMap()` discards it — the caller then holds a map whose
    references the boundary has never heard of."""
    fresh = RemoteIdentityMap(nonce=FIXED)
    assert RemoteToolBoundary(scope=STUDENT_SCOPE, identities=fresh).identities is fresh
    # And a caller that supplies none still gets one.
    assert RemoteToolBoundary(scope=STUDENT_SCOPE).identities is not None


def test_the_cache_pair_is_frozen() -> None:
    """A mutable pair invites "just patch the provider half here", which is the
    cross-boundary reconstruction the type exists to prevent."""
    pair = CachedToolExecution({"a": 1}, {"b": 2})
    with pytest.raises(dataclasses.FrozenInstanceError):
        pair.local_result = {}  # type: ignore[misc]


def test_telemetry_records_the_models_arguments_not_the_resolved_ones(roster, monkeypatch) -> None:
    """Telemetry is stored and shipped. After resolution the arguments carry a
    real student id, and the dedup key built from them carries it too — neither
    may appear."""
    identities = RemoteIdentityMap(nonce=FIXED)
    reference = identities.reference_for(MINE)
    ExecutionSpy(monkeypatch, result={"ok": True, "student_context": {"student": {}}})
    client = ScriptedClient([_call("get_student_context", {"student_ref": reference})])
    _, _, _, telemetry = _run_loop(
        client, RemoteToolBoundary(scope=ADVISER_SCOPE, identities=identities), ADVISER_SCOPE
    )

    recorded = json.dumps(telemetry, ensure_ascii=False, default=str)
    assert reference in recorded
    assert str(MINE) not in recorded


# ── 4. the adviser round trip, end to end ────────────────────────


def test_adviser_question_with_a_real_id_makes_the_whole_round_trip(roster, monkeypatch) -> None:
    """The sequence the boundary exists for, in one test:

    question carries a real accessible id
    -> the provider sees only STUDENT_REF_<nonce>_1
    -> the model calls a tool with student_ref
    -> the server resolves it
    -> scope is checked AGAIN
    -> the capability executes for the correct student
    -> the projected result comes back carrying no identity
    """
    identities = RemoteIdentityMap(nonce=FIXED)
    boundary = RemoteToolBoundary(scope=ADVISER_SCOPE, identities=identities, known_names=(NAME,))
    spy = ExecutionSpy(
        monkeypatch,
        result={
            "ok": True,
            "student_context": {"student": {"student_id": MINE, "name": NAME, "gpa": 3.4}},
        },
    )

    question = f"ما وضع الطالب {MINE}؟"
    messages = boundary.sanitise_messages([{"role": "user", "content": question}])
    reference = f"STUDENT_REF_{FIXED}_1"
    assert messages[0]["content"] == f"ما وضع الطالب {reference}؟"

    client = ScriptedClient([_call("get_student_context", {"student_ref": reference})])
    _, local, provider, _ = _run_loop(client, boundary, ADVISER_SCOPE, messages=messages)

    # Resolved to the right person, and the executor saw the real id.
    assert spy.calls == [("get_student_context", {"student_id": MINE})]
    assert local[0]["student_context"]["student"]["student_id"] == MINE
    assert provider[0]["student_context"]["student"] == {"gpa": 3.4}
    assert str(MINE) not in client.sent_text
    assert NAME not in client.sent_text
    assert reference in client.sent_text


def test_student_schemas_offer_no_identity_parameter_at_all(roster) -> None:
    student_schemas = _remote().tool_schemas(
        caps.get_default_registry().tool_schemas_for_scope(STUDENT_SCOPE)
    )
    for schema in student_schemas:
        properties = (schema["function"].get("parameters") or {}).get("properties") or {}
        assert "student_id" not in properties
        assert "student_ref" not in properties, "a student's session already names them"


def test_adviser_schemas_substitute_student_ref_for_student_id(roster) -> None:
    adviser_schemas = _remote(ADVISER_SCOPE).tool_schemas(
        caps.get_default_registry().tool_schemas_for_scope(ADVISER_SCOPE)
    )
    by_name = {s["function"]["name"]: s for s in adviser_schemas}
    properties = (by_name["get_student_context"]["function"]["parameters"] or {})["properties"]
    assert "student_id" not in properties
    assert "student_ref" in properties


def test_a_student_session_refuses_a_student_ref_argument(roster, monkeypatch) -> None:
    """Never advertised, so a model sending one is confused or probing. Both get
    the same answer as a forged `student_id`."""
    identities = RemoteIdentityMap(nonce=FIXED)
    reference = identities.reference_for(MINE)
    spy = ExecutionSpy(monkeypatch)
    client = ScriptedClient([_call("get_student_context", {"student_ref": reference})])
    _, _, _, telemetry = _run_loop(
        client, RemoteToolBoundary(scope=STUDENT_SCOPE, identities=identities)
    )
    assert spy.count == 0
    assert telemetry["boundary_refusals"][0]["stage"] == "pre_execution"


# ── 5. the split survives every answer path ──────────────────────


def _seed_courses() -> None:
    course = Course.objects.create(course_code="AI221", department="AI", credit_hours=3)
    StudentCourse.objects.create(
        student=Student.objects.get(student_id=MINE), course=course, status="Passed"
    )


def _principal() -> AdvisorPrincipal:
    return AdvisorPrincipal(role=ROLE_STUDENT, student_id=MINE)


def context_json_of(payload: dict) -> str:
    return json.dumps(payload["verified_context"], ensure_ascii=False, default=str)


@override_settings(LLM_BACKEND="alibaba")
def test_full_entry_no_path_ever_sends_the_students_identity(roster, monkeypatch) -> None:
    """The whole entry point, not the loop in isolation.

    Drives `answer_virtual_advisor` through the loop, a duplicate call, the
    forced final answer and the grounding retry in one run, then applies one
    assertion to everything that was sent.
    """
    _seed_courses()
    monkeypatch.setattr(va, "_max_tool_iterations", lambda: 2)
    ExecutionSpy(
        monkeypatch,
        result={
            "ok": True,
            "student_context": {"student": {"student_id": MINE, "name": NAME, "gpa": 3.4}},
        },
    )
    client = ScriptedClient(
        [
            _call("get_student_context", {}, "a"),
            _call("get_student_context", {}, "b"),  # duplicate -> cached pair
        ],
        # An id that is in no evidence anywhere, so the grounding retry fires.
        # `MINE` would not: it is in the local context, which is exactly what
        # the grounding check reads.
        final="الطالب 8887771 يمكنه التسجيل.",
    )

    payload = va.answer_virtual_advisor(
        question=f"أنا {NAME}، رقمي {MINE}. كم ساعة أستطيع تسجيلها؟",
        principal=_principal(),
        academic_year=1448,
        term=1,
        history=[{"role": "user", "content": f"سؤالي السابق عن {MINE}"}],
        client=client,
    )

    assert payload["ok"] is True
    assert payload["agent"]["forced_final"] is True
    assert payload["agent"]["grounding_retry"] is True

    sent = client.sent_text
    assert str(MINE) not in sent, "the id reached a provider on some path"
    assert NAME not in sent, "the name reached a provider on some path"
    assert "STUDENT_REF_" in sent, "the question's id should survive as a reference"

    # AND the projection actually ran. The two assertions above are necessary and
    # NOT sufficient: the sanitiser aliases the student's own id and redacts their
    # own name wherever it finds them, including inside an unprojected context
    # blob — so removing `project_context` entirely leaves both of them green.
    # `advisor_id` and `verification_status` are the proof, because only the
    # projector drops them: they are key names, so no digit or name rule touches
    # them, and they are in the local context of every student.
    assert "advisor_id" in context_json_of(payload), "marker missing from the local context"
    assert "advisor_id" not in sent, "the verified context was sent unprojected"
    assert "verification_status" not in sent, "the verified context was sent unprojected"

    # The local record is untouched: the evidence panel still has everything.
    assert payload["verified_context"]["student"]["student_id"] == MINE
    assert any(
        r.get("student_context", {}).get("student", {}).get("name") == NAME
        for r in payload["agent"]["tool_results"]
    )


@override_settings(LLM_BACKEND="alibaba")
def test_the_single_shot_fallback_rebuilds_its_context_from_projections(
    roster, monkeypatch
) -> None:
    """Disabling tools does not relax the boundary. This path serialises the whole
    verified context into one message, which makes it the largest payload the
    adviser ever sends — and the easiest place to reach for the local object
    because "there are no tool results to project"."""
    _seed_courses()

    class NoToolsClient(ScriptedClient):
        chat_with_tools = None  # the loop is skipped entirely

    client = NoToolsClient([])
    payload = va.answer_virtual_advisor(
        question=f"رقمي {MINE}، كم ساعة؟",
        principal=_principal(),
        academic_year=1448,
        term=1,
        client=client,
    )

    assert payload["agent"]["loop_used"] is False
    assert payload["agent"]["fallback_reason"] == "client_has_no_tool_support"
    assert str(MINE) not in client.sent_text
    assert NAME not in client.sent_text
    assert "STUDENT_REF_" in client.sent_text
    # The markers that prove the projection ran rather than the sanitiser having
    # cleaned up after it — see the full-entry test for why the id and the name
    # alone cannot distinguish the two.
    assert "advisor_id" in context_json_of(payload)
    assert "advisor_id" not in client.sent_text
    assert "verification_status" not in client.sent_text
    # …and the local context the grounding check reads is still complete.
    assert payload["verified_context"]["student"]["student_id"] == MINE


@override_settings(LLM_BACKEND="local")
def test_the_local_backend_still_sends_the_verified_context_unchanged(roster, monkeypatch) -> None:
    """Reversibility at the entry point. `LLM_BACKEND=local` is the shipped
    default, and it must not have acquired a projection by accident."""
    _seed_courses()

    class NoToolsClient(ScriptedClient):
        chat_with_tools = None

    client = NoToolsClient([])
    va.answer_virtual_advisor(
        question=f"رقمي {MINE}",
        principal=_principal(),
        academic_year=1448,
        term=1,
        client=client,
    )
    assert str(MINE) in client.sent_text
    assert "STUDENT_REF_" not in client.sent_text


@override_settings(LLM_BACKEND="alibaba")
def test_the_grounding_correction_does_not_quote_the_identifiers_back(roster, monkeypatch) -> None:
    """The remote correction must not list what it is objecting to. Every id in
    that list is unverified by definition — invented, or a real student who was
    never part of this request."""
    _seed_courses()

    class NoToolsClient(ScriptedClient):
        chat_with_tools = None

    invented = "8887771"
    client = NoToolsClient([], final=f"الطالب {invented} مؤهل.")
    va.answer_virtual_advisor(
        question="كم ساعة؟", principal=_principal(), academic_year=1448, term=1, client=client
    )
    corrections = [
        m["content"]
        for req in client.requests
        for m in req
        if isinstance(m, dict)
        and m.get("role") == "user"
        and "Rewrite the answer" in str(m.get("content"))
    ]
    assert corrections, "the grounding retry should have fired"
    assert invented not in corrections[0]


# ── 6. the factory ───────────────────────────────────────────────


def test_the_backend_alone_decides_whether_a_boundary_exists() -> None:
    assert isinstance(boundary_for_scope(STUDENT_SCOPE, backend="local"), LocalToolBoundary)
    assert isinstance(boundary_for_scope(STUDENT_SCOPE, backend="alibaba"), RemoteToolBoundary)
    # Anything unrecognised is treated as remote: an unknown processor is the
    # case that most needs the boundary, not the one that least needs it.
    assert isinstance(boundary_for_scope(STUDENT_SCOPE, backend=""), RemoteToolBoundary)
    assert isinstance(
        boundary_for_scope(STUDENT_SCOPE, backend="something-new"), RemoteToolBoundary
    )
