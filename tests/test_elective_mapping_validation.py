"""Publication and student selection share one current-term elective contract."""

from __future__ import annotations

import json

import pytest
from django.contrib.auth.models import Group, User
from django.db import IntegrityError
from django.test import Client
from django.urls import reverse

from core.models import (
    Course,
    ElectiveCourse,
    ElectiveMappingScope,
    ElectiveTermMapping,
    PlannerDraft,
    ProgrammeRequirement,
    Student,
    StudentCourse,
    StudentTermSection,
)
from core.services import planner_drafts
from core.services.academic_state import _load_elective_options
from core.services.db_admin_ops import set_elective_term_mapping
from core.services.elective_readiness import slot_status
from core.services.elective_validation import ElectiveMappingError
from core.services.rbac import ROLE_SUPER_ADMIN, ensure_role_groups
from core.services.reporting import resolve_elective_recommendations
from core.services.student_otp import provision_student_user
from core.services.student_planner import (
    PlannerRequest,
    build_student_options,
    permitted_course_codes,
)
from core.services.virtual_advisor_capabilities import (
    _exec_course_prerequisites,
    _resolve_elective_slot,
)

pytestmark = pytest.mark.django_db
PROG, SLOT, YEAR, TERM, SID = "EVA", "VE1", "1448", "1", 9700801
URL = "/ops/electives/mapping/set/"


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setattr(planner_drafts, "planning_term", lambda: (YEAR, TERM))
    ProgrammeRequirement.objects.create(
        program=PROG, course_code=SLOT, type="Program Elective", credit_hours=3
    )
    ProgrammeRequirement.objects.create(
        program=PROG, course_code="MAND", type="Mandatory", credit_hours=3
    )
    options = {
        code: ElectiveCourse.objects.create(
            programme=PROG, course_code=code, course_name=code, credit_hours=3
        )
        for code in ("VX401", "VX402")
    }
    Student.objects.create(student_id=SID, name="Synthetic", program=PROG, section="M")
    ensure_role_groups()
    admin = User.objects.create_user("elective-admin")
    admin.groups.add(Group.objects.get(name=ROLE_SUPER_ADMIN))
    client = Client()
    client.force_login(admin)
    return options, client


def rows(*codes):
    return [{"placeholder_code": SLOT, "course_code": code} for code in codes]


def publish(*codes):
    return set_elective_term_mapping(YEAR, TERM, PROG, rows(*codes))


def stored():
    return set(
        ElectiveTermMapping.objects.filter(
            programme=PROG, academic_year=YEAR, term=TERM
        ).values_list("placeholder_code", "elective__course_code")
    )


def post(client, mappings, **scope):
    return client.post(
        URL,
        json.dumps(
            {"academic_year": YEAR, "term": TERM, "programme": PROG, "mappings": mappings, **scope}
        ),
        content_type="application/json",
    )


@pytest.mark.parametrize(
    "invalid",
    [
        rows("MISSING"),
        [{"placeholder_code": "UNKNOWN", "course_code": "VX402"}],
        [{"placeholder_code": "MAND", "course_code": "VX402"}],
        rows("VX402", "vx402"),
        [{"placeholder_code": SLOT}],
        [None],
        ["VX402"],
        [{"placeholder_code": [], "course_code": "VX402"}],
        None,
        {},
    ],
)
def test_invalid_replacement_does_not_erase_existing_rows(world, invalid):
    _, client = world
    publish("VX401")
    original = list(ElectiveTermMapping.objects.values())
    response = post(client, invalid)
    assert response.status_code == 400
    assert response.json()["ok"] is False and response.json()["errors"]
    assert list(ElectiveTermMapping.objects.values()) == original


@pytest.mark.parametrize(
    "scope",
    [
        {"term": "bad"},
        {"term": True},
        {"term": 4},
        {"academic_year": "x"},
        {"programme": "UNKNOWN"},
        {"programme": []},
    ],
)
def test_invalid_scope_is_controlled_and_preserves_data(world, scope):
    _, client = world
    publish("VX401")
    assert post(client, rows("VX402"), **scope).status_code == 400
    assert stored() == {(SLOT, "VX401")}


def test_missing_array_is_not_clear_but_explicit_empty_is(world):
    _, client = world
    publish("VX401")
    response = client.post(
        URL,
        json.dumps({"academic_year": YEAR, "term": TERM, "programme": PROG}),
        content_type="application/json",
    )
    assert response.status_code == 400 and stored()
    assert post(client, []).status_code == 200
    assert not stored()


def test_mixed_submission_is_all_or_nothing(world):
    _, client = world
    publish("VX401")
    assert post(client, rows("VX402", "UNKNOWN")).status_code == 400
    assert stored() == {(SLOT, "VX401")}


@pytest.mark.parametrize(
    "owner,credits", [("", 3), ("OTHER", 3), ("  ", 3), (PROG, 2), (PROG, 0), (PROG, -1)]
)
def test_invalid_option_is_withheld_by_every_actionable_reader_and_http(world, owner, credits):
    options, admin = world
    option = options["VX402"]
    option.programme, option.credit_hours = owner, credits
    option.save()
    publish("VX401")
    assert post(admin, rows("VX402")).status_code == 400
    # Simulate an old/admin-shell mapping that predates publication validation.
    ElectiveTermMapping.objects.create(
        programme=PROG, academic_year=YEAR, term=TERM, placeholder_code=SLOT, elective=option
    )
    assert slot_status(PROG, SLOT, YEAR, TERM)[0] == "INVALID_MAPPING"
    assert _resolve_elective_slot(SLOT, PROG, academic_year=YEAR, term=TERM) == []
    assert "VX401" not in permitted_course_codes(PROG)  # invalid sibling closes the whole slot
    assert "VX402" not in permitted_course_codes(PROG)
    assert resolve_elective_recommendations(
        {SID: [SLOT, "MAND"]}, year=int(YEAR), semester=int(TERM), program=PROG
    ) == {SID: ["MAND"]}
    result = build_student_options(
        PlannerRequest(
            student_id=SID,
            year=int(YEAR),
            term=int(TERM),
            must_include=("VX402",),
            include_recommendations=False,
        )
    )
    assert not result["alternatives"]
    assert result["unplaced"][0]["reason_code"] == "COURSE_NOT_CURRENTLY_SELECTABLE"
    catalogue = _exec_course_prerequisites(
        {"course_code": "VX401", "program": PROG}, {}, {"academic_year": YEAR, "term": TERM}
    )
    assert catalogue["is_concrete_elective"]
    assert catalogue["per_program"][0]["fulfills_elective_slots"] == []
    student = Client()
    student.force_login(provision_student_user(SID))
    response = student.get(reverse("student_course_detail", args=[SLOT]))
    assert response.status_code == 200 and response.json()["options"] == []
    draft = student.post(
        reverse("planner_draft_create"),
        json.dumps({"course_codes": ["VX402"]}),
        content_type="application/json",
    )
    assert draft.status_code == 400
    assert not PlannerDraft.objects.exists()
    assert not StudentCourse.objects.exists() and not StudentTermSection.objects.exists()


@pytest.mark.parametrize("credits", [None, 0, -1])
def test_unverified_slot_credits_cannot_be_published(world, credits):
    ProgrammeRequirement.objects.filter(program=PROG, course_code=SLOT).update(credit_hours=credits)
    with pytest.raises(ElectiveMappingError):
        publish("VX401")
    assert not ElectiveTermMapping.objects.exists()


def test_atomic_failure_after_delete_restores_existing_rows(world, monkeypatch):
    publish("VX401")

    def fail(*args, **kwargs):
        raise IntegrityError("injected bulk insert failure")

    monkeypatch.setattr(ElectiveTermMapping.objects, "bulk_create", fail)
    with pytest.raises(IntegrityError):
        publish("VX402")
    assert stored() == {(SLOT, "VX401")}


def test_replacement_scope_idempotence_and_commit_notifications(
    world, monkeypatch, django_capture_on_commit_callbacks
):
    options, client = world
    ElectiveTermMapping.objects.create(
        programme=PROG,
        placeholder_code=SLOT,
        elective=options["VX401"],
        academic_year="1447",
        term=2,
    )
    cache_events, audits = [], []
    monkeypatch.setattr(
        "core.services.reporting.clear_aggregate_cache", lambda: cache_events.append(True)
    )
    monkeypatch.setattr(
        "core.db_admin_views.log_audit_event", lambda *args, **kw: audits.append(kw)
    )
    with django_capture_on_commit_callbacks(execute=True):
        response = post(client, rows("VX402"))
        assert not cache_events and not audits
    assert response.status_code == 200 and cache_events and audits[0]["details"]["created"] == 1
    mapping_ids = list(ElectiveTermMapping.objects.values_list("id", flat=True))
    result = publish("VX402")
    assert result["created"] == result["cleared"] == 0 and result["retained"] == 1
    assert list(ElectiveTermMapping.objects.values_list("id", flat=True)) == mapping_ids
    assert (
        ElectiveMappingScope.objects.filter(programme=PROG, academic_year=YEAR, term=TERM).count()
        == 1
    )


def test_canonical_codes_and_versioned_programme_fallback(world):
    options, _ = world
    publish("VX401")
    ProgrammeRequirement.objects.create(
        program="EVA2", course_code=SLOT, type="Program Elective", credit_hours=3
    )
    assert slot_status("eva2", " ve1 ", YEAR, TERM)[1][0]["course_code"] == "VX401"
    exact = ElectiveCourse.objects.create(
        programme="EVA2", course_code="VX401", course_name="Exact variant", credit_hours=3
    )
    set_elective_term_mapping(YEAR, TERM, " eva2 ", rows("vx401"))
    actual = slot_status("EVA2", SLOT, YEAR, TERM)[1]
    assert len(actual) == 1 and actual[0]["id"] == exact.id
    assert options["VX401"].id != exact.id


def test_ambiguous_canonical_catalogue_identity_is_not_guessed(world):
    options, _ = world
    ElectiveCourse.objects.create(programme=PROG.lower(), course_code="vx401", credit_hours=3)
    with pytest.raises(ElectiveMappingError, match="ambiguous"):
        publish("VX401")
    ElectiveTermMapping.objects.create(
        programme=PROG,
        placeholder_code=SLOT,
        elective=options["VX401"],
        academic_year=YEAR,
        term=TERM,
    )
    assert slot_status(PROG, SLOT, YEAR, TERM)[0] == "INVALID_MAPPING"


def test_old_term_does_not_authorize_a_current_draft(world):
    options, _ = world
    ElectiveTermMapping.objects.create(
        programme=PROG,
        placeholder_code=SLOT,
        elective=options["VX401"],
        academic_year="1447",
        term=2,
    )
    assert "VX401" not in permitted_course_codes(PROG)
    with pytest.raises(ValueError):
        planner_drafts.create_draft(student_id=SID, course_codes=["VX401"])
    assert "VX401" in permitted_course_codes(PROG, academic_year="1447", term=2)


@pytest.mark.parametrize("action", ["edit", "generate"])
def test_withdrawal_invalidates_an_existing_draft(world, action):
    publish("VX401")
    draft = planner_drafts.create_draft(student_id=SID, course_codes=["VX401"])
    set_elective_term_mapping(YEAR, TERM, PROG, [])
    with pytest.raises(ValueError):
        if action == "edit":
            planner_drafts.edit_draft(draft, course_codes=["VX401"])
        else:
            planner_drafts.generate(draft)
    draft.refresh_from_db()
    assert draft.generated_version == 0


def test_historical_evidence_and_passed_placeholder_are_not_rewritten(world):
    options, _ = world
    option = options["VX401"]
    option.credit_hours = 0
    option.save()
    ElectiveTermMapping.objects.create(
        programme=PROG, placeholder_code=SLOT, elective=option, academic_year=YEAR, term=TERM
    )
    assert _load_elective_options(PROG, YEAR, TERM)[0].course_code == "VX401"
    assert slot_status(PROG, SLOT, YEAR, TERM)[0] == "INVALID_MAPPING"
    course = Course.objects.create(course_code=SLOT, description="Slot", credit_hours=3)
    StudentCourse.objects.create(student_id=SID, course=course, status="passed")
    from core.services.course_detail import build_course_detail

    detail = build_course_detail(SID, SLOT)
    assert detail["your_status"] == "passed" and detail["options"] == []


def test_students_cannot_publish(world):
    _, _admin = world
    client = Client()
    client.force_login(provision_student_user(SID))
    assert post(client, rows("VX401")).status_code == 403
    assert not ElectiveTermMapping.objects.exists()


def test_recommendations_require_a_declared_current_slot(world):
    options, _ = world
    for slot, year in ((SLOT, "1447"), ("UNKNOWN", YEAR)):
        ElectiveTermMapping.objects.create(
            programme=PROG,
            placeholder_code=slot,
            elective=options["VX401"],
            academic_year=year,
            term=TERM,
        )
    assert resolve_elective_recommendations(
        {SID: [SLOT, "UNKNOWN", "MAND"]}, year=int(YEAR), semester=int(TERM), program=PROG
    ) == {SID: ["MAND"]}


@pytest.mark.parametrize(
    "year,term", [(YEAR, 0), (YEAR, None), (YEAR, ""), (YEAR, 4), ("", 1), (None, 1), ("bad", 1)]
)
@pytest.mark.parametrize("program", [PROG, [PROG], None])
def test_unspecified_or_invalid_term_preserves_ordinary_recommendations_only(
    world, year, term, program
):
    publish("VX401")
    # Existing current mappings cannot authorize choices for an unknown scope.
    # Even a concrete catalogue code already present in input must be withheld.
    assert resolve_elective_recommendations(
        {SID: ["MAND", SLOT, "VX401"]}, year=year, semester=term, program=program
    ) == {SID: ["MAND"]}


@pytest.mark.parametrize("base_owner", [False, True])
@pytest.mark.parametrize("legacy_spelling", [False, True])
def test_admin_dialog_round_trip_retains_every_valid_choice(world, base_owner, legacy_spelling):
    options, client = world
    programme = f"{PROG}2"
    canonical_slots = [SLOT, "VE2"]

    def stored_identity(value):
        return f" {value.lower()} " if legacy_spelling else value

    for slot, slot_type in zip(
        canonical_slots, ("Program Elective", "Programme Elective"), strict=True
    ):
        ProgrammeRequirement.objects.create(
            program=stored_identity(programme),
            course_code=stored_identity(slot),
            type=slot_type,
            credit_hours=3,
        )
    for code, option in options.items():
        owner = PROG if base_owner and code == "VX402" else programme
        option.programme = stored_identity(owner)
        option.course_code = stored_identity(code)
        option.save()
    submitted = [
        {"placeholder_code": slot, "course_code": code}
        for slot, code in zip(canonical_slots, options, strict=True)
    ]
    assert post(client, submitted, programme=programme).status_code == 200
    if legacy_spelling:
        for mapping in ElectiveTermMapping.objects.filter(programme=programme):
            mapping.programme = stored_identity(programme)
            mapping.placeholder_code = stored_identity(mapping.placeholder_code)
            mapping.save()
    before = list(ElectiveTermMapping.objects.order_by("id").values())
    scope = {"academic_year": YEAR, "term": TERM, "programme": programme.lower()}
    responses = {
        kind: client.get(f"/ops/electives/{kind}/", scope)
        for kind in ("catalogue", "mapping", "placeholders")
    }
    assert all(response.status_code == 200 for response in responses.values())
    data = {kind: response.json()["items"] for kind, response in responses.items()}
    slots = {row["course_code"] for row in data["placeholders"]}
    catalogue = {row["course_code"] for row in data["catalogue"]}
    # Both dialogs render the Cartesian slot/catalogue checkboxes and check
    # only those whose canonical pair appears in the loaded mapping rows.
    checked = [
        {"placeholder_code": row["placeholder_code"], "course_code": row["course_code"]}
        for row in data["mapping"]
        if row["placeholder_code"] in slots and row["course_code"] in catalogue
    ]
    assert checked == submitted
    unchanged = post(client, checked, programme=programme)
    assert unchanged.status_code == 200 and unchanged.json()["retained"] == 2
    assert list(ElectiveTermMapping.objects.order_by("id").values()) == before


def test_builder_uses_validated_elective_credits_and_keeps_historical_baseline(world, monkeypatch):
    options, _ = world
    ProgrammeRequirement.objects.filter(program=PROG, course_code=SLOT).update(credit_hours=4)
    options["VX401"].credit_hours = 4
    options["VX401"].save()
    publish("VX401")
    calls = []

    def solver(**kwargs):
        calls.append(kwargs)
        return {"options": []}

    monkeypatch.setattr("core.services.student_planner.run_solver", solver)
    build_student_options(
        PlannerRequest(
            student_id=SID,
            year=int(YEAR),
            term=int(TERM),
            must_include=("VX401",),
            include_recommendations=False,
            course_credits_override=(("VX401", 3),),
        )
    )
    assert calls[0]["shortlist"][0]["credits"] == 4
    set_elective_term_mapping(YEAR, TERM, PROG, [])
    result = build_student_options(
        PlannerRequest(
            student_id=SID,
            year=int(YEAR),
            term=int(TERM),
            must_include=("VX401",),
            include_recommendations=False,
            keep_current_sections=True,
            baseline_override=({"course_code": "VX401", "section": "M1", "term_section_id": 9981},),
            course_credits_override=(("VX401", 4),),
        )
    )
    assert len(calls) == 1  # retained evidence is not scheduled again
    assert result["alternatives"][0]["course_count"] == 1
