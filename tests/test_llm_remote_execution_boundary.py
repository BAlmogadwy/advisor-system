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

    #: DECLARED, not inferred. The boundary is derived from the client that will
    #: actually receive the payload, so a fake standing in for a remote provider
    #: has to say so — `@override_settings(LLM_BACKEND=...)` alone no longer
    #: decides privacy behaviour, and that is the point of the rule.
    backend = "local"
    supports_assistant_prefill = True

    def __init__(
        self,
        turns: list[ToolChatResult],
        final: str = "الإجابة النهائية.",
        backend: str = "local",
    ) -> None:
        self.backend = backend
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


def test_failed_course_evidence_reaches_remote_llm_without_identity_leakage(
    roster: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operator_note = "INTERNAL-FAILED-RESULT-NOTE"
    failed_result = {
        "course_code": "CS289",
        "course_name": "Data Structures",
        "grade": "F",
        "mark": 49.0,
        "student_id": MINE,
        "student_name": NAME,
        "advisor_id": "ADV-1",
        "verification_status": "operator-confirmed",
        "operator_note": operator_note,
    }
    ExecutionSpy(
        monkeypatch,
        result={
            "ok": True,
            "student_context": {
                "student": {"student_id": MINE, "name": NAME, "program": "AI"},
                "course_evidence": {
                    "failed": ["CS289"],
                    "failed_results": [failed_result],
                },
            },
        },
    )
    client = ScriptedClient([_call("get_student_context", {})])
    _, local, provider, _ = _run_loop(client, _remote())

    assert local[0]["student_context"]["course_evidence"]["failed_results"] == [failed_result]
    projected_evidence = provider[0]["student_context"]["course_evidence"]
    assert projected_evidence["failed"] == ["CS289"]
    assert projected_evidence["failed_results"] == [
        {
            "course_code": "CS289",
            "course_name": "Data Structures",
            "grade": "F",
            "mark": 49.0,
        }
    ]
    tool_messages = [message for message in client.requests[-1] if message.get("role") == "tool"]
    assert json.loads(tool_messages[-1]["content"])["student_context"]["course_evidence"] == (
        projected_evidence
    )
    assert str(MINE) not in client.sent_text
    assert NAME not in client.sent_text
    assert operator_note not in client.sent_text


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
        backend="alibaba",
    )

    payload = va.answer_virtual_advisor(
        question=f"أنا {NAME}، رقمي {MINE}. وش عندي بكرة الأحد؟",
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

    client = NoToolsClient([], backend="alibaba")
    payload = va.answer_virtual_advisor(
        question=f"رقمي {MINE}، وش عندي بكرة؟",
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


def _corrections(client) -> list[str]:
    return [
        m["content"]
        for req in client.requests
        for m in req
        if isinstance(m, dict)
        and m.get("role") == "user"
        and "output contract" in str(m.get("content"))
    ]


@override_settings(LLM_BACKEND="alibaba")
def test_the_grounding_correction_does_not_quote_the_identifiers_back(roster, monkeypatch) -> None:
    """The remote correction must not list what it is objecting to. Every id in
    that list is unverified by definition — invented, or a real student who was
    never part of this request."""
    _seed_courses()

    class NoToolsClient(ScriptedClient):
        chat_with_tools = None

    invented = "8887771"
    client = NoToolsClient([], final=f"الطالب {invented} مؤهل.", backend="alibaba")
    payload = va.answer_virtual_advisor(
        question="وش عندي بكرة الأحد؟",
        principal=_principal(),
        academic_year=1448,
        term=1,
        client=client,
    )
    corrections = _corrections(client)
    assert corrections, "the output-contract retry should have fired"
    assert invented not in corrections[0]
    assert invented not in client.sent_text
    # …and the retry returned the same bad draft, so the answer is the refusal,
    # never the draft that was already proven to breach the contract.
    assert payload["answer"] == va._GROUNDING_REFUSAL_AR
    assert payload["agent"]["grounding_refused"] is True


# ── 7. the output contract ───────────────────────────────────────
#
# The gate is about what a PERSON may be shown, which is a different question
# from what a PROVIDER may be sent. These tests exercise it through the entry
# point, because the failure it replaces was a control-flow inversion — detect,
# then ship anyway — and a unit test of the checker would not have seen it.


class _ScriptedAnswers(ScriptedClient):
    """A client with no tools whose successive `chat` calls return a script, so a
    draft and its correction can differ."""

    chat_with_tools = None

    def __init__(self, answers: list[str], backend: str = "local") -> None:
        super().__init__([], final=answers[-1])
        self.answers = list(answers)
        self.backend = backend

    def chat(self, messages, **kwargs):
        import json as _json

        self.requests.append(_json.loads(_json.dumps(messages, default=str)))
        text = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return ChatResult(content=text, model="test-model", usage={})


@override_settings(LLM_BACKEND="local")
def test_a_clean_rewrite_is_accepted(roster) -> None:
    """The retry is not decorative: a corrected answer that passes is returned."""
    _seed_courses()
    client = _ScriptedAnswers(["الطالب 8887771 مؤهل.", "أنت مؤهل للتسجيل هذا الفصل."])
    payload = va.answer_virtual_advisor(
        question="وش عندي بكرة الأحد؟",
        principal=_principal(),
        academic_year=1448,
        term=1,
        client=client,
    )
    assert payload["answer"] == "أنت مؤهل للتسجيل هذا الفصل."
    assert payload["agent"]["grounding_retry"] is True
    assert payload["agent"].get("grounding_refused") is None


@override_settings(LLM_BACKEND="local")
def test_a_retry_that_invents_a_DIFFERENT_identifier_is_refused(roster) -> None:
    """The corrected answer is re-validated, not trusted. Accepting it unchecked
    let a retry swap one invented identifier for another and ship it."""
    _seed_courses()
    client = _ScriptedAnswers(["الطالب 8887771 مؤهل.", "بل الطالب 7776663 هو المؤهل."])
    payload = va.answer_virtual_advisor(
        question="وش عندي بكرة الأحد؟",
        principal=_principal(),
        academic_year=1448,
        term=1,
        client=client,
    )
    assert payload["answer"] == va._GROUNDING_REFUSAL_AR
    assert payload["agent"]["output_violations_after_retry"] == ["unverified_student_id"]


@override_settings(LLM_BACKEND="local")
def test_a_retry_that_raises_refuses_rather_than_keeping_the_draft(roster, monkeypatch) -> None:
    """THE regression this gate exists for. The old code logged the failure and
    returned the original draft — the one the system had just proved contains an
    unverified identifier."""
    _seed_courses()
    draft = "الطالب 8887771 مؤهل."

    class ExplodingRetry(_ScriptedAnswers):
        def chat(self, messages, **kwargs):
            if self.requests:  # the first call is the draft; the second is the retry
                self.requests.append([])
                raise RuntimeError("provider fell over during the retry")
            return super().chat(messages, **kwargs)

    payload = va.answer_virtual_advisor(
        question="وش عندي بكرة الأحد؟",
        principal=_principal(),
        academic_year=1448,
        term=1,
        client=ExplodingRetry([draft]),
    )
    assert payload["answer"] != draft
    assert payload["answer"] == va._GROUNDING_REFUSAL_AR
    assert payload["agent"]["grounding_retry_failed"] is True
    assert payload["agent"]["grounding_refused"] is True


@override_settings(LLM_BACKEND="local")
def test_an_english_question_gets_the_english_refusal(roster) -> None:
    _seed_courses()
    client = _ScriptedAnswers(["Student 8887771 is eligible."])
    payload = va.answer_virtual_advisor(
        question="What is on my timetable tomorrow?",
        principal=_principal(),
        academic_year=1448,
        term=1,
        client=client,
    )
    assert payload["answer"] == va._GROUNDING_REFUSAL_EN


@override_settings(LLM_BACKEND="local")
def test_a_redaction_marker_never_reaches_a_student(roster) -> None:
    """«راسل [EMAIL_REDACTED]» is not a safe answer that lost a detail — it is an
    instruction that cannot be followed, and it announces that something was
    removed."""
    _seed_courses()
    client = _ScriptedAnswers(["راسل [EMAIL_REDACTED] للاستفسار."])
    payload = va.answer_virtual_advisor(
        question="وش عندي بكرة الأحد؟",
        principal=_principal(),
        academic_year=1448,
        term=1,
        client=client,
    )
    assert payload["answer"] == va._GROUNDING_REFUSAL_AR
    assert payload["agent"]["output_violations"] == ["redaction_marker_in_answer"]


@override_settings(LLM_BACKEND="local")
def test_a_fabricated_reference_in_a_local_answer_is_refused(roster) -> None:
    """No reference is ever issued locally, so a STUDENT_REF token in a locally
    produced answer was invented — and would read to a student as a real handle."""
    _seed_courses()
    client = _ScriptedAnswers(["الطالب STUDENT_REF_ABCD1234_1 مؤهل."])
    payload = va.answer_virtual_advisor(
        question="وش عندي بكرة الأحد؟",
        principal=_principal(),
        academic_year=1448,
        term=1,
        client=client,
    )
    assert payload["answer"] == va._GROUNDING_REFUSAL_AR
    assert set(payload["agent"]["output_violations"]) == {
        "unissued_student_reference",
        "reference_shown_to_a_student",
    }


@override_settings(LLM_BACKEND="alibaba")
def test_a_real_id_is_refused_remotely_even_though_it_is_in_the_local_evidence(
    roster,
) -> None:
    """The distinction the transport sanitiser cannot make.

    `MINE` is in the local evidence — it is the asking student's own id, it is in
    the question and in the verified context — so `_unverified_student_ids`
    considers it grounded. But the PROVIDER was never given it: the question was
    aliased before it left. An answer stating it was not read from anywhere; it
    was reconstructed, and local authorisation does not make that a fact.
    """
    _seed_courses()
    client = _ScriptedAnswers([f"رقمك هو {MINE} وأنت مؤهل."], backend="alibaba")
    payload = va.answer_virtual_advisor(
        question=f"رقمي {MINE}، وش عندي بكرة؟",
        principal=_principal(),
        academic_year=1448,
        term=1,
        client=client,
    )
    assert va._unverified_student_ids(f"رقمك هو {MINE}", [f"رقمي {MINE}"]) == [], (
        "the local grounding rule alone would have cleared this answer"
    )
    assert payload["answer"] == va._GROUNDING_REFUSAL_AR
    assert payload["agent"]["output_violations"] == ["identifier_the_provider_never_saw"]


@override_settings(LLM_BACKEND="alibaba")
def test_an_arabic_indic_identifier_does_not_slip_past_the_gate(roster) -> None:
    """`_STUDENT_ID_RE` knows only Western digits. On an Arabic-first adviser,
    writing «٤٥٠٢١٥٦» would otherwise be a way round the whole check."""
    _seed_courses()
    client = _ScriptedAnswers(["رقمك هو ٤٥٠٢١٥٦ وأنت مؤهل."], backend="alibaba")
    payload = va.answer_virtual_advisor(
        question="وش عندي بكرة الأحد؟",
        principal=_principal(),
        academic_year=1448,
        term=1,
        client=client,
    )
    assert payload["answer"] == va._GROUNDING_REFUSAL_AR


@override_settings(LLM_BACKEND="local")
def test_a_clean_answer_is_never_touched(roster) -> None:
    """The gate must not fire on ordinary prose. Course codes, credit hours,
    pages and academic years all contain digits and none is an identity.

    The original fixture also asserted «الأحد 09:00» beside the course code -
    a meeting-time claim with zero schedule evidence in the turn, which is the
    exact shape of the audited fabricated-timetable failure.  The evidence
    postconditions now challenge that on this path too, so the identity-gate
    assertion keeps every digit shape EXCEPT the ungrounded meeting time, and
    a companion test below pins the new behaviour explicitly.
    """
    _seed_courses()
    clean = "مقرر AI221 بثلاث ساعات، والحد 19 ساعة معتمدة، صفحة 28."
    client = _ScriptedAnswers([clean])
    payload = va.answer_virtual_advisor(
        question="كم ساعة مقرر AI221؟",
        principal=_principal(),
        academic_year=1448,
        term=1,
        client=client,
    )
    assert payload["answer"] == clean
    assert payload["agent"].get("output_violations") is None


def test_an_ungrounded_meeting_time_is_challenged_on_the_legacy_path_too(roster) -> None:
    """A schedule claim without schedule evidence no longer ships from legacy.

    Any environment without the V2 flag - a preview, a rollback - runs this
    path, and it used to skip the evidence postconditions entirely because
    check_answer was called without question=.  That silent bypass is how the
    audited fabrications would return on the next rollback.
    """
    _seed_courses()
    fabricated = "مقرر AI221 بثلاث ساعات، الأحد 09:00، والحد 19 ساعة معتمدة."
    client = _ScriptedAnswers([fabricated, fabricated])
    payload = va.answer_virtual_advisor(
        question="وش عندي بكرة الأحد؟",
        principal=_principal(),
        academic_year=1448,
        term=1,
        client=client,
    )
    assert payload["answer"] != fabricated


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


def test_a_forged_argument_on_an_UNADVERTISED_tool_is_also_scrubbed(roster, monkeypatch) -> None:
    """The second refusal branch, which had no coverage of its own.

    Three places scrub a refused call's arguments — withheld, never-offered, and
    failed-the-execution-order — and a test that only drives the third leaves the
    other two free to regress. This drives the never-offered branch: a DENY
    capability, requested by name, carrying a real student id it invented.
    """
    spy = ExecutionSpy(monkeypatch)
    client = ScriptedClient([_call("portfolio_triage", {"student_id": OUTSIDE})])
    _, local, provider, telemetry = _run_loop(client, _remote(ADVISER_SCOPE), ADVISER_SCOPE)

    assert spy.count == 0
    assert local == [] and provider == []
    assert telemetry["boundary_refusals"][0]["stage"] == "pre_execution"
    assert str(OUTSIDE) not in client.sent_text, "the forged id survived in the transcript"


# ── 8. the backend actually in use ───────────────────────────────


def test_the_boundary_follows_the_CLIENT_not_the_settings(roster) -> None:
    """Two sources for one fact is how a remote client gets full records.

    A caller can inject a client the settings do not describe — a test, a
    management command, a queue worker — and deriving privacy behaviour from
    `settings.LLM_BACKEND` would then hand a remote provider `LocalToolBoundary`
    and every identifier in the record. The client is what will receive the
    payload, so it decides what may be in it.
    """
    remote_client = ScriptedClient([], backend="alibaba")
    with override_settings(LLM_BACKEND="local"):
        boundary = boundary_for_scope(STUDENT_SCOPE, backend=remote_client.backend)
    assert isinstance(boundary, RemoteToolBoundary)

    local_client = ScriptedClient([], backend="local")
    with override_settings(LLM_BACKEND="alibaba"):
        boundary = boundary_for_scope(STUDENT_SCOPE, backend=local_client.backend)
    assert isinstance(boundary, LocalToolBoundary)


@override_settings(LLM_BACKEND="alibaba")
def test_production_refuses_when_the_factory_disagrees_with_the_settings(
    roster, monkeypatch
) -> None:
    """Only on the production path. An injected client may disagree — that is
    what makes it injectable — but the factory building something the deployment
    did not ask for is a configuration fault, and answering anyway would answer
    through the wrong provider."""
    from core.services.llm_backend import LLMConfigError

    monkeypatch.setattr(va, "get_llm_client", lambda: ScriptedClient([], backend="local"))
    with pytest.raises(LLMConfigError, match="unintended provider"):
        va.answer_virtual_advisor(
            question="وش عندي بكرة؟",
            principal=AdvisorPrincipal(role=ROLE_STUDENT, student_id=MINE),
            academic_year=1448,
            term=1,
        )


def test_no_prefill_is_sent_to_a_provider_that_refuses_one(roster) -> None:
    """Model Studio rejects a trailing assistant turn, and the client raises
    rather than discarding it — so deciding the prefill from the model NAME
    breaks every plain-chat path the moment the backend is switched: single
    shot, forced final, and all three retries, each as a 500."""
    _seed_courses()

    class NoPrefill(ScriptedClient):
        chat_with_tools = None
        supports_assistant_prefill = False

        def chat(self, messages, **kwargs):
            assert kwargs.get("assistant_prefill") is None, "a prefill reached the provider"
            return super().chat(messages, **kwargs)

    payload = va.answer_virtual_advisor(
        question="وش عندي بكرة الأحد؟",
        principal=AdvisorPrincipal(role=ROLE_STUDENT, student_id=MINE),
        academic_year=1448,
        term=1,
        client=NoPrefill([], backend="alibaba"),
    )
    assert payload["ok"] is True


def test_a_prefill_IS_sent_to_a_provider_that_accepts_one(roster) -> None:
    """The other half: the local Qwen build still gets its `<think>` suppression,
    so a provider-aware helper cannot be a blanket removal."""
    _seed_courses()
    seen: list[object] = []

    class Recording(ScriptedClient):
        chat_with_tools = None

        def resolve_model(self, model=None):
            return "qwen3.6-35b-a3b"

        def chat(self, messages, **kwargs):
            seen.append(kwargs.get("assistant_prefill"))
            return super().chat(messages, **kwargs)

    va.answer_virtual_advisor(
        question="وش عندي بكرة الأحد؟",
        principal=AdvisorPrincipal(role=ROLE_STUDENT, student_id=MINE),
        academic_year=1448,
        term=1,
        client=Recording([], backend="local"),
    )
    assert seen and seen[0] is not None


def test_a_staff_grounding_refusal_does_not_tell_the_adviser_to_ask_the_adviser(
    roster,
) -> None:
    """The staff console's reader IS an adviser.

    The legacy path's evidence postconditions now run for staff turns too, and
    the original refusal text told the reader to «راجع مرشدك الأكاديمي» - a
    hand-off to themselves.  Staff refusals point at the dedicated screens
    instead.  This is also the first test that arms the postcondition battery
    in staff mode at all.
    """
    _seed_courses()
    fabricated = "محاضرة AI221 للطالب يوم الأحد الساعة 09:00."
    client = _ScriptedAnswers([fabricated, fabricated])
    payload = va.answer_virtual_advisor(
        question="متى محاضرة AI221 لطالبي؟",
        principal=AdvisorPrincipal(role=ROLE_ADVISOR, student_id=MINE, advisor_id="A100"),
        academic_year=1448,
        term=1,
        client=client,
    )
    assert payload["answer"] != fabricated
    assert "مرشدك الأكاديمي" not in payload["answer"]
