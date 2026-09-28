"""Rooms-table seed for an EMPTY rooms table (a fresh development database).

Its lists are the original 64-room set from two source PDFs plus
supplemental labs:
  F: 33 rooms (hall_capacity_cleaned) — 7 labs + 26 lectures
  M: 29 lecture rooms (العدد الفعلي للقاعات والمعامل) + 2 AI/DS labs
They are NOT the current room sheet: migrations (0073, the 1448 T2
women's-campus sheet) and edits since have moved on from them.

Usage, on an empty rooms table (Windows bash / SQLite):

    DJANGO_SECRET_KEY=dev python -c "import django, os; \\
        os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings'); \\
        django.setup(); exec(open('scripts/seed_rooms.py', encoding='utf-8').read())"

On a rooms table that already has rooms it refuses and changes nothing. A
re-run REWRITES the table to its lists: it deletes every women's room and
re-creates only the listed ones, deletes every men's room not in its lists,
and resets each listed men's room's capacity, department, type, building and
wing to the list's (stale) values. Against the local database of 2026-09-28
that deletes 12 rooms (the three rooms migration 0073 added among them), resets
13 rooms' capacities, gives every re-created women's room exam seats equal to
its capacity (the 0073 exam seats are lost), and re-creates a room the table
no longer has. An upserted men's room keeps the exam seats it had, so its exam
and teaching seats may drift apart. Never run it against production or the
authoritative local database; forcing a re-run on a populated table takes
``SEED_ROOMS_OVERWRITE=1`` in the environment.

Exam seats: every room this creates starts ``exam_capacity`` at its
``capacity`` (``core.models.ExamCapacityField``).
"""

import os

from core.models import Room

F_ROOMS = [
    # (code, wing, building, room_type, capacity, department)
    ("239GC008", "G05", "TV", "lecture", 35, "CS,CS2"),
    ("239GC007", "G07", "TV", "lecture", 25, "CS,CS2"),
    ("239GC006", "G08", "TV", "lecture", 25, "CS,CS2"),
    ("239GC004", "G10", "TV", "lecture", 40, "CS,CS2"),
    ("239GC003", "G11", "TV", "lecture", 30, "CS,CS2"),
    ("239FC009", "F03", "TV", "lecture", 28, "CS,CS2"),
    ("239FC007", "F04", "TV", "lecture", 70, "CS,CS2"),
    ("239FC004", "F08", "TV", "lecture", 23, "CS,CS2"),
    ("239FC003", "F09", "TV", "lecture", 20, "CS,CS2"),
    ("239FC002", "F10", "TV", "lecture", 30, "CS,CS2"),
    ("239FC001", "F12", "TV", "lecture", 60, "CS,CS2"),
    ("239FC008", "F13", "TV", "lecture", 40, "CS,CS2"),
    ("239GC002", "G12", "TV", "lecture", 28, "IS,IS2"),
    ("239GC005", "G09", "TV", "lecture", 20, "IS,IS2"),
    ("239GC009", "G04", "TV", "lecture", 35, "IS,IS2"),
    ("239GC012", "G03", "TV", "lecture", 35, "IS,IS2"),
    ("239GC011", "G13", "TV", "lecture", 28, "IS,IS2"),
    ("239FC006", "F06", "TV", "lecture", 28, "IS,IS2"),
    ("239FC005", "F07", "TV", "lecture", 20, "IS,IS2"),
    ("227GC003", "109", "227", "lecture", 30, "AI,AI2,DS,DS2"),
    ("227FC001", "205", "227", "lecture", 42, "AI,AI2,DS,DS2"),
    ("227FC002", "206", "227", "lecture", 42, "AI,AI2,DS,DS2"),
    ("227FC003", "207", "227", "lecture", 42, "AI,AI2,DS,DS2"),
    ("227FC006", "210", "227", "lecture", 55, "AI,AI2,DS,DS2"),
    ("227FC005", "209", "227", "lecture", 30, "CYP,CYP2"),
    ("227FC004", "208", "227", "lecture", 44, "CYP,CYP2"),
    ("LIB847", "847", "LIB", "lab", 30, "AI,AI2,DS,DS2"),
    ("LIB830", "830", "LIB", "lab", 30, "AI,AI2,DS,DS2"),
    ("LIB826", "826", "LIB", "lab", 40, "AI,AI2,DS,DS2"),
    ("LIB845", "845", "LIB", "lab", 24, "AI,AI2,DS,DS2"),
    ("LIB838", "838", "LIB", "lab", 26, "CYP,CYP2"),
    ("LIB839A", "839A", "LIB", "lab", 26, "CYP,CYP2"),
    ("LIB839B", "839B", "LIB", "lab", 26, "CYP,CYP2"),
]

M_ROOMS = [
    # (code, capacity, department)
    ("172GD003", 40, "COE,COE2"),
    ("172FB005", 20, "COE,COE2"),
    ("172FB006", 20, "COE,COE2"),
    ("172FB011", 15, "COE,COE2"),
    ("172FA001", 45, "CS,CS2"),
    ("172FA002", 48, "CS,CS2"),
    ("172FA003", 25, "CS,CS2"),
    ("172FA011", 15, "CS,CS2"),
    ("172FB003", 25, "CS,CS2"),
    ("172FB010", 25, "CS,CS2"),
    ("172SA002", 50, "CS,CS2"),
    ("172FA010", 15, "IS,IS2"),
    ("172FA009", 60, "IS,IS2"),
    ("172FA004", 25, "IS,IS2"),
    ("172FA005", 20, "IS,IS2"),
    ("172FA006", 15, "IS,IS2"),
    ("172FB001", 50, "IS,IS2"),
    ("172FB009", 25, "IS,IS2"),
    ("172GD005", 30, "IS,IS2"),
    ("172FA007", 15, "CYP,CYP2"),
    ("172FB007", 15, "CYP,CYP2"),
    ("172FB008", 15, "CYP,CYP2"),
    ("172FB002", 50, "CYP,CYP2"),
    ("172FAA001", 60, "AI,AI2,DS,DS2"),
    ("172FAA002", 35, "AI,AI2,DS,DS2"),
    ("172FB004", 30, "AI,AI2,DS,DS2"),
    ("172FA008", 30, "AI,AI2,DS,DS2"),
    ("172FB012", 25, "AI,AI2,DS,DS2"),
    ("172GD004", 40, "SC,SC2"),
]

M_LABS = [
    # (code, capacity, department) — M-side labs (building 172)
    ("172GB001", 25, "AI,AI2,DS,DS2"),
    ("172GB005", 25, "AI,AI2,DS,DS2"),
]

print(
    f"Before: F={Room.objects.filter(section='F').count()}, M={Room.objects.filter(section='M').count()}, total={Room.objects.count()}"
)

if Room.objects.exists() and os.environ.get("SEED_ROOMS_OVERWRITE") != "1":
    raise SystemExit(
        "seed_rooms: the rooms table already has rooms, and this re-run would delete "
        "and reset them to its stale lists (see the docstring). Nothing was changed. "
        "Set SEED_ROOMS_OVERWRITE=1 only to rewrite a disposable database's rooms."
    )

# Wipe + seed F
Room.objects.filter(section="F").delete()
for code, wing, bld, rtype, cap, dept in F_ROOMS:
    Room.objects.create(
        room_code=code,
        wing=wing,
        building=bld,
        room_type=rtype,
        capacity=cap,
        department=dept,
        section="F",
    )

# Sync M: drop orphans, upsert canonical (lectures + labs)
pdf_m_codes = {c for c, _, _ in M_ROOMS} | {c for c, _, _ in M_LABS}
Room.objects.filter(section="M").exclude(room_code__in=pdf_m_codes).delete()
for code, cap, dept in M_ROOMS:
    Room.objects.update_or_create(
        room_code=code,
        section="M",
        defaults={
            "capacity": cap,
            "department": dept,
            "room_type": "lecture",
            "building": "",
            "wing": "",
        },
    )
for code, cap, dept in M_LABS:
    Room.objects.update_or_create(
        room_code=code,
        section="M",
        defaults={
            "capacity": cap,
            "department": dept,
            "room_type": "lab",
            "building": "",
            "wing": "",
        },
    )

print(
    f"After: F={Room.objects.filter(section='F').count()}, M={Room.objects.filter(section='M').count()}, total={Room.objects.count()}"
)
