"""The rooms an exam may seat students in, and how many seats each one has.

Exams seat by ``Room.exam_capacity``; lecture timetabling, the planner and
section sizing keep ``Room.capacity`` (owner, 2026-09-28). Every exam path that
reads the rooms table - Build, multistart, Check, Save, Optimise, Fix, the
exam CSV import - reads it here, and gets each room's exam seats under the key
the exam engine has always read, ``capacity``. So the allocator, the
feasibility check, the room QA, the invigilator counts and the input
fingerprint are untouched: only where the number comes from changed. While a
room's exam seats equal its capacity, every exam result is byte for byte what
it was.

A saved timetable never reads this. Each room row it saved carries the seats
(``room_capacity``), building and floor its room had when it was built, and
viewing, exporting, copying or listing students from a saved run reads those,
so changing a room's exam seats changes no saved timetable. Check on a saved
run does read the inventory afresh, and its input fingerprint hashes the WHOLE
exam room inventory - every room's exam seats, cohort (section), department,
building and floor - so any change to it makes Check report that the inputs
changed since the save: a room the run never used included, a new room, a
deleted room. A teaching-capacity edit does not. That is the truth - the room
inventory changed - and nothing is written unless the user saves.

Rooms Available (the room QA's ``rooms_available``, and a result's
``rooms_count``) counts every room this returns, a room with 0 exam seats
included - exactly as rooms of capacity 0 were counted before exam seats
existed. Such a room seats nobody; counting only rooms with seats would change
that number from what the exam timetable reported before, so it is left as it
was.
"""

from __future__ import annotations

from collections.abc import Sequence

from core.models import Room

#: Room columns every exam path reads, in the order it has always read them.
#: ``capacity`` is filled from ``Room.exam_capacity``.
EXAM_ROOM_FIELDS = ("room_code", "capacity", "section", "department", "building", "floor")


def exam_room_inventory(
    *, order_by: Sequence[str] = (), extra_fields: Sequence[str] = ()
) -> list[dict]:
    """Every room as an exam sees it: ``capacity`` is its exam seats.

    ``order_by`` orders the rows (none: the table's own order, as the build
    has always read it); ``extra_fields`` adds other columns after the usual
    ones.
    """
    fields = (*EXAM_ROOM_FIELDS, *extra_fields)
    columns = ["exam_capacity" if field == "capacity" else field for field in fields]
    rooms = Room.objects.all()
    if order_by:
        rooms = rooms.order_by(*order_by)
    return [dict(zip(fields, row, strict=True)) for row in rooms.values_list(*columns)]
