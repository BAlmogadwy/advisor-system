"""The API key must not be printable by accident.

Every assertion here uses a SENTINEL key and then goes looking for it in the
places a secret actually escapes from — not the places anyone deliberately
prints. Nobody writes `print(api_key)`. Secrets leak through the defaults:

  * a frozen dataclass prints every field in its `repr`, so one
    `logger.debug("%s", config)` or one pytest assertion diff over a config
    object emits the bearer token in full;
  * an exception that interpolates the object it failed on carries it into a
    traceback, and tracebacks are the one thing that always gets logged;
  * an HTTP error handler that quotes the request — headers included — puts the
    Authorization header into an error string.

So the sentinel is deliberately distinctive and searched for everywhere.
"""

from __future__ import annotations

import json
import logging
from urllib.error import HTTPError

import pytest
from django.test import override_settings

from core.services import llm_backend
from core.services.llm_backend import (
    LLMAuthenticationError,
    LLMConfigError,
    OpenAICompatibleLLMClient,
    check_llm_health,
    endpoint_config,
)


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    """Nothing in this module may reach the network.

    Not a belt-and-braces nicety. An earlier version of the 401 test defined its
    stub and forgot to install it, so the client made a REAL https request to
    Alibaba and the assertions were quietly reading the live server's error code
    instead of the fixture's. The test failed for the right reason by luck.

    Every test here installs its own stub over this; what this catches is the one
    that forgets.
    """

    def refuse(request, timeout=None):  # noqa: ARG001
        raise AssertionError(
            f"a test reached the network: {getattr(request, 'full_url', request)!r} — "
            "install a stub over llm_backend.urlopen"
        )

    monkeypatch.setattr(llm_backend, "_http_open", refuse)


SENTINEL_KEY = "sk-SENTINELdoNOTleak0000000000000000"
#: Correct SHAPE, invented value. The real workspace id is a live identifier
#: and belongs in the deployment secret store, not in git.
WORKSPACE = "ws-synthetic0000000"
REGION = "ap-southeast-1"
GOOD_URL = f"https://{WORKSPACE}.{REGION}.maas.aliyuncs.com/compatible-mode/v1"

ALIBABA = {
    "LLM_BACKEND": "alibaba",
    "ALIBABA_LLM_BASE_URL": GOOD_URL,
    "ALIBABA_LLM_API_KEY": SENTINEL_KEY,
    "ALIBABA_LLM_MODEL": "qwen3.7-plus",
    # The egress kill switch is ON for the transport tests in this file, and the
    # tests are still offline: `conftest.forbid_llm_network` blocks the socket
    # repo-wide and each test installs its own fake over it. Enabling the flag
    # here buys the ability to test retries, redirects and error typing at all —
    # with it off, every one of those requests stops at the switch and asserts
    # nothing about the transport. `test_the_kill_switch_*` covers the off case.
    "ALIBABA_LLM_ALLOW_LIVE_REQUESTS": True,
}


@override_settings(**ALIBABA)
def test_the_config_repr_does_not_contain_the_key():
    """`field(repr=False)`. Without it every debug print of a config object is a
    credential disclosure."""
    config = endpoint_config("alibaba")
    assert config.api_key == SENTINEL_KEY, "the fixture is not exercising a real key"
    assert SENTINEL_KEY not in repr(config)
    assert SENTINEL_KEY not in str(config)
    assert SENTINEL_KEY not in f"{config}"
    assert SENTINEL_KEY not in f"{config!r}"


@override_settings(**ALIBABA)
def test_the_key_is_absent_from_health_output():
    """The health payload is browser-reachable. It reports a hostname, never a
    URL that might carry a workspace path, and never a credential."""
    health = check_llm_health("alibaba")
    body = json.dumps(health, ensure_ascii=False)
    assert SENTINEL_KEY not in body
    assert "Authorization" not in body
    # REGION, never a hostname: the first label of a Model Studio host IS the
    # workspace identifier, so "hostname only" leaks what the rule protects.
    assert health["region"] == REGION
    assert "endpoint_host" not in health
    assert WORKSPACE not in body, "the workspace identifier reached the health payload"
    assert GOOD_URL not in body, "the full endpoint URL reached the health payload"


@override_settings(**ALIBABA)
def test_the_key_is_absent_from_a_401_error_and_its_logs(caplog, monkeypatch):
    """A rejected credential is the one error path guaranteed to be triggered by
    a wrong key — so it is the path most likely to print one."""
    client = OpenAICompatibleLLMClient(endpoint_config("alibaba"))

    def fail_401(request, *args, **kwargs):  # noqa: ARG001
        raise HTTPError(
            GOOD_URL,
            401,
            "Unauthorized",
            {},
            # A provider that echoes the request back is not hypothetical.
            _body(
                {
                    "error": {
                        "code": "InvalidApiKey",
                        "message": f"key {SENTINEL_KEY} PROVIDER_ECHO_SENTINEL",
                    }
                }
            ),
        )

    monkeypatch.setattr(llm_backend, "_http_open", fail_401)

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(LLMAuthenticationError) as caught:
            client.chat([{"role": "user", "content": "hi"}], model="qwen3.7-plus")

    assert SENTINEL_KEY not in str(caught.value)
    assert SENTINEL_KEY not in repr(caught.value)
    assert "Authorization" not in str(caught.value)
    # The provider's MESSAGE is discarded entirely; only its short code survives.
    # ("rejected" is our own wording, so it is a useless thing to search for — the
    # first version of this assertion searched for it and failed on our own text.)
    assert "PROVIDER_ECHO_SENTINEL" not in str(caught.value)
    assert "InvalidApiKey" in str(caught.value)
    assert SENTINEL_KEY not in caplog.text


@override_settings(**ALIBABA)
def test_a_config_error_does_not_interpolate_the_whole_config():
    """Validation failures name the field, never the object."""
    with override_settings(ALIBABA_LLM_BASE_URL="https://evil.example.com/v1"):
        with pytest.raises(LLMConfigError) as caught:
            endpoint_config("alibaba")
    assert SENTINEL_KEY not in str(caught.value)
    assert "evil.example.com" in str(caught.value), "the operator cannot see what was wrong"


@override_settings(**ALIBABA)
def test_the_bearer_header_exists_but_never_reaches_a_comparison_diff():
    """The header must be built correctly AND a failing test that compares request
    objects must not print it. `_headers()` is the only place it exists."""
    client = OpenAICompatibleLLMClient(endpoint_config("alibaba"))
    headers = client._headers()
    assert headers["Authorization"] == f"Bearer {SENTINEL_KEY}"
    # The client itself is what an assertion diff would print.
    assert SENTINEL_KEY not in repr(client.config)


@override_settings(LLM_BACKEND="local")
def test_the_local_backend_sends_no_authorization_header():
    """A local server needs no credential, and sending one to a machine on
    localhost is how a key ends up in somebody's LM Studio log."""
    client = OpenAICompatibleLLMClient(endpoint_config("local"))
    assert "Authorization" not in client._headers()
    assert client.config.api_key == ""


def _body(payload: dict) -> object:
    """An `HTTPError` body must be a file-like object with `.read()`."""
    import io

    return io.BytesIO(json.dumps(payload).encode("utf-8"))
