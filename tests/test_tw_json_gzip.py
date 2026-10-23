"""Stage 3, B5: the Timetable Workspace's JSON answers are gzipped (D-14).

``config.middleware.TwJsonGZipMiddleware`` is Django's ``GZipMiddleware``
behind a filter. It compresses an answer only when its path is under
``/ops/tw/``, its media type is ``application/json``, it is not a stream and
not a file, and its body is at least 1 KB. Everything else passes through
untouched, without ``Vary: Accept-Encoding``.

Two layers:

* **The filter, one predicate at a time.** Each case in ``FILTER_CASES`` is
  decided by exactly one predicate (the others would let it through), so
  removing any one predicate turns a case red. The plan's own examples could
  not do that alone: the workspace HTML page is outside ``/ops/tw/`` *and* not
  JSON, and the XLSX export is a stream *and* not JSON.
* **The real stack.** The board read, the workspace page, an HTML error page
  under ``/ops/tw/``, the XLSX export and a small JSON answer, through every
  middleware in ``settings.MIDDLEWARE``, under the defaults and under the
  three flags production runs with (render.yaml).

The cost: a CI budget for the largest answer the page reads (the Place
dialog's, about 1 MB), beside the probe's p50 on QA scenario 665
(``docs/stage3/B5.md``).
"""

from __future__ import annotations

import gzip
import io
import json
import random
import statistics
import time
from collections.abc import Callable

import pytest
from django.contrib.auth.models import Group, User
from django.http import (
    FileResponse,
    HttpResponse,
    JsonResponse,
    StreamingHttpResponse,
)
from django.http.response import HttpResponseBase
from django.test import Client, RequestFactory

from config.middleware import TwJsonGZipMiddleware
from core.services.rbac import ROLE_SUPER_ADMIN, ensure_role_groups
from tests.one_teacher_support import budget, place, scenario, section

# What Chrome sends.
GZIP = "gzip, deflate, br, zstd"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# The flags production runs with (render.yaml).
PRODUCTION_FLAGS = {
    "TIMETABLE_ONE_TEACHER_PER_SECTION": True,
    "TIMETABLE_INSTRUCTOR_DAY_OFF_POLICY": True,
    "TIMETABLE_SECTION_PROGRAMME_PREFERENCE": True,
}


def json_bytes(length: int) -> bytes:
    """A JSON document of exactly ``length`` bytes."""
    shell = b'{"pad": ""}'
    out = b'{"pad": "' + b"x" * (length - len(shell)) + b'"}'
    assert len(out) == length
    return out


def vary(response: HttpResponseBase) -> set[str]:
    return {part.strip().lower() for part in response.get("Vary", "").split(",") if part.strip()}


def through_middleware(
    path: str,
    response: HttpResponseBase,
    *,
    accept: str | None = GZIP,
    **environ: str,
) -> HttpResponseBase:
    """``response`` as the browser receives it after the middleware."""
    if accept is not None:
        environ["HTTP_ACCEPT_ENCODING"] = accept
    request = RequestFactory().get(path, **environ)
    return TwJsonGZipMiddleware(lambda _request: response)(request)


def streamed(response: HttpResponseBase) -> bytes:
    return b"".join(response.streaming_content)  # type: ignore[attr-defined]


# ── The filter, one predicate at a time ──────────────────────────────────

BIG = 5000

FilterCase = tuple[str, str, Callable[[], HttpResponseBase], bool, dict[str, str]]

FILTER_CASES: list[FilterCase] = [
    # Compressed: every predicate holds.
    (
        "workspace JSON",
        "/ops/tw/boards/7/",
        lambda: HttpResponse(json_bytes(BIG), content_type="application/json"),
        True,
        {},
    ),
    (
        "a JsonResponse",
        "/ops/tw/scenarios/7/quality/",
        lambda: JsonResponse({"rows": [{"course": "AI201", "seats": n} for n in range(200)]}),
        True,
        {},
    ),
    (
        "media type with a charset parameter",
        "/ops/tw/boards/7/",
        lambda: HttpResponse(json_bytes(BIG), content_type="application/json; charset=utf-8"),
        True,
        {},
    ),
    (
        "media type in another case",
        "/ops/tw/boards/7/",
        lambda: HttpResponse(json_bytes(BIG), content_type="Application/JSON"),
        True,
        {},
    ),
    (
        "exactly 1 KB",
        "/ops/tw/boards/7/",
        lambda: HttpResponse(json_bytes(1024), content_type="application/json"),
        True,
        {},
    ),
    (
        "under a script prefix, the API path still counts",
        "/ops/tw/boards/7/",
        lambda: HttpResponse(json_bytes(BIG), content_type="application/json"),
        True,
        {"SCRIPT_NAME": "/advisor"},
    ),
    # Untouched: exactly one predicate fails.
    (
        "one byte under 1 KB",
        "/ops/tw/boards/7/",
        lambda: HttpResponse(json_bytes(1023), content_type="application/json"),
        False,
        {},
    ),
    (
        "JSON outside /ops/tw/",
        "/ops/exam-timetable/list/",
        lambda: HttpResponse(json_bytes(BIG), content_type="application/json"),
        False,
        {},
    ),
    (
        "a path that only starts like /ops/tw",
        "/ops/tw-archive/boards/7/",
        lambda: HttpResponse(json_bytes(BIG), content_type="application/json"),
        False,
        {},
    ),
    (
        "HTML under /ops/tw/",
        "/ops/tw/boards/7/",
        lambda: HttpResponse(b"<!doctype html>" + b"<p>x</p>" * 600, content_type="text/html"),
        False,
        {},
    ),
    (
        "a workbook held in memory under /ops/tw/",
        "/ops/tw/scenarios/7/export.xlsx",
        lambda: HttpResponse(b"PK\x03\x04" + b"\x00" * BIG, content_type=XLSX),
        False,
        {},
    ),
    (
        "a media type that only starts like JSON",
        "/ops/tw/boards/7/",
        lambda: HttpResponse(json_bytes(BIG), content_type="application/json-seq"),
        False,
        {},
    ),
    (
        "JSON a response names as an attachment",
        "/ops/tw/boards/7/",
        lambda: _with_header(
            HttpResponse(json_bytes(BIG), content_type="application/json"),
            "Content-Disposition",
            'attachment; filename="board.json"',
        ),
        False,
        {},
    ),
    (
        "JSON a response names as an inline file",
        "/ops/tw/boards/7/",
        lambda: _with_header(
            HttpResponse(json_bytes(BIG), content_type="application/json"),
            "Content-Disposition",
            'inline; filename="board.json"',
        ),
        False,
        {},
    ),
    (
        "streamed JSON",
        "/ops/tw/boards/7/",
        lambda: StreamingHttpResponse(
            iter([json_bytes(BIG)[:2500], json_bytes(BIG)[2500:]]),
            content_type="application/json",
        ),
        False,
        {},
    ),
    (
        "a FileResponse of JSON",
        "/ops/tw/boards/7/",
        lambda: FileResponse(io.BytesIO(json_bytes(BIG)), content_type="application/json"),
        False,
        {},
    ),
    (
        "an answer that is already encoded",
        "/ops/tw/boards/7/",
        lambda: _with_header(
            HttpResponse(json_bytes(BIG), content_type="application/json"),
            "Content-Encoding",
            "br",
        ),
        False,
        {},
    ),
]


def _with_header(response: HttpResponseBase, name: str, value: str) -> HttpResponseBase:
    response[name] = value
    return response


def _identity_bytes(response: HttpResponseBase) -> bytes:
    if response.streaming:
        return streamed(response)
    return response.content  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "name,path,make,compressed,environ", FILTER_CASES, ids=[c[0] for c in FILTER_CASES]
)
def test_the_filter_decides_each_answer_by_one_rule(name, path, make, compressed, environ):
    original = _identity_bytes(make())

    out = through_middleware(path, make(), **environ)

    if compressed:
        assert out["Content-Encoding"] == "gzip", name
        assert "accept-encoding" in vary(out), name
        assert gzip.decompress(out.content) == original, name
        assert int(out["Content-Length"]) == len(out.content), name
        assert len(out.content) < len(original), name
    else:
        assert out.get("Content-Encoding", "") in ("", "br"), name
        assert "accept-encoding" not in vary(out), f"{name}: its bytes never vary"
        assert _identity_bytes(out) == original, name


def test_the_filter_cases_cover_every_predicate():
    """Each rule has a case that it alone refuses (the table's own guard)."""
    refused = {name for name, _path, _make, compressed, _env in FILTER_CASES if not compressed}
    assert {
        "one byte under 1 KB",  # size
        "JSON outside /ops/tw/",  # path
        "a path that only starts like /ops/tw",  # path, exactly
        "HTML under /ops/tw/",  # media type
        "a workbook held in memory under /ops/tw/",  # media type (a file type)
        "JSON a response names as an attachment",  # a file
        "streamed JSON",  # a stream
    } <= refused


def test_an_eligible_answer_varies_on_accept_encoding_even_when_not_compressed():
    """A cache must keep the gzip and the identity copies of one URL apart."""
    for accept in (None, "identity", "br"):
        out = through_middleware(
            "/ops/tw/boards/7/",
            HttpResponse(json_bytes(BIG), content_type="application/json"),
            accept=accept,
        )

        assert "Content-Encoding" not in out, accept
        assert "accept-encoding" in vary(out), accept
        assert out.content == json_bytes(BIG), accept


def test_the_breach_mitigation_is_kept():
    """Django pads every gzip header with a random-length file name, so the
    compressed length does not track a secret in the body. The subclass keeps
    it: the FNAME flag is set, and the length of one body varies."""
    lengths = set()
    for _ in range(12):
        out = through_middleware(
            "/ops/tw/boards/7/",
            HttpResponse(json_bytes(BIG), content_type="application/json"),
        )
        assert out.content[3] & gzip.FNAME, "no random file name in the gzip header"
        assert gzip.decompress(out.content) == json_bytes(BIG)
        lengths.add(len(out.content))

    assert len(lengths) > 1, "every compression of one body had the same length"


# ── The real stack ───────────────────────────────────────────────────────


@pytest.fixture(params=["defaults", "production flags"])
def flags(request, settings):
    if request.param == "production flags":
        for flag, value in PRODUCTION_FLAGS.items():
            setattr(settings, flag, value)
    return request.param


@pytest.fixture
def http(db):
    ensure_role_groups()
    user = User.objects.create_user(username="gzip-admin")
    user.groups.add(Group.objects.get(name=ROLE_SUPER_ADMIN))
    client = Client()
    client.force_login(user)
    return client


DAYS = ("SUN", "MON", "TUE", "WED", "THU")
TIMES = (("08:00", "09:15"), ("09:30", "10:45"), ("11:00", "12:15"), ("13:00", "14:15"))


@pytest.fixture
def board(db):
    """A board with twenty placed sections, ten courses of two sections."""
    sc, board = scenario("gzip", programs=("AI",))
    for n in range(10):
        key = f"AI{201 + n}"
        budget(sc, key, 2)
        for s in range(2):
            ts = section(sc, key, f"S{s + 1}")
            day = DAYS[(n + s) % len(DAYS)]
            start, end = TIMES[(n * 2 + s) % len(TIMES)]
            place(board, ts, [(day, start, end)], room=f"R{n % 4 + 1}")
    return board


@pytest.mark.django_db
def test_a_board_read_is_gzipped_and_decompresses_to_the_same_answer(flags, http, board):
    identity = http.get(f"/ops/tw/boards/{board.id}/")
    response = http.get(f"/ops/tw/boards/{board.id}/", HTTP_ACCEPT_ENCODING=GZIP)

    assert identity.status_code == response.status_code == 200
    assert "Content-Encoding" not in identity
    assert "accept-encoding" in vary(identity), "the identity copy varies too"
    assert response["Content-Encoding"] == "gzip"
    assert "accept-encoding" in vary(response)
    # LocaleMiddleware's and the session's Vary entries survive.
    assert {"accept-language", "cookie"} <= vary(response)
    assert int(response["Content-Length"]) == len(response.content)
    body = gzip.decompress(response.content)
    assert json.loads(body) == identity.json()
    assert len(json.loads(body)["placements"]) == 20
    # The [CI] twin of the probe's 63,258 -> about 2.6 KB on QA 665: a board
    # answer shrinks severalfold.
    assert len(response.content) * 5 < len(identity.content), (
        len(response.content),
        len(identity.content),
    )


@pytest.mark.django_db
def test_the_workspace_html_page_is_never_compressed(flags, http):
    response = http.get("/timetable-workspace/", HTTP_ACCEPT_ENCODING=GZIP)

    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/html")
    assert len(response.content) >= 1024, "big enough that only the filter keeps it plain"
    assert "Content-Encoding" not in response
    assert "accept-encoding" not in vary(response)
    assert b"<html" in response.content


@pytest.mark.django_db
def test_an_html_error_page_under_ops_tw_is_never_compressed(flags, http):
    """Under ``/ops/tw/`` and over 1 KB: only the media type keeps it plain."""
    response = http.get("/ops/tw/no-such-endpoint/", HTTP_ACCEPT_ENCODING=GZIP)

    assert response.status_code == 404
    assert response["Content-Type"].startswith("text/html")
    assert len(response.content) >= 1024
    assert "Content-Encoding" not in response
    assert "accept-encoding" not in vary(response)


@pytest.mark.django_db
def test_the_xlsx_export_is_never_compressed(flags, http, board):
    response = http.get(
        f"/ops/tw/scenarios/{board.scenario_id}/export.xlsx", HTTP_ACCEPT_ENCODING=GZIP
    )

    assert response.status_code == 200
    assert response["Content-Type"] == XLSX
    assert response.streaming
    assert "Content-Encoding" not in response
    assert "accept-encoding" not in vary(response)
    assert b"".join(response.streaming_content).startswith(b"PK\x03\x04")


@pytest.mark.django_db
def test_a_small_json_answer_is_left_alone(flags, http):
    response = http.get("/ops/tw/boards/987654/", HTTP_ACCEPT_ENCODING=GZIP)

    assert response.status_code == 404
    assert response["Content-Type"] == "application/json"
    assert len(response.content) < 1024
    assert "Content-Encoding" not in response
    assert "accept-encoding" not in vary(response)
    assert response.json()["error"]["code"] == "NOT_FOUND"


# ── The cost ─────────────────────────────────────────────────────────────


def _place_read_sized_answer() -> bytes:
    """About 1 MB shaped like the Place dialog's read for a three-meeting course
    (``limit=1000&verdict=1``): 190 candidates, each with its meetings, evidence,
    ranking reasons and verdict, about 5.4 KB apiece. On QA 665 that read is
    1,024,949 bytes and gzips to about 30 KB; values here vary, drawn from small
    vocabularies as the real sentences are."""
    rng = random.Random(14)
    days = ("SUN", "MON", "TUE", "WED", "THU")
    times = (("08:00", "09:15"), ("09:30", "10:45"), ("11:00", "12:15"), ("13:00", "14:15"))
    courses = [
        f"{dept}{n}"
        for dept in ("AI", "CS", "DS", "IS", "STAT", "MATH")
        for n in (101, 103, 112, 124, 201, 236)
    ]
    tones = ("ok", "warning", "critical", "avoid")

    def moment() -> str:
        start, end = rng.choice(times)
        return f"{rng.choice(days)} {start}-{end}"

    def evidence() -> dict[str, object]:
        count = rng.randrange(1, 160)
        course = rng.choice(courses)
        return {
            "kind": rng.choice(("students", "same_course", "room", "teacher")),
            "tone": rng.choice(tones),
            "title": f"{count} students also need {course}-S{rng.randrange(1, 7)}",
            "detail": f"Same-board overlap at {moment()}",
            "student_count": count,
        }

    candidates = []
    for n in range(190):
        meetings = []
        for kind in ("lect", "lab", "lect"):
            start, end = rng.choice(times)
            meetings.append(
                {
                    "kind": kind,
                    "day": rng.choice(days),
                    "start": start,
                    "end": end,
                    "slot_idx": rng.randrange(8),
                }
            )
        candidates.append(
            {
                "candidate_id": f"pattern-{n}",
                "kind": "lect",
                "is_pattern": True,
                "required_meetings": 3,
                "meetings": meetings,
                "score": rng.randrange(10_000),
                "tone": rng.choice(tones),
                "critical_count": rng.randrange(6),
                "student_affected_count": rng.randrange(200),
                "timetable_quality": {
                    "policy": "timetable-quality-v1",
                    "penalty": rng.randrange(2000),
                    "components": {
                        "meeting_quality": rng.randrange(1500),
                        "slot_consistency": rng.randrange(100),
                        "day_spacing": rng.randrange(100),
                    },
                },
                "evidence": [evidence() for _ in range(rng.randrange(4, 15))],
                "ranking_reasons": [
                    f"{rng.randrange(1, 90)} students lose {rng.randrange(30, 240)} minutes on {rng.choice(days)}"
                    for _ in range(rng.randrange(10, 26))
                ],
                "verdict": {
                    "tone": rng.choice(tones),
                    "headline": f"Clashes with {rng.choice(courses)} for {rng.randrange(1, 90)} students",
                    "items": [evidence() for _ in range(rng.randrange(8, 22))],
                },
            }
        )
    return json.dumps({"ok": True, "candidates": candidates}).encode()


def test_compressing_the_largest_answer_costs_milliseconds():
    """The [CI] twin of the probe (p50 over 20 reads of 665 rises by at most
    10 ms). On the probe machine this body takes about 7 ms (the real 1 MB
    read, more repetitive, 4.4 ms); the budget, 50 ms, leaves room for a slow
    CI runner. It is a ceiling against a pathological change, not the claim."""
    body = _place_read_sized_answer()
    assert 800_000 < len(body) < 1_300_000, len(body)
    timings = []
    for _ in range(7):
        response = HttpResponse(body, content_type="application/json")
        started = time.perf_counter()
        out = through_middleware("/ops/tw/boards/7/planned-slot-candidates/", response)
        timings.append(time.perf_counter() - started)
        assert out["Content-Encoding"] == "gzip"

    assert statistics.median(timings) < 0.050, timings
    assert gzip.decompress(out.content) == body
