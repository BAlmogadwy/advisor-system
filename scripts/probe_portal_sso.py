"""Check the scraper's SSO selectors against the LIVE portal. No credentials.

WHY THIS EXISTS

Every test of `authenticate_portal_page` drives a fake page, so the whole suite
stays green while the real portal renames a button. That is not a hypothetical:
this scraper's previous login filled `userName` / `password` on
`teachers_login.jsp` and kept passing its tests for as long as it took somebody to
run a scrape and watch it fail at the first step.

This probe walks as far into the real chain as can be walked without
authenticating — the portal's pre-login page, its freshly keyed `authLogin` link,
and the Microsoft sign-in page that link lands on — and reports which of the
module's own selectors actually match. It is the cheap first check after a portal
change, a tenant policy change, or a failed scrape.

WHAT IT DELIBERATELY DOES NOT DO

It never fills a credential field, never submits a form, and never reads
`PORTAL_ADMIN_PASSWORD`. It therefore cannot verify the second half of the chain
(username submit -> home-realm discovery -> ADFS -> callback); that needs a real
sign-in, and the runbook for it is a one-row CSV scrape, not this script.

It prints host names and match counts only. Microsoft authorize URLs carry
one-time state and nonce values and the portal's link carries a per-load key, so
nothing here prints a full URL or any page content — the same rule
`portal_scraper` follows for its own exception messages.

USAGE

    .venv/Scripts/python.exe scripts/probe_portal_sso.py

Exit code 0 when the chain matches, 1 when it does not. A non-zero exit means the
scraper's login is broken NOW, before anyone spends a roster run finding out.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from urllib.parse import urlsplit

#: Selector name -> (attribute on ``portal_scraper``, expected matches at the
#: EMAIL step of a fresh sign-in).
#:
#: The zero-expectations are the load-bearing half. Microsoft ships the password
#: input in the DOM of the email page, moved off-screen; a password selector that
#: matched it would make the scraper submit the password into the username field
#: and burn a failed-login attempt against the account on every single run.
EMAIL_STEP_EXPECTATIONS: dict[str, tuple[str, str]] = {
    "portal SSO link": ("_PORTAL_SSO_LINK_SELECTOR", "at_least_one"),
    "Microsoft username": ("_MICROSOFT_USERNAME_SELECTOR", "at_least_one"),
    "Microsoft submit": ("_MICROSOFT_SUBMIT_SELECTOR", "at_least_one"),
    "Microsoft password": ("_MICROSOFT_PASSWORD_SELECTOR", "none"),
    "Microsoft stay-signed-in": ("_MICROSOFT_KMSI_SELECTOR", "none"),
    "Microsoft credential error": ("_MICROSOFT_CREDENTIAL_ERROR_SELECTOR", "none"),
    "Microsoft policy error": ("_MICROSOFT_POLICY_ERROR_SELECTOR", "none"),
    "Microsoft interactive step": ("_MICROSOFT_INTERACTIVE_SELECTOR", "none"),
}


@dataclass(frozen=True)
class Finding:
    name: str
    ok: bool
    detail: str


def host_of(url: str) -> str:
    """Scheme and host only. Never the path or query — those carry the secrets."""
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or "<none>"
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme}://{host}{port}"
    except ValueError:
        return "<unparseable>"


def evaluate(counts: dict[str, int]) -> list[Finding]:
    """Turn observed selector counts into findings.

    Pure, so the expectations are testable without a browser or a network. The
    driver below is the only part that needs either.
    """
    findings: list[Finding] = []
    for name, (_attr, expectation) in EMAIL_STEP_EXPECTATIONS.items():
        if name not in counts:
            findings.append(Finding(name, False, "not observed"))
            continue
        count = counts[name]
        if expectation == "at_least_one":
            findings.append(Finding(name, count >= 1, f"{count} match(es), expected at least 1"))
        else:
            findings.append(Finding(name, count == 0, f"{count} match(es), expected 0"))
    return findings


def credential_shape(username: str, password: str) -> Finding:
    """Is the configured username the SHAPE Entra needs? Values are never read out.

    Entra signs in on the full UPN, `someone@taibahu.edu.sa`. The retired portal
    form took a bare staff id, and an `.env` carried over from it looks perfectly
    configured — both variables set, non-empty — while every sign-in dies at the
    first Microsoft step. That is a slow, confusing failure worth one cheap check.
    """
    if not username or not password:
        missing = " and ".join(
            n
            for n, v in (("PORTAL_ADMIN_USERNAME", username), ("PORTAL_ADMIN_PASSWORD", password))
            if not v
        )
        return Finding("credentials configured", False, f"{missing} is empty")
    if "@" not in username:
        return Finding(
            "credentials configured",
            False,
            "PORTAL_ADMIN_USERNAME has no '@' — Entra needs the full UPN "
            "(someone@taibahu.edu.sa), not the retired portal id",
        )
    return Finding("credentials configured", True, "username looks like a UPN")


#: Recorded when a selector raises during observation. Deliberately negative:
#: a zero would satisfy every `none` expectation and read as a clean run.
SELECTOR_ERROR = -1


async def count_or_error(locator: object) -> int:
    """Match count, or :data:`SELECTOR_ERROR` if the locator raises.

    A malformed selector, a detached frame or a navigation mid-probe all raise
    here. Scoring those as 0 would mark all five fail-closed selectors OK.
    """
    try:
        return int(await locator.count())  # type: ignore[attr-defined]
    except Exception:
        return SELECTOR_ERROR


async def _observe() -> tuple[dict[str, int], list[str], list[Finding]]:
    """Walk the live chain and return (selector counts, notes, host findings)."""
    from django.conf import settings
    from playwright.async_api import async_playwright

    from core.services import portal_scraper as ps

    notes: list[str] = []
    counts: dict[str, int] = {}
    checks: list[Finding] = []
    login_url = str(settings.PORTAL_LOGIN_URL)
    notes.append(f"portal pre-login: {host_of(login_url)}")

    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(headless=True)
    try:
        page = await browser.new_page()
        await page.goto(login_url, wait_until="domcontentloaded", timeout=45000)

        html = await ps.safe_page_content(page)
        detectors_agree = ps.is_logged_out_html(html) and not ps.is_staff_login_success_html(html)
        checks.append(
            Finding(
                "detectors read the sign-in page as signed out",
                detectors_agree,
                f"logged_out={ps.is_logged_out_html(html)} "
                f"signed_in={ps.is_staff_login_success_html(html)}",
            )
        )

        link = page.locator(ps._PORTAL_SSO_LINK_SELECTOR)
        counts["portal SSO link"] = await link.count()
        if counts["portal SSO link"] < 1:
            notes.append("!! the portal's authLogin link was not found; the chain stops here")
            return counts, notes, checks

        await link.first.click()
        try:
            await page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            notes.append("(the sign-in page did not go idle; probing it anyway)")

        current = page.url
        notes.append(f"sign-in page: {host_of(current)}")
        accepted = ps._is_microsoft_login_url(current) or ps._is_taibah_adfs_url(current)
        # A FINDING, not a note. The tenant can move the sign-in host — b2clogin, a
        # new federation endpoint, login.microsoft.us — to a page that still carries
        # `#i0116`, `#idSIButton9` and none of the forbidden selectors. Every
        # selector expectation would pass while the scraper refuses the host on the
        # very next run, and a probe that printed that as a note exited 0 saying the
        # chain matched.
        checks.append(
            Finding(
                "sign-in host is one the scraper accepts",
                accepted,
                f"{host_of(current)} "
                + ("accepted" if accepted else "would be REFUSED by the scraper"),
            )
        )

        for name, (attr, _expectation) in EMAIL_STEP_EXPECTATIONS.items():
            if name == "portal SSO link":
                continue
            counts[name] = await count_or_error(page.locator(getattr(ps, attr)))
        return counts, notes, checks
    finally:
        await browser.close()
        await playwright.stop()


def main() -> int:
    import asyncio

    import django

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    django.setup()

    from django.conf import settings

    counts, notes, host_checks = asyncio.run(_observe())
    for note in notes:
        print(note)

    credentials = credential_shape(
        str(getattr(settings, "PORTAL_ADMIN_USERNAME", "") or "").strip(),
        str(getattr(settings, "PORTAL_ADMIN_PASSWORD", "") or ""),
    )
    selector_findings = evaluate(counts)
    print("\nselector expectations at the email step:")
    width = max(len(f.name) for f in selector_findings)
    for finding in selector_findings:
        print(f"  {'OK  ' if finding.ok else 'FAIL'}  {finding.name:<{width}}  {finding.detail}")

    print("\nconfiguration:")
    print(f"  {'OK  ' if credentials.ok else 'FAIL'}  {credentials.name}: {credentials.detail}")

    findings = [credentials, *host_checks, *selector_findings]
    failed = [f for f in findings if not f.ok]
    print(
        "\nNo credential field was filled. The second half of the chain "
        "(username submit -> ADFS -> callback) needs a real sign-in; use a one-row CSV scrape."
    )
    if failed:
        print(
            f"\n{len(failed)} check(s) FAILED — the scraper cannot sign in as configured: "
            + ", ".join(f.name for f in failed)
        )
        return 1
    print("\nThe login chain and the configuration match the live portal, as far as they can")
    print("be checked without signing in.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
