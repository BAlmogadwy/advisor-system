"""Women's-campus room data for 1448 term 2, from the department's room-capacity sheet.

Source: "سعة القاعات - شطر الطالبات - الفصل الدراسي الثاني 1448" (CS department, 29 rooms).
Owner decisions (2026-09-28):
- exam_capacity = the sheet's periodic (midterm) exam capacity, for every sheet room on the
  women's campus (section F).
- department is left as it is: the database is the authority for it.
- building: the sheet wins where it disagrees (six IS rooms recorded as "239" are in the TV
  building).
- the four sheet rooms missing from the database are inserted, on the women's campus, for
  CS,CS2, with capacity = the sheet's actual capacity - but only when no room with that code
  exists on either campus, and no women's-campus room already holds that hall in that building.

Rows are matched by (room_code, section), never by id, so the same migration gives the same
result on the local database and on production. An empty rooms table (a test database, or a
fresh database about to be loaded from a release seed) is left alone.
"""

from django.db import migrations

WOMEN = "F"

# room_code -> periodic exam capacity (sheet column "Exam Capacity - دوري").
EXAM_CAPACITY = {
    "239GC008": 35,
    "239GC007": 30,
    "239GC006": 30,
    "239GC004": 45,
    "239GC003": 28,
    "239FC009": 28,
    "239FC007": 56,
    "239FC004": 20,
    "239FC003": 30,
    "239FC002": 30,
    "239FC001": 54,
    "239FC008": 40,
    "239GC002": 28,
    "239GC005": 20,
    "239GC009": 35,
    "239GC012": 35,
    "239GC011": 28,
    "239FC006": 28,
    "227GC003": 30,
    "227FC001": 40,
    "227FC002": 40,
    "227FC003": 40,
    "227FC006": 44,
    "227FC005": 30,
    "227FC004": 44,
}

# room_code -> (building on the sheet, building recorded before this migration).
BUILDING_FIX = {
    "239GC002": ("TV", "239"),
    "239GC005": ("TV", "239"),
    "239GC009": ("TV", "239"),
    "239GC012": ("TV", "239"),
    "239GC011": ("TV", "239"),
    "239FC006": ("TV", "239"),
}

NEW_ROOMS = (
    {"room_code": "239FC005", "wing": "F07", "building": "TV", "capacity": 54, "exam_capacity": 20},
    {
        "room_code": "204SC006",
        "wing": "326",
        "building": "204",
        "capacity": 40,
        "exam_capacity": 42,
    },
    {
        "room_code": "204SC003",
        "wing": "360",
        "building": "204",
        "capacity": 40,
        "exam_capacity": 42,
    },
    {
        "room_code": "204GC007",
        "wing": "121 B",
        "building": "204",
        "capacity": 40,
        "exam_capacity": 45,
    },
)
NEW_ROOM_DEFAULTS = {
    "section": WOMEN,
    "department": "CS,CS2",
    "room_type": "lecture",
    "floor": None,
}


def _code_taken(rooms, code: str) -> bool:
    """A room with this code on either campus, ignoring case and surrounding spaces."""
    wanted = code.strip().upper()
    return any(
        str(existing).strip().upper() == wanted
        for existing in rooms.values_list("room_code", flat=True)
    )


def apply_sheet(apps, schema_editor):
    Room = apps.get_model("core", "Room")
    rooms = Room.objects.using(schema_editor.connection.alias)
    if not rooms.exists():
        return

    report = []
    set_exam = 0
    for code, value in EXAM_CAPACITY.items():
        updated = rooms.filter(room_code=code, section=WOMEN).update(exam_capacity=value)
        if updated:
            set_exam += updated
        else:
            report.append(f"exam_capacity: {code} is not on the women's campus - skipped")

    moved = 0
    for code, (building, _before) in BUILDING_FIX.items():
        moved += (
            rooms.filter(room_code=code, section=WOMEN)
            .exclude(building=building)
            .update(building=building)
        )

    inserted = 0
    for spec in NEW_ROOMS:
        code = spec["room_code"]
        if _code_taken(rooms, code):
            report.append(f"insert: {code} already exists - skipped")
            continue
        if rooms.filter(section=WOMEN, building=spec["building"], wing=spec["wing"]).exists():
            report.append(
                f"insert: hall {spec['wing']} in building {spec['building']} already has a room - {code} skipped"
            )
            continue
        rooms.create(**NEW_ROOM_DEFAULTS, **spec)
        inserted += 1

    print(
        f"\n  rooms 1448 T2 women's campus: exam_capacity set on {set_exam} rooms, "
        f"building corrected on {moved}, inserted {inserted}"
    )
    for line in report:
        print(f"  {line}")


def revert_sheet(apps, schema_editor):
    """Undo exactly what apply_sheet can have done."""
    Room = apps.get_model("core", "Room")
    rooms = Room.objects.using(schema_editor.connection.alias)
    for spec in NEW_ROOMS:
        rooms.filter(
            room_code=spec["room_code"],
            section=WOMEN,
            building=spec["building"],
            wing=spec["wing"],
            department=NEW_ROOM_DEFAULTS["department"],
        ).delete()
    for code, (building, before) in BUILDING_FIX.items():
        rooms.filter(room_code=code, section=WOMEN, building=building).update(building=before)
    for code in EXAM_CAPACITY:
        for room in rooms.filter(room_code=code, section=WOMEN):
            room.exam_capacity = room.capacity
            room.save(update_fields=["exam_capacity"])


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0072_room_exam_capacity"),
    ]

    operations = [
        migrations.RunPython(apply_sheet, revert_sheet),
    ]
