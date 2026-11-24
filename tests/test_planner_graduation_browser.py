"""Build and select schedules through the staff UI with an actual forecast."""

import pytest
from django.conf import settings

from tests.test_planner_graduation import STUDENT_ID, TERM, YEAR, _seed_curriculum

playwright = pytest.importorskip("playwright.sync_api")


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("language", ["en", "ar"])
def test_option_cards_and_reasons_follow_the_selected_schedule(
    language, live_server, admin_client, monkeypatch
):
    monkeypatch.setenv("DJANGO_ALLOW_ASYNC_UNSAFE", "1")
    _seed_curriculum()
    session = admin_client.cookies[settings.SESSION_COOKIE_NAME].value
    with playwright.sync_playwright() as driver:
        browser = driver.chromium.launch()
        context = browser.new_context(viewport={"width": 390, "height": 844})
        context.add_cookies(
            [
                {"name": settings.SESSION_COOKIE_NAME, "value": session, "url": live_server.url},
                {"name": settings.LANGUAGE_COOKIE_NAME, "value": language, "url": live_server.url},
            ]
        )
        page = context.new_page()
        page.goto(f"{live_server.url}/planner/?student_id={STUDENT_ID}&year={YEAR}&term={TERM}")
        page.locator("#step2").click()
        page.locator("#maxCredits").fill("3")
        page.locator("#runBuilder").click()
        panel = page.locator("#graduationProjection")
        cards = page.locator("#optionCards .option-card")
        faster = "1 term sooner" if language == "en" else "أبكر بفصل واحد"
        same = "Same completion term" if language == "en" else "نفس فصل التسجيل الحالي"
        fast_card = cards.filter(has_text=faster).first
        fast_card.wait_for()
        assert (
            panel.locator("#graduationProjectionBaseline")
            .inner_text()
            .startswith("5 terms" if language == "en" else "٥ فصول")
        )
        badges = cards.locator("[data-option-projection]").all_inner_texts()
        assert len(badges) == cards.count() and all(faster in b or same in b for b in badges)
        cards.filter(has_text=same).first.locator("button").click()
        assert same in panel.locator("#graduationProjectionSelected").inner_text()
        fast_card.locator("button").click()
        assert faster in panel.locator("#graduationProjectionSelected").inner_text()
        panel.locator("summary").click()
        assert "AI451" in panel.locator("#graduationProjectionReasons").inner_text()
        assert panel.evaluate("(el) => el.scrollWidth <= el.clientWidth + 1")
        browser.close()
