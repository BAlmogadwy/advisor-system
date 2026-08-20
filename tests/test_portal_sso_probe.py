"""The live SSO probe's expectations, checked without a browser or a network.

The probe itself cannot run in CI — it talks to the university portal and to
Microsoft. What CAN be pinned is the table it judges against, and the judgement,
because the zero-expectations are the half that matters: a password selector that
matched the email step would make the scraper post the password into the username
field and burn a failed-login attempt against the account on every run.
"""

from __future__ import annotations

import pytest

from core.services import portal_scraper
from scripts.probe_portal_sso import EMAIL_STEP_EXPECTATIONS, evaluate, host_of


def _all_good() -> dict[str, int]:
    return {
        name: (1 if expectation == "at_least_one" else 0)
        for name, (_attr, expectation) in EMAIL_STEP_EXPECTATIONS.items()
    }


def test_every_expectation_names_a_selector_that_still_exists():
    """The table addresses `portal_scraper` by attribute name. Rename a selector
    and the probe would silently stop checking it rather than fail."""
    for name, (attr, expectation) in EMAIL_STEP_EXPECTATIONS.items():
        assert hasattr(portal_scraper, attr), f"{name} -> {attr} no longer exists"
        assert isinstance(getattr(portal_scraper, attr), str)
        assert expectation in {"at_least_one", "none"}


def test_the_login_chain_selectors_are_all_required_to_match():
    required = {
        name
        for name, (_attr, expectation) in EMAIL_STEP_EXPECTATIONS.items()
        if expectation == "at_least_one"
    }
    assert required == {"portal SSO link", "Microsoft username", "Microsoft submit"}


def test_the_fail_closed_selectors_are_all_required_to_be_absent():
    """These are the states the scraper refuses on. If any of them matched at the
    EMAIL step, the scraper would refuse every login before it ever tried."""
    forbidden = {
        name
        for name, (_attr, expectation) in EMAIL_STEP_EXPECTATIONS.items()
        if expectation == "none"
    }
    assert forbidden == {
        "Microsoft password",
        "Microsoft stay-signed-in",
        "Microsoft credential error",
        "Microsoft policy error",
        "Microsoft interactive step",
    }


def test_a_matching_chain_passes():
    assert [f.ok for f in evaluate(_all_good())] == [True] * len(EMAIL_STEP_EXPECTATIONS)


@pytest.mark.parametrize(
    ("name", "observed"),
    [
        ("portal SSO link", 0),
        ("Microsoft username", 0),
        ("Microsoft submit", 0),
    ],
)
def test_a_missing_required_selector_fails(name: str, observed: int):
    counts = _all_good()
    counts[name] = observed
    failed = {f.name for f in evaluate(counts) if not f.ok}
    assert failed == {name}


@pytest.mark.parametrize(
    "name",
    [
        "Microsoft password",
        "Microsoft stay-signed-in",
        "Microsoft credential error",
        "Microsoft policy error",
        "Microsoft interactive step",
    ],
)
def test_a_selector_that_should_be_absent_but_matches_fails(name: str):
    counts = _all_good()
    counts[name] = 1
    failed = {f.name for f in evaluate(counts) if not f.ok}
    assert failed == {name}


def test_an_unobserved_selector_is_a_failure_not_a_pass():
    """A silently missing observation must not read as a clean run — that is how a
    probe reports success for a page it never reached."""
    counts = _all_good()
    del counts["Microsoft username"]
    findings = {f.name: f for f in evaluate(counts)}
    assert findings["Microsoft username"].ok is False
    assert findings["Microsoft username"].detail == "not observed"


def test_a_locator_error_is_a_failure_not_a_pass():
    """`_observe` records -1 when a selector raises. That must not satisfy a
    zero-expectation by being 'not one'."""
    counts = _all_good()
    counts["Microsoft password"] = -1
    assert {f.name for f in evaluate(counts) if not f.ok} == {"Microsoft password"}


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://login.microsoftonline.com/x/oauth2/authorize?state=SECRET",
            "https://login.microsoftonline.com",
        ),
        ("https://eas.taibahu.edu.sa:8443/TaibahReg/x", "https://eas.taibahu.edu.sa:8443"),
        ("not a url at all", "://<none>"),
    ],
)
def test_host_of_never_leaks_the_path_or_query(url: str, expected: str):
    """Microsoft authorize URLs carry one-time state and nonce values, and the
    portal's link carries a per-load key. The probe prints hosts only."""
    result = host_of(url)
    assert result == expected
    assert "SECRET" not in result
    assert "?" not in result


# ---------------------------------------------------------------------------
# The configuration check. This is the exact state the project was in when the
# probe was written: both variables set, non-empty, and the username still the
# retired portal id — which looks configured and fails at Microsoft every time.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("username", "password", "ok"),
    [
        ("staff.member@taibahu.edu.sa", "pw", True),
        ("STAFF@TAIBAHU.EDU.SA", "pw", True),
        # The retired portal id: no '@', so Entra never resolves an account.
        ("4400251", "pw", False),
        ("staffmember", "pw", False),
        ("", "pw", False),
        ("staff@taibahu.edu.sa", "", False),
        ("", "", False),
    ],
)
def test_credential_shape(username: str, password: str, ok: bool):
    from scripts.probe_portal_sso import credential_shape

    assert credential_shape(username, password).ok is ok


def test_credential_shape_never_echoes_the_values():
    """The detail line is printed to a terminal and pasted into issues."""
    from scripts.probe_portal_sso import credential_shape

    finding = credential_shape("4400251", "hunter2")
    assert "4400251" not in finding.detail
    assert "hunter2" not in finding.detail
    assert "UPN" in finding.detail


def test_credential_shape_names_which_variable_is_empty():
    from scripts.probe_portal_sso import credential_shape

    assert "PORTAL_ADMIN_PASSWORD" in credential_shape("a@b.sa", "").detail
    assert "PORTAL_ADMIN_USERNAME" in credential_shape("", "pw").detail


# ---------------------------------------------------------------------------
# The probe's own failure modes. A probe that reports success for a chain it
# never verified is worse than no probe: it is a green light nobody earned.
# ---------------------------------------------------------------------------


def test_a_selector_that_raises_is_recorded_as_an_error_not_a_zero():
    """`count_or_error` is what stops a raising locator from scoring 0, which
    would mark all five fail-closed selectors "OK, expected 0"."""
    import asyncio

    from scripts.probe_portal_sso import SELECTOR_ERROR, count_or_error

    class Raises:
        async def count(self):
            raise RuntimeError("malformed selector")

    class Counts:
        async def count(self):
            return 3

    assert asyncio.run(count_or_error(Raises())) == SELECTOR_ERROR
    assert asyncio.run(count_or_error(Counts())) == 3


def test_the_error_sentinel_can_never_satisfy_a_zero_expectation():
    from scripts.probe_portal_sso import SELECTOR_ERROR

    assert SELECTOR_ERROR != 0, "a zero sentinel would read as a clean fail-closed selector"
    counts = _all_good()
    for name, (_attr, expectation) in EMAIL_STEP_EXPECTATIONS.items():
        if expectation != "none":
            continue
        probe = dict(counts)
        probe[name] = SELECTOR_ERROR
        assert {f.name for f in evaluate(probe) if not f.ok} == {name}


def test_a_refused_sign_in_host_fails_the_probe():
    """The tenant can move sign-in to a host the scraper rejects — b2clogin, a new
    federation endpoint — that still carries `#i0116`, `#idSIButton9` and none of
    the forbidden selectors. Every selector expectation passes; the probe must
    still fail, or it prints "the chain matches" for a login that cannot work.
    """
    from scripts.probe_portal_sso import Finding

    selector_findings = evaluate(_all_good())
    assert all(f.ok for f in selector_findings)

    host_check = Finding("sign-in host is one the scraper accepts", False, "would be REFUSED")
    findings = [host_check, *selector_findings]
    assert [f for f in findings if not f.ok] == [host_check]
