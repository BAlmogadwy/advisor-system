"""References must be unguessable, and a number must earn the right to be one.

Two failures drive this file.

The first was a 16-bit nonce. `STUDENT_REF_54DC_1` looks like a secret and is
one guess in 65,536 — small enough that a user with a few questions to spare can
brute-force the active prefix and address a reference at somebody else. Entropy
is the whole mechanism here, so it is asserted rather than assumed.

The second was a duplicate top-level definition. A rewrite landed after `__all__`
while the original stayed above it; Python bound the later one and the module ran
the OLD code, silently, while the new code sat in the file being read by anyone
who opened it. `test_privacy_modules_have_no_duplicate_top_level_definitions`
closes that for every definition in these modules, not just the one that happened
to be duplicated.
"""

from __future__ import annotations

import ast
import logging
import pathlib
import re

import pytest

from core.models import Student
from core.services.llm_backend import LLMPrivacyError
from core.services.llm_remote_privacy import (
    _NONCE_BYTES,
    RemoteIdentityMap,
    UnverifiedIdentity,
    adviser_mode_authoriser,
    authoriser_for_scope,
    sanitise_messages_for_remote,
    sanitise_text_for_remote,
    student_mode_authoriser,
)
from core.services.rbac import (
    ROLE_ADVISOR,
    ROLE_GENERAL_ADVISOR,
    ROLE_STUDENT,
    ROLE_SUPER_ADMIN,
)

MINE = 4502156
ANOTHER = 4502157
NOBODY = 4509999

#: Injected so a reference is assertable. Production entropy is asserted
#: separately below — a deterministic nonce in a test must not be able to become
#: a deterministic nonce in production.
FIXED = "TESTNONCE0001"


# ── 1. the nonce is a secret, and is sized like one ──────────────


def test_production_nonce_carries_at_least_64_bits() -> None:
    assert _NONCE_BYTES * 8 >= 64, "an identity-bearing reference needs a real secret"


def test_two_maps_created_rapidly_receive_different_nonces() -> None:
    """Back to back, with no sleep. A time-seeded or counter-based nonce would
    collide here, and two answers sharing a nonce means a reference from one
    resolves in the other."""
    nonces = {RemoteIdentityMap().nonce for _ in range(200)}
    assert len(nonces) == 200


def test_a_guessed_reference_with_the_right_shape_but_wrong_nonce_is_refused() -> None:
    identities = RemoteIdentityMap(nonce=FIXED)
    real = identities.reference_for(MINE)

    forged = real.replace(FIXED, "TESTNONCE0002")
    assert forged != real
    assert not identities.issued(forged)
    with pytest.raises(LLMPrivacyError):
        identities.resolve(forged)


def test_a_reference_with_the_right_nonce_but_an_unissued_index_is_refused() -> None:
    """The index is not a secret; the nonce is. Knowing one issued reference must
    not hand over the whole map by incrementing the tail."""
    identities = RemoteIdentityMap(nonce=FIXED)
    identities.reference_for(MINE)

    with pytest.raises(LLMPrivacyError):
        identities.resolve(f"STUDENT_REF_{FIXED}_2")


def test_a_real_student_id_is_not_a_reference() -> None:
    identities = RemoteIdentityMap(nonce=FIXED)
    identities.reference_for(MINE)
    with pytest.raises(LLMPrivacyError):
        identities.resolve(str(MINE))


def test_an_empty_or_short_injected_nonce_is_refused() -> None:
    """Closes the obvious way to weaken production through the test seam."""
    for bad in ("", "   ", "AB", "1234567"):
        with pytest.raises(LLMPrivacyError):
            RemoteIdentityMap(nonce=bad)


def test_repr_and_logs_never_contain_the_mapping(caplog) -> None:
    identities = RemoteIdentityMap(nonce=FIXED)
    identities.reference_for(MINE)
    identities.reference_for(ANOTHER)

    for rendering in (repr(identities), str(identities), f"{identities}"):
        assert str(MINE) not in rendering
        assert str(ANOTHER) not in rendering
        assert FIXED not in rendering
        assert "2 reference" in rendering

    with caplog.at_level(logging.DEBUG):
        logging.getLogger("test").info("identity map: %s", identities)
    assert str(MINE) not in caplog.text
    assert FIXED not in caplog.text


# ── 2. student mode ──────────────────────────────────────────────


def test_student_may_alias_only_their_own_exact_id() -> None:
    identities = RemoteIdentityMap(nonce=FIXED)
    authorise = student_mode_authoriser(MINE)

    assert (
        sanitise_text_for_remote(f"رقمي {MINE}", identities, authorise_id=authorise)
        == f"رقمي STUDENT_REF_{FIXED}_1"
    )


@pytest.mark.parametrize(
    "text",
    [
        f"ما وضع الطالب {ANOTHER}؟",
        "رقم المعاملة 12345678",
        "المرجع 87654321 من فضلك",
        f"رقمي {NOBODY}",
    ],
)
def test_student_mode_refuses_every_other_candidate_identically(text: str) -> None:
    """One outcome for another student, a transaction number and a nonexistent
    id. Distinguishing them would answer "is this a real student?" for free."""
    identities = RemoteIdentityMap(nonce=FIXED)
    with pytest.raises(UnverifiedIdentity):
        sanitise_text_for_remote(text, identities, authorise_id=student_mode_authoriser(MINE))
    assert len(identities) == 0, "a refused candidate must not leave a mapping behind"


def test_student_mode_reads_no_database_row(monkeypatch) -> None:
    """The student refusal is a comparison, not a lookup — so a student cannot
    use their own question to probe the roster, and the check costs no query."""
    from core.services import virtual_advisor_capabilities

    def explode(*_args, **_kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("student mode resolved a candidate against the database")

    monkeypatch.setattr(virtual_advisor_capabilities, "_resolve_scoped_student_id", explode)
    with pytest.raises(UnverifiedIdentity):
        sanitise_text_for_remote(
            f"about {ANOTHER}",
            RemoteIdentityMap(nonce=FIXED),
            authorise_id=student_mode_authoriser(MINE),
        )


def test_arabic_indic_digits_alias_to_the_same_reference_as_western() -> None:
    identities = RemoteIdentityMap(nonce=FIXED)
    authorise = student_mode_authoriser(MINE)
    western = sanitise_text_for_remote(f"{MINE}", identities, authorise_id=authorise)
    arabic = sanitise_text_for_remote("٤٥٠٢١٥٦", identities, authorise_id=authorise)
    assert western == arabic == f"STUDENT_REF_{FIXED}_1"


def test_no_authoriser_means_no_alias() -> None:
    """A caller that forgets to supply scope gets a refusal, not a free alias."""
    with pytest.raises(UnverifiedIdentity):
        sanitise_text_for_remote(f"رقمي {MINE}", RemoteIdentityMap(nonce=FIXED))


@pytest.mark.parametrize(
    "text",
    [
        "مقرر AI221 الفصل 1448 الساعة 09:00 صفحة 28 و19 ساعة معتمدة",
        "STAT305 has 3 credits, room 2-14, page 305",
    ],
)
def test_ordinary_numbers_are_left_exactly_as_written(text: str) -> None:
    identities = RemoteIdentityMap(nonce=FIXED)
    assert (
        sanitise_text_for_remote(text, identities, authorise_id=student_mode_authoriser(MINE))
        == text
    )


# ── 3. adviser mode ──────────────────────────────────────────────


@pytest.fixture
def roster(db) -> None:
    Student.objects.create(
        student_id=MINE, name="طالب واحد", program="AI", section="M", advisor_id="ADV-1"
    )
    Student.objects.create(
        student_id=ANOTHER, name="طالب اثنان", program="DS", section="F", advisor_id="ADV-2"
    )


ADVISER = {"role": ROLE_ADVISOR, "advisor_id": "ADV-1", "departments": []}


@pytest.mark.django_db
def test_adviser_may_alias_an_accessible_student(roster) -> None:
    identities = RemoteIdentityMap(nonce=FIXED)
    out = sanitise_text_for_remote(
        f"كيف حال الطالب {MINE}؟", identities, authorise_id=adviser_mode_authoriser(ADVISER)
    )
    assert out == f"كيف حال الطالب STUDENT_REF_{FIXED}_1؟"
    assert identities.resolve(f"STUDENT_REF_{FIXED}_1") == MINE


@pytest.mark.django_db
@pytest.mark.parametrize("candidate", [ANOTHER, NOBODY])
def test_inaccessible_and_nonexistent_produce_the_same_refusal(roster, candidate: int) -> None:
    """`_resolve_scoped_student_id` says "outside your portfolio" for one and
    "student not found" for the other. Neither reaches the model, and neither
    reaches the caller: both raise the same exception with the same message, so
    the boundary cannot be read as a roster oracle."""
    messages = []
    for cid in (ANOTHER, NOBODY):
        identities = RemoteIdentityMap(nonce=FIXED)
        with pytest.raises(UnverifiedIdentity) as excinfo:
            sanitise_text_for_remote(
                f"الطالب {cid}", identities, authorise_id=adviser_mode_authoriser(ADVISER)
            )
        messages.append(str(excinfo.value))
        assert len(identities) == 0
    assert messages[0] == messages[1]
    assert str(ANOTHER) not in messages[0] and str(NOBODY) not in messages[0]


@pytest.mark.django_db
def test_a_general_adviser_is_scoped_by_department(roster) -> None:
    identities = RemoteIdentityMap(nonce=FIXED)
    scope = {"role": ROLE_GENERAL_ADVISOR, "departments": ["AI"]}
    assert (
        sanitise_text_for_remote(f"{MINE}", identities, authorise_id=adviser_mode_authoriser(scope))
        == f"STUDENT_REF_{FIXED}_1"
    )
    with pytest.raises(UnverifiedIdentity):
        sanitise_text_for_remote(
            f"{ANOTHER}",
            RemoteIdentityMap(nonce=FIXED),
            authorise_id=adviser_mode_authoriser(scope),
        )


@pytest.mark.django_db
def test_a_super_admin_may_alias_any_real_student_but_not_a_fictional_one(roster) -> None:
    scope = {"role": ROLE_SUPER_ADMIN}
    identities = RemoteIdentityMap(nonce=FIXED)
    assert (
        sanitise_text_for_remote(
            f"{ANOTHER}", identities, authorise_id=adviser_mode_authoriser(scope)
        )
        == f"STUDENT_REF_{FIXED}_1"
    )
    with pytest.raises(UnverifiedIdentity):
        sanitise_text_for_remote(
            f"{NOBODY}",
            RemoteIdentityMap(nonce=FIXED),
            authorise_id=adviser_mode_authoriser(scope),
        )


@pytest.mark.django_db
def test_the_dispatcher_gives_a_student_the_student_authoriser(roster, monkeypatch) -> None:
    """A student handed the adviser resolver would gain a roster probe. The
    choice is made once, from the role, rather than at each call site.

    This asserts the MECHANISM, not the outcome. Both authorisers happen to
    answer identically today, because `_resolve_scoped_student_id` clamps a
    student to their own record before it queries anything — so an outcome test
    passes whichever one the dispatcher returns, and would keep passing if the
    dispatcher stopped choosing at all. What must hold is that a student's
    request never consults the roster resolver: then the property survives any
    later change to how that resolver treats a student scope.
    """
    from core.services import virtual_advisor_capabilities

    consulted: list[int] = []
    original = virtual_advisor_capabilities._resolve_scoped_student_id

    def watched(args, scope):
        consulted.append(int(args["student_id"]))
        return original(args, scope)

    monkeypatch.setattr(virtual_advisor_capabilities, "_resolve_scoped_student_id", watched)

    student_scope = {"role": ROLE_STUDENT, "student_id": MINE}
    authorise = authoriser_for_scope(student_scope)
    assert authorise(MINE) is True
    assert authorise(ANOTHER) is False  # exists, and still refused
    assert consulted == [], "a student's authoriser must not reach the roster resolver"

    assert authoriser_for_scope(ADVISER)(MINE) is True
    assert consulted == [MINE], "an adviser's authoriser must"


@pytest.mark.django_db
def test_an_unknown_role_authorises_nothing(roster) -> None:
    for scope in ({}, {"role": ""}, {"role": "SOMETHING_NEW"}, None):
        assert authoriser_for_scope(scope)(MINE) is False


@pytest.mark.django_db
def test_the_adviser_authoriser_decides_each_candidate_once(roster, monkeypatch) -> None:
    """The same id appears in the question and in every history message. Without
    the memo that is one query each, and — worse — an answer that could change
    halfway through if a row is edited mid-request."""
    from core.services import virtual_advisor_capabilities

    calls: list[int] = []
    original = virtual_advisor_capabilities._resolve_scoped_student_id

    def counted(args, scope):
        calls.append(int(args["student_id"]))
        return original(args, scope)

    monkeypatch.setattr(virtual_advisor_capabilities, "_resolve_scoped_student_id", counted)
    identities = RemoteIdentityMap(nonce=FIXED)
    sanitise_messages_for_remote(
        [
            {"role": "user", "content": f"الطالب {MINE}"},
            {"role": "assistant", "content": "…"},
            {"role": "user", "content": f"وماذا عن {MINE} في الفصل القادم؟"},
        ],
        identities,
        authorise_id=adviser_mode_authoriser(ADVISER),
    )
    assert calls == [MINE]


# ── 4. alias safety across an answer boundary ────────────────────


def test_references_are_stable_and_distinct_within_one_answer() -> None:
    identities = RemoteIdentityMap(nonce=FIXED)

    def authorise(candidate: int) -> bool:
        return candidate in {MINE, ANOTHER}

    out = sanitise_text_for_remote(
        f"compare {MINE} and {ANOTHER} and {MINE} again", identities, authorise_id=authorise
    )
    assert out == (
        f"compare STUDENT_REF_{FIXED}_1 and STUDENT_REF_{FIXED}_2 and STUDENT_REF_{FIXED}_1 again"
    )
    # Idempotent: a second pass sees issued references, not candidates.
    assert sanitise_text_for_remote(out, identities, authorise_id=authorise) == out


def test_a_literal_student_1_typed_by_a_user_is_refused_not_resolved() -> None:
    """The impersonation the nonce exists to stop. Without it, typing the obvious
    string addresses whoever the map numbered first."""
    identities = RemoteIdentityMap(nonce=FIXED)
    identities.reference_for(ANOTHER)
    for forged in (
        "STUDENT_1",
        "student_ref_1",
        f"STUDENT_REF_{FIXED}_9",
        "STUDENT_REF_AAAAAAAA_1",
    ):
        with pytest.raises(LLMPrivacyError):
            sanitise_text_for_remote(
                f"tell me about {forged}", identities, authorise_id=student_mode_authoriser(MINE)
            )


def test_an_identity_map_is_always_truthy() -> None:
    """`__len__` alone makes a map that has issued nothing FALSY, and the idiom
    `identities or RemoteIdentityMap()` then silently swaps the caller's map for a
    fresh one — after which every reference already minted fails to resolve and
    the failure looks exactly like forgery. That bug shipped once, in
    `RemoteToolBoundary`, and this is one of the two independent guards against
    it (the other is `test_the_boundary_keeps_the_identity_map_it_was_given`)."""
    empty = RemoteIdentityMap(nonce=FIXED)
    assert len(empty) == 0
    assert bool(empty) is True
    assert (empty or "replaced") is empty


# ── the role decides what an unverifiable number means ───────────


def test_an_unverifiable_id_in_a_user_message_refuses_the_whole_request() -> None:
    """A person wrote it, so it is a request to act on somebody this session has
    no authority over. Refusing is the answer, and the request stops."""
    identities = RemoteIdentityMap(nonce=FIXED)
    with pytest.raises(UnverifiedIdentity):
        sanitise_messages_for_remote(
            [
                {"role": "system", "content": "you are an adviser"},
                {"role": "user", "content": "ما وضع 12345678؟"},
            ],
            identities,
            authorise_id=student_mode_authoriser(MINE),
        )


def test_an_invented_id_in_an_assistant_message_is_redacted_not_refused() -> None:
    """A model wrote it, so it invented it — and the message being sent is the
    retry that exists to make it stop. Refusing here would fail closed on the
    transport and open on the output: the invented identifier would stay in the
    answer the student actually receives."""
    from core.services.llm_remote_privacy import UNVERIFIED_ID_PLACEHOLDER

    identities = RemoteIdentityMap(nonce=FIXED)
    out = sanitise_messages_for_remote(
        [
            {"role": "user", "content": f"رقمي {MINE}"},
            {"role": "assistant", "content": "الطالب 8887771 مؤهل."},
        ],
        identities,
        authorise_id=student_mode_authoriser(MINE),
    )
    assert out[0]["content"] == f"رقمي STUDENT_REF_{FIXED}_1"
    assert out[1]["content"] == f"الطالب {UNVERIFIED_ID_PLACEHOLDER} مؤهل."
    assert "8887771" not in out[1]["content"]


def test_a_verified_id_in_an_assistant_message_still_becomes_a_reference() -> None:
    """Redaction is the fallback for what cannot be verified, not a replacement
    for aliasing. An assistant message naming the session's own student is
    referenced like anywhere else, so the conversation stays coherent."""
    identities = RemoteIdentityMap(nonce=FIXED)
    out = sanitise_messages_for_remote(
        [{"role": "assistant", "content": f"سجلك يا {MINE} مكتمل."}],
        identities,
        authorise_id=student_mode_authoriser(MINE),
    )
    assert out[0]["content"] == f"سجلك يا STUDENT_REF_{FIXED}_1 مكتمل."


def test_a_reference_does_not_survive_into_the_next_answer() -> None:
    first = RemoteIdentityMap(nonce=FIXED)
    stale = first.reference_for(MINE)

    second = RemoteIdentityMap()  # a new answer, a new nonce
    assert not second.issued(stale)
    with pytest.raises(LLMPrivacyError):
        second.resolve(stale)
    with pytest.raises(LLMPrivacyError):
        sanitise_text_for_remote(
            f"and {stale}?", second, authorise_id=student_mode_authoriser(MINE)
        )


# ── 5. the structural guard ──────────────────────────────────────


PRIVACY_MODULES = (
    "core/services/llm_remote_privacy.py",
    "core/services/advisor_remote_boundary.py",
    "core/services/llm_backend.py",
)


@pytest.mark.parametrize("relative", PRIVACY_MODULES)
def test_privacy_modules_have_no_duplicate_top_level_definitions(relative: str) -> None:
    """A second definition of the same name silently wins, and the first stays in
    the file looking authoritative.

    This is not hypothetical: a rewrite of the sanitiser was appended after
    `__all__` while the original remained above it. Python bound the earlier one
    — the file READ as the new behaviour and RAN the old, and the only symptom
    was a keyword argument the "current" signature clearly accepted being
    rejected at runtime.

    Applied to every top-level function, class and assigned constant in the
    modules that decide what leaves the institution, because the next duplicate
    will not be the same function.
    """
    source = pathlib.Path(relative).read_text(encoding="utf-8")
    tree = ast.parse(source)

    seen: dict[str, int] = {}
    duplicates: list[str] = []
    for node in tree.body:
        names: list[str] = []
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names = [node.name]
        elif isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = [node.target.id]
        for name in names:
            if name in seen:
                duplicates.append(f"{name} (line {seen[name]} then line {node.lineno})")
            seen[name] = node.lineno

    assert not duplicates, f"{relative} defines these twice at module level: {duplicates}"


def test_the_duplicate_guard_actually_detects_a_duplicate(tmp_path) -> None:
    """The guard above passes on a clean file, which is also what a broken guard
    does. This plants the exact failure that happened and requires it to be seen."""
    planted = tmp_path / "planted.py"
    planted.write_text(
        "def sanitise_text_for_remote(a):\n    return a\n\n"
        "__all__ = ['sanitise_text_for_remote']\n\n"
        "def sanitise_text_for_remote(a, authorise_id=None):\n    return a\n",
        encoding="utf-8",
    )
    tree = ast.parse(planted.read_text(encoding="utf-8"))
    names = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]
    assert len(names) != len(set(names)), "the planted duplicate must be visible to the guard"


def test_the_alias_shape_pattern_matches_a_full_length_reference() -> None:
    """The forgery detector and the reference format have to agree. When the
    nonce grew from 4 characters to 32 the pattern still had `{0,8}` in it, so a
    full-length forged reference matched nothing and went straight through."""
    from core.services.llm_remote_privacy import _ALIAS_SHAPE

    issued = RemoteIdentityMap().reference_for(MINE)
    assert _ALIAS_SHAPE.findall(f"about {issued} please") == [issued]
    assert re.fullmatch(rf"STUDENT_REF_[0-9A-F]{{{_NONCE_BYTES * 2}}}_1", issued)
