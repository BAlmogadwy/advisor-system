"""Section Planning's weekly-meetings editor: a programme's vote, listed and saved safely.

A programme votes on how each 3-credit course of its study plan meets each week
(``ProgrammeRequirement.meeting_pattern``: "" not set, "standard", "2x100").
The editor lists one entry per course IDENTITY (CODE::NAME) for the programmes
on screen, and saves like the seat limits do: only the programmes on screen
unless one course is widened to every programme with a row of that exact
identity; a value only on a row that may vote (others skipped, with why);
nothing without a preview whose token the commit carries; each row written only
while it still holds the previewed value, name and credit hours; every row
audited strictly inside one audited transaction.
"""

from __future__ import annotations

import json
from contextlib import contextmanager

import pytest
from django.contrib.auth.models import Group, User
from django.test import Client
from django.urls import reverse

from core.authz import _rate_buckets
from core.models import (
    AuditLog,
    DeliveryBoard,
    ProgrammeRequirement,
    ScenarioSectionBudget,
    TimetableScenario,
)
from core.services import audit as audit_service
from core.services import programme_meeting_pattern_editor as editor
from core.services.programme_meeting_patterns import pre_key
from core.services.rbac import (
    ROLE_ADVISOR,
    ROLE_GENERAL_ADVISOR,
    ensure_role_groups,
    ensure_scope_schema,
    set_user_scope,
)
from core.services.timetable_meeting_shape import merge_programme_patterns

pytestmark = pytest.mark.django_db

LIST_URL = "/ops/section-planning/meeting-patterns/"
SAVE_URL = "/ops/section-planning/meeting-patterns/save/"

STAT = "STAT103::PRINCIPLES_OF_PROBABILITY_AND_STATISTICS"
MATH = "MATH243::243_LINEAR_ALGEBRA_I"
MATH_CYP2 = "MATH243::LINEAR_ALGEBRA"
MATH_NAME = ")\t243\t LINEAR ALGEBRA I"


@pytest.fixture(autouse=True)
def _fresh_throttle():
    _rate_buckets.clear()
    yield
    _rate_buckets.clear()


def _login(client: Client, role: str = ROLE_GENERAL_ADVISOR, username: str = "sp-meet") -> User:
    ensure_role_groups()
    ensure_scope_schema()
    user, _ = User.objects.get_or_create(username=username)
    user.groups.clear()
    user.groups.add(Group.objects.get(name=role))
    set_user_scope(user.id, advisor_id="", departments="")
    client.force_login(user)
    return user


def _req(
    program: str,
    code: str,
    name: str,
    *,
    credits: int | None = 3,
    kind: str = "Mandatory",
    pattern: str = "",
) -> ProgrammeRequirement:
    return ProgrammeRequirement.objects.create(
        program=program,
        course_code=code,
        course_name=name,
        credit_hours=credits,
        type=kind,
        meeting_pattern=pattern,
    )


def _votes() -> dict[tuple[str, str], str]:
    return {
        (r.program, r.course_code): r.meeting_pattern for r in ProgrammeRequirement.objects.all()
    }


def _post(client: Client, body: dict):
    return client.post(SAVE_URL, json.dumps(body), content_type="application/json")


def _save(client: Client, programs: list[str], changes: list[dict]):
    """Preview, then commit with the preview's token (what the page does)."""
    preview = _post(client, {"programs": programs, "changes": changes, "dry_run": True})
    assert preview.status_code == 200, preview.content
    token = preview.json()["preview_token"]
    return preview, _post(
        client,
        {"programs": programs, "changes": changes, "dry_run": False, "preview_token": token},
    )


def _actor() -> editor.EditorActor:
    return editor.EditorActor(
        username="tester", role="GENERAL_ADVISOR", endpoint="/t/", method="POST"
    )


@pytest.fixture
def plan():
    """STAT103 in the '2' programmes; MATH243 under two names (CYP2's is another course)."""
    _req("AI2", "STAT103", "PRINCIPLES OF PROBABILITY AND STATISTICS", pattern="2x100")
    _req("DS2", "STAT103", "PRINCIPLES OF PROBABILITY AND STATISTICS")
    _req("CS2", "STAT103", "PRINCIPLES OF PROBABILITY AND STATISTICS", pattern="standard")
    _req("IS2", "STAT103", "Principles of Probability and Statistics")
    # The same course at 4 credits: it can never vote, so it is no co-owner.
    _req("CYP2", "STAT103", "PRINCIPLES OF PROBABILITY AND STATISTICS", credits=4)
    for program in ("AI", "AI2", "DS", "DS2"):
        _req(program, "MATH243", MATH_NAME, pattern="2x100" if program == "AI2" else "")
    _req("CYP2", "MATH243", "LINEAR ALGEBRA")


# ── the listing ──────────────────────────────────────────────────────────────


def test_the_listing_is_one_entry_per_identity_for_the_programmes_on_screen(plan) -> None:
    listing = editor.meeting_pattern_rows(["ai2", "DS2"])

    assert listing["programs"] == ["AI2", "DS2"]
    by_key = {c["course_key"]: c for c in listing["courses"]}
    assert sorted(by_key) == [MATH, STAT]
    assert by_key[STAT] == {
        "course_key": STAT,
        "course_code": "STAT103",
        "course_name": "PRINCIPLES OF PROBABILITY AND STATISTICS",
        "credit_hours": 3,
        "programmes": [
            {"program": "AI2", "meeting_pattern": "2x100"},
            {"program": "DS2", "meeting_pattern": ""},
        ],
        # DS2 has not chosen: it abstains, so AI2's choice is the agreement.
        "status": "agree",
        "meeting_pattern": "2x100",
        # The other programmes that teach this very course, with their votes.
        "co_owners": [
            {"program": "CS2", "meeting_pattern": "standard"},
            {"program": "IS2", "meeting_pattern": ""},
        ],
        "same_code_elsewhere": [],
        "clear_only": False,
        "reason": None,
    }
    math = by_key[MATH]
    assert math["course_name"] == MATH_NAME.strip()
    assert [c["program"] for c in math["co_owners"]] == ["AI", "DS"]
    # CYP2's MATH243 is another course: named, never a co-owner.
    assert math["same_code_elsewhere"] == [
        {"course_key": MATH_CYP2, "course_name": "LINEAR ALGEBRA", "programmes": ["CYP2"]}
    ]


def test_another_course_under_the_code_is_named_only_when_it_can_be_set() -> None:
    _req("IS2", "PHYS103", "PHYSICS")
    for program in ("AI", "IS"):
        _req(program, "PHYS103", "PHYSICS I", credits=4)
    _req("AI2", "PHYS103", "Physics II", kind="Program Elective")

    [physics] = editor.meeting_pattern_rows(["IS2"])["courses"]

    # PHYSICS I is 4 credits and PHYSICS II an elective slot: neither can be set.
    assert physics["course_key"] == "PHYS103::PHYSICS"
    assert physics["same_code_elsewhere"] == []


def test_programmes_that_differ_are_mixed_and_choose_nothing(plan) -> None:
    listing = editor.meeting_pattern_rows(["AI2", "CS2"])
    stat = next(c for c in listing["courses"] if c["course_key"] == STAT)
    assert (stat["status"], stat["meeting_pattern"]) == ("mixed", "")
    nobody = editor.meeting_pattern_rows(["DS2", "IS2"])["courses"]
    assert [(c["course_key"], c["status"], c["meeting_pattern"]) for c in nobody] == [
        (MATH, "none", ""),
        (STAT, "none", ""),
    ]


def test_the_listing_status_of_rows_that_may_vote_is_the_rules_merge(plan) -> None:
    rows = list(ProgrammeRequirement.objects.values(*editor.EDITOR_FIELDS))
    for programs in (["AI2", "DS2"], ["AI2", "CS2"], ["DS2", "IS2"], ["CS2"]):
        for course in editor.meeting_pattern_rows(programs)["courses"]:
            mine = [
                r
                for r in rows
                if r["program"] in programs and r["course_code"] == course["course_code"]
            ]
            rule = merge_programme_patterns(
                row for row in pre_key(mine) if row.identity == course["course_key"]
            )
            assert (course["status"], course["meeting_pattern"]) == (
                rule.status,
                rule.meeting_pattern,
            ), (programs, course["course_key"])


@pytest.mark.parametrize(
    ("code", "name", "credits", "kind"),
    [
        ("PHYS103", "PHYSICS I", 4, "Mandatory"),  # not 3 credits
        ("AI1", "Program Elective", 3, "Program Elective"),  # an elective slot
        ("AI492", "GRADUATION PROJECT II", 3, "Mandatory"),  # off the timetable
        ("MATH101", "CALCULUS", 3, "Mandatory"),  # a 101 course: off the timetable
        ("ENG102", "ENGLISH II", 3, "Mandatory"),  # a fixed English bundle
        ("CS250", "", 3, "Mandatory"),  # no course name: no identity both engines read
    ],
)
def test_a_row_that_may_not_vote_is_not_offered(code, name, credits, kind) -> None:
    _req("AI2", code, name, credits=credits, kind=kind)
    _req("AI2", "STAT103", "PRINCIPLES OF PROBABILITY AND STATISTICS")

    listing = editor.meeting_pattern_rows(["AI2"])

    assert [c["course_code"] for c in listing["courses"]] == ["STAT103"]


@pytest.mark.parametrize(
    ("code", "name", "kind", "reason"),
    [
        ("CS250", "", "Mandatory", "not_named"),
        ("AI1", "Program Elective", "Program Elective", "placeholder"),
        ("AI492", "GRADUATION PROJECT II", "Mandatory", "excluded"),
        ("ENG102", "ENGLISH II", "Mandatory", "policy"),
    ],
)
def test_a_row_that_may_no_longer_vote_but_holds_a_value_is_listed_to_be_cleared(
    code, name, kind, reason
) -> None:
    _req("AI2", code, name, kind=kind, pattern="2x100")

    [course] = editor.meeting_pattern_rows(["AI2"])["courses"]

    assert course["clear_only"] is True
    assert course["reason"] == reason
    assert course["programmes"] == [{"program": "AI2", "meeting_pattern": "2x100"}]
    # It shows what the row HOLDS (a nameless row never votes, yet holds 2x100).
    assert (course["status"], course["meeting_pattern"]) == ("agree", "2x100")


def test_the_choices_are_the_three_votes_in_the_engines_minutes(monkeypatch) -> None:
    choices = editor.meeting_pattern_rows([])["choices"]
    assert choices == [
        {"value": "", "label": "Not set", "label_ar": "غير محدد"},
        {"value": "standard", "label": "Standard (2 × 75 min)", "label_ar": "قياسي (٢ × ٧٥ دقيقة)"},
        {"value": "2x100", "label": "2 × 100 min", "label_ar": "٢ × ١٠٠ دقيقة"},
    ]
    # The standard minutes are the engine's, never written into the editor.
    from core.services import timetable_autoplace

    monkeypatch.setitem(timetable_autoplace.MEETING_PATTERNS, 3, [80, 80])
    moved = editor.meeting_pattern_rows([])["choices"][1]
    assert (moved["label"], moved["label_ar"]) == ("Standard (2 × 80 min)", "قياسي (٢ × ٨٠ دقيقة)")


def test_with_no_programme_the_listing_reads_nothing(django_assert_num_queries) -> None:
    with django_assert_num_queries(0):
        listing = editor.meeting_pattern_rows(["", "  "])
    assert (listing["programs"], listing["courses"]) == ([], [])


def test_an_unknown_programme_is_refused(plan) -> None:
    with pytest.raises(editor.PatternRequestError) as refused:
        editor.meeting_pattern_rows(["AI2", "XYZ"])
    assert (refused.value.code, refused.value.fields) == ("unknown_program", {"program": "XYZ"})


def test_the_listing_reads_the_study_plan_once(plan, django_assert_num_queries) -> None:
    with django_assert_num_queries(1):
        editor.meeting_pattern_rows(["AI", "AI2", "DS", "DS2"])


# ── the endpoints ────────────────────────────────────────────────────────────


def test_the_listing_endpoint_answers_the_programmes_on_screen(client: Client, plan) -> None:
    _login(client)
    response = client.get(LIST_URL, {"program": "AI2, DS2"})
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["programs"] == ["AI2", "DS2"]
    assert [c["course_key"] for c in data["courses"]] == [MATH, STAT]
    assert [c["value"] for c in data["choices"]] == ["", "standard", "2x100"]

    refused = client.get(LIST_URL, {"program": "AI2,NOPE"})
    assert refused.status_code == 400
    assert refused.json()["code"] == "unknown_program"


def test_the_listing_needs_a_general_advisor_answers_get_only_and_is_not_throttled(
    client: Client, plan
) -> None:
    assert client.get(LIST_URL, {"program": "AI2"}).status_code == 401
    _login(client, role=ROLE_ADVISOR, username="plain-advisor")
    assert client.get(LIST_URL, {"program": "AI2"}).status_code == 403
    _login(client)
    assert client.post(LIST_URL, {"program": "AI2"}).status_code == 405
    # Opening the panel, a programme change, a reload after each save: reads
    # never use up an allowance.
    for _ in range(25):
        assert client.get(LIST_URL, {"program": "AI2"}).status_code == 200


def test_saving_needs_a_general_advisor_and_a_post(client: Client, plan) -> None:
    body = json.dumps(
        {"programs": ["AI2"], "changes": [{"course_key": STAT, "meeting_pattern": ""}]}
    )
    assert client.post(SAVE_URL, body, content_type="application/json").status_code == 401
    _login(client, role=ROLE_ADVISOR, username="plain-advisor")
    assert client.post(SAVE_URL, body, content_type="application/json").status_code == 403
    _login(client)
    assert client.get(SAVE_URL).status_code == 405
    assert _votes()[("AI2", "STAT103")] == "2x100"


def test_saving_is_throttled_like_the_seat_limits(client: Client, plan) -> None:
    _login(client)
    body = {
        "programs": ["AI2"],
        "changes": [{"course_key": STAT, "meeting_pattern": "standard"}],
        "dry_run": True,
    }
    answers = [_post(client, body).status_code for _ in range(21)]
    assert answers == [200] * 20 + [429]


def test_the_routes_are_named() -> None:
    assert reverse("section_plan_meeting_patterns") == LIST_URL
    assert reverse("section_plan_save_meeting_patterns") == SAVE_URL


# ── scope ────────────────────────────────────────────────────────────────────


def test_a_save_writes_only_the_programmes_on_screen(client: Client, plan) -> None:
    _login(client)
    before = _votes()

    _, response = _save(client, ["DS2"], [{"course_key": STAT, "meeting_pattern": "2x100"}])

    assert response.status_code == 200, response.content
    after = _votes()
    assert after[("DS2", "STAT103")] == "2x100"
    assert {k: v for k, v in after.items() if k != ("DS2", "STAT103")} == {
        k: v for k, v in before.items() if k != ("DS2", "STAT103")
    }
    assert response.json()["changed"] == [
        {
            "course_key": STAT,
            "course_code": "STAT103",
            "course_name": "PRINCIPLES OF PROBABILITY AND STATISTICS",
            "program": "DS2",
            "credit_hours": 3,
            "old": "",
            "new": "2x100",
            "scope": "programmes",
        }
    ]


def test_all_programmes_widens_one_course_to_every_row_of_that_exact_identity(
    client: Client, plan
) -> None:
    _login(client)

    _, response = _save(
        client,
        ["AI2"],
        [{"course_key": MATH, "meeting_pattern": "standard", "all_programmes": True}],
    )

    assert response.status_code == 200, response.content
    after = _votes()
    assert [after[(p, "MATH243")] for p in ("AI", "AI2", "DS", "DS2")] == ["standard"] * 4
    # CYP2's MATH243 LINEAR ALGEBRA is another course: never reached.
    assert after[("CYP2", "MATH243")] == ""
    # STAT103 was not widened.
    assert after[("CS2", "STAT103")] == "standard"
    assert {c["scope"] for c in response.json()["changed"]} == {"all_programmes"}


def test_a_course_the_programmes_on_screen_do_not_teach_is_refused(client: Client, plan) -> None:
    _login(client)
    before = _votes()

    response = _post(
        client,
        {
            "programs": ["CYP2"],
            "changes": [{"course_key": MATH, "meeting_pattern": "2x100"}],
            "dry_run": True,
        },
    )

    assert response.status_code == 400
    assert response.json()["code"] == "course_not_in_programmes"
    assert response.json()["course_key"] == MATH
    assert _votes() == before


def test_a_value_is_set_only_on_rows_that_may_vote_and_the_rest_are_named(
    client: Client,
) -> None:
    # One course, one name: AI teaches it at 3 credits, AI2 at 4, DS as an elective slot.
    _req("AI", "CS330", "SOFTWARE ENGINEERING")
    _req("AI2", "CS330", "SOFTWARE ENGINEERING", credits=4)
    _req("DS", "CS330", "SOFTWARE ENGINEERING", kind="Program Elective")
    _login(client)

    preview, response = _save(
        client,
        ["AI"],
        [
            {
                "course_key": "CS330::SOFTWARE_ENGINEERING",
                "meeting_pattern": "2x100",
                "all_programmes": True,
            }
        ],
    )

    assert response.status_code == 200, response.content
    assert [(c["program"], c["new"]) for c in preview.json()["changes"]] == [("AI", "2x100")]
    assert [(s["program"], s["reason"]) for s in preview.json()["skipped"]] == [
        ("AI2", "not_three_credits"),
        ("DS", "placeholder"),
    ]
    assert _votes() == {("AI", "CS330"): "2x100", ("AI2", "CS330"): "", ("DS", "CS330"): ""}


def test_clearing_is_allowed_on_any_row_that_holds_a_value(client: Client) -> None:
    _req("AI2", "AI492", "GRADUATION PROJECT II", pattern="2x100")
    _req("AI2", "CS250", "", pattern="standard")
    _login(client)

    _, response = _save(
        client,
        ["AI2"],
        [
            {"course_key": "AI492::GRADUATION_PROJECT_II", "meeting_pattern": ""},
            {"course_key": "CS250", "meeting_pattern": ""},
        ],
    )

    assert response.status_code == 200, response.content
    assert _votes() == {("AI2", "AI492"): "", ("AI2", "CS250"): ""}
    # Setting one again is refused there: such a row may only be cleared.
    preview = _post(
        client,
        {
            "programs": ["AI2"],
            "changes": [{"course_key": "CS250", "meeting_pattern": "2x100"}],
            "dry_run": True,
        },
    ).json()
    assert preview["changes"] == []
    assert [(s["program"], s["reason"]) for s in preview["skipped"]] == [("AI2", "not_named")]


def test_rows_that_already_hold_the_value_are_counted_not_written(client: Client, plan) -> None:
    _login(client)

    preview, response = _save(
        client,
        ["AI2", "DS2"],
        [{"course_key": STAT, "meeting_pattern": "2x100"}],
    )

    assert preview.json()["unchanged"] == 1
    assert [c["program"] for c in preview.json()["changes"]] == ["DS2"]
    assert response.json()["changed_count"] == 1
    assert response.json()["unchanged"] == 1


# ── the preview is what gets written ─────────────────────────────────────────


def test_a_preview_writes_nothing_and_names_what_it_would_write(client: Client, plan) -> None:
    _login(client)
    before = _votes()

    response = _post(
        client,
        {
            "programs": ["AI2", "DS2"],
            "changes": [{"course_key": STAT, "meeting_pattern": "standard"}],
            "dry_run": True,
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["dry_run"] is True
    assert [(c["program"], c["old"], c["new"]) for c in data["changes"]] == [
        ("AI2", "2x100", "standard"),
        ("DS2", "", "standard"),
    ]
    assert len(data["preview_token"]) == 64
    assert _votes() == before
    assert not AuditLog.objects.filter(action=editor.AUDIT_ACTION).exists()


def test_a_commit_without_the_previews_token_writes_nothing(client: Client, plan) -> None:
    _login(client)
    before = _votes()

    response = _post(
        client, {"programs": ["DS2"], "changes": [{"course_key": STAT, "meeting_pattern": "2x100"}]}
    )

    assert response.status_code == 409
    assert response.json()["code"] == "preview_stale"
    assert _votes() == before


def test_a_commit_after_the_rows_moved_is_refused(client: Client, plan) -> None:
    _login(client)
    changes = [{"course_key": STAT, "meeting_pattern": "standard"}]
    preview = _post(client, {"programs": ["DS2"], "changes": changes, "dry_run": True}).json()
    # Someone else saves DS2 STAT103 between the preview and the commit.
    ProgrammeRequirement.objects.filter(program="DS2", course_code="STAT103").update(
        meeting_pattern="2x100"
    )

    response = _post(
        client,
        {
            "programs": ["DS2"],
            "changes": changes,
            "dry_run": False,
            "preview_token": preview["preview_token"],
        },
    )

    assert response.status_code == 409
    assert response.json()["code"] == "preview_stale"
    assert response.json()["changes"][0]["old"] == "2x100"
    assert _votes()[("DS2", "STAT103")] == "2x100"


WRITE = editor.PlannedPatternWrite(
    requirement_id=7,
    program="AI2",
    course_key=STAT,
    course_code="STAT103",
    course_name="PRINCIPLES OF PROBABILITY AND STATISTICS",
    credit_hours=3,
    old="",
    new="2x100",
    scope="programmes",
)


@pytest.mark.parametrize(
    "changed",
    [
        {"course_key": MATH},
        {"requirement_id": 8},
        {"program": "DS2"},
        {"old": "standard"},
        {"new": "standard"},
        {"credit_hours": 4},
        {"course_name": "Principles of Probability and Statistics"},
        {"scope": "all_programmes"},
    ],
)
def test_the_token_covers_every_field_a_row_was_previewed_with(changed) -> None:
    from dataclasses import replace

    assert editor.preview_token(["AI2"], [WRITE]) != editor.preview_token(
        ["AI2"], [replace(WRITE, **changed)]
    )


def test_the_token_names_the_action_and_the_programmes_on_screen(monkeypatch) -> None:
    token = editor.preview_token(["AI2"], [WRITE])
    assert token != editor.preview_token(["AI2", "DS2"], [WRITE])
    assert token == editor.preview_token(["AI2"], [WRITE])
    monkeypatch.setattr(editor, "AUDIT_ACTION", "section_planning.limit_change")
    assert token != editor.preview_token(["AI2"], [WRITE])


@contextmanager
def _moved_after_the_plan_reads(monkeypatch, **update):
    """The study plan is read for the commit, THEN the row changes (an import)."""
    real = editor._all_rows

    def read_then_move():
        rows = real()
        ProgrammeRequirement.objects.filter(program="DS2", course_code="STAT103").update(**update)
        return rows

    monkeypatch.setattr(editor, "_all_rows", read_then_move)
    yield


@pytest.mark.parametrize(
    "update",
    [
        {"course_name": "STATISTICS FOR EVERYONE"},  # a re-import renamed the course
        {"credit_hours": 4},  # a re-import changed its credits
        {"meeting_pattern": "standard"},  # another save
    ],
)
def test_a_row_that_moves_while_it_is_written_saves_nothing(
    client: Client, plan, monkeypatch, update
) -> None:
    _login(client)
    changes = [
        {"course_key": STAT, "meeting_pattern": "2x100", "all_programmes": True},
    ]
    preview = _post(client, {"programs": ["DS2"], "changes": changes, "dry_run": True}).json()
    before = _votes()

    with _moved_after_the_plan_reads(monkeypatch, **update):
        response = _post(
            client,
            {
                "programs": ["DS2"],
                "changes": changes,
                "dry_run": False,
                "preview_token": preview["preview_token"],
            },
        )

    assert response.status_code == 409
    assert response.json()["code"] == "row_changed"
    # Every row of the save is rolled back with it, and nothing is audited.
    moved = dict(before)
    if "meeting_pattern" in update:
        moved[("DS2", "STAT103")] = update["meeting_pattern"]
    assert _votes() == moved
    assert not AuditLog.objects.filter(action=editor.AUDIT_ACTION).exists()


# ── audit, fail-closed ───────────────────────────────────────────────────────


def test_every_changed_row_is_audited(client: Client, plan) -> None:
    _login(client, username="pattern-auditor")

    preview, response = _save(
        client,
        ["DS2"],
        [{"course_key": STAT, "meeting_pattern": "2x100", "all_programmes": True}],
    )

    assert response.status_code == 200, response.content
    rows = list(AuditLog.objects.filter(action=editor.AUDIT_ACTION).order_by("id"))
    ids = {r.program: r.id for r in ProgrammeRequirement.objects.filter(course_code="STAT103")}
    batch = preview.json()["preview_token"][:16]
    assert [json.loads(row.details_json) for row in rows] == [
        {
            "requirement_id": ids[program],
            "program": program,
            "course_code": "STAT103",
            "course_key": STAT,
            "old": old,
            "new": "2x100",
            "scope": "all_programmes",
            "programs_on_screen": ["DS2"],
            "batch": batch,
            "position": position,
            "of": 3,
            "source": "section_planning",
        }
        for position, (program, old) in enumerate(
            (("CS2", "standard"), ("DS2", ""), ("IS2", "")), 1
        )
    ]
    assert {row.actor_username for row in rows} == {"pattern-auditor"}
    assert {row.endpoint for row in rows} == {SAVE_URL}
    assert {row.method for row in rows} == {"POST"}
    assert {row.status for row in rows} == {"success"}


def test_a_failed_audit_write_saves_nothing(client: Client, plan, monkeypatch) -> None:
    _login(client)
    before = _votes()
    calls = []
    real_append = audit_service._append_audit_row

    def failing_second(**kwargs):
        calls.append(kwargs)
        if len(calls) == 2:
            raise RuntimeError("audit store down")
        return real_append(**kwargs)

    monkeypatch.setattr(audit_service, "_append_audit_row", failing_second)

    _, response = _save(
        client,
        ["DS2"],
        [{"course_key": STAT, "meeting_pattern": "2x100", "all_programmes": True}],
    )

    assert response.status_code == 503
    assert response.json()["code"] == "audit_unavailable"
    # The first row was written and audited before the second audit failed:
    # both are rolled back together.
    assert len(calls) == 2
    assert _votes() == before
    assert not AuditLog.objects.filter(action=editor.AUDIT_ACTION).exists()


def test_the_rows_and_their_audit_are_written_in_one_audited_transaction(
    client: Client, plan, monkeypatch
) -> None:
    """The process audit lock is held from BEGIN to COMMIT (section_limits' reason)."""
    entered = []
    real = editor.audited_transaction

    @contextmanager
    def watched():
        with real():
            entered.append(_votes()[("DS2", "STAT103")])
            yield
            entered.append(_votes()[("DS2", "STAT103")])

    monkeypatch.setattr(editor, "audited_transaction", watched)
    _login(client)

    _, response = _save(client, ["DS2"], [{"course_key": STAT, "meeting_pattern": "2x100"}])

    assert response.status_code == 200
    assert entered == ["", "2x100"], "the write happened inside the audited transaction"


# ── validation ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"changes": [{"course_key": STAT, "meeting_pattern": ""}]}, "programs_required"),
        (
            {"programs": [], "changes": [{"course_key": STAT, "meeting_pattern": ""}]},
            "programs_required",
        ),
        (
            {"programs": ["AI2"] * 21, "changes": [{"course_key": STAT, "meeting_pattern": ""}]},
            "programs_required",
        ),
        (
            {"programs": ["XYZ"], "changes": [{"course_key": STAT, "meeting_pattern": ""}]},
            "unknown_program",
        ),
        ({"programs": ["AI2"], "changes": []}, "changes_required"),
        (
            {"programs": ["AI2"], "changes": [{"course_key": STAT, "meeting_pattern": ""}] * 301},
            "too_many_changes",
        ),
        ({"programs": ["AI2"], "changes": [{"meeting_pattern": "2x100"}]}, "invalid_course"),
        (
            {"programs": ["AI2"], "changes": [{"course_key": " ", "meeting_pattern": "2x100"}]},
            "invalid_course",
        ),
        ({"programs": ["AI2"], "changes": ["STAT103"]}, "invalid_course"),
        ({"programs": ["AI2"], "changes": [{"course_key": STAT}]}, "invalid_pattern"),
        (
            {"programs": ["AI2"], "changes": [{"course_key": STAT, "meeting_pattern": "2x75"}]},
            "invalid_pattern",
        ),
        (
            {"programs": ["AI2"], "changes": [{"course_key": STAT, "meeting_pattern": None}]},
            "invalid_pattern",
        ),
        (
            {"programs": ["AI2"], "changes": [{"course_key": STAT, "meeting_pattern": 2}]},
            "invalid_pattern",
        ),
        (
            {
                "programs": ["AI2"],
                "changes": [{"course_key": STAT, "meeting_pattern": "", "all_programmes": "yes"}],
            },
            "invalid_scope",
        ),
        (
            {
                "programs": ["AI2"],
                "changes": [
                    {"course_key": STAT, "meeting_pattern": ""},
                    {
                        "course_key": "stat103::Principles of probability and statistics",
                        "meeting_pattern": "2x100",
                    },
                ],
            },
            "duplicate_course",
        ),
    ],
)
def test_invalid_requests_are_refused_with_a_code(
    client: Client, plan, body: dict, code: str
) -> None:
    _login(client)
    before = _votes()

    response = _post(client, {**body, "dry_run": True})

    assert response.status_code == 400
    assert response.json()["code"] == code
    assert _votes() == before


def test_a_body_that_is_not_an_object_is_refused(client: Client, plan) -> None:
    _login(client)
    assert _post(client, ["AI2"]).json()["code"] == "invalid_json"
    bad = client.post(SAVE_URL, "{not json", content_type="application/json")
    assert (bad.status_code, bad.json()["code"]) == (400, "invalid_json")


def test_a_vote_is_read_in_any_case_and_spacing(client: Client, plan) -> None:
    _login(client)
    _, response = _save(client, ["DS2"], [{"course_key": STAT, "meeting_pattern": " 2 X 100 "}])
    assert response.status_code == 200
    assert _votes()[("DS2", "STAT103")] == "2x100"


# ── what a change means elsewhere ────────────────────────────────────────────


def _scenario(
    name: str,
    programs: list[str],
    *,
    status: str = "draft",
    budget_key: str | None = STAT,
    budget_pattern: str = "",
    credit_hours: int = 3,
) -> TimetableScenario:
    scenario = TimetableScenario.objects.create(
        academic_year="1448", term="2", name=name, programs=programs, status=status
    )
    if budget_key:
        code, _, name_key = budget_key.partition("::")
        ScenarioSectionBudget.objects.create(
            scenario=scenario,
            course_key=budget_key,
            course_code=code,
            course_name=name_key.replace("_", " ") or "PRINCIPLES OF PROBABILITY AND STATISTICS",
            credit_hours=credit_hours,
            meeting_pattern=budget_pattern,
        )
    return scenario


def test_the_preview_names_co_owners_other_names_and_the_draft_scenarios_it_reaches(
    client: Client, plan
) -> None:
    hand_made = _scenario("hand-made 1448-2", ["AI", "AI2", "DS", "DS2"])
    already = _scenario("already 2x100", ["DS2"], budget_pattern="2x100")
    shared = _scenario("IS run", ["IS", "IS2"])
    older = _scenario("older run", [])
    DeliveryBoard.objects.create(scenario=older, label="T3", program="DS2,AI2", nominal_term=3)
    bare = _scenario("bare key", ["DS2"], budget_key="STAT103")
    _scenario("published", ["DS2"], status="published")
    _scenario("no budget row", ["DS2"], budget_key=None)
    _scenario("other programmes", ["CS2"])
    _scenario("another course", ["DS2"], budget_key="STAT103::STATISTICS_FOR_EVERYONE")
    # The same identity budgeted at 4 credits: 2 x 100 is no choice there.
    four = _scenario("4-credit budget", ["DS2"], credit_hours=4)
    _login(client)

    response = _post(
        client,
        {
            "programs": ["DS2"],
            "changes": [{"course_key": STAT, "meeting_pattern": "2x100"}],
            "dry_run": True,
        },
    )

    assert response.status_code == 200, response.content
    [course] = response.json()["courses"]
    assert course["course_key"] == STAT
    # Who else teaches it, and keeps its own vote under the unanimity rule.
    assert course["co_owners"] == [
        {"program": "AI2", "meeting_pattern": "2x100"},
        {"program": "CS2", "meeting_pattern": "standard"},
        {"program": "IS2", "meeting_pattern": ""},
    ]
    assert course["same_code_elsewhere"] == []
    # Only draft scenarios whose programmes include DS2 and that budget STAT103,
    # each resolved over ITS programmes with the new value, newest first.
    assert course["scenarios"] == [
        {
            "id": four.id,
            "name": "4-credit budget",
            "budget_pattern": "",
            "programme_pattern": "2x100",
            "status": "agree",
            # Its own credit hours' choices: 2 x 100 is not one, so no drift.
            "differs": False,
        },
        {
            "id": bare.id,
            "name": "bare key",
            "budget_pattern": "",
            "programme_pattern": "2x100",
            "status": "agree",
            "differs": True,
        },
        {
            "id": older.id,
            "name": "older run",
            "budget_pattern": "",
            "programme_pattern": "2x100",
            "status": "agree",
            "differs": True,
        },
        {
            "id": already.id,
            "name": "already 2x100",
            "budget_pattern": "2x100",
            "programme_pattern": "2x100",
            "status": "agree",
            "differs": False,
        },
        {
            "id": hand_made.id,
            "name": "hand-made 1448-2",
            "budget_pattern": "",
            "programme_pattern": "2x100",
            "status": "agree",
            "differs": True,
        },
    ]
    assert course["scenarios_more"] == 0
    assert shared.id not in {s["id"] for s in course["scenarios"]}


def test_the_preview_names_the_newest_draft_scenarios_and_counts_the_rest(
    client: Client, plan, monkeypatch
) -> None:
    monkeypatch.setattr(editor, "MAX_SCENARIOS", 2)
    made = [_scenario(f"run {n}", ["DS2"]) for n in range(4)]
    # The oldest budgets the course twice (exact and bare): it is counted once.
    ScenarioSectionBudget.objects.create(
        scenario=made[0], course_key="STAT103", course_code="STAT103", credit_hours=3
    )
    # Not a DS2 timetable: neither named nor counted.
    _scenario("not DS2", ["CS2"])
    _login(client)

    preview = _post(
        client,
        {
            "programs": ["DS2"],
            "changes": [{"course_key": STAT, "meeting_pattern": "2x100"}],
            "dry_run": True,
        },
    ).json()

    [course] = preview["courses"]
    # The timetables being worked on now are the newest: they are named.
    assert [s["id"] for s in course["scenarios"]] == [made[3].id, made[2].id]
    assert course["scenarios_more"] == 2


def test_an_english_course_budget_keeps_its_fixed_meetings_in_the_preview(client: Client) -> None:
    """ENG101 meets in a fixed weekly bundle: a scenario's budget for it never
    differs from its programmes, whatever they hold (here a vote left from
    before the policy, being cleared)."""
    _req("AI2", "ENG101", "ENGLISH LANGUAGE I", pattern="2x100")
    _req("DS2", "ENG101", "ENGLISH LANGUAGE I", pattern="2x100")
    english = _scenario("with English", ["AI2", "DS2"], budget_key="ENG101::ENGLISH_LANGUAGE_I")
    _login(client)

    preview = _post(
        client,
        {
            "programs": ["DS2"],
            "changes": [{"course_key": "ENG101::ENGLISH_LANGUAGE_I", "meeting_pattern": ""}],
            "dry_run": True,
        },
    ).json()

    assert [(c["program"], c["old"], c["new"]) for c in preview["changes"]] == [
        ("DS2", "2x100", "")
    ]
    # AI2 still holds 2 x 100 and DS2 abstains: they agree, yet the budget's
    # only choice is the English bundle, so it does not differ.
    assert preview["courses"][0]["scenarios"] == [
        {
            "id": english.id,
            "name": "with English",
            "budget_pattern": "",
            "programme_pattern": "2x100",
            "status": "agree",
            "differs": False,
        }
    ]


def test_a_draft_of_a_targeted_programme_is_named_even_when_its_row_already_holds_the_value(
    client: Client, plan
) -> None:
    """AI2 chose 2 x 100 earlier; its timetable, generated before, still teaches
    STAT103 2 x 75. Saving 2 x 100 for AI2 and DS2 changes only DS2's row, yet
    the AI2 timetable is named: it differs from what its programme decided."""
    ai2_only = _scenario("AI2 before the choice", ["AI2"])
    _login(client)

    preview = _post(
        client,
        {
            "programs": ["AI2", "DS2"],
            "changes": [{"course_key": STAT, "meeting_pattern": "2x100"}],
            "dry_run": True,
        },
    ).json()

    assert [c["program"] for c in preview["changes"]] == ["DS2"]
    assert preview["courses"][0]["scenarios"] == [
        {
            "id": ai2_only.id,
            "name": "AI2 before the choice",
            "budget_pattern": "",
            "programme_pattern": "2x100",
            "status": "agree",
            "differs": True,
        }
    ]


def test_a_scenario_whose_programmes_still_differ_is_said_to_keep_its_own(
    client: Client, plan
) -> None:
    both = _scenario("CS2 and DS2", ["CS2", "DS2"])
    _login(client)

    preview = _post(
        client,
        {
            "programs": ["DS2"],
            "changes": [{"course_key": STAT, "meeting_pattern": "2x100"}],
            "dry_run": True,
        },
    ).json()

    # CS2 chose Standard: with DS2's 2 x 100 they differ, so nothing is chosen.
    assert preview["courses"][0]["scenarios"] == [
        {
            "id": both.id,
            "name": "CS2 and DS2",
            "budget_pattern": "",
            "programme_pattern": "",
            "status": "mixed",
            "differs": False,
        }
    ]


def test_a_widened_preview_names_no_co_owner_it_already_reaches(client: Client, plan) -> None:
    _login(client)
    preview = _post(
        client,
        {
            "programs": ["AI2"],
            "changes": [
                {"course_key": MATH, "meeting_pattern": "standard", "all_programmes": True}
            ],
            "dry_run": True,
        },
    ).json()
    [course] = preview["courses"]
    assert course["co_owners"] == []
    assert course["same_code_elsewhere"] == [
        {"course_key": MATH_CYP2, "course_name": "LINEAR ALGEBRA", "programmes": ["CYP2"]}
    ]


def test_a_preview_reads_a_fixed_number_of_queries_however_many_courses_change(
    client: Client, plan, django_assert_max_num_queries
) -> None:
    for name in ("one", "two", "three"):
        _scenario(name, ["AI2", "DS2"])
    # Older scenarios saved without their programmes: their boards stand in.
    for name in ("old one", "old two", "old three"):
        older = _scenario(name, [])
        DeliveryBoard.objects.create(scenario=older, label="T3", program="AI2,DS2", nominal_term=3)
    body = {
        "programs": ["AI2", "DS2"],
        "changes": [
            {"course_key": STAT, "meeting_pattern": "standard"},
            {"course_key": MATH, "meeting_pattern": "standard"},
        ],
        "dry_run": True,
    }
    result = editor.save_meeting_patterns(body, actor=_actor(), source="test")
    assert result.status == 200
    assert {s["name"] for c in result.body["courses"] for s in c["scenarios"]} >= {
        "old one",
        "old two",
        "old three",
    }
    # The study plan, the budgets, their scenarios, the older ones' boards:
    # four, not one per course or per older scenario.
    with django_assert_max_num_queries(4):
        editor.save_meeting_patterns(body, actor=_actor(), source="test")


def test_many_scenarios_programmes_are_read_with_one_board_query(
    django_assert_num_queries,
) -> None:
    from core.services.programme_meeting_patterns import (
        scenario_programmes,
        scenarios_programmes,
    )

    named = _scenario("named", [" ds2", "AI2", "ds2"], budget_key=None)
    older = _scenario("older", [], budget_key=None)
    DeliveryBoard.objects.create(scenario=older, label="T1", program="DS, ai2", nominal_term=1)
    DeliveryBoard.objects.create(scenario=older, label="T2", program="AI2,DS2", nominal_term=2)
    bare = _scenario("no boards", [], budget_key=None)
    scenarios = [named, older, bare]

    with django_assert_num_queries(1):
        batch = scenarios_programmes(scenarios)

    assert batch == {s.id: scenario_programmes(s) for s in scenarios}
    assert batch == {named.id: ["AI2", "DS2"], older.id: ["AI2", "DS", "DS2"], bare.id: []}
    with django_assert_num_queries(0):
        assert scenarios_programmes([named]) == {named.id: ["AI2", "DS2"]}


# ── the page ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("language", "standard", "moved"),
    [
        ("en", "Standard (2 × 75 min)", "Standard (2 × 80 min)"),
        ("ar", "قياسي (٢ × ٧٥ دقيقة)", "قياسي (٢ × ٨٠ دقيقة)"),
    ],
)
def test_the_panel_note_names_the_standard_meetings_in_the_engines_minutes(
    client: Client, monkeypatch, language: str, standard: str, moved: str
) -> None:
    from bs4 import BeautifulSoup

    from core.services import timetable_autoplace

    _login(client)

    def note() -> str:
        page = client.get("/section-planning/", HTTP_ACCEPT_LANGUAGE=language)
        assert page.status_code == 200
        soup = BeautifulSoup(page.content.decode("utf-8"), "html.parser")
        return soup.find(id="spMeetNote").get_text(" ", strip=True)

    assert standard in note()
    monkeypatch.setitem(timetable_autoplace.MEETING_PATTERNS, 3, [80, 80])
    assert moved in note()
