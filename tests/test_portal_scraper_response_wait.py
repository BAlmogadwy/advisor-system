from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from unittest.mock import AsyncMock

import pytest

from core.services import portal_scraper
from core.services.portal_scraper import (
    _wait_for_plan_results,
    create_fresh_page_from_context,
    is_logged_out_html,
    is_staff_login_success_html,
)


class _UnusedTables:
    async def count(self) -> int:
        raise AssertionError("service response must be detected before table polling")


class _ServicePage:
    url = "https://portal.example/studentStudyPlan"

    def is_closed(self) -> bool:
        return False

    async def content(self) -> str:
        return "<html><head><title>Service Unavailable</title></head><body></body></html>"

    def locator(self, selector: str) -> _UnusedTables:
        assert selector == 'table[dir="rtl"]'
        return _UnusedTables()


@pytest.mark.parametrize(
    "html",
    [
        "<html><head><title>Service Unavailable</title></head></html>",
        "<html><head><title>Internal Server Error</title></head></html>",
        "<html><head><title>HTTP Status 503</title></head></html>",
    ],
)
def test_service_pages_are_treated_as_expired_portal_sessions(html: str) -> None:
    assert is_logged_out_html(html) is True


def test_staff_login_success_requires_an_authenticated_link() -> None:
    public_page = (
        '<html><a href="teachers_login.jsp">Staff login</a>'
        "<div>الخدمات الالكترونية لعمادة القبول والتسجيل</div></html>"
    )

    assert is_staff_login_success_html(public_page) is False
    assert is_staff_login_success_html('<a href="signOut.do">Logout</a>') is True


def test_fresh_worker_page_uses_authenticated_referrer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker_page = object()

    class FakeContext:
        async def new_page(self) -> object:
            return worker_page

    safe_goto = AsyncMock()
    safe_wait_network = AsyncMock()
    monkeypatch.setattr(portal_scraper, "_safe_goto", safe_goto)
    monkeypatch.setattr(portal_scraper, "_safe_wait_network", safe_wait_network)

    async def run() -> object:
        return await create_fresh_page_from_context(
            FakeContext(),  # type: ignore[arg-type]
            "https://portal.example/student-plan",
            referer_url="https://portal.example/staffLogin.do",
        )

    assert asyncio.run(run()) is worker_page
    safe_goto.assert_awaited_once_with(
        worker_page,
        "https://portal.example/student-plan",
        referer="https://portal.example/staffLogin.do",
    )
    safe_wait_network.assert_awaited_once_with(worker_page, timeout_ms=30000)


def test_study_plan_wait_surfaces_service_page_for_relogin_immediately() -> None:
    async def wait() -> None:
        with pytest.raises(RuntimeError, match="SESSION_LOGGED_OUT_HTML"):
            await _wait_for_plan_results(_ServicePage(), timeout_ms=500)  # type: ignore[arg-type]

    asyncio.run(wait())


@pytest.mark.parametrize(
    "navigate",
    [
        portal_scraper.navigate_to_student_study_plan,
        portal_scraper.navigate_to_student_timetable,
    ],
)
def test_pre_navigation_logout_uses_the_relogin_signal(
    navigate: Callable[..., Awaitable[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(portal_scraper, "is_logged_out", AsyncMock(return_value=True))

    async def run() -> None:
        with pytest.raises(RuntimeError, match="SESSION_LOGGED_OUT_HTML"):
            await navigate(object(), "4713672", verbose=False)

    asyncio.run(run())
