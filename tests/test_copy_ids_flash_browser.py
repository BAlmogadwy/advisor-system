"""Eligibility's "Copy IDs" comes back as itself, however often it is clicked.

The handler saves the button's markup, shows "Copied" for 1.5s, then puts the
saved markup back. A second click inside that window saved the FLASH as the
label, so the button read "Copied" for good. Found by the reviewer of #239,
when this button was restyled as an export action and became more prominent.
"""

from __future__ import annotations

import os
import re

os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

import pytest  # noqa: E402
from django.conf import settings  # noqa: E402
from django.contrib.auth.models import User  # noqa: E402
from django.contrib.staticfiles.testing import StaticLiveServerTestCase  # noqa: E402
from django.test import Client  # noqa: E402
from django.urls import reverse  # noqa: E402

playwright = pytest.importorskip("playwright.sync_api")

#: The flash state alone, not the whole class list.
COPIED = re.compile(r"(?:^|\s)copied(?:$|\s)")


class CopyIdsFlashTests(StaticLiveServerTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._pw = playwright.sync_playwright().start()
        cls.browser = cls._pw.chromium.launch()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls._pw.stop()
        super().tearDownClass()

    def test_a_second_click_during_the_flash_does_not_keep_it(self) -> None:
        user = User.objects.create_superuser(
            username="copy-flash", email="copy-flash@example.test", password="unused"
        )
        client = Client()
        client.force_login(user)
        context = self.browser.new_context()
        self.addCleanup(context.close)
        context.grant_permissions(
            ["clipboard-read", "clipboard-write"], origin=self.live_server_url
        )
        context.add_cookies(
            [
                {
                    "name": settings.SESSION_COOKIE_NAME,
                    "value": client.cookies[settings.SESSION_COOKIE_NAME].value,
                    "url": self.live_server_url,
                }
            ]
        )
        page = context.new_page()
        page.goto(f"{self.live_server_url}{reverse('dashboard')}#eligibility")
        button = page.locator("#elCopyIds")
        playwright.expect(button).to_be_visible()
        # Two eligible students, as a loaded report would leave them in the table.
        page.evaluate(
            """() => {
              const body = document.querySelector('#elTable tbody') || document.querySelector('#elTable');
              body.insertAdjacentHTML('beforeend',
                '<tr><td><span class="cr-id">4410001</span></td></tr>' +
                '<tr><td><span class="cr-id">4410002</span></td></tr>');
            }"""
        )
        before = button.inner_html()
        assert "<svg" in before and "Copy" in button.inner_text(), before

        button.click()
        playwright.expect(button).to_have_class(COPIED)
        flash = button.inner_html()
        assert flash != before, "the click never flashed; this asserts nothing"
        button.click()  # inside the 1.5s window

        playwright.expect(button).not_to_have_class(COPIED, timeout=5_000)
        page.wait_for_timeout(1_800)  # past any timer the second click could have started
        assert button.inner_html() == before
        # Lines, not one string: the Windows clipboard turns "\n" into "\r\n".
        copied = page.evaluate("() => navigator.clipboard.readText()")
        assert copied.splitlines() == ["4410001", "4410002"], copied
