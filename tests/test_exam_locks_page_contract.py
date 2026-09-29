"""The page reads only the fields the server puts in each lock issue.

``exam_locks_qa`` (and the registrations drift) build each issue as a dict
literal with a ``"kind"``; the page words each kind in ``LOCK_ISSUE`` from
``issue.<field>``. A renamed server field would otherwise read as ``undefined``
on the page (shown as 0 or blank) with every test green: step 1 renamed
``exam_capacity`` to ``room_capacity`` after the page was written.
"""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "core" / "services" / "exam_locks.py"
PAGE = ROOT / "static" / "js" / "page-exam-timetable.js"


def _server_fields() -> dict[str, set[str]]:
    """kind -> the keys of every issue dict literal of that kind."""
    fields: dict[str, set[str]] = {}
    for node in ast.walk(ast.parse(SERVER.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Dict):
            continue
        keys = [key.value for key in node.keys if isinstance(key, ast.Constant)]
        kind = next(
            (
                value.value
                for key, value in zip(node.keys, node.values, strict=True)
                if isinstance(key, ast.Constant)
                and key.value == "kind"
                and isinstance(value, ast.Constant)
            ),
            None,
        )
        if isinstance(kind, str):
            fields.setdefault(kind, set()).update(str(key) for key in keys)
    return fields


def _page_fields() -> dict[str, set[str]]:
    """kind -> the ``issue.<field>`` names its entry in LOCK_ISSUE reads."""
    text = PAGE.read_text(encoding="utf-8")
    start = text.index("const LOCK_ISSUE = {")
    block = text[start : text.index("\n};", start)]
    parts = re.split(r"^  (\w+): \{", block, flags=re.MULTILINE)
    return {
        kind: set(re.findall(r"\bissue\.(\w+)", body))
        for kind, body in zip(parts[1::2], parts[2::2], strict=True)
    }


def test_the_page_words_every_kind_the_server_reports():
    server, page = _server_fields(), _page_fields()
    assert set(page) == set(server), (set(page), set(server))


def test_every_field_the_page_reads_is_one_the_server_sends():
    server, page = _server_fields(), _page_fields()
    missing = {
        kind: sorted(read - server[kind]) for kind, read in page.items() if read - server[kind]
    }
    assert missing == {}


def test_over_capacity_reads_the_seats_and_the_rooms_exam_seats():
    assert {"seated", "room_capacity"} <= _page_fields()["room_over_capacity"]
