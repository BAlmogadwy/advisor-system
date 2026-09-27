"""A linked exam is one sitting of several real courses, and nothing may split it.

These pin the contract of ``core.services.linked_exams`` itself: what a request
may send and what it is refused for (always naming the field), what the solvers
see once a link is collapsed to one unit, how their answers expand back to real
courses, and what "together" means - OVERFLOW at any index included.
"""

import pytest

from core.services.linked_exams import (
    COURSE_NOT_SELECTED,
    COURSE_REPEATED,
    INVALID,
    NO_LINKS,
    PINS_DISAGREE,
    SPLIT,
    TOO_FEW_MEMBERS,
    LinkedExamsError,
    link_index,
    linked_exams_qa,
    resolve_linked_exams,
)

#: Display code -> metadata, as a build selects them. "PHYS103 (1)" and
#: "PHYS103 (2)" are one registrar code that two study plans name differently.
META = {
    "AI212": {"course_identity": "AI212::intro ai"},
    "AI225": {"course_identity": "AI225::intro ai"},
    "PHYS103 (1)": {"course_identity": "PHYS103::physics i", "is_online": True},
    "PHYS103 (2)": {"course_identity": "PHYS103::general physics"},
    "CS101": {"course_identity": "CS101"},
    "MATH106": {"course_identity": "MATH106::calculus"},
}


def member(code):
    return {"course_identity": META[code]["course_identity"], "course_code": code}


def link(*codes):
    return {"members": [member(code) for code in codes]}


def resolve(raw, **kwargs):
    return resolve_linked_exams(raw, META, **kwargs)


def entry(code, day, period, slot_index=0):
    return {"course_code": code, "day": day, "period": period, "slot_index": slot_index}


# ── what a request may send ──────────────────────────────────────────────────


def test_a_link_is_resolved_by_identity_and_saved_in_code_order():
    links = resolve([link("PHYS103 (2)", "PHYS103 (1)"), link("AI225", "AI212")])
    assert links.saved() == [
        {"members": [member("AI212"), member("AI225")]},
        {"members": [member("PHYS103 (1)"), member("PHYS103 (2)")]},
    ]
    assert links.unit("AI225") == links.unit("AI212") == "AI212+AI225"
    assert links.members_of("AI212+AI225") == ("AI212", "AI225")
    assert links.weight("PHYS103 (1)+PHYS103 (2)") == 2
    assert links.unit("CS101") == "CS101" and links.weight("CS101") == 1


def test_the_identity_is_the_authority_and_the_code_is_rewritten():
    """Display numbers change with the population; a stale code is not an error."""
    links = resolve(
        [
            {
                "members": [
                    {"course_identity": META["PHYS103 (1)"]["course_identity"], "course_code": "X"},
                    {"course_identity": META["CS101"]["course_identity"]},
                ]
            }
        ]
    )
    assert [m["course_code"] for m in links.saved()[0]["members"]] == ["CS101", "PHYS103 (1)"]
    padded = resolve([{"members": [{"course_identity": " CS101 "}, member("AI212")]}])
    assert padded.unit("CS101") == "AI212+CS101", "Surrounding spaces are not part of an identity"


@pytest.mark.parametrize("raw", [None, []])
def test_no_links_is_the_empty_answer(raw):
    assert resolve(raw) is NO_LINKS
    assert not NO_LINKS


@pytest.mark.parametrize(
    ("raw", "code", "field"),
    [
        ("AI212", INVALID, "linked_exams"),
        ({"members": []}, INVALID, "linked_exams"),
        ([["AI212", "AI225"]], INVALID, "linked_exams[0].members"),
        ([{"courses": [member("AI212")]}], INVALID, "linked_exams[0].members"),
        ([{"members": "AI212,AI225"}], INVALID, "linked_exams[0].members"),
        ([{"members": [member("AI212")]}], TOO_FEW_MEMBERS, "linked_exams[0].members"),
        ([{"members": []}], TOO_FEW_MEMBERS, "linked_exams[0].members"),
        (
            [{"members": [member("AI212"), "AI225"]}],
            INVALID,
            "linked_exams[0].members[1].course_identity",
        ),
        (
            [{"members": [member("AI212"), {"course_code": "AI225"}]}],
            INVALID,
            "linked_exams[0].members[1].course_identity",
        ),
        (
            [{"members": [member("AI212"), {"course_identity": "  "}]}],
            INVALID,
            "linked_exams[0].members[1].course_identity",
        ),
        (
            [{"members": [member("AI212"), {"course_identity": 7}]}],
            INVALID,
            "linked_exams[0].members[1].course_identity",
        ),
        (
            [link("AI212", "AI225"), {"members": [member("CS101"), {"course_identity": "NOPE"}]}],
            COURSE_NOT_SELECTED,
            "linked_exams[1].members[1].course_identity",
        ),
        (
            [link("AI212", "AI225"), link("CS101", "AI225")],
            COURSE_REPEATED,
            "linked_exams[1].members[1].course_identity",
        ),
        (
            [link("AI212", "AI212")],
            COURSE_REPEATED,
            "linked_exams[0].members[1].course_identity",
        ),
    ],
    ids=[
        "not-a-list",
        "a-dict",
        "link-not-a-dict",
        "no-members-key",
        "members-not-a-list",
        "one-member",
        "no-member",
        "member-not-a-dict",
        "member-without-identity",
        "blank-identity",
        "identity-not-text",
        "not-selected",
        "course-in-two-links",
        "course-twice-in-one-link",
    ],
)
def test_every_refusal_names_the_field_it_is_about(raw, code, field):
    with pytest.raises(LinkedExamsError) as refused:
        resolve(raw)
    assert (refused.value.code, refused.value.field) == (code, field)
    assert isinstance(refused.value, ValueError), "Every caller already turns these into a 400"


def test_a_link_whose_key_is_already_a_course_is_refused():
    """A unit is keyed by its members' codes joined with "+". Were a real course
    ever named that, the unit and the course would be one exam to every solver."""
    meta = {**META, "AI212+AI225": {"course_identity": "AI212+AI225"}}
    with pytest.raises(LinkedExamsError) as refused:
        resolve_linked_exams([link("AI225", "AI212")], meta)
    assert (refused.value.code, refused.value.field) == (INVALID, "linked_exams")


def test_a_missing_course_is_named_by_the_code_the_page_sent():
    with pytest.raises(LinkedExamsError, match="Linked course CS999 is not selected"):
        resolve([{"members": [member("AI212"), {"course_identity": "x", "course_code": "CS999"}]}])


def test_members_pinned_to_different_times_are_refused():
    pins = [
        {"course_code": "AI212", "day": "Sun", "period": "P1"},
        {"course_code": "AI225", "day": "Mon", "period": "P1"},
    ]
    with pytest.raises(LinkedExamsError) as refused:
        resolve([link("CS101", "MATH106"), link("AI212", "AI225")], pinned=pins)
    assert (refused.value.code, refused.value.field) == (PINS_DISAGREE, "linked_exams[0]")


def test_one_pinned_member_or_members_pinned_alike_are_accepted():
    one = [{"course_code": "AI212", "day": "Sun", "period": "P1"}]
    alike = [*one, {"course_code": "AI225", "day": "Sun", "period": "P1"}]
    for pins in (one, alike):
        links = resolve([link("AI212", "AI225")], pinned=pins)
        assert links.pins(pins) == [{"course_code": "AI212+AI225", "day": "Sun", "period": "P1"}]


def test_a_board_that_splits_a_link_is_refused_never_moved():
    board = [entry("AI212", "Sun", "P1"), entry("AI225", "Sun", "P2"), entry("CS101", "Mon", "P1")]
    with pytest.raises(LinkedExamsError) as refused:
        resolve([link("CS101", "MATH106"), link("AI212", "AI225")], schedule_entries=board)
    assert refused.value.code == SPLIT
    # Both links are split - MATH106 is not on the board at all - and the
    # first in code order is the one named.
    assert refused.value.field == "linked_exams[0]"
    assert "AI212, AI225" in str(refused.value)


# ── together ─────────────────────────────────────────────────────────────────


def test_together_means_one_day_and_period():
    links = resolve([link("AI212", "AI225")])
    assert links.together([entry("AI212", "Sun", "P1"), entry("AI225", "Sun", "P1")])
    assert not links.together([entry("AI212", "Sun", "P1"), entry("AI225", "Sun", "P2")])
    assert not links.together([entry("AI212", "Sun", "P1"), entry("AI225", "Mon", "P1")])


def test_every_member_in_overflow_is_together_at_any_index():
    """Pinning an unrelated exam renumbers OVERFLOW on the page, and a Check
    gives an unnumbered entry the next free index: neither splits a link."""
    links = resolve([link("AI212", "AI225")])
    assert links.together(
        [entry("AI212", "OVERFLOW", "Extra-12", 12), entry("AI225", "OVERFLOW", "Extra-15", 15)]
    )


def test_a_link_half_in_overflow_is_split():
    links = resolve([link("AI212", "AI225")])
    assert links.split_units(
        [entry("AI212", "OVERFLOW", "Extra-12", 12), entry("AI225", "Sun", "P1")]
    ) == ["AI212+AI225"]


def test_a_member_missing_from_the_board_is_not_together():
    links = resolve([link("AI212", "AI225")])
    assert not links.together([entry("AI212", "Sun", "P1")])


def test_require_together_names_the_link_that_is_split():
    links = resolve([link("AI212", "AI225"), link("CS101", "MATH106")])
    board = [
        entry("AI212", "Sun", "P1"),
        entry("AI225", "Sun", "P1"),
        entry("CS101", "Mon", "P1"),
        entry("MATH106", "Tue", "P1"),
    ]
    with pytest.raises(LinkedExamsError) as refused:
        links.require_together(board)
    assert (refused.value.code, refused.value.field) == (SPLIT, "linked_exams[1]")


# ── collapse and expand ──────────────────────────────────────────────────────


def test_no_links_hands_every_input_back_itself():
    """Not an equal copy: the same object, so no solver can tell links exist."""
    enrolled = {"A": {1}}
    adj = {"A": {"B": 1}}
    buckets, course_buckets = {("AI", 1): {"A"}}, {"A": [("AI", 1)]}
    credits = {"A": 3}
    pins = [{"course_code": "A", "day": "Sun", "period": "P1"}]
    placements = {"A": 0}
    preferred = {"A": 2}
    entries = [entry("A", "Sun", "P1")]
    codes = ["A"]
    assert NO_LINKS.enrolled(enrolled) is enrolled
    assert NO_LINKS.adjacency(adj) is adj
    ptb, cb = NO_LINKS.buckets(buckets, course_buckets)
    assert ptb is buckets and cb is course_buckets
    assert NO_LINKS.credits(credits, default=3) is credits
    assert NO_LINKS.student_credits(credits, enrolled, default=3) == {}
    assert NO_LINKS.pins(pins) is pins
    assert NO_LINKS.placements(placements) is placements
    assert NO_LINKS.preferred(preferred, enrolled) is preferred
    assert NO_LINKS.expand_entries(entries) is entries
    assert NO_LINKS.expand_codes(codes) is codes
    assert NO_LINKS.units(codes) is codes
    assert NO_LINKS.units_of(codes) is codes
    assert NO_LINKS.split_units(entries) == []
    assert NO_LINKS.saved() == []


def test_a_unit_holds_every_members_students():
    links = resolve([link("AI212", "AI225")])
    enrolled = {"AI212": {1, 2}, "AI225": {3}, "CS101": {1, 9}}
    collapsed = links.enrolled(enrolled)
    assert collapsed == {"AI212+AI225": {1, 2, 3}, "CS101": {1, 9}}
    assert collapsed["CS101"] is enrolled["CS101"]
    assert enrolled["AI212"] == {1, 2}, "The input sets are never mutated"


def test_the_unit_graph_unites_neighbours_and_drops_the_edge_inside_the_link():
    """A student in two linked courses is a real clash no solver can fix; the
    solvers must not see it as one, or every placement would look illegal."""
    links = resolve([link("AI212", "AI225")])
    adj = {
        "AI212": {"AI225": 1, "CS101": 2},
        "AI225": {"AI212": 1, "CS101": 3, "MATH106": 1},
        "CS101": {"AI212": 2, "AI225": 3},
        "MATH106": {"AI225": 1},
    }
    assert links.adjacency(adj) == {
        "AI212+AI225": {"CS101": 5, "MATH106": 1},
        "CS101": {"AI212+AI225": 5},
        "MATH106": {"AI212+AI225": 1},
    }


def test_buckets_count_a_link_once():
    links = resolve([link("AI212", "AI225")])
    buckets = {("AI", 3): {"AI212", "CS101"}, ("AI2", 3): {"AI225", "AI212"}}
    course_buckets = {
        "AI212": [("AI", 3), ("AI2", 3)],
        "AI225": [("AI2", 3)],
        "CS101": [("AI", 3)],
    }
    unit_buckets, unit_course_buckets = links.buckets(buckets, course_buckets)
    assert unit_buckets == {("AI", 3): {"AI212+AI225", "CS101"}, ("AI2", 3): {"AI212+AI225"}}
    assert unit_course_buckets == {"AI212+AI225": [("AI", 3), ("AI2", 3)], "CS101": [("AI", 3)]}


def test_a_units_credit_is_its_heaviest_member_and_each_student_sits_their_own():
    links = resolve(
        [link("AI212", "AI225"), link("CS101", "MATH106"), link("PHYS103 (1)", "PHYS103 (2)")]
    )
    credits = {"AI212": 3, "AI225": 4, "CS101": 4, "MATH106": 3, "PHYS103 (1)": 4, "PHYS103 (2)": 4}
    enrolled = {
        "AI212": {1, 2},
        "AI225": {2, 3},
        "CS101": {4, 6},
        "MATH106": {5, 6},
        "PHYS103 (1)": {7},
        "PHYS103 (2)": {8},
    }
    assert links.credits(credits, default=3) == {
        "AI212+AI225": 4,
        "CS101+MATH106": 4,
        "PHYS103 (1)+PHYS103 (2)": 4,
    }
    # Only a mixed-credit link needs per-student credits. Students 2 and 6 sit
    # both members, and are scored on the heavier, whichever member it is.
    assert links.student_credits(credits, enrolled, default=3) == {
        "AI212+AI225": {1: 3, 2: 4, 3: 4},
        "CS101+MATH106": {4: 4, 5: 3, 6: 4},
    }
    assert links.credits({"AI212": 3, "CS101": 2}, default=3)["CS101+MATH106"] == 3, (
        "A member missing from the map counts at the default"
    )


def test_a_unit_prefers_its_largest_members_slot():
    links = resolve([link("AI212", "AI225")])
    enrolled = {"AI212": {1}, "AI225": {2, 3}}
    assert links.preferred({"AI212": 4, "AI225": 7, "CS101": 1}, enrolled) == {
        "CS101": 1,
        "AI212+AI225": 7,
    }


def test_placements_collapse_only_when_every_member_agrees():
    links = resolve([link("AI212", "AI225")])
    assert links.placements({"AI212": 3, "AI225": 3, "CS101": 1}) == {"AI212+AI225": 3, "CS101": 1}
    assert links.placements({"CS101": 1}) == {"CS101": 1}, "A link wholly in OVERFLOW is absent"
    for split in ({"AI212": 3, "AI225": 4}, {"AI212": 3}):
        with pytest.raises(LinkedExamsError) as refused:
            links.placements(split)
        assert refused.value.code == SPLIT


def test_collapsed_pins_that_disagree_are_refused_even_unvalidated():
    links = resolve([link("AI212", "AI225")])
    with pytest.raises(LinkedExamsError) as refused:
        links.pins(
            [
                {"course_code": "AI212", "day": "Sun", "period": "P1"},
                {"course_code": "AI225", "day": "Sun", "period": "P2"},
            ]
        )
    assert refused.value.code == PINS_DISAGREE


def test_a_unit_expands_to_every_member_at_its_one_slot():
    links = resolve([link("AI212", "AI225")])
    placed = [
        {"course_code": "AI212+AI225", "slot_index": 9, "day": "OVERFLOW", "period": "Extra-9"},
        {"course_code": "CS101", "slot_index": 0, "day": "Sun", "period": "P1"},
    ]
    assert links.expand_entries(placed) == [
        {"course_code": "CS101", "slot_index": 0, "day": "Sun", "period": "P1"},
        {"course_code": "AI212", "slot_index": 9, "day": "OVERFLOW", "period": "Extra-9"},
        {"course_code": "AI225", "slot_index": 9, "day": "OVERFLOW", "period": "Extra-9"},
    ]
    assert links.expand_codes(["CS101", "AI212+AI225"]) == ["AI212", "AI225", "CS101"]
    assert links.units(["CS101", "AI225", "AI212"]) == ["AI212+AI225", "CS101"]
    assert links.units_of({"AI225"}) == {"AI212+AI225"}


# ── readers ──────────────────────────────────────────────────────────────────


def test_a_saved_run_is_read_by_identity_not_by_its_stored_code():
    """The link was saved when "physics i" was PHYS103 (1); in this run the
    same identity is PHYS103 (2), and PHYS103 (1) is another course."""
    result = {
        "schedule": [
            {"course_code": "PHYS103 (2)", "course_identity": "PHYS103::physics i"},
            {"course_code": "PHYS103 (1)", "course_identity": "PHYS103::general physics"},
            {"course_code": "CS101", "course_identity": "CS101"},
        ],
        "linked_exams": [
            {
                "members": [
                    {"course_identity": "CS101", "course_code": "CS101"},
                    {"course_identity": "PHYS103::physics i", "course_code": "PHYS103 (1)"},
                ]
            }
        ],
    }
    assert link_index(result) == {"CS101": ("PHYS103 (2)",), "PHYS103 (2)": ("CS101",)}
    assert link_index({"schedule": [], "linked_exams": []}) == {}
    # A member this run does not hold leaves its partner linked to nothing.
    result["schedule"] = result["schedule"][1:]
    assert link_index(result) == {}


def test_the_committee_is_warned_in_counts_never_in_student_ids():
    links = resolve([link("AI212", "AI225"), link("PHYS103 (1)", "PHYS103 (2)")])
    enrolled = {
        "AI212": {1, 2, 3},
        "AI225": {3, 4, 2},
        "PHYS103 (1)": {7},
        "PHYS103 (2)": {8},
    }
    credits = {"AI212": 3, "AI225": 4, "PHYS103 (1)": 4, "PHYS103 (2)": 4}
    assert linked_exams_qa(links, enrolled, credits, META) == {
        "links": 2,
        "courses": 4,
        "students_in_two_linked_courses": 2,
        "mixed_credit_links": 1,
        "online_courses": 1,
    }
