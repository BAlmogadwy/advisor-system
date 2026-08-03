"""Real concurrency, on a database that can express it.

Everything else in the planner suite forces interleavings inside a single
transaction, because SQLite cannot do otherwise: `select_for_update` is silently a
no-op there — Django nests the "are we in a transaction" check inside a
`has_select_for_update` feature flag, so the request is discarded without error.
Those tests prove the conditional-UPDATE guards work. They cannot prove the LOCK
works, and a guarantee that only holds in production is a guarantee nobody has
seen.

These use two real threads on two real connections, and skip unless the backend
supports row locking. Production is PostgreSQL, so this is the engine that matters;
run them with:

    DATABASE_URL=postgres://…  pytest tests/test_planner_draft_concurrency.py

`transaction=True` is not decoration. The default pytest-django fixture wraps each
test in one transaction and rolls it back, which means a second thread on a second
connection cannot see anything the first one wrote — the test would deadlock or
silently prove nothing.

**What these tests pin, exactly.** Each protected operation has TWO guards, and on
PostgreSQL either one alone is sufficient — so removing just one leaves these green
and that is not a gap in the tests, it is what defence in depth means:

* generation is guarded by the row lock AND by the conditional claim. Without the
  lock, the claim's own `UPDATE` still takes a row lock and re-evaluates its filter
  after the winner commits, so the loser still finds the version taken.
* editing is guarded by the re-read under lock AND by the `version=` guard on the
  write.

Removing BOTH guards of a pair does fail these tests, which was verified by
mutation rather than assumed. On SQLite only the conditional-UPDATE half is
available, and `tests/test_planner_draft_lifecycle.py` pins that half on its own.
"""

from __future__ import annotations

import os
import threading

import pytest
from django.db import connection, connections

from core.models import Course, PlannerDraft, ProgrammeRequirement, Student, TermSection
from core.services import planner_drafts as svc

pytestmark = [pytest.mark.django_db(transaction=True)]

OWNER = 910001

#: Set by the PostgreSQL CI job. Without it these tests skip, which is right for
#: everyday SQLite development; with it they must FAIL rather than skip.
REQUIRE_ENV = "REQUIRE_POSTGRES_TESTS"


def require_postgresql() -> None:
    """Skip on SQLite; refuse to be skipped where PostgreSQL was promised.

    Called INSIDE each test rather than as a module-level `skipif`, because a
    module-level marker is evaluated at import — before any fixture has opened a
    connection, and long before a misconfigured `DATABASE_URL` would show itself.

    The environment variable is the whole point. A green job containing five skips
    looks exactly like a green job containing five passes, so a malformed
    connection string, a settings override or a service that never became healthy
    would silently retire the only tests that exercise the row lock. Concurrency
    correctness is part of this feature's contract; it is not allowed to opt out
    quietly.
    """
    if connection.vendor == "postgresql":
        return

    if os.environ.get(REQUIRE_ENV) == "1":
        pytest.fail(
            f"PostgreSQL planner tests were required ({REQUIRE_ENV}=1), "
            f"but Django is using {connection.vendor!r}. Check DATABASE_URL and "
            "that the database service is reachable."
        )

    pytest.skip(
        "Requires PostgreSQL transaction semantics — `select_for_update` is a "
        "silent no-op here. Run with DATABASE_URL=postgres://…"
    )


@pytest.fixture(autouse=True)
def _postgresql_only():
    """Autouse, so a test added later cannot forget it and quietly run on SQLite."""
    require_postgresql()


@pytest.fixture
def world():
    Student.objects.create(student_id=OWNER, name="S", program="AI", section="M", status="active")
    made = {}
    for code, name in (("CS113", "PROGRAMMING II"), ("AI221", "AI FUNDAMENTALS")):
        Course.objects.create(course_code=code, description=name)
        ProgrammeRequirement.objects.create(
            program="AI", course_code=code, course_name=name, credit_hours=3, type="required"
        )
        made[code] = TermSection.objects.create(
            course_code=code, course_key=code, course_name=name, section="M1"
        )
    return made


def _run_together(*fns):
    """Start every callable at the SAME MOMENT and collect what each returned or raised.

    The barrier is the whole point. Starting the threads and hoping is not a
    concurrency test: opening a PostgreSQL connection takes longer than any
    plausible stubbed solver, so the second thread reliably arrived after the first
    had committed and every guard tested clean with the guards removed. Each thread
    therefore connects first, then waits until all of them are ready, and only then
    does the work.

    Each closes its own connection afterwards: a thread that leaves one open holds
    locks the main thread then waits on, and the test hangs instead of failing.
    """
    results: list = [None] * len(fns)
    ready = threading.Barrier(len(fns), timeout=30)

    def wrap(i, fn):
        def inner():
            try:
                # Force the connection open BEFORE the barrier, so the barrier
                # releases into the work rather than into connection setup.
                with connections["default"].cursor() as cursor:
                    cursor.execute("SELECT 1")
                ready.wait()
                results[i] = ("ok", fn())
            except Exception as exc:  # noqa: BLE001 — the exception IS the result here
                results[i] = ("raised", exc)
            finally:
                connections.close_all()

        return inner

    threads = [threading.Thread(target=wrap(i, fn)) for i, fn in enumerate(fns)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not any(t.is_alive() for t in threads), "a thread never finished — likely a deadlock"
    return results


def _stub(monkeypatch, tag):
    """A solver that is slow enough to overlap, and says which run produced it."""
    import time

    def fake(request):
        time.sleep(1.0)  # wide enough that the second request is inside the window
        return {
            "alternatives": [
                {
                    "key": f"{tag}-{'-'.join(sorted(request.must_include))}",
                    "courses": [
                        {"course_code": c, "section": "M1", "credits": 3}
                        for c in request.must_include
                    ],
                    "meetings": [],
                    "credit_hours": 3 * len(request.must_include),
                }
            ],
            "unplaced": [],
            "generated": 1,
        }

    monkeypatch.setattr(svc, "build_student_options", fake)


def test_two_simultaneous_edits_do_not_lose_one(world):
    """Both land, in some order, and the version counts both.

    The failure this rules out is the lost update: two writers computing `version+1`
    from the same read, both writing 2, and the row ending up with one writer's
    courses under the other writer's version number — which is what makes a
    correctly-issued confirmation authorise content the student never saw.
    """
    draft = svc.create_draft(student_id=OWNER, course_codes=["CS113"])

    results = _run_together(
        lambda: svc.edit_draft(draft, course_codes=["CS113", "AI221"]),
        lambda: svc.edit_draft(PlannerDraft.objects.get(pk=draft.pk), keep_current_sections=False),
    )
    outcomes = [kind for kind, _ in results]
    conflicts = [v for kind, v in results if kind == "raised"]
    assert all(isinstance(c, svc.DraftConflict) for c in conflicts), (
        f"an unexpected exception escaped: {conflicts}"
    )

    draft.refresh_from_db()
    if outcomes == ["ok", "ok"]:
        # Serialised by the lock: the second re-read after the first committed.
        assert draft.version == 3, "one edit was lost"
        assert draft.course_codes == ["CS113", "AI221"]
        assert draft.keep_current_sections is False
    else:
        # One was refused rather than allowed to overwrite. Also correct.
        assert draft.version == 2, draft.version


def test_two_simultaneous_generations_run_the_solver_once(world, monkeypatch):
    """The claim the whole design rests on, tested where the lock is real."""
    draft = svc.create_draft(student_id=OWNER, course_codes=["CS113"])
    runs: list[str] = []
    import time

    def counting_solver(request):
        runs.append("run")
        time.sleep(1.0)
        return {
            "alternatives": [
                {
                    "key": f"opt-{len(runs)}",
                    "courses": [{"course_code": "CS113", "section": "M1", "credits": 3}],
                    "meetings": [],
                    "credit_hours": 3,
                }
            ],
            "unplaced": [],
            "generated": 1,
        }

    monkeypatch.setattr(svc, "build_student_options", counting_solver)

    results = _run_together(
        lambda: svc.generate(draft),
        lambda: svc.generate(PlannerDraft.objects.get(pk=draft.pk)),
    )
    assert all(kind == "ok" for kind, _ in results), results
    assert len(runs) == 1, f"the solver ran {len(runs)} times for one version"

    keys = [[a["key"] for a in value.alternatives] for _, value in results]
    assert keys[0] == keys[1], "the two tabs were shown different timetables"
    draft.refresh_from_db()
    assert [a["key"] for a in draft.alternatives] == keys[0]


def test_a_confirmation_is_spent_by_only_one_of_two_simultaneous_rebuilds(world, monkeypatch):
    """One token, two requests, one rebuild.

    The token is what authorises discarding the student's current sections. Two
    requests arriving together must not both be authorised by it — and the second
    must get the first's result rather than a refusal, or a reload after a rebuild
    would demand permission the server has already consumed.
    """
    draft = svc.create_draft(student_id=OWNER, course_codes=["CS113"])
    svc.edit_draft(draft, keep_current_sections=False)
    token = svc.issue_rebuild_token(draft)
    draft.refresh_from_db()
    _stub(monkeypatch, "rebuild")

    results = _run_together(
        lambda: svc.generate(draft, confirmation=token),
        lambda: svc.generate(PlannerDraft.objects.get(pk=draft.pk), confirmation=token),
    )
    assert all(kind == "ok" for kind, _ in results), results

    draft.refresh_from_db()
    assert draft.rebuild_token_hash == "", "the token survived a rebuild"
    assert draft.has_current_generation
    keys = [[a["key"] for a in value.alternatives] for _, value in results]
    assert keys[0] == keys[1]


def test_two_simultaneous_selections_leave_one_coherent_preference(world, monkeypatch):
    """Whichever wins, the stored key must be one that was actually offered."""
    draft = svc.create_draft(student_id=OWNER, course_codes=["CS113"])
    _stub(monkeypatch, "sel")
    svc.generate(draft)
    draft.refresh_from_db()
    key = draft.alternatives[0]["key"]

    results = _run_together(
        lambda: svc.select_alternative(draft, key),
        lambda: svc.select_alternative(PlannerDraft.objects.get(pk=draft.pk), key),
    )
    for kind, value in results:
        assert kind == "ok" or isinstance(value, svc.DraftConflict), value

    draft.refresh_from_db()
    assert draft.selected_alternative == key
    assert draft.selected_alternative in {a["key"] for a in draft.alternatives}


def test_the_migration_constraints_exist_on_this_backend(world):
    """The schema the tests ran against is the one the migrations describe.

    SQLite recreates tables wholesale for many operations, so a constraint present
    in the model but absent from a migration can look fine there. This asserts the
    indexes the query patterns actually need are on the real table.
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT indexname FROM pg_indexes WHERE tablename = %s", ["planner_drafts"])
        indexes = {row[0] for row in cursor.fetchall()}
    assert "idx_draft_student" in indexes, indexes
    assert "idx_draft_expiry" in indexes, indexes

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT column_name, is_nullable FROM information_schema.columns "
            "WHERE table_name = %s AND column_name IN "
            "('expires_at','version','generated_version','rebuild_token_hash')",
            ["planner_drafts"],
        )
        columns = dict(cursor.fetchall())
    assert columns["expires_at"] == "NO", "a draft with no expiry could never be purged"
    assert columns["version"] == "NO"
    assert columns["generated_version"] == "NO"
