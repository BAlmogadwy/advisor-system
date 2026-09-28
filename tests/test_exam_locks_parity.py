"""Without locks, every request the page sends returns what master returned.

Locks reach every action, and two of them read something new even when there
are no locks: a Build now reads the saved run named by ``previous_run_id``
(the page sends it whenever a run is loaded) for its locks, and every loaded
action takes the loaded run's locks when the request sends none - which is
every request the current page sends. So this drives the page's own requests
through the real endpoints, with no ``exam_locks`` key anywhere: Build, Check,
Save, Optimise, "Fix with fewest moves" and a second Build from the saved
run, answered at once and as background jobs.

The digests were recorded by running this very module against origin/master
(ae056bd), before locks existed, under two PYTHONHASHSEED values, which agreed
(``EXAM_LOCKS_PARITY_RECORD=1`` prints them instead of comparing). Nothing here
imports the locks code, so the recording ran the same requests on master.
"""

import os
from copy import deepcopy

import pytest
from django.core.cache import cache
from django.urls import reverse

from core import models
from core.services import exam_input_fingerprint
from tests import exam_linked_parity_corpus as corpus

pytestmark = pytest.mark.django_db

DAYS = corpus.POPULATION_DAYS
PERIODS = corpus.POPULATION_PERIODS
SCOPE = {"programs": ["AI", "CS"], "sections": ["F", "M"]}
PINS = [{"course_code": "LX103", "day": "Mon", "period": PERIODS[1]}]

#: Answered at once. A background job returns each of them byte for byte (a
#: Check never runs as one); a second Build from the saved run is the first.
MASTER_NOW = {
    "build": "124e0bfc17e550883acf44b2984a7b1179fe26a6f695eeef7547b7e84125b22a",
    "check": "35100947114d92b04ad2a8396e5fcabf11cdae748f425ab56af3a4e99ab757f3",
    "save": "ce75487d1053cf17b6c7ea72846e98b1d35eedaecd4a421d110e00fbe0bc40b5",
    "optimise": "f7ed8fba2a376103ec503f202d763128239e719caae193a836b44c8cce01c4c6",
    "fix": "f72ada04f1afbf2c55c873b81f856e7e1db7f88fd445a6f94b2f2be6191a8bb5",
    "rebuild": "124e0bfc17e550883acf44b2984a7b1179fe26a6f695eeef7547b7e84125b22a",
}
MASTER = {
    "now": MASTER_NOW,
    "jobs": {key: value for key, value in MASTER_NOW.items() if key != "check"},
}


@pytest.fixture
def client_(client, django_user_model, monkeypatch):
    monkeypatch.setattr("core.authz._rate_buckets", {})
    # Master's room-allocation policy is version 2 as well; pinned so a bump
    # made later for another reason is not read as a lock changing a result.
    monkeypatch.setattr(exam_input_fingerprint, "ROOM_ALLOCATION_POLICY_VERSION", 2)
    cache.clear()
    client.force_login(django_user_model.objects.create_superuser(username="parity-admin"))
    return client


def _answer(client, payload, *, url="exam_timetable_build", jobs=False) -> dict:
    headers = {"HTTP_X_EXAM_JOBS": "1"} if jobs and url == "exam_timetable_build" else {}
    response = client.post(reverse(url), payload, content_type="application/json", **headers)
    if jobs and url == "exam_timetable_build":
        assert response.status_code == 202, response.content
        job = response.json()["job"]["id"]
        response = client.get(reverse("exam_timetable_job_result", args=[job]))
    assert response.status_code == 200, response.content
    return response.json()


def _header(**changes) -> dict:
    return {
        "label": "Parity",
        "days": DAYS,
        "periods": PERIODS,
        "max_per_day": 2,
        **SCOPE,
        "assign_rooms": True,
        "thin_conflict_threshold": 1,
        **changes,
    }


def _loaded(client, source, board, mode, *, jobs=False, **changes) -> dict:
    payload = _header(
        previous_run_id=source["run_id"],
        base_schedule=deepcopy(board),
        pinned=source["pinned"],
        **changes,
    )
    if mode is None:
        return _answer(client, payload, url="exam_timetable_draft_impact")
    return _answer(client, {**payload, "mode": mode}, jobs=jobs)


def _drag(result) -> list[dict]:
    """The first clash-free exam dragged onto a neighbour's slot (as run_population)."""
    placed = {entry["course_code"]: entry for entry in result["schedule"]}
    pinned = {pin["course_code"] for pin in PINS}
    edge = next(
        row
        for row in result["conflicts"]
        if not {row["course_a"], row["course_b"]} & pinned
        and placed[row["course_a"]]["day"] != "OVERFLOW"
        and placed[row["course_b"]]["day"] != "OVERFLOW"
    )
    anchor = placed[edge["course_a"]]
    return [
        {**entry, **{key: anchor[key] for key in ("day", "period", "slot_index")}}
        if entry["course_code"] == edge["course_b"]
        else entry
        for entry in result["schedule"]
    ]


def _digest(body: dict) -> str:
    body = corpus.comparable(body)
    body.pop("source_input_fingerprint", None)  # the saved run's own, compared as the build
    return corpus.digest(body)


def run_page_flow(client, *, jobs: bool) -> dict[str, str]:
    digests: dict[str, str] = {}
    build = _answer(client, _header(pinned=PINS), jobs=jobs)
    digests["build"] = _digest(build)
    check = _loaded(client, build, build["schedule"], None)
    if not jobs:
        digests["check"] = _digest(check)
    save = _loaded(
        client,
        build,
        build["schedule"],
        "save_loaded_changes",
        jobs=jobs,
        expected_input_fingerprint=check["input_fingerprint"],
    )
    digests["save"] = _digest(save)
    digests["optimise"] = _digest(
        _loaded(client, save, save["schedule"], "optimize_loaded", jobs=jobs)
    )
    digests["fix"] = _digest(_loaded(client, save, _drag(save), "minimum_change_repair", jobs=jobs))
    digests["rebuild"] = _digest(
        _answer(client, _header(pinned=PINS, previous_run_id=save["run_id"]), jobs=jobs)
    )
    return digests


@pytest.mark.parametrize("jobs", [False, True], ids=["now", "jobs"])
def test_the_pages_requests_without_locks_return_what_master_returned(client_, settings, jobs):
    if jobs:
        settings.EXAM_JOBS_ENABLED = True
        settings.EXAM_JOBS_RUN_INLINE = True
    corpus.create_population(models)
    digests = run_page_flow(client_, jobs=jobs)
    if os.environ.get("EXAM_LOCKS_PARITY_RECORD"):
        print("\nRECORDED", "jobs" if jobs else "now", digests)
        return
    assert digests == MASTER["jobs" if jobs else "now"]
