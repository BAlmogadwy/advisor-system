"""A room written without exam seats starts at its capacity (owner decision 5, 2026-09-28).

Migration 0072 started every room that existed then at its capacity; this holds
every room written since to the same rule, whichever way it is written: an ORM
save, ``create()``, ``update_or_create()``, ``bulk_create()`` and the
``scripts/seed_rooms.py`` that re-creates the women's-campus rooms. A given value
is kept - 0 included, which is a room no exam seats anyone in - and a raw load
(a fixture, the release seed) keeps what it carries.
"""

from __future__ import annotations

import runpy
from pathlib import Path

import pytest
from django.core import serializers

from core.models import Room

pytestmark = pytest.mark.django_db

SEED_ROOMS = Path(__file__).resolve().parents[1] / "scripts" / "seed_rooms.py"


def _seats() -> dict[tuple[str, str], tuple[int, int]]:
    return {
        (room.room_code, room.section): (room.capacity, room.exam_capacity)
        for room in Room.objects.all()
    }


def test_create_without_exam_seats_starts_at_capacity():
    room = Room.objects.create(room_code="R40", capacity=40, section="M")
    assert room.exam_capacity == 40
    room.refresh_from_db()
    assert room.exam_capacity == 40


def test_given_exam_seats_are_kept_zero_and_above_capacity_included():
    Room.objects.create(room_code="R-zero", capacity=40, exam_capacity=0, section="M")
    Room.objects.create(room_code="R-more", capacity=40, exam_capacity=42, section="F")
    Room.objects.create(room_code="R-less", capacity=40, exam_capacity=20, section="F")
    assert _seats() == {
        ("R-zero", "M"): (40, 0),
        ("R-more", "F"): (40, 42),
        ("R-less", "F"): (40, 20),
    }


def test_the_capacity_at_the_first_save_is_the_one_taken():
    room = Room(room_code="R-late", section="F")
    room.capacity = 35
    room.save()
    room.refresh_from_db()
    assert (room.capacity, room.exam_capacity) == (35, 35)


def test_an_existing_rooms_exam_seats_are_its_own_after_that():
    room = Room.objects.create(room_code="R30", capacity=30, section="M")
    room.capacity = 50
    room.save()
    room.refresh_from_db()
    # Teaching seats changed; the exam seats stay what they were.
    assert (room.capacity, room.exam_capacity) == (50, 30)
    # Cleared, they follow the capacity again when the row is written.
    room.exam_capacity = None
    room.save()
    room.refresh_from_db()
    assert room.exam_capacity == 50


def test_bulk_create_starts_each_room_without_exam_seats_at_its_capacity():
    Room.objects.bulk_create(
        [
            Room(room_code="B25", capacity=25, section="M"),
            Room(room_code="B60", capacity=60, section="F"),
            Room(room_code="B-zero", capacity=60, exam_capacity=0, section="F"),
            Room(room_code="B-given", capacity=60, exam_capacity=45, section="M"),
        ]
    )
    assert _seats() == {
        ("B25", "M"): (25, 25),
        ("B60", "F"): (60, 60),
        ("B-zero", "F"): (60, 0),
        ("B-given", "M"): (60, 45),
    }


def test_update_or_create_creates_at_capacity_and_updates_only_what_it_names():
    Room.objects.update_or_create(room_code="U1", section="M", defaults={"capacity": 33})
    Room.objects.create(room_code="U2", section="M", capacity=20, exam_capacity=12)
    Room.objects.update_or_create(room_code="U2", section="M", defaults={"capacity": 48})
    assert _seats() == {("U1", "M"): (33, 33), ("U2", "M"): (48, 12)}


def test_a_raw_load_keeps_the_exam_seats_it_carries():
    """The release seed and fixtures load rows raw: what they carry is what lands."""
    Room.objects.create(room_code="S0", capacity=40, exam_capacity=0, section="F")
    Room.objects.create(room_code="S1", capacity=40, exam_capacity=44, section="F")
    dumped = serializers.serialize("json", Room.objects.order_by("id"))
    Room.objects.all().delete()
    for record in serializers.deserialize("json", dumped):
        record.save()
    assert _seats() == {("S0", "F"): (40, 0), ("S1", "F"): (40, 44)}


def test_seed_rooms_script_starts_every_room_it_creates_at_its_capacity(capsys, monkeypatch):
    # A men's room the script only updates: its exam seats are its own. The
    # table is not empty, so the re-run has to be forced.
    Room.objects.create(room_code="172FA001", section="M", capacity=10, exam_capacity=30)
    monkeypatch.setenv("SEED_ROOMS_OVERWRITE", "1")
    runpy.run_path(str(SEED_ROOMS))
    capsys.readouterr()
    seats = _seats()
    assert len(seats) == 64
    assert seats.pop(("172FA001", "M")) == (45, 30)
    assert all(capacity == exam for capacity, exam in seats.values())
    # Both campuses, rooms and labs, reached the rule.
    assert {section for _code, section in seats} == {"M", "F"}
    assert seats[("239FC007", "F")] == (70, 70)
    assert seats[("LIB826", "F")] == (40, 40)
    assert seats[("172GB001", "M")] == (25, 25)


def test_seed_rooms_seeds_an_empty_rooms_table_without_being_forced(capsys, monkeypatch):
    monkeypatch.delenv("SEED_ROOMS_OVERWRITE", raising=False)
    runpy.run_path(str(SEED_ROOMS))
    capsys.readouterr()
    seats = _seats()
    assert len(seats) == 64
    assert all(capacity == exam for capacity, exam in seats.values())


@pytest.mark.parametrize("flag", [None, "0", "yes"])
def test_seed_rooms_refuses_a_populated_rooms_table_and_changes_nothing(capsys, monkeypatch, flag):
    """A re-run would delete rooms outside its lists and reset the listed ones."""
    if flag is None:
        monkeypatch.delenv("SEED_ROOMS_OVERWRITE", raising=False)
    else:
        monkeypatch.setenv("SEED_ROOMS_OVERWRITE", flag)
    # A room the lists do not have (0073 added it), a listed women's room whose
    # capacity and exam seats moved on, and a listed men's room.
    Room.objects.create(room_code="204SC003", section="F", capacity=40, exam_capacity=42)
    Room.objects.create(room_code="239FC005", section="F", capacity=54, exam_capacity=20)
    Room.objects.create(room_code="172FA009", section="M", capacity=70, exam_capacity=70)
    before = _seats()
    with pytest.raises(SystemExit, match="Nothing was changed"):
        runpy.run_path(str(SEED_ROOMS))
    capsys.readouterr()
    assert _seats() == before
