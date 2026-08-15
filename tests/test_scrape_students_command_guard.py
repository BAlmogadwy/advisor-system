from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock

import pytest
from django.core.management.base import CommandError
from pytest import MonkeyPatch

from core.management.commands import scrape_students
from core.management.commands.scrape_students import (
    StudentScrapeOutcome,
    _elective_scope_for_program,
)
from core.services import portal_scraper


def test_read_csv_skips_blank_spacer_rows_and_duplicate_students(
    tmp_path: Path,
) -> None:
    csv_path = tmp_path / "students.csv"
    csv_path.write_text(
        "student_id,program,section\r\n,,\r\n4714604, CS2 , M \r\n,,\r\n4714604,CS2,M\r\n",
        encoding="utf-8",
    )

    rows = scrape_students.Command()._read_csv(str(csv_path))

    assert rows == [{"student_id": "4714604", "program": "CS2", "section": "M"}]


def test_read_csv_rejects_partially_populated_invalid_student(
    tmp_path: Path,
) -> None:
    csv_path = tmp_path / "students.csv"
    csv_path.write_text(
        "student_id,program,section\nnot-an-id,CS2,M\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match=r"CSV line 2.*invalid student_id"):
        scrape_students.Command()._read_csv(str(csv_path))


@pytest.mark.parametrize("student_id", ["٤٧١٤٦٠٤", "¹", "471A604"])
def test_read_csv_rejects_non_ascii_student_id(
    tmp_path: Path,
    student_id: str,
) -> None:
    csv_path = tmp_path / "students.csv"
    csv_path.write_text(
        f"student_id,program,section\n{student_id},CS2,M\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="invalid student_id"):
        scrape_students.Command()._read_csv(str(csv_path))


def test_read_csv_rejects_missing_program_or_section(tmp_path: Path) -> None:
    csv_path = tmp_path / "students.csv"
    csv_path.write_text(
        "student_id,program,section\n4714604,,M\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="must include program and section"):
        scrape_students.Command()._read_csv(str(csv_path))


def test_read_csv_rejects_conflicting_duplicate_student(tmp_path: Path) -> None:
    csv_path = tmp_path / "students.csv"
    csv_path.write_text(
        "student_id,program,section\n4714604,CS2,M\n4714604,CS,F\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match=r"line 3 conflicts with line 2"):
        scrape_students.Command()._read_csv(str(csv_path))


def test_read_csv_rejects_ambiguous_shape(tmp_path: Path) -> None:
    duplicate_header = tmp_path / "duplicate.csv"
    duplicate_header.write_text(
        "student_id,program,section,program\n4714604,CS2,M,CS\n",
        encoding="utf-8",
    )
    overflow = tmp_path / "overflow.csv"
    overflow.write_text(
        "student_id,program,section\n4714604,CS2,M,unexpected\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="duplicate column names"):
        scrape_students.Command()._read_csv(str(duplicate_header))
    with pytest.raises(RuntimeError, match="too many columns"):
        scrape_students.Command()._read_csv(str(overflow))


def test_read_csv_rejects_file_with_only_blank_spacer_rows(tmp_path: Path) -> None:
    csv_path = tmp_path / "students.csv"
    csv_path.write_text(
        "student_id,program,section\n,,\n,,\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="CSV contains no valid student rows"):
        scrape_students.Command()._read_csv(str(csv_path))


def test_handle_holds_blocking_snapshot_guard_for_entire_run(monkeypatch: MonkeyPatch) -> None:
    events: list[str] = []
    guard_owned = False

    @contextmanager
    def fake_guard(*, blocking: bool) -> Iterator[bool]:
        nonlocal guard_owned
        assert blocking is True
        guard_owned = True
        events.append("guard_acquired")
        try:
            yield True
        finally:
            events.append("guard_released")
            guard_owned = False

    async def fake_run(options: dict[str, object]) -> None:
        assert guard_owned is True
        assert options["csv"] == "students.csv"
        events.append("run")

    command = scrape_students.Command()
    monkeypatch.setattr(scrape_students, "HAS_PLAYWRIGHT", True)
    monkeypatch.setattr(scrape_students, "section_snapshot_operation_guard", fake_guard)
    monkeypatch.setattr(command, "_run", fake_run)

    command.handle(csv="students.csv")

    assert events == ["guard_acquired", "run", "guard_released"]


@pytest.mark.parametrize(
    ("academic_year", "term"),
    [("1448", ""), ("", "1")],
)
def test_empty_snapshot_scope_options_must_be_supplied_together(
    monkeypatch: MonkeyPatch,
    academic_year: str,
    term: str,
) -> None:
    command = scrape_students.Command()
    monkeypatch.setattr(scrape_students, "HAS_PLAYWRIGHT", False)

    with pytest.raises(CommandError, match="must be supplied together"):
        command.handle(
            csv="students.csv",
            empty_snapshot_year=academic_year,
            empty_snapshot_term=term,
        )


def test_handle_refuses_to_run_when_blocking_guard_unexpectedly_fails(
    monkeypatch: MonkeyPatch,
) -> None:
    @contextmanager
    def failed_guard(*, blocking: bool) -> Iterator[bool]:
        assert blocking is True
        yield False

    command = scrape_students.Command()
    run = AsyncMock()
    monkeypatch.setattr(scrape_students, "HAS_PLAYWRIGHT", True)
    monkeypatch.setattr(scrape_students, "section_snapshot_operation_guard", failed_guard)
    monkeypatch.setattr(command, "_run", run)

    with pytest.raises(CommandError, match="section snapshot operation guard"):
        command.handle(csv="students.csv")

    run.assert_not_called()


def test_missing_playwright_fails_before_acquiring_snapshot_guard(
    monkeypatch: MonkeyPatch,
) -> None:
    def unexpected_guard(*, blocking: bool):  # type: ignore[no-untyped-def]
        raise AssertionError("guard must not be entered without Playwright")

    command = scrape_students.Command()
    run = AsyncMock()
    monkeypatch.setattr(scrape_students, "HAS_PLAYWRIGHT", False)
    monkeypatch.setattr(scrape_students, "section_snapshot_operation_guard", unexpected_guard)
    monkeypatch.setattr(command, "_run", run)

    with pytest.raises(CommandError, match="playwright is not installed"):
        command.handle(csv="students.csv")

    run.assert_not_called()


def test_partial_student_failures_make_the_batch_process_fail(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    class Page:
        context = object()

    async def login() -> tuple[object, object, Page]:
        return object(), object(), Page()

    command = scrape_students.Command()
    monkeypatch.setattr(scrape_students, "BASE_DIR", tmp_path)
    monkeypatch.setattr(scrape_students, "login_to_portal", login)
    monkeypatch.setattr(scrape_students, "close_browser", AsyncMock())
    monkeypatch.setattr(
        command,
        "_read_csv",
        lambda path: [{"student_id": "4000001", "program": "DS", "section": "M"}],
    )
    monkeypatch.setattr(
        command,
        "_scrape_one",
        AsyncMock(
            return_value={
                "student_id": "4000001",
                "program": "DS",
                "ok": False,
                "verified_study_plan": False,
                "verified_timetable": False,
            }
        ),
    )

    with pytest.raises(CommandError, match="1 failed student"):
        asyncio.run(
            command._run(
                {
                    "database_students": False,
                    "csv": "students.csv",
                    "concurrency": 1,
                    "max_retries": 0,
                    "save_html": False,
                    "debug_dir": str(tmp_path / "debug"),
                    "empty_snapshot_year": "",
                    "empty_snapshot_term": "",
                }
            )
        )

    assert (tmp_path / "data" / "failed_scrapes.csv").read_text(encoding="utf-8") == (
        "failed_student_id\n4000001\n"
    )


def test_worker_attributes_failure_when_its_page_cannot_be_created(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def fail_page_creation(
        context: object,
        *,
        referer_url: str | None = None,
    ) -> None:
        assert referer_url == "https://portal.example/staffLogin.do"
        raise RuntimeError("browser context unavailable")

    class Anchor:
        url = "https://portal.example/staffLogin.do"

    command = scrape_students.Command()
    command._shared = {"context": object(), "page": Anchor()}
    save_debug = AsyncMock()
    monkeypatch.setattr(scrape_students, "create_fresh_page_from_context", fail_page_creation)
    monkeypatch.setattr(command, "_save_debug", save_debug)

    async def run_worker() -> StudentScrapeOutcome:
        return await command._scrape_one(
            {"student_id": "4713672", "program": "AI", "section": "M"},
            asyncio.Semaphore(1),
            asyncio.Semaphore(1),
            asyncio.Lock(),
            max_retries=1,
            save_html=False,
            debug_dir=str(tmp_path),
        )

    outcome = asyncio.run(run_worker())

    assert outcome == {
        "student_id": "4713672",
        "program": "AI",
        "ok": False,
        "schedule_state": "",
        "academic_year": "",
        "term": "",
        "error": "browser context unavailable",
    }
    save_debug.assert_awaited_once()


def test_elective_scope_excludes_failures_and_empty_schedule_terms() -> None:
    outcomes: list[StudentScrapeOutcome] = [
        {
            "student_id": "4710001",
            "program": "AI",
            "ok": True,
            "schedule_state": "complete_schedule",
            "academic_year": "1448",
            "term": "1",
            "error": "",
        },
        {
            "student_id": "4710002",
            "program": "AI",
            "ok": True,
            "schedule_state": "confirmed_empty_current_schedule",
            "academic_year": "",
            "term": "",
            "error": "",
        },
        {
            "student_id": "4710003",
            "program": "AI",
            "ok": False,
            "schedule_state": "",
            "academic_year": "",
            "term": "",
            "error": "failed",
        },
    ]

    student_ids, snapshots = _elective_scope_for_program(outcomes, "AI")

    assert student_ids == [4710001, 4710002]
    assert snapshots == {4710001: ("1448", "1")}


def test_relogin_is_coalesced_without_closing_unrelated_workers(
    monkeypatch: MonkeyPatch,
) -> None:
    class FakePage:
        def __init__(self, context: object) -> None:
            self.context = context
            self.closed = False

        async def wait_for_selector(self, selector: str, timeout: int) -> None:
            assert selector in {
                'input[name="userName"]',
                'input[name="StudentNumber"]',
            }
            assert timeout == 60000

        async def fill(self, selector: str, value: str) -> None:
            assert selector in {'input[name="userName"]', 'input[name="password"]'}
            assert isinstance(value, str)

        async def click(self, selector: str) -> None:
            assert selector == 'input[name="submit"]'

        async def content(self) -> str:
            return '<html><body><a href="signOut.do">Logout</a></body></html>'

        async def close(self) -> None:
            self.closed = True

    class FakeContext:
        def __init__(self) -> None:
            self.created = 0
            self.login_page = FakePage(self)

        async def new_page(self) -> FakePage:
            self.created += 1
            return self.login_page

    context = FakeContext()
    old_anchor = FakePage(context)
    unrelated_worker = FakePage(context)
    command = scrape_students.Command()
    command._shared = {
        "context": context,
        "page": old_anchor,
        "session_generation": 0,
    }
    safe_goto = AsyncMock()
    safe_wait_network = AsyncMock()
    monkeypatch.setattr(portal_scraper, "_safe_goto", safe_goto)
    monkeypatch.setattr(portal_scraper, "_safe_wait_network", safe_wait_network)

    async def relogin_twice() -> tuple[int, int]:
        lock = asyncio.Lock()
        first = await command._force_relogin(lock, observed_generation=0)
        second = await command._force_relogin(lock, observed_generation=0)
        return first, second

    assert asyncio.run(relogin_twice()) == (1, 1)
    assert context.created == 1
    assert old_anchor.closed is True
    assert unrelated_worker.closed is False
    assert context.login_page.closed is False
    safe_goto.assert_awaited_once()
    safe_wait_network.assert_awaited_once()


def test_failed_relogin_is_attempted_once_and_closes_candidate_page(
    monkeypatch: MonkeyPatch,
) -> None:
    class FakePage:
        def __init__(self, context: object) -> None:
            self.context = context
            self.closed = False

        async def wait_for_selector(self, selector: str, timeout: int) -> None:
            assert selector == 'input[name="userName"]'
            assert timeout == 60000

        async def fill(self, selector: str, value: str) -> None:
            assert selector in {'input[name="userName"]', 'input[name="password"]'}
            assert isinstance(value, str)

        async def click(self, selector: str) -> None:
            assert selector == 'input[name="submit"]'

        async def content(self) -> str:
            return "<html><body>Public portal landing page</body></html>"

        async def close(self) -> None:
            self.closed = True

    class FakeContext:
        def __init__(self) -> None:
            self.created = 0
            self.login_page = FakePage(self)

        async def new_page(self) -> FakePage:
            self.created += 1
            return self.login_page

    context = FakeContext()
    command = scrape_students.Command()
    command._shared = {
        "context": context,
        "page": FakePage(context),
        "session_generation": 0,
    }
    monkeypatch.setattr(portal_scraper, "_safe_goto", AsyncMock())
    monkeypatch.setattr(portal_scraper, "_safe_wait_network", AsyncMock())

    async def relogin_twice() -> tuple[object, object]:
        lock = asyncio.Lock()
        return cast(
            tuple[object, object],
            await asyncio.gather(
                command._force_relogin(lock, observed_generation=0),
                command._force_relogin(lock, observed_generation=0),
                return_exceptions=True,
            ),
        )

    results = asyncio.run(relogin_twice())

    assert context.created == 1
    assert context.login_page.closed is True
    assert all(isinstance(result, scrape_students.PortalSessionRecoveryError) for result in results)
