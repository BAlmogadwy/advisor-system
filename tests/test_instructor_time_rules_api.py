"""Instructor teaching-time rules: the table's database invariants and the staff API.

The table holds its invariants itself (``bulk_create`` skips ``save()``), the
API replaces an instructor's rules as a set, reports every validation problem
at once, refuses a stale save with the fresh rules, audits before/after, and the
roster list carries a per-instructor summary in one extra query.
"""

from __future__ import annotations

import json

import pytest
from django.contrib.auth.models import Group, User
from django.db import IntegrityError, connection, transaction
from django.test import Client
from django.test.utils import CaptureQueriesContext

from core.models import Instructor, InstructorTimeRule
from core.services.instructor_availability import (
    KIND_AVAILABLE_ONLY,
    KIND_UNAVAILABLE,
    MAX_NOTE_LENGTH,
    MAX_RULES_PER_INSTRUCTOR,
    rules_revision,
)
from core.services.rbac import ROLE_ADVISOR, ROLE_SUPER_ADMIN, ensure_role_groups

pytestmark = pytest.mark.django_db

GET_URL = "/ops/instructors/time-rules/"
SET_URL = "/ops/instructors/time-rules/set/"
LIST_URL = "/ops/instructors/list/"
EMPTY_REVISION = rules_revision([])
EVERY_DAY = ["SUN", "MON", "TUE", "WED", "THU"]
SHAPE_KEYS = ("1x75", "1x100", "2x75", "2x100", "2x75+100")


def _summary(lecture, lab, *, days, lab_days=None, infeasible=(), state):
    """The API's ``open`` object on the default grid (35 lecture, 25 lab periods),
    from hand-derived counts, open days, infeasible shapes and state."""
    lab_days = days if lab_days is None else lab_days
    return {
        "lecture": {"open": lecture, "total": 35},
        "lab": {"open": lab, "total": 25},
        "days": {
            "lecture": [code in days for code in EVERY_DAY],
            "lab": [code in lab_days for code in EVERY_DAY],
        },
        "shapes": [{"key": key, "feasible": key not in infeasible} for key in SHAPE_KEYS],
        "no_teaching_days": [c for c in EVERY_DAY if c not in days and c not in lab_days],
        "state": state,
    }


FULL_OPEN = _summary(35, 25, days=EVERY_DAY, state="any")
NOTHING_OPEN = _summary(0, 0, days=[], infeasible=SHAPE_KEYS, state="none")


def _client(role: str = ROLE_SUPER_ADMIN) -> Client:
    ensure_role_groups()
    user, _ = User.objects.get_or_create(username=f"itr-{role.lower()}")
    user.groups.clear()
    user.groups.add(Group.objects.get(name=role))
    client = Client()
    client.force_login(user)
    return client


def _instructor(name: str = "Dr Rules", **kw) -> Instructor:
    return Instructor.objects.create(full_name=name, normalised_name=name.lower(), **kw)


def _wire(kind=KIND_UNAVAILABLE, days=("SUN",), start="09:00", end="10:15", note=""):
    return {"kind": kind, "days": list(days), "start": start, "end": end, "note": note}


def _set(client: Client, payload: dict):
    return client.post(SET_URL, data=json.dumps(payload), content_type="application/json")


def _save(client: Client, instructor: Instructor, rules: list[dict], revision=None):
    if revision is None:
        revision = client.get(GET_URL, {"instructor_id": instructor.pk}).json()["revision"]
    return _set(
        client, {"instructor_id": instructor.pk, "expected_revision": revision, "rules": rules}
    )


def _stored(instructor: Instructor) -> set[tuple]:
    return set(
        InstructorTimeRule.objects.filter(instructor=instructor).values_list(
            "kind", "day_mask", "start_minute", "end_minute", "note"
        )
    )


def _codes(response) -> list[tuple[int, str, str]]:
    assert response.status_code == 400, response.content
    error = response.json()["error"]
    assert error["code"] == "VALIDATION"
    return [(e["index"], e["field"], e["code"]) for e in error["details"]["errors"]]


# ── Database invariants (bulk_create skips save()) ───────────────


def _bulk(instructor: Instructor, **overrides) -> None:
    fields = {"kind": KIND_UNAVAILABLE, "day_mask": 1, "start_minute": 540, "end_minute": 600}
    InstructorTimeRule.objects.bulk_create(
        [InstructorTimeRule(instructor=instructor, **{**fields, **overrides})]
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"kind": "sometimes"},
        {"kind": "Unavailable"},
        {"day_mask": 0},
        {"day_mask": 32},
        {"start_minute": 600, "end_minute": 600},
        {"start_minute": 700, "end_minute": 600},
        {"start_minute": 0, "end_minute": 1441},
    ],
)
def test_the_database_refuses_an_invalid_rule(overrides):
    instructor = _instructor()
    with pytest.raises(IntegrityError), transaction.atomic():
        _bulk(instructor, **overrides)
    assert not InstructorTimeRule.objects.exists()


def test_the_database_accepts_the_edges_of_every_range():
    instructor = _instructor()
    _bulk(instructor, day_mask=1, start_minute=0, end_minute=1)
    _bulk(instructor, day_mask=31, start_minute=1439, end_minute=1440)
    _bulk(instructor, kind=KIND_AVAILABLE_ONLY, start_minute=0, end_minute=1440)
    assert InstructorTimeRule.objects.count() == 3


def test_the_database_refuses_the_same_rule_twice_for_one_instructor():
    first, second = _instructor("Dr One"), _instructor("Dr Two")
    _bulk(first)
    _bulk(second)  # another instructor may hold the same rule
    _bulk(first, note="differs only by note", day_mask=2)
    with pytest.raises(IntegrityError), transaction.atomic():
        _bulk(first, note="differs only by note")
    assert InstructorTimeRule.objects.count() == 3


def test_rules_belong_to_their_instructor_and_go_with_it():
    instructor = _instructor()
    _bulk(instructor)
    instructor.delete()
    assert not InstructorTimeRule.objects.exists()


# ── GET ──────────────────────────────────────────────────────────


def test_get_returns_rules_sorted_with_revision_grid_and_open_counts():
    instructor = _instructor(full_name_ar="د. القواعد", is_active=False)
    rows = [
        (KIND_UNAVAILABLE, 16, 0, 1440, "Thursday off"),
        (KIND_UNAVAILABLE, 2 | 4, 600, 660, ""),
        (KIND_AVAILABLE_ONLY, 31, 480, 960, "office hours"),
        (KIND_UNAVAILABLE, 2, 540, 600, ""),
        (KIND_UNAVAILABLE, 1 | 2, 540, 600, ""),
    ]
    InstructorTimeRule.objects.bulk_create(
        InstructorTimeRule(
            instructor=instructor, kind=k, day_mask=m, start_minute=s, end_minute=e, note=n
        )
        for k, m, s, e, n in rows
    )
    response = _client().get(GET_URL, {"instructor_id": instructor.pk})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["instructor"] == {
        "id": instructor.pk,
        "full_name": "Dr Rules",
        "full_name_ar": "د. القواعد",
        "is_active": False,
    }
    assert body["rules"] == [
        {
            "kind": "available_only",
            "days": EVERY_DAY,
            "start": "08:00",
            "end": "16:00",
            "note": "office hours",
            "all_day": False,
        },
        {
            "kind": "unavailable",
            "days": ["SUN", "MON"],
            "start": "09:00",
            "end": "10:00",
            "note": "",
            "all_day": False,
        },
        {
            "kind": "unavailable",
            "days": ["MON"],
            "start": "09:00",
            "end": "10:00",
            "note": "",
            "all_day": False,
        },
        {
            "kind": "unavailable",
            "days": ["MON", "TUE"],
            "start": "10:00",
            "end": "11:00",
            "note": "",
            "all_day": False,
        },
        {
            "kind": "unavailable",
            "days": ["THU"],
            "start": "00:00",
            "end": "24:00",
            "note": "Thursday off",
            "all_day": True,
        },
    ]
    assert body["revision"] == rules_revision(
        [
            {"kind": k, "day_mask": m, "start_minute": s, "end_minute": e, "note": n}
            for k, m, s, e, n in rows
        ]
    )
    assert body["grid"]["days"] == EVERY_DAY
    assert len(body["grid"]["lecture"]) == 7 and len(body["grid"]["lab"]) == 5
    # Available 08:00-16:00 keeps six lecture and three lab periods a day. Thursday is
    # off; the carve-outs leave Sun 5/2, Mon 3/1, Tue 3/1 and Wed 6/3. Four days
    # with both kinds of period: every course shape still fits.
    assert body["open"] == _summary(17, 7, days=["SUN", "MON", "TUE", "WED"], state="limited")


def test_get_without_rules_is_fully_open_with_the_empty_revision():
    instructor = _instructor()
    body = _client().get(GET_URL, {"instructor_id": instructor.pk}).json()
    assert (body["rules"], body["revision"], body["open"]) == ([], EMPTY_REVISION, FULL_OPEN)


@pytest.mark.parametrize("value", [None, "", "abc", "1.5", "-3", "9" * 30, chr(0x661)])
def test_get_with_a_bad_id_is_a_validation_error(value):
    params = {} if value is None else {"instructor_id": value}
    response = _client().get(GET_URL, params)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION"


def test_get_and_set_for_an_unknown_instructor_are_not_found():
    client = _client()
    response = client.get(GET_URL, {"instructor_id": 987654})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"
    response = _set(
        client, {"instructor_id": 987654, "expected_revision": EMPTY_REVISION, "rules": []}
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


@pytest.mark.parametrize("role", [ROLE_ADVISOR])
def test_a_role_below_general_advisor_is_denied_everything(role):
    instructor = _instructor()
    client = _client(role)
    assert client.get(GET_URL, {"instructor_id": instructor.pk}).status_code == 403
    response = _save(client, instructor, [_wire()], revision=EMPTY_REVISION)
    assert response.status_code == 403
    assert client.get(LIST_URL).status_code == 403
    assert not InstructorTimeRule.objects.exists()


def test_reading_is_get_only_and_saving_is_post_only():
    instructor = _instructor()
    client = _client()
    assert client.post(GET_URL, {"instructor_id": instructor.pk}).status_code == 405
    assert client.get(SET_URL).status_code == 405


# ── POST: save ───────────────────────────────────────────────────


def test_save_replaces_the_whole_set_and_returns_the_new_state():
    instructor = _instructor()
    client = _client()
    response = _save(
        client,
        instructor,
        [
            _wire(days=EVERY_DAY, start="00:00", end="13:00", note="  no mornings  "),
            _wire(KIND_AVAILABLE_ONLY, ["THU", "sun"], "08:00", "24:00"),
        ],
        revision=EMPTY_REVISION,
    )
    assert response.status_code == 200, response.content
    body = response.json()
    assert _stored(instructor) == {
        (KIND_UNAVAILABLE, 31, 0, 780, "no mornings"),
        (KIND_AVAILABLE_ONLY, 17, 480, 1440, ""),
    }
    assert [rule["days"] for rule in body["rules"]] == [["SUN", "THU"], EVERY_DAY]
    assert (
        body["revision"] == client.get(GET_URL, {"instructor_id": instructor.pk}).json()["revision"]
    )
    assert body["revision"] != EMPTY_REVISION
    # Only Sun and Thu are open at all, and only from 13:00 (4 lecture, 3 lab
    # periods each): two days cannot hold a three-meeting course.
    assert body["open"] == _summary(
        8, 6, days=["SUN", "THU"], infeasible=("2x75+100",), state="narrow"
    )
    assert body["instructor"]["id"] == instructor.pk

    response = _save(client, instructor, [_wire()], revision=body["revision"])
    assert response.status_code == 200
    assert _stored(instructor) == {(KIND_UNAVAILABLE, 1, 540, 615, "")}
    response = _save(client, instructor, [], revision=response.json()["revision"])
    assert response.status_code == 200
    assert response.json()["revision"] == EMPTY_REVISION
    assert not InstructorTimeRule.objects.exists()


def test_owner_only_between_11_and_13_saves_and_reports_no_open_period():
    instructor = _instructor()
    response = _save(
        _client(),
        instructor,
        [_wire(KIND_AVAILABLE_ONLY, EVERY_DAY, "11:00", "13:00")],
        revision=EMPTY_REVISION,
    )
    assert response.status_code == 200
    assert response.json()["open"] == NOTHING_OPEN


def test_a_stale_revision_is_refused_and_loses_nothing():
    instructor = _instructor()
    first, second = _client(), _client(ROLE_SUPER_ADMIN)
    loaded = first.get(GET_URL, {"instructor_id": instructor.pk}).json()["revision"]
    # Another editor saves first, from the same loaded state.
    winner = _save(second, instructor, [_wire(days=["MON"])], revision=loaded)
    assert winner.status_code == 200
    kept = _stored(instructor)

    response = _save(first, instructor, [_wire(days=["TUE"]), _wire(days=["WED"])], loaded)
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "STALE"
    assert error["details"]["revision"] == winner.json()["revision"]
    assert error["details"]["rules"] == winner.json()["rules"]
    assert _stored(instructor) == kept
    # Retrying with the fresh revision succeeds.
    retry = _save(first, instructor, [_wire(days=["TUE"])], error["details"]["revision"])
    assert retry.status_code == 200
    assert _stored(instructor) == {(KIND_UNAVAILABLE, 4, 540, 615, "")}


def test_a_note_edit_changes_the_revision_so_it_is_never_overwritten_unseen():
    instructor = _instructor()
    client = _client()
    first = _save(client, instructor, [_wire(note="a")], revision=EMPTY_REVISION).json()
    second = _save(client, instructor, [_wire(note="b")], revision=first["revision"])
    assert second.status_code == 200
    assert second.json()["revision"] != first["revision"]
    assert _save(client, instructor, [_wire(note="c")], first["revision"]).status_code == 409


def test_save_audits_before_and_after_without_notes(monkeypatch):
    from core import instructor_views

    seen = []
    monkeypatch.setattr(
        instructor_views,
        "log_audit_event",
        lambda request, **kw: seen.append((kw["action"], kw["status"], kw["details"])),
    )
    instructor = _instructor()
    client = _client()
    first = _save(
        client,
        instructor,
        [
            _wire(days=["MON", "SUN"], note="secret"),
            _wire(KIND_AVAILABLE_ONLY, ["THU"], "00:00", "24:00"),
        ],
        revision=EMPTY_REVISION,
    ).json()
    assert seen == [
        (
            "instructor_time_rules_set",
            "success",
            {
                "id": instructor.pk,
                "before": [],
                "after": [
                    "available_only|THU|00:00-24:00",
                    "unavailable|SUN,MON|09:00-10:15",
                ],
                # A note was written; its text stays out of the audit.
                "notes_changed": True,
            },
        )
    ]
    _save(client, instructor, [_wire(days=["TUE"], end="12:00")], revision=first["revision"])
    assert seen[-1] == (
        "instructor_time_rules_set",
        "success",
        {
            "id": instructor.pk,
            "before": ["available_only|THU|00:00-24:00", "unavailable|SUN,MON|09:00-10:15"],
            "after": ["unavailable|TUE|09:00-12:00"],
            "notes_changed": True,  # the "secret" note went with its rule
        },
    )
    assert "secret" not in json.dumps(seen)


def _audited_save(monkeypatch, client, instructor, rules, revision):
    from core import instructor_views

    seen = []
    monkeypatch.setattr(instructor_views, "log_audit_event", lambda request, **kw: seen.append(kw))
    response = _save(client, instructor, rules, revision=revision)
    assert response.status_code == 200, response.content
    assert len(seen) == 1
    return seen[0]["details"], response.json()["revision"]


def test_the_audit_flags_a_note_change_only_when_a_note_changed(monkeypatch):
    instructor = _instructor()
    client = _client()
    noted = _wire(days=["SUN"], note="Council")
    plain = _wire(days=["MON"])
    # Rules without notes: no flag.
    details, revision = _audited_save(monkeypatch, client, instructor, [plain], EMPTY_REVISION)
    assert "notes_changed" not in details
    # A rule with a note added.
    details, revision = _audited_save(monkeypatch, client, instructor, [plain, noted], revision)
    assert details["notes_changed"] is True
    # The noted rule's window moves, its note stays: no note changed.
    moved = {**noted, "end": "11:00"}
    details, revision = _audited_save(monkeypatch, client, instructor, [plain, moved], revision)
    assert "notes_changed" not in details
    assert details["before"] != details["after"]
    # Another rule without a note is added: still no note changed.
    extra = _wire(days=["TUE"])
    details, revision = _audited_save(
        monkeypatch, client, instructor, [plain, moved, extra], revision
    )
    assert "notes_changed" not in details
    # Only a note's text edited: the rules read the same, the flag says why it saved.
    edited = {**moved, "note": "Department council"}
    details, revision = _audited_save(
        monkeypatch, client, instructor, [plain, edited, extra], revision
    )
    assert details["before"] == details["after"]
    assert details["notes_changed"] is True
    # The same note moved to another rule.
    details, revision = _audited_save(
        monkeypatch,
        client,
        instructor,
        [{**plain, "note": "Department council"}, {**edited, "note": ""}, extra],
        revision,
    )
    assert details["before"] == details["after"]
    assert details["notes_changed"] is True
    assert "Department council" not in json.dumps(details)


def test_saving_the_stored_set_again_rewrites_nothing_and_audits_nothing(monkeypatch):
    from core import instructor_views

    seen = []
    monkeypatch.setattr(instructor_views, "log_audit_event", lambda request, **kw: seen.append(kw))
    instructor = _instructor()
    client = _client()
    rules = [_wire(days=["SUN", "MON"]), _wire(KIND_AVAILABLE_ONLY, EVERY_DAY, "08:00", "16:00")]
    saved = _save(client, instructor, rules, revision=EMPTY_REVISION).json()
    assert len(seen) == 1
    rows_before = list(InstructorTimeRule.objects.order_by("pk").values("pk", "updated_at"))

    # Same rules, other order, other day order: an identical set.
    again = list(reversed(rules))
    again[1] = {**again[1], "days": ["MON", "SUN"]}
    response = _save(client, instructor, again, revision=saved["revision"])
    assert response.status_code == 200
    assert response.json() == saved
    assert len(seen) == 1
    assert list(InstructorTimeRule.objects.order_by("pk").values("pk", "updated_at")) == rows_before


# ── POST: validation ─────────────────────────────────────────────


def _bad(rules, revision=EMPTY_REVISION, instructor=None):
    instructor = instructor or _instructor(f"Dr Invalid {Instructor.objects.count()}")
    payload = {"instructor_id": instructor.pk, "expected_revision": revision, "rules": rules}
    return _set(_client(), payload)


@pytest.mark.parametrize(
    ("rule", "expected"),
    [
        ("not a rule", [(0, "rule", "rule_not_object")]),
        (_wire(kind="sometimes"), [(0, "kind", "kind_invalid")]),
        (_wire(kind=None), [(0, "kind", "kind_invalid")]),
        ({**_wire(), "days": []}, [(0, "days", "days_empty")]),
        ({k: v for k, v in _wire().items() if k != "days"}, [(0, "days", "days_empty")]),
        ({**_wire(), "days": "SUN"}, [(0, "days", "days_invalid")]),
        ({**_wire(), "days": ["SUN", "FRI"]}, [(0, "days", "days_invalid")]),
        (_wire(start="9h"), [(0, "start", "start_invalid")]),
        (_wire(start="24:00", end="24:00"), [(0, "start", "start_invalid")]),
        (_wire(start=None), [(0, "start", "start_invalid")]),
        (_wire(end="25:00"), [(0, "end", "end_invalid")]),
        (_wire(end=""), [(0, "end", "end_invalid")]),
        (_wire(start="10:00", end="10:00"), [(0, "end", "end_not_after_start")]),
        (_wire(start="10:00", end="09:00"), [(0, "end", "end_not_after_start")]),
        (_wire(start="00:00", end="00:00"), [(0, "end", "end_not_after_start")]),
        (_wire(note="x" * (MAX_NOTE_LENGTH + 1)), [(0, "note", "note_too_long")]),
    ],
)
def test_each_rule_problem_has_its_own_code(rule, expected):
    instructor = _instructor()
    assert _codes(_bad([rule], instructor=instructor)) == expected
    assert not InstructorTimeRule.objects.filter(instructor=instructor).exists()


ARABIC_INDIC_NINE = chr(0x660) + chr(0x669) + ":" + chr(0x660) + chr(0x660)  # "09:00"
EXTENDED_ARABIC_INDIC_NINE = chr(0x6F0) + chr(0x6F9) + ":" + chr(0x6F0) + chr(0x6F0)


@pytest.mark.parametrize(
    ("rule", "expected"),
    [
        # days: strings equal to a day code, nothing else.
        ({**_wire(), "days": [0]}, [(0, "days", "days_invalid")]),
        ({**_wire(), "days": [True]}, [(0, "days", "days_invalid")]),
        ({**_wire(), "days": ["Sunday"]}, [(0, "days", "days_invalid")]),
        ({**_wire(), "days": ["SUN", 4]}, [(0, "days", "days_invalid")]),
        ({**_wire(), "days": [None]}, [(0, "days", "days_invalid")]),
        ({**_wire(), "days": {"SUN": True}}, [(0, "days", "days_invalid")]),
        # start / end: an ASCII "H:MM" string; never a number, a bool or seconds.
        (_wire(start=540), [(0, "start", "start_invalid")]),
        (_wire(start=True), [(0, "start", "start_invalid")]),
        (_wire(start=9.0), [(0, "start", "start_invalid")]),
        (_wire(start="09:00:00"), [(0, "start", "start_invalid")]),
        (_wire(start=ARABIC_INDIC_NINE), [(0, "start", "start_invalid")]),
        (_wire(start=EXTENDED_ARABIC_INDIC_NINE), [(0, "start", "start_invalid")]),
        (_wire(start="900"), [(0, "start", "start_invalid")]),
        (_wire(start="009:00"), [(0, "start", "start_invalid")]),
        (_wire(start="09:0"), [(0, "start", "start_invalid")]),
        (_wire(start="24:00", end="24:00"), [(0, "start", "start_invalid")]),
        (_wire(start="00:00", end=1440), [(0, "end", "end_invalid")]),
        (_wire(end=False), [(0, "end", "end_invalid")]),
        (_wire(end="24:00:00"), [(0, "end", "end_invalid")]),
        (_wire(end="10:15:00"), [(0, "end", "end_invalid")]),
        (_wire(end="24:01"), [(0, "end", "end_invalid")]),
        (_wire(end="10:60"), [(0, "end", "end_invalid")]),
        (_wire(end=ARABIC_INDIC_NINE), [(0, "end", "end_invalid")]),
        # note: a string when present.
        (_wire(note=None), [(0, "note", "note_invalid")]),
        (_wire(note=5), [(0, "note", "note_invalid")]),
        (_wire(note=["a"]), [(0, "note", "note_invalid")]),
    ],
)
def test_the_wire_is_strings_only_and_nothing_is_coerced(rule, expected):
    instructor = _instructor()
    assert _codes(_bad([rule], instructor=instructor)) == expected
    assert not InstructorTimeRule.objects.filter(instructor=instructor).exists()


def test_the_strict_wire_still_reads_every_canonical_form():
    instructor = _instructor()
    response = _bad(
        [
            {**_wire(days=[" sun", "Sun", "SUN", "thu "], start=" 9:05 ", end="24:00")},
            {
                k: v
                for k, v in _wire(KIND_AVAILABLE_ONLY, ["MON"], "00:00", "23:59").items()
                if k != "note"
            },
        ],
        instructor=instructor,
    )
    assert response.status_code == 200, response.content
    # Repeats of one day are one day; a missing note is an empty one.
    assert _stored(instructor) == {
        (KIND_UNAVAILABLE, 1 | 16, 545, 1440, ""),
        (KIND_AVAILABLE_ONLY, 2, 0, 1439, ""),
    }


def test_all_wire_problems_of_one_rule_are_reported_together():
    rule = {"kind": "unavailable", "days": [1], "start": 540, "end": "24:00:00", "note": 0}
    assert _codes(_bad([rule])) == [
        (0, "days", "days_invalid"),
        (0, "start", "start_invalid"),
        (0, "end", "end_invalid"),
        (0, "note", "note_invalid"),
    ]


def test_a_note_of_the_maximum_length_is_accepted():
    instructor = _instructor()
    response = _bad([_wire(note="x" * MAX_NOTE_LENGTH)], instructor=instructor)
    assert response.status_code == 200


def test_list_level_problems_use_index_minus_one():
    assert _codes(_bad("not a list")) == [(-1, "rules", "rules_not_list")]
    assert _codes(_bad(None)) == [(-1, "rules", "rules_not_list")]
    too_many = [_wire(start=f"{8 + i // 6:02d}:{(i % 6) * 10:02d}", end="20:00") for i in range(21)]
    assert len(too_many) == MAX_RULES_PER_INSTRUCTOR + 1
    assert _codes(_bad(too_many)) == [(-1, "rules", "too_many")]
    instructor = _instructor("Dr Twenty")
    assert _bad(too_many[:-1], instructor=instructor).status_code == 200


def test_duplicates_are_the_same_kind_days_and_window_whatever_the_order_or_note():
    response = _bad(
        [
            _wire(days=["SUN", "MON"]),
            _wire(days=["MON", "SUN"], note="again"),
            _wire(KIND_AVAILABLE_ONLY, ["SUN", "MON"]),
            _wire(days=["SUN", "MON"], end="10:16"),
            _wire(days=["SUN"]),
            _wire(days=["SUN", "MON"], start="09:01"),
            _wire(days=["SUN", "MON"]),
        ]
    )
    assert _codes(response) == [(1, "rule", "duplicate"), (6, "rule", "duplicate")]


@pytest.mark.parametrize("value", ["1", True, 1.0, 0, -1, None, [1], 2**63])
def test_the_body_instructor_id_is_a_positive_int_never_a_string_or_bool(value):
    # Instructor 1 exists, so "1", True and 1.0 would each find it if coerced.
    Instructor.objects.create(pk=1, full_name="Dr One", normalised_name="dr one")
    payload = {"expected_revision": EMPTY_REVISION, "rules": [_wire()]}
    if value is not None:
        payload["instructor_id"] = value
    assert _codes(_set(_client(), payload)) == [(-1, "instructor_id", "instructor_id_invalid")]
    assert not InstructorTimeRule.objects.exists()


def test_the_body_instructor_id_as_an_int_saves():
    instructor = _instructor()
    response = _set(
        _client(),
        {"instructor_id": instructor.pk, "expected_revision": EMPTY_REVISION, "rules": [_wire()]},
    )
    assert response.status_code == 200


@pytest.mark.parametrize("template", [" {pk}", "{pk} ", "+{pk}", "{pk}.0", "0", "00"])
def test_the_query_instructor_id_is_ascii_digits_only(template):
    instructor = _instructor()
    client = _client()
    assert client.get(GET_URL, {"instructor_id": str(instructor.pk)}).status_code == 200
    response = client.get(GET_URL, {"instructor_id": template.format(pk=instructor.pk)})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION"


def test_the_empty_rule_set_has_a_fixed_non_empty_revision_the_first_save_uses():
    assert isinstance(EMPTY_REVISION, str) and len(EMPTY_REVISION) == 16
    assert EMPTY_REVISION == rules_revision([]) == rules_revision(())
    instructor = _instructor()
    client = _client()
    loaded = client.get(GET_URL, {"instructor_id": instructor.pk}).json()["revision"]
    assert loaded == EMPTY_REVISION
    assert _save(client, instructor, [_wire()], revision=loaded).status_code == 200


def test_the_revision_is_required():
    instructor = _instructor()
    for revision in (None, "", 7, ["x"], {"r": 1}, True):
        payload = {"instructor_id": instructor.pk, "rules": []}
        if revision is not None:
            payload["expected_revision"] = revision
        assert _codes(_set(_client(), payload)) == [(-1, "expected_revision", "revision_required")]


def test_every_problem_of_a_request_is_reported_at_once():
    response = _set(
        _client(),
        {
            "instructor_id": "x",
            "rules": [
                _wire(),
                {"kind": "nope", "days": ["SUN", 9], "start": "8", "end": "07:00", "note": ""},
                7,
                _wire(start="12:00", end="11:00", note="y" * 201),
                _wire(),
            ]
            + [_wire(start=f"13:{i:02d}", end="20:00") for i in range(16)],
        },
    )
    assert _codes(response) == [
        (-1, "instructor_id", "instructor_id_invalid"),
        (-1, "expected_revision", "revision_required"),
        (-1, "rules", "too_many"),
        (1, "kind", "kind_invalid"),
        (1, "days", "days_invalid"),
        (1, "start", "start_invalid"),
        (2, "rule", "rule_not_object"),
        (3, "end", "end_not_after_start"),
        (3, "note", "note_too_long"),
        (4, "rule", "duplicate"),
    ]
    assert response.json()["error"]["message"]
    assert not InstructorTimeRule.objects.exists()


def test_a_body_that_is_not_json_is_refused():
    client = _client()
    response = client.post(SET_URL, data="{oops", content_type="application/json")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_JSON"


def test_other_errors_keep_their_shape_without_details():
    response = _client().get(GET_URL, {"instructor_id": 987654})
    assert response.json() == {
        "ok": False,
        "error": {"code": "NOT_FOUND", "message": "Instructor not found"},
    }


# ── Roster list summary ──────────────────────────────────────────


def _list(client: Client) -> dict[int, dict]:
    response = client.get(LIST_URL, {"include_inactive": "1"})
    assert response.status_code == 200
    return {row["id"]: row for row in response.json()["instructors"]}


def test_the_list_carries_each_instructors_rule_count_and_open_periods():
    free = _instructor("Dr Free")
    mornings = _instructor("Dr Afternoons")
    stuck = _instructor("Dr Stuck", is_active=False)
    client = _client()
    _save(client, mornings, [_wire(days=EVERY_DAY, start="00:00", end="13:00")], EMPTY_REVISION)
    _save(
        client,
        stuck,
        [_wire(KIND_AVAILABLE_ONLY, EVERY_DAY, "11:00", "13:00"), _wire(days=["SUN"])],
        EMPTY_REVISION,
    )
    rows = _list(client)
    assert rows[free.pk]["time_rules"] == {"count": 0, "open": FULL_OPEN}
    assert rows[mornings.pk]["time_rules"] == {
        "count": 1,
        "open": _summary(20, 15, days=EVERY_DAY, state="limited"),
    }
    assert rows[stuck.pk]["time_rules"] == {"count": 2, "open": NOTHING_OPEN}
    # The roster row keeps every field it had.
    assert rows[free.pk]["full_name"] == "Dr Free" and "max_weekly_hours" in rows[free.pk]


def _list_queries(client: Client) -> list[str]:
    with CaptureQueriesContext(connection) as ctx:
        _list(client)
    return [query["sql"] for query in ctx.captured_queries]


def test_the_list_summary_costs_one_query_however_many_instructors():
    client = _client()
    first = _instructor("Dr First")
    _save(client, first, [_wire()], EMPTY_REVISION)
    one = _list_queries(client)
    for n in range(6):
        extra = _instructor(f"Dr Extra {n}")
        _save(client, extra, [_wire(days=["MON"]), _wire(days=["TUE"])], EMPTY_REVISION)
    # Every state of the feasibility summary is in the list, still for one query.
    narrow = _instructor("Dr Narrow")
    _save(client, narrow, [_wire(KIND_AVAILABLE_ONLY, ["THU"], "00:00", "24:00")], EMPTY_REVISION)
    none = _instructor("Dr None")
    _save(client, none, [_wire(KIND_AVAILABLE_ONLY, EVERY_DAY, "11:00", "13:00")], EMPTY_REVISION)
    free = _instructor("Dr Free")
    many = _list_queries(client)
    assert len(many) == len(one)
    assert sum("instructor_time_rules" in sql for sql in many) == 1
    states = {pk: row["time_rules"]["open"]["state"] for pk, row in _list(client).items()}
    assert (states[first.pk], states[narrow.pk], states[none.pk], states[free.pk]) == (
        "limited",
        "narrow",
        "none",
        "any",
    )
