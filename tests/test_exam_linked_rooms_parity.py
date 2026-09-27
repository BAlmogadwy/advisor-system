"""With no links, rooming and everything read from it is exactly master's.

Linked courses may now share a room (room-allocation policy 2), which reaches
the allocator, the invigilator counts, the room QA, the building footprint,
the master Excel, the Department files, the student-data export and the
Student lists. Without links each one must return, byte for byte, what
origin/master (84704ad) returned before sharing existed.

The boards live in ``exam_linked_rooms_corpus``. The digests below were
recorded by running those very boards through master's own functions, under
three PYTHONHASHSEED values, which agreed. Re-recording them from the branch
would defeat the test.

The policy bump itself reaches a result only through its input fingerprint,
so the database test pins the fingerprint's copy of the version to master's.
"""

import functools
import io
import zipfile
from types import SimpleNamespace

import pytest
from openpyxl import Workbook
from openpyxl import xml as openpyxl_xml

from core import models
from core.services import (
    exam_input_fingerprint,
    exam_room_allocation,
    exam_run_schema,
    exam_timetable,
)
from core.services.exam_department_export import (
    department_export_options,
    export_department_workbooks,
)
from core.services.exam_roster_view import (
    build_roster_view,
    navigator_facts,
    parse_view_scope,
    scope_payload,
    select_scope,
)
from core.services.exam_rosters import build_roster_model
from core.services.exam_student_export import parse_export_options, prepare_export, render_export
from core.services.linked_exams import NO_LINKS
from tests import exam_linked_parity_corpus as base
from tests import exam_linked_rooms_corpus as corpus

GOLDEN = {
    "periods": """
        e762f044b31a38f3 f3e9620442018836 b572295702f4e732 9523c1262a2a4ad0
        c19243fe74545be8 0891a8639650042c f4ceddc4870b5098 28c713af264464cf
        2968a3e7b1ffdb54 84fd890103b0deef 97a8e5caa0b1f49e d02caefa3ceee991
        48091018915eb552 d46238466d07d1d0 65a0050410b16629 7731c6d735650970
        ca73a1798c29ca18 39c6c8ec3aad9c03 f2fbcec7b7b4babd 28330d3f07f77211
        b5b1c06329e7a70b a698fb1b8cf1552a b871be2cf6d5064e 06df3d64434aa1c7
        80d9755547e9821a de2885b06dfa68db 249bd6c3d15aa5ac 2af16cc9856bbdaf
        c7615beca6cf611d 15fc29f3e6cb6ff9 943d43d43232dfc5 076b74687db0a1a4
        684fd959966dfae5 58d756ce02dec7ab ae566a00b4d759dd 832c42a06825c9c0
        fcad63e5a2d152d8 a31889f96d108d1e 792fcbbb6a254c43 79ac75f10cabff6b
        3a2ed34532b31049 1f2da70284252edf d72dc7141c73dfb4 6a7c087cbea3edf2
        d2054e63ce7e5c28 18879fd2ce1406ff aa57b1ed6a4855e8 bedd54c09828499c
        0c57edfe7059ebf7 1bb1cb1a5b57825d 3953b0824b32e075 4334af9bd4993e12
        ff68c155a21951b8 55f31deaa80c93f7 1f352e708494f20f 71b54410c405d838
        e2da5592df448978 06e7c3ce99c9bbf6 9a6f7ccba2541e13 bc31aa5b64fc6364
    """.split(),
    "room_reports": """
        016bb2442d9af9b9 1ad420ca7aeca40c 749912c3f5c1027a 61c1f6565fa0093e
        64f0b4cef6359064 09b70431a256155c 2a3872a36818106d a05b3ebb34ebf84d
        1d16a2eaa43491f8 8b1c6f1abc7b5575 7a8b3e3190df87f3 7307f43bf064d190
        1c813081b3d9de5e bd4c8bf3e8b0699c 12cc2a8f9d858114 15c6b68d9486fa76
        9bdb52a32db97d17 013de67007e33218 bc04dff68c5bf830 dc31ca7da465dbb7
        1d4eb23c079d05ac d7897f001a27f781 e01f4a417d025d8f a6c07f118a1e3847
        5ffd26411fd02d97 8490a3c3fbadcd9a d1070a9f7a4db2ce cb5679df2152e425
        e9eaa73612eb4e56 72463e87749dcc8d 101935e3d7e59a3b e5682e2125a7bcd6
        e97eb839d391ba0a 1321961e49fbe38e 1b2c39204dc5c8a2 9cfb93569b7d3946
        8a9182cb322ee8ed b20f201ce294be37 3d8190b162a4a979 fc2ae330005ee971
        9e58ce89af22fffa 3072ad3e6f2828f7 fd36162a370e6280 83675394fef41b12
        85443a05338004ec bbeb43535aadf68d 5308e6756a774d5e 3f9a02493840fcc0
        409f060ad9cbc12b 4d6f45328ba407b1 64cc0ecc3af07a03 71e57b194fa4d71f
        bdc6b82c907c0763 39b7f84ca2c67c2f 2bfe511b93051dd3 566039a9e062ab6b
        e01eb90db56c3b7d 5def182864dba8f7 d75652808d84e269 c6833007a976df94
        578da47530df351a 7e858c324f258954 be1350e33601a79a 1714fbe23daf356a
    """.split(),
}
#: openpyxl serialises a workbook's XML through lxml when it can and through
#: its own writer otherwise, and the two write different bytes for the same
#: workbook. CI has no lxml, a developer machine may: master was recorded both
#: ways, and each run is held to the bytes master wrote in the same setting.
#: Either way a part is read with its line endings as XML reads them (see
#: ``workbook_parts``), so Windows and Linux are held to the same digests.
MASTER_EXPORTS_WITHOUT_LXML = {
    "build": "d23570e7c5a3f04f570a8a091bc873fb632c1053b701f35b1fc338df4fa53220",
    "department_ar": "d3cf4a76a11532b00a53b31e48506f5f0b7d1528c68b9b281c84e18581d4a418",
    "department_en": "ad525f75c33a7441f880a1dabca46897e1eb3cbfba35f62c75369d0d5856c22c",
    "master_excel": "ab807040449964f53e7a5ad87e8050cdd27628e8104dd7ddc9829460c624b223",
    "rosters": "bf51b1612515ee352b647ac851d05dd5b57f5f2e5cbcf04012e8a453e0b83535",
    "student_ar": "4f7cfaff55e9fa83ced0810bcbd1dccc0738ae41b8c4fbbd8bfc574464efbe51",
    "student_en": "e2948a19a7bd094d2c96f3480e6cd8e1c978869230b503480f69bac6e2858e01",
}
MASTER_EXPORTS_WITH_LXML = {
    "build": "d23570e7c5a3f04f570a8a091bc873fb632c1053b701f35b1fc338df4fa53220",
    "department_ar": "70ed90614f2f670df6b2d7be8f94ff80a9f6b06ab3328531c867837a2f7c511a",
    "department_en": "8ab73c3bf018278d2bbd63de28c7e83157045fa82a2f267258e3c1884c697436",
    "master_excel": "202a99bc5941ac4dab3f1a3405c823cff6ee0a0326546b3fdcf08d7e18b9b0ef",
    "rosters": "bf51b1612515ee352b647ac851d05dd5b57f5f2e5cbcf04012e8a453e0b83535",
    "student_ar": "0a4b23894d8d79e7e250263e650e83ce164ced76448e6a198af02f9e52ccebff",
    "student_en": "11bc102a58492deb86555e44608caeef13c4e4585bd50b458e8a7169bdb76a8b",
}
MASTER_EXPORTS = MASTER_EXPORTS_WITH_LXML if openpyxl_xml.LXML else MASTER_EXPORTS_WITHOUT_LXML

#: See the module docstring: master's allocation policy, for the fingerprint.
MASTER_ROOM_POLICY = 1


@pytest.fixture(autouse=True)
def _fresh_room_cache():
    """A period another test allocated must not answer for these boards."""
    with exam_room_allocation._CACHE_LOCK:
        exam_room_allocation._CACHE.clear()
    yield
    with exam_room_allocation._CACHE_LOCK:
        exam_room_allocation._CACHE.clear()


def _assert_master(kind: str, digests: list[str]) -> None:
    expected = GOLDEN[kind]
    assert len(digests) == len(expected), f"{kind}: the corpus itself changed"
    changed = [
        number
        for number, (digest, recorded) in enumerate(zip(digests, expected, strict=True))
        if digest[:16] != recorded
    ]
    assert not changed, f"{kind}: boards {changed} no longer return what master returned"


def test_one_period_is_allocated_as_master_allocated_it():
    """Tight inventories: consolidation, both searches, split and unseated sections."""
    _assert_master(
        "periods",
        [
            corpus.run_period(
                board,
                exam_room_allocation.allocate_period,
                exam_room_allocation.RoomAllocationContext,
            )
            for board in corpus.period_boards()
        ],
    )


@pytest.mark.parametrize("explicit", [False, True], ids=["default", "no-links"])
def test_rooms_invigilators_room_qa_and_footprint_match_master_without_links(explicit):
    """Rooms, the invigilator pass, room QA (seats, staff, double bookings) and
    the building footprint, on boards with rooms to spare and on tight ones."""
    extra = {"links": NO_LINKS} if explicit else {}
    digests = []
    for board, rooms in corpus.room_boards():
        _conflicts, adj, _thin = base.graph(
            board, exam_timetable.build_conflict_graph, exam_timetable.apply_thin_conflict_policy
        )
        digests.append(
            corpus.run_room_reports(
                board,
                rooms,
                functools.partial(exam_timetable.assign_rooms_to_schedule, **extra),
                functools.partial(exam_timetable._rebalance_invigilators_pass, **extra),
                exam_room_allocation.RoomAllocationContext.for_periods,
                functools.partial(exam_timetable._build_room_qa, **extra),
                exam_run_schema.derive_building_footprint,
                adj,
            )
        )
    _assert_master("room_reports", digests)


def _api():
    return SimpleNamespace(
        build_exam_timetable=exam_timetable.build_exam_timetable,
        export_exam_timetable_xlsx=exam_timetable.export_exam_timetable_xlsx,
        load_normalised_run=exam_run_schema.load_normalised_run,
        department_export_options=department_export_options,
        export_department_workbooks=export_department_workbooks,
        build_roster_model=build_roster_model,
        parse_export_options=parse_export_options,
        prepare_export=prepare_export,
        render_export=render_export,
        build_roster_view=build_roster_view,
        parse_view_scope=parse_view_scope,
        scope_payload=scope_payload,
        select_scope=select_scope,
        navigator_facts=navigator_facts,
    )


@pytest.mark.django_db
@pytest.mark.parametrize("extra", [None, {"linked_exams": []}], ids=["default", "no-links"])
def test_every_export_matches_master_without_links(extra, monkeypatch):
    """A saved build with rooms and the invigilator pass: its result, the master
    Excel, the Department files and the student-data export in English and
    Arabic, and the Student lists of every room."""
    monkeypatch.setattr(
        exam_input_fingerprint, "ROOM_ALLOCATION_POLICY_VERSION", MASTER_ROOM_POLICY
    )
    corpus.create_rooms_population(models)
    api = _api()
    result, run = corpus.build_population_run(api, models, extra)
    digests = corpus.export_digests(run, api)
    digests["build"] = base.digest(base.comparable(result))
    assert digests == MASTER_EXPORTS


def _rezip(parts: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in parts.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def test_windows_and_linux_bytes_of_one_workbook_have_one_digest():
    """Without lxml a cell's newline reaches the file as CRLF on Windows and
    LF on Linux (CI): one workbook to Excel, so one digest here. Everything
    else a part holds - its text and where its newlines fall - still counts."""
    workbook = Workbook()
    workbook.active["A1"] = "MATH106 4cr\nCALCULUS"
    saved = io.BytesIO()
    workbook.save(saved)
    with zipfile.ZipFile(io.BytesIO(saved.getvalue())) as archive:
        # Whichever platform saved it, start from the bytes Linux writes.
        linux = {name: archive.read(name).replace(b"\r\n", b"\n") for name in archive.namelist()}
    sheet = "xl/worksheets/sheet1.xml"
    assert b"4cr\nCALCULUS" in linux[sheet]
    windows = {**linux, sheet: linux[sheet].replace(b"\n", b"\r\n")}

    def digest(parts: dict[str, bytes]) -> str:
        return base.digest(corpus.workbook_parts(_rezip(parts)))

    assert digest(windows) == digest(linux)
    for changed in (
        linux[sheet].replace(b"CALCULUS", b"CALCULUX"),
        linux[sheet].replace(b"4cr\nCALCULUS", b"4crCALCULUS"),
        linux[sheet].replace(b"4cr\nCALCULUS", b"4cr\n\nCALCULUS"),
        linux[sheet].replace(b"4cr\nCALCULUS", b"4cr \nCALCULUS"),
    ):
        assert digest({**linux, sheet: changed}) != digest(linux)
    # Only a CRLF pair is a line ending a platform wrote; a lone CR is kept.
    lone_cr = linux[sheet].replace(b"4cr\nCALCULUS", b"4cr\rCALCULUS")
    joined = linux[sheet].replace(b"4cr\nCALCULUS", b"4crCALCULUS")
    assert digest({**linux, sheet: lone_cr}) != digest({**linux, sheet: joined})
