import pytest

from core.services import llm_backend, rbac


@pytest.fixture(autouse=True)
def _reset_rbac_flags() -> None:  # noqa: PT004
    """Reset module-level flags so ensure_role_groups() re-creates groups after
    each test's transaction rollback."""
    rbac._groups_ensured = False
    rbac._scope_schema_ensured = False


@pytest.fixture(autouse=True)
def forbid_llm_network(monkeypatch) -> None:  # noqa: PT004
    """No test, anywhere, may make a real LLM request.

    REPOSITORY-WIDE, AND WITH NO ESCAPE HATCH. Not a marker, not an environment
    variable — a test that needs a transport installs its own fake over this one,
    which is explicit and local to that test.

    It exists because the failure already happened. A secret-containment test
    defined its HTTP stub and never installed it, so the client made a genuine
    https request to Alibaba's public endpoint, and the assertions were reading
    the LIVE server's error code instead of the fixture's. The test failed for
    the right reason by luck: the only reason anyone noticed is that the code the
    server returned differed from the code the fixture sent.

    A stub that is written but not installed reads exactly like a stub that is.
    This converts "remember to patch" into "cannot forget", for every test file
    including the ones not written yet.

    The paid smoke test lives in a management command, outside pytest, and stays
    there deliberately.
    """

    def blocked(request, *args, **kwargs):  # noqa: ARG001
        url = getattr(request, "full_url", request)
        raise AssertionError(
            f"a test attempted a real LLM network request to {url!r}. "
            "Install a fake over core.services.llm_backend._http_open in the test."
        )

    # BOTH seams. `_http_open` is what the transport calls; `urlopen` is what it
    # delegates to for a redirect-following (local) request. Blocking only the
    # outer one would leave a direct `urlopen` added later uncovered, and the
    # whole point of this fixture is that it cannot be forgotten.
    monkeypatch.setattr(llm_backend, "_http_open", blocked)
    monkeypatch.setattr(llm_backend, "urlopen", blocked)


@pytest.fixture(autouse=True)
def _no_real_provider_settings(settings) -> None:  # noqa: PT004
    """No test sees the developer's real Alibaba configuration.

    A `.env` with genuine values made two tests pass locally and fail in CI —
    and worse, one of them built a client against the REAL workspace URL to
    assert a refusal. A test that only passes where the secrets live is not
    testing the code, and one that reads them is a test that could one day send
    them somewhere.

    Neutralised for every test; a test that needs a provider configuration
    declares a synthetic one with `override_settings`, which wins over this.
    """
    settings.LLM_BACKEND = "local"
    settings.ALIBABA_LLM_BASE_URL = ""
    settings.ALIBABA_LLM_API_KEY = ""
    settings.ALIBABA_LLM_MODEL = ""
    settings.ALIBABA_LLM_ALLOW_LIVE_REQUESTS = False
