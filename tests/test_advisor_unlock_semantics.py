"""The forward-unlock relations, and what "ready" is allowed to mean.

Two defects, both found in the 50-question live batch against Alibaba
(`runtime/evals/planner_priority_alibaba_20260806-081850.json`):

  1. `why_course_locked` published ONE forward number, built from every graph edge
     naming the course, under the name `unlocks_directly`. That counts courses that
     LIST it as a prerequisite. The name promised the other thing — courses that
     open when it is passed — and the two are different numbers. Measured on the
     live database for the controlled evaluation record (programme AI), AI331 scores:

         listed as a prerequisite for          5   AI352 AI371 AI433 AI482 AI491
         sole remaining prerequisite for       3   AI352 AI371 AI433
         somewhere in the remaining chain of   6   the five above plus AI492

     AI482 is also waiting on COE332 and AI491 on CS289, so the answer to CP04
     («كم مقرر ينتظر AI331 وحده») was 5 and the true answer is 3. The fixture below
     reproduces that neighbourhood exactly so the three numbers stay 5 / 3 / 6.

  2. `open_now`, `can_register` and "Every prerequisite is satisfied; it can be
     registered." all state a REGISTRATION permission from prerequisite records
     alone — while the same payloads said, a field or two away, that they do not
     know what runs this term or whether a seat is free.

`build_unlock_report` is exercised through the capability registry rather than
directly wherever the claim under test is one a MODEL reads, because the payload is
the product surface here; the shared computation is tested directly where the claim
is arithmetic.
"""

from __future__ import annotations

import pytest

from core.models import Course, Prerequisite, ProgrammeRequirement, Student, StudentCourse
from core.services.rbac import ROLE_SUPER_ADMIN
from core.services.student_unlock import build_unlock_report

pytestmark = pytest.mark.django_db

#: A synthetic fixture id. Deliberately NOT the controlled evaluation record's real
#: number: this file builds its own plan rows, so the identifier carries no meaning
#: here, and a real student id in tracked source is a disclosure with no purpose.
SID = 9900001
PROG = "AIT"
YEAR, TERM = 1447, 2

#: (code, plan level, credit hours, prerequisites)
#
# A faithful replica of the controlled evaluation record's AI331 neighbourhood.
# The two courses that make the whole distinction — AI482 (also waiting on COE332)
# and AI491 (also waiting on CS289) — are the reason `listed` and `sole_remaining`
# differ; drop either and the two numbers collapse and this file stops testing
# anything.
_PLAN: tuple[tuple[str, int, int, tuple[str, ...]], ...] = (
    ("AI221", 4, 3, ()),
    ("STAT301", 4, 3, ()),
    ("AI212", 4, 3, ()),
    ("CS289", 6, 3, ()),
    ("CS323", 6, 4, ()),
    ("AI331", 6, 4, ("AI221", "STAT301")),
    ("COE332", 7, 3, ("CS323",)),
    ("AI352", 7, 3, ("AI331",)),
    ("AI371", 7, 3, ("AI212", "AI331")),
    ("AI433", 8, 3, ("AI331",)),
    ("AI482", 8, 3, ("AI331", "COE332")),
    ("AI491", 8, 3, ("AI212", "AI331", "CS289")),
    ("AI492", 9, 3, ("AI491",)),
)

_PASSED = ("AI221", "STAT301", "AI212")


@pytest.fixture
def plan():
    Student.objects.update_or_create(
        student_id=SID,
        defaults={
            "name": "Unlock Semantics",
            "program": PROG,
            "section": "M",
            "total_earned_credits": 90,
            "current_registered_credits": 0,
        },
    )
    for code, level, credits, prereqs in _PLAN:
        Course.objects.update_or_create(
            course_code=code, defaults={"description": f"{code} NAME", "credit_hours": credits}
        )
        ProgrammeRequirement.objects.update_or_create(
            program=PROG,
            course_code=code,
            defaults={"programme_term": level, "credit_hours": credits, "type": "Mandatory"},
        )
        for p in prereqs:
            Prerequisite.objects.update_or_create(
                program=PROG, course_code=code, prerequisite_course_code=p
            )
    levels = {code: level for code, level, _, _ in _PLAN}
    for code in _PASSED:
        StudentCourse.objects.update_or_create(
            student_id=SID,
            course=Course.objects.get(course_code=code),
            defaults={"status": "passed", "programme_term": levels[code]},
        )
    yield


def _why(code: str) -> dict:
    from core.services.virtual_advisor_capabilities import get_default_registry

    return get_default_registry().execute(
        "why_course_locked",
        {"student_id": SID, "course_code": code},
        scope={"role": ROLE_SUPER_ADMIN},
        ctx={"academic_year": YEAR, "term": TERM},
    )


def _progress() -> dict:
    from core.services.virtual_advisor_capabilities import get_default_registry

    return get_default_registry().execute(
        "my_progress",
        {"student_id": SID},
        scope={"role": ROLE_SUPER_ADMIN},
        ctx={"academic_year": YEAR, "term": TERM},
    )


def _add(code: str, level: int, credits: int, prereqs: tuple[str, ...], kind: str) -> None:
    """One extra plan row, added by a test rather than by the fixture.

    The headline counts are 5 / 3 / 6 and several tests assert them literally, so
    anything that would change AI331's neighbourhood is created inside the test that
    needs it.
    """
    Course.objects.update_or_create(
        course_code=code, defaults={"description": f"{code} NAME", "credit_hours": credits}
    )
    ProgrammeRequirement.objects.update_or_create(
        program=PROG,
        course_code=code,
        defaults={"programme_term": level, "credit_hours": credits, "type": kind},
    )
    for p in prereqs:
        Prerequisite.objects.update_or_create(
            program=PROG, course_code=code, prerequisite_course_code=p
        )


# ── (b) the two relations are different numbers ──────────────────


def test_listed_and_sole_remaining_are_not_the_same_set(plan):
    """The whole defect in one assertion: 5 name AI331, 3 are waiting only on it."""
    out = _why("AI331")

    listed = [r["code"] for r in out["listed_as_prerequisite_for"]]
    sole = [r["code"] for r in out["sole_remaining_prerequisite_for"]]

    assert listed == ["AI352", "AI371", "AI433", "AI482", "AI491"]
    assert sole == ["AI352", "AI371", "AI433"]
    assert out["listed_as_prerequisite_count"] == 5
    assert out["sole_remaining_prerequisite_count"] == 3
    # Stated as a relation, not two constants: a change that made the counts equal
    # again would have to break this line, whatever the numbers became.
    assert out["sole_remaining_prerequisite_count"] < out["listed_as_prerequisite_count"]
    assert set(sole) < set(listed)


def test_listed_survives_the_prerequisite_being_passed(plan):
    """`listed` is a CATALOGUE fact — "AI352 names AI331 among its prerequisites" —
    and stays true whatever the student has done. Derived from `missing` instead, it
    would quietly empty out as the student progressed: pass AI331 and every course
    naming it drops off the list of what it opens, which is the moment a student is
    most likely to ask.

    Both counts are asserted, because only `sole_remaining` may move here.
    """
    StudentCourse.objects.update_or_create(
        student=Student.objects.get(student_id=SID),
        course=Course.objects.get(course_code="AI331"),
        defaults={"status": "passed"},
    )
    out = _why("AI331")

    assert [r["code"] for r in out["listed_as_prerequisite_for"]] == [
        "AI352",
        "AI371",
        "AI433",
        "AI482",
        "AI491",
    ]
    assert out["listed_as_prerequisite_count"] == 5
    # And it is no longer the thing they are waiting for: it is passed.
    assert out["sole_remaining_prerequisite_count"] == 0


def test_a_listed_course_says_what_else_it_is_waiting_for(plan):
    """Naming the count is not enough — the answer has to survive "why only three?"."""
    rows = {r["code"]: r for r in _why("AI331")["listed_as_prerequisite_for"]}

    assert rows["AI482"]["still_also_waiting_on"] == ["COE332"]
    assert rows["AI491"]["still_also_waiting_on"] == ["CS289"]
    assert rows["AI352"]["still_also_waiting_on"] == []
    assert rows["AI433"]["still_also_waiting_on"] == []


def test_the_chain_count_is_larger_than_both_and_present_when_the_course_is_open(plan):
    """AI331 is PREREQUISITES_SATISFIED for this student.

    `opens_n_courses` used to live only on the blocked branch, so a question about
    a course the student can already take — which AI331 is, and which is exactly the
    course they ask about — got no chain number at all.
    """
    out = _why("AI331")

    assert out["status"] == "PREREQUISITES_SATISFIED"
    assert out["on_prerequisite_chain_of_count"] == 6
    assert out["on_prerequisite_chain_of_count"] > out["listed_as_prerequisite_count"]


def test_a_course_listed_once_can_unlock_nothing_on_its_own(plan):
    """CS289 is named by AI491 alone, and AI491 also needs AI331.

    The count-the-edges rule reports 1 here and reads as "pass CS289 and AI491
    opens". Nothing opens.
    """
    out = _why("CS289")

    assert out["listed_as_prerequisite_count"] == 1
    assert out["sole_remaining_prerequisite_count"] == 0
    assert out["sole_remaining_prerequisite_for"] == []


def test_a_credit_hour_gate_is_something_else_to_wait_for(plan):
    """`unlock_leaders` compared course codes only, so an hour gate was invisible.

    AI492's only outstanding COURSE is AI491. Gate it on more credit hours than the
    student has and passing AI491 no longer makes it takeable — so it must leave
    `sole_remaining_prerequisite_for` while staying in the listed set.
    """
    before = _why("AI491")
    assert [r["code"] for r in before["sole_remaining_prerequisite_for"]] == ["AI492"]

    Prerequisite.objects.create(
        program=PROG, course_code="AI492", prerequisite_course_code="146(HOURS)"
    )
    after = _why("AI491")

    assert after["listed_as_prerequisite_count"] == 1, "still listed — the record did not change"
    assert after["sole_remaining_prerequisite_count"] == 0
    assert after["listed_as_prerequisite_for"][0]["also_short_on_credit_hours"] is True


def test_the_removed_field_name_is_gone(plan):
    """`unlocks_directly` is not aliased. Its NAME is the false claim.

    It counted the listed set and read as the sole-remaining set, so keeping it
    beside the two honest fields would republish exactly what this removes.
    """
    out = _why("AI331")

    assert "unlocks_directly" not in out
    assert "unlocks_directly_count" not in out
    assert "opens_n_courses" not in out


def test_the_shared_computation_is_the_only_one(plan):
    """`student_home_cards.unlock_leaders` reads the report instead of recounting.

    Three modules derived this from `graph.items` independently and disagreed.
    """
    from core.services.student_home_cards import unlock_leaders

    report = build_unlock_report(SID, YEAR, TERM)
    leader = next(row for row in unlock_leaders(report, limit=5) if row["course_code"] == "AI331")

    assert leader["frees"] == 3
    assert leader["frees_codes"] == ["AI352", "AI371", "AI433"]
    # The rule it now consumes, asserted at the source so the two cannot drift.
    assert report["dependents"]["AI331"]["waiting_only_on_this"] == ["AI352", "AI371", "AI433"]


def test_every_sole_remaining_course_is_also_listed(plan):
    """A structural invariant over the whole plan, not one hand-picked course.

    `waiting_only_on_this` is derived from `missing`, and `missing` is a subset of
    `course_prereqs`, so a row can never be sole-remaining without being listed. A
    change that computed one of them from a different source would break here first.
    """
    report = build_unlock_report(SID, YEAR, TERM)

    checked = 0
    for code, entry in report["dependents"].items():
        listed = {row["code"] for row in entry["listed"]}
        assert set(entry["waiting_only_on_this"]) <= listed, code
        assert entry["on_chain_of_count"] >= len(entry["waiting_only_on_this"]), code
        checked += 1
    assert checked == len(_PLAN)


# ── (c) prerequisite state is not registration permission ────────


def test_readiness_status_never_claims_registration(plan):
    """PREREQUISITES_SATISFIED / PREREQUISITE_BLOCKED, and the disclaimer sentence."""
    ready = _why("AI331")
    blocked = _why("AI482")

    assert ready["status"] == "PREREQUISITES_SATISFIED"
    assert ready["prerequisites_satisfied"] is True
    assert blocked["status"] == "PREREQUISITE_BLOCKED"
    assert blocked["prerequisites_satisfied"] is False

    assert "does not confirm that a section is offered" in ready["explanation"]
    assert "registration is permitted" in ready["explanation"]
    assert "seat is available" in ready["explanation"]
    # The sentence it replaced, and the two field values it replaced.
    assert "it can be registered" not in ready["explanation"]
    assert ready["status"] != "open_now"
    assert blocked["status"] != "blocked"


def test_my_progress_uses_the_canonical_names(plan):
    """`open_now` named a permission the payload could not establish."""
    out = _progress()

    assert "prerequisites_satisfied" in out
    assert "prerequisite_blocked" in out
    assert "open_now" not in out
    assert "blocked" not in out
    # Renaming without saying so would strand any reader of a stored turn.
    assert out["renamed_fields"]["open_now"] == "prerequisites_satisfied"
    assert out["renamed_fields"]["blocked"] == "prerequisite_blocked"

    assert "AI331" in [c["code"] for c in out["prerequisites_satisfied"]]
    assert "AI482" in [c["code"] for c in out["prerequisite_blocked"]]
    assert "does not confirm that a section is offered" in out["note"]


def test_my_progress_ranks_by_impact_and_names_which_number_is_which(plan):
    """CP02 and CP07 need an ORDER over several courses; the payload had a max()."""
    out = _progress()

    top = out["most_useful_course_to_pass"]
    assert top["code"] == "AI331"
    assert top["sole_remaining_prerequisite_count"] == 3
    assert top["on_prerequisite_chain_of_count"] == 6
    # `frees_now` / `frees_eventually` are `build_unlock_report`'s internal names and
    # say nothing about which relation they hold. They must not reach the model.
    assert "frees_now" not in top
    assert "frees_eventually" not in top

    ranking = out["unlock_impact_ranking"]
    assert [r["code"] for r in ranking[:2]] == ["AI331", "CS323"]
    assert [r["sole_remaining_prerequisite_count"] for r in ranking] == sorted(
        (r["sole_remaining_prerequisite_count"] for r in ranking), reverse=True
    )
    # An elective placeholder cannot be passed, so it cannot free anything, and it
    # is not a candidate the student can act on.
    assert all(
        not r["code"].endswith(("1", "2", "3")) or r["code"] in dict((c, 1) for c, *_ in _PLAN)
        for r in ranking
    )


def test_my_plan_by_term_carries_the_canonical_field_beside_the_legacy_one(plan):
    """`can_register` STAYS: fifteen JS call sites and `report_views` read it.

    The canonical name is added at the boundary a model reads, and it must MEAN what
    it says. The first version copied `can_register` bit-for-bit, which is
    `status == "not_taken" and prereqs_ok` — so every course the student had already
    PASSED came back as `prerequisites_satisfied: false`, 32 of 32 on the live
    student. A rename that carries the old predicate under the new word moves the
    defect instead of removing it, and this one told the model that a course the
    student passed still has prerequisites outstanding.

    So the two are DELIBERATELY different booleans now, and the note has to say so.
    """
    from core.services.virtual_advisor_capabilities import get_default_registry

    out = get_default_registry().execute(
        "my_plan_by_term",
        {"student_id": SID},
        scope={"role": ROLE_SUPER_ADMIN},
        ctx={},
    )

    rows = [c for level in out["terms"] for c in level["courses"]]
    assert rows, "the fixture plan has courses"
    for row in rows:
        # The predicate the NAME states: nothing outstanding, whatever the student
        # has done about it.
        assert row["prerequisites_satisfied"] == (not row["missing_prereqs"]), row["course_code"]

    passed = [r for r in rows if r["status"] == "passed"]
    assert passed, "the fixture has passed courses"
    assert all(r["prerequisites_satisfied"] for r in passed), (
        "a course the student has PASSED cannot have prerequisites outstanding"
    )
    assert all(not r["can_register"] for r in passed), (
        "and the legacy field still says false for them — which is why it is not the "
        "same boolean and must not be read as prerequisite state"
    )
    assert any(not r["prerequisites_satisfied"] for r in rows)
    assert "Never read can_register as prerequisite state" in out["note"]
    assert "does not confirm that a section is offered" in out["note"]


# ── the remote path: the payload the model actually receives ─────


def test_the_remote_projection_carries_the_forward_relations(plan):
    """The projectors were written against payloads these executors never emitted.

    `_project_why_course_locked` kept `locked`, `reason`, `missing_prerequisites` and
    `unlocks`; the executor emits none of those four. `_keep` is silent about a name
    that is absent, so the projection produced `{ok, course_code}` and looked like a
    filled payload. On the backend the batch was run against, the tool that owns the
    forward direction contributed nothing at all.
    """
    from core.services.llm_remote_privacy import PROJECTORS, RemoteIdentityMap

    ids = RemoteIdentityMap()
    projected = PROJECTORS["why_course_locked"](_why("AI331"), ids)

    assert projected["listed_as_prerequisite_count"] == 5
    assert projected["sole_remaining_prerequisite_count"] == 3
    assert projected["on_prerequisite_chain_of_count"] == 6
    assert [r["code"] for r in projected["sole_remaining_prerequisite_for"]] == [
        "AI352",
        "AI371",
        "AI433",
    ]
    assert projected["status"] == "PREREQUISITES_SATISFIED"
    # Rows are allowlisted field by field, so a person added to a row tomorrow does
    # not travel with it.
    assert set(projected["listed_as_prerequisite_for"][0]) == {
        "code",
        "course_name",
        "current_status",
        "still_also_waiting_on",
        "also_short_on_credit_hours",
    }
    # A bare `name` is where a person's name hides, and the canary matrix seeds one
    # into every poisoned payload for exactly that reason. These are COURSE names,
    # and they say so.
    assert "name" not in projected
    assert projected["course_name"]


def test_the_remote_projection_of_my_progress_is_not_three_empty_lists(plan):
    """It kept `passed`, `studying` and `remaining` — three names never emitted.

    `_course_rows` returns `[]` for a list that is not there, so the result was
    `{ok, counts, passed: [], studying: [], remaining: []}`: five numbers and three
    empty lists, from the tool that owns the priority ranking.
    """
    from core.services.llm_remote_privacy import PROJECTORS, RemoteIdentityMap

    projected = PROJECTORS["my_progress"](_progress(), RemoteIdentityMap())

    assert projected["most_useful_course_to_pass"]["code"] == "AI331"
    assert [r["code"] for r in projected["unlock_impact_ranking"][:2]] == ["AI331", "CS323"]
    assert [c["code"] for c in projected["prerequisites_satisfied"]]
    assert [c["code"] for c in projected["prerequisite_blocked"]]
    assert "passed" not in projected
    assert "remaining" not in projected


# ── (a) routing: the server decides which capability owns the question ──


@pytest.mark.parametrize(
    ("question", "capability"),
    [
        # The two the brief names.
        ("وش يفتح AI331؟", "why_course_locked"),
        ("وش أهم مقرر عندي؟", "my_progress"),
        # The forward direction in its other Arabic surfaces.
        ("كم مقرر ينتظر AI331 وحده، وما هي هذه المقررات؟", "why_course_locked"),
        ("أي مقرر عندي يفتح أكبر عدد من المقررات مباشرة؟", "why_course_locked"),
        ("كم مقرر يعتمد على AI331؟", "why_course_locked"),
        # English, which the dependency-verb class was added for: both of these
        # classified GENERAL_AGENT before it existed.
        ("what does AI331 unlock?", "why_course_locked"),
        ("how many courses depend on AI331?", "why_course_locked"),
        # Priority, and the backward direction for a named course.
        ("وش أهم مقرر أسجله الآن عشان ما أتأخر في الخطة؟", "my_progress"),
        ("ليش AI331 مقفل؟", "why_course_locked"),
    ],
)
def test_owning_capability_routes_the_question(question, capability):
    from core.services.advisor_intent import owning_capability

    assert owning_capability(question) == capability


def test_a_question_no_family_owns_gets_no_route():
    """`GENERAL_AGENT` must map to nothing rather than to a plausible default.

    A wrong confident route is worse than none — the module docstring's rule, and
    the reason the map is `.get()` rather than a fallback.
    """
    from core.services.advisor_intent import owning_capability

    assert owning_capability("هل الشعب فيها مقاعد؟") is None
    assert owning_capability("") is None


def test_the_dependency_verb_does_not_swallow_the_reverse_direction():
    """«يشترط» / "requires" point the OTHER way and are deliberately not markers.

    «AI352 يشترط AI331» is a statement about AI352's own prerequisites — the
    question `course_prerequisites` answers. Routing it to the forward tool would
    rebuild the direction confusion from the other side.
    """
    from core.services.advisor_intent import IntentFamily, classify_intent

    assert classify_intent("وش يشترط AI331؟") is not IntentFamily.COURSE_UNLOCKS
    assert classify_intent("what does AI331 require?") is not IntentFamily.COURSE_UNLOCKS


def test_every_owned_capability_is_a_capability_that_exists():
    """The map cannot rot into naming a tool nobody registers.

    A route to a name the registry does not know is worse than no route: it reads as
    a decision and produces a missing-tool error at the far end.
    """
    from core.services.advisor_intent import CAPABILITY_FOR_FAMILY
    from core.services.virtual_advisor_capabilities import get_default_registry

    registered = set(get_default_registry().capabilities)
    for family, name in CAPABILITY_FOR_FAMILY.items():
        assert name in registered, f"{family} routes to unregistered {name}"


def test_the_forward_direction_is_advertised_where_the_model_reads_it():
    """(a) of the brief: the description has to say what the payload returns.

    The model reached for `course_prerequisites` — the REVERSE relation — on
    forward-unlock questions because neither description mentioned that
    `why_course_locked` answers them. A description is the routing signal for every
    question the deterministic families abstain on, which is 19 of the batch's 50.
    """
    from core.services.virtual_advisor_capabilities import get_default_registry

    by_name = get_default_registry().capabilities

    forward = by_name["why_course_locked"].description
    assert "listed_as_prerequisite_for" in forward
    assert "sole_remaining_prerequisite_for" in forward
    assert "on_prerequisite_chain_of_count" in forward
    # Naming the displaced tool is not enough — «use X, not Y» leaves Y looking like
    # a second way to the same answer. The description has to say Y answers the
    # OTHER relation, which is why calling it on a forward question is not a
    # near-miss but a category error.
    assert "course_prerequisites" in forward, "it must name the tool it is displacing"
    # In ONE sentence, not two. "Use this, not that" plus a separate remark about a
    # reverse relation leaves the reader to join them, and joining them is the step
    # that was getting skipped. Asserted as co-occurrence rather than as an exact
    # string so the sentence can be rewritten without breaking the contract.
    assert any(
        "course_prerequisites" in sentence and "REVERSE" in sentence
        for sentence in forward.split(". ")
    ), "the reverse relation must be attributed to that tool by name"
    assert "what this course" in forward and "itself requires" in forward

    progress = by_name["my_progress"].description
    assert "unlock_impact_ranking" in progress
    assert "sole_remaining_prerequisite_count" in progress
    # Neither may advertise a permission either of them says it cannot establish.
    for text in (forward, progress):
        assert "open to register" not in text


# ── the holes a 34-mutant run found in the tests above ───────────


def test_an_elective_placeholder_is_never_waiting_on_a_course(plan):
    """A slot is a choice, not a course. Nothing about passing AI331 opens one.

    `Program Elective` rows carry prerequisites in the data, so the reverse relation
    finds them and a rule that only checked "not taken" would report the student's
    elective slot as one of the courses AI331 unlocks — the placeholder confusion
    `student_unlock` refuses everywhere else, arriving through a new door.
    """
    _add("AI1", 7, 3, ("AI331",), "Program Elective")
    out = _why("AI331")

    listed = [r["code"] for r in out["listed_as_prerequisite_for"]]
    sole = [r["code"] for r in out["sole_remaining_prerequisite_for"]]

    assert "AI1" in listed, "the prerequisite record is real and is not hidden"
    assert "AI1" not in sole
    assert out["listed_as_prerequisite_count"] == 6
    assert out["sole_remaining_prerequisite_count"] == 3


def test_an_elective_placeholder_is_never_a_course_to_pass(plan):
    """`open` does not exclude placeholders — «PROGRAM ELECTIVE COURSE I» is "not
    taken with nothing missing" — so the ranking offered six of them as candidates.

    Given a dependent, an unfiltered ranking puts a slot the student cannot register
    at the top of a list headed "pass this next".
    """
    _add("AI1", 7, 3, (), "Program Elective")
    _add("AI777", 8, 3, ("AI1",), "Mandatory")

    report = build_unlock_report(SID, YEAR, TERM)
    assert report["dependents"]["AI1"]["waiting_only_on_this"] == ["AI777"], (
        "the slot really does have a dependent, so exclusion is doing the work"
    )
    assert "AI1" not in [r["code"] for r in report["blockers"]]
    assert "AI1" not in [r["code"] for r in _progress()["unlock_impact_ranking"]]


def test_the_ranking_omits_courses_that_would_free_nothing(plan):
    """A ranked list of zeroes is not a ranking.

    Seven of this student's open courses free nothing at all. Listing them under
    "which course opens the most" invites the model to name one.
    """
    _add("GS101", 1, 2, (), "Mandatory")
    ranking = _progress()["unlock_impact_ranking"]

    assert "GS101" not in [r["code"] for r in ranking]
    assert [r["code"] for r in ranking] == ["AI331", "CS323", "CS289"]
    assert all(
        r["sole_remaining_prerequisite_count"] or r["on_prerequisite_chain_of_count"]
        for r in ranking
    )


def test_my_plan_by_term_does_not_write_into_the_payload_it_was_handed(plan):
    """The rows it decorates are the ones `report_views` serves to two screens.

    `_build_student_plan_payload` is shared with `page-dashboard.js` and
    `page-planner.js`; writing `prerequisites_satisfied` into those dicts would leak
    a field into the browser's JSON from a change made for a language model.
    """
    from core.report_views import _build_student_plan_payload
    from core.services.virtual_advisor_capabilities import _plan_terms_with_canonical_readiness

    payload, _err = _build_student_plan_payload(SID)
    original = payload["terms"]
    decorated = _plan_terms_with_canonical_readiness(original)

    assert any("prerequisites_satisfied" in c for level in decorated for c in level["courses"]), (
        "the decoration happened"
    )
    assert not any(
        "prerequisites_satisfied" in c for level in original for c in level["courses"]
    ), "and it did not happen to the caller's rows"


def test_the_impact_rows_are_allowlisted_not_copied(plan):
    """`_keep` field by field, like every other row in the module.

    The two impact structures are dicts, and `_keep(result, "most_useful_...")`
    copies a dict WHOLESALE — the one thing this module's docstring says none of its
    projectors do. A field added to the row upstream would travel to the provider
    without anyone deciding it should.
    """
    from core.services.llm_remote_privacy import PROJECTORS, RemoteIdentityMap

    poisoned = _progress()
    poisoned["most_useful_course_to_pass"]["adviser_note"] = "CANARY_INTERNAL"
    poisoned["unlock_impact_ranking"][0]["adviser_note"] = "CANARY_INTERNAL"

    projected = PROJECTORS["my_progress"](poisoned, RemoteIdentityMap())

    assert "adviser_note" not in projected["most_useful_course_to_pass"]
    assert "adviser_note" not in projected["unlock_impact_ranking"][0]
    assert projected["most_useful_course_to_pass"]["code"] == "AI331"


def test_the_course_screen_separates_the_two_relations(plan):
    """The student screen carried the same false claim as the tool.

    Its heading read «اجتيازه يفتح لك» / "Passing this opens" over every course that
    NAMES this one — five, when three open. The rows now say which is which, and
    they come from the report rather than from a fourth local count.
    """
    from core.services.course_detail import build_course_detail

    detail = build_course_detail(SID, "AI331", academic_year=str(YEAR), term=str(TERM), report=None)
    rows = {r["course_code"]: r for r in detail["unlocks"]}

    assert set(rows) == {"AI352", "AI371", "AI433", "AI482", "AI491"}
    assert rows["AI352"]["waiting_only_on_this"] is True
    assert rows["AI482"]["waiting_only_on_this"] is False
    assert rows["AI482"]["also_waiting_on"] == ["COE332"]
    assert rows["AI491"]["also_waiting_on"] == ["CS289"]
    assert sum(1 for r in rows.values() if r["waiting_only_on_this"]) == 3


@pytest.mark.parametrize(
    "question",
    [
        # No course NOUN — only the code marker can carry these.
        "what depends on AI331?",
        "ما الذي يعتمد على AI331؟",
    ],
)
def test_the_dependency_verb_routes_without_a_course_noun(question):
    from core.services.advisor_intent import owning_capability

    assert owning_capability(question) == "why_course_locked"


@pytest.mark.parametrize(
    "question",
    [
        # No course CODE — only the noun markers can carry these.
        "كم مقرر يعتمد على هذا المقرر؟",
        "أي المقررات تعتمد على هذا المقرر؟",
    ],
)
def test_the_dependency_verb_routes_without_a_course_code(question):
    from core.services.advisor_intent import owning_capability

    assert owning_capability(question) == "why_course_locked"
