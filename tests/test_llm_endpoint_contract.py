"""What may be reached, what may be sent there, and what happens when it fails.

The endpoint contract is a SECURITY boundary, not a convenience check. A base URL
that validates loosely is an outbound channel: it decides where a bearer token
and — once the adviser is wired — a student's question actually go.

Every hostname here is SYNTHETIC. The real workspace id is a live identifier and
belongs in the deployment's secret store, not in a test fixture that lives in git
forever.
"""

from __future__ import annotations

import io
import json
from urllib.error import HTTPError, URLError

import pytest
from django.test import override_settings

from core.services import llm_backend
from core.services.llm_backend import (
    LLMAuthenticationError,
    LLMBadRequest,
    LLMConfigError,
    LLMRateLimited,
    LLMTimeout,
    LLMUnavailable,
    OpenAICompatibleLLMClient,
    endpoint_config,
)

#: Correct SHAPE, invented value.
WORKSPACE = "ws-synthetic0000000"
REGION = "ap-southeast-1"
GOOD_URL = f"https://{WORKSPACE}.{REGION}.maas.aliyuncs.com/compatible-mode/v1"
KEY = "sk-ws-synthetic-key-value-for-tests-only"

ALIBABA = {
    "LLM_BACKEND": "alibaba",
    "ALIBABA_LLM_BASE_URL": GOOD_URL,
    "ALIBABA_LLM_API_KEY": KEY,
    "ALIBABA_LLM_MODEL": "qwen3.7-max",
}


def _ok(payload: dict, url: str = GOOD_URL):
    """A urlopen replacement returning one successful body."""

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def geturl(self):
            return url

        def read(self):
            return json.dumps(payload).encode("utf-8")

    def fake(request, timeout=None):  # noqa: ARG001
        return Response()

    return fake


ANSWER = {
    "model": "qwen3.7-max",
    "choices": [{"message": {"content": "نعم"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
}


# ── the URL contract ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "because"),
    [
        (f"http://{WORKSPACE}.{REGION}.maas.aliyuncs.com/compatible-mode/v1", "http downgrade"),
        (f"https://u:p@{WORKSPACE}.{REGION}.maas.aliyuncs.com/compatible-mode/v1", "userinfo"),
        (f"https://{WORKSPACE}.{REGION}.maas.aliyuncs.com:8443/compatible-mode/v1", "odd port"),
        (f"https://{WORKSPACE}.{REGION}.maas.aliyuncs.com/compatible-mode/v1?x=1", "query"),
        (f"https://{WORKSPACE}.{REGION}.maas.aliyuncs.com/compatible-mode/v1#f", "fragment"),
        (f"https://{WORKSPACE}.{REGION}.maas.aliyuncs.com/api/v1", "dashscope-native path"),
        (
            f"https://{WORKSPACE}.eu-west-1.maas.aliyuncs.com/compatible-mode/v1",
            "region not allowed",
        ),
        (
            f"https://{WORKSPACE}.{REGION}.maas.aliyuncs.com.evil.test/compatible-mode/v1",
            "suffix trick",
        ),
        (
            "https://oss-cn-hangzhou.aliyuncs.com/compatible-mode/v1",
            "aliyuncs but not Model Studio",
        ),
        ("https://evil.test/compatible-mode/v1", "not Alibaba at all"),
        (f"https://a.b.{WORKSPACE}.{REGION}.maas.aliyuncs.com/compatible-mode/v1", "extra labels"),
    ],
)
def test_the_endpoint_contract_refuses(url, because):
    """`*.aliyuncs.com` was the first check and it accepts nine of these eleven.
    Every clause closes one way for a request to leave for somewhere unintended —
    the `oss-` case is a real Alibaba host that is not Model Studio, and the
    suffix trick is a domain anyone can register."""
    with override_settings(**{**ALIBABA, "ALIBABA_LLM_BASE_URL": url}):
        with pytest.raises(LLMConfigError):
            endpoint_config("alibaba")


def test_the_expected_endpoint_is_accepted():
    with override_settings(**ALIBABA):
        config = endpoint_config("alibaba")
    assert config.region == REGION
    assert config.base_url == GOOD_URL


def test_a_trailing_slash_is_tolerated():
    """A URL copied out of a console often carries one; refusing it would be
    pedantry rather than safety."""
    with override_settings(**{**ALIBABA, "ALIBABA_LLM_BASE_URL": GOOD_URL + "/"}):
        assert endpoint_config("alibaba").base_url == GOOD_URL


# ── the key is opaque ────────────────────────────────────────────


@pytest.mark.parametrize(
    "key",
    [
        "sk-" + "a" * 32,  # the old short form
        "sk-ws-" + "b" * 110,  # the newer workspace form
        "x" * 8,  # no recognisable prefix at all
        "sk-with.dots_and-dashes-1234567890",
    ],
)
def test_any_non_empty_key_without_control_characters_is_accepted(key):
    """The key is OPAQUE.

    An earlier version of this work looked at a real 117-character key with dots
    and underscores, expected `sk-` + 32 hex, and called it suspicious. It
    authenticated perfectly — Model Studio's newer workspace keys are longer.
    Asserting a provider's credential format is a guess about someone else's
    roadmap that fails closed on THEIR next change."""
    with override_settings(**{**ALIBABA, "ALIBABA_LLM_API_KEY": key}):
        assert endpoint_config("alibaba").api_key == key


@pytest.mark.parametrize("key", ["sk-abc\ndef", "sk-abc\rdef", "sk-abc\x00def", "sk-abc\tdef"])
def test_a_key_with_control_characters_is_refused(key):
    """What can actually hurt: a newline in a credential becomes header injection
    the moment it is written into an HTTP request."""
    with override_settings(**{**ALIBABA, "ALIBABA_LLM_API_KEY": key}):
        with pytest.raises(LLMConfigError) as caught:
            endpoint_config("alibaba")
    assert key not in str(caught.value), "the refusal quoted the credential"


# ── redirects ────────────────────────────────────────────────────


def test_a_redirect_to_another_host_is_refused(monkeypatch):
    """Validating the URL we ASKED for does not constrain where we ended up.
    urllib follows redirects by default, and a 30x to another host would carry
    the bearer token there and hand back a response we would have parsed."""
    monkeypatch.setattr(
        llm_backend, "urlopen", _ok(ANSWER, url="https://evil.test/compatible-mode/v1")
    )
    with override_settings(**ALIBABA):
        client = OpenAICompatibleLLMClient(endpoint_config("alibaba"))
        with pytest.raises(LLMConfigError, match="redirected"):
            client.chat([{"role": "user", "content": "hi"}])


def test_a_redirect_within_the_same_host_is_fine(monkeypatch):
    """The check is on the destination, not on whether a redirect happened."""
    monkeypatch.setattr(llm_backend, "urlopen", _ok(ANSWER, url=GOOD_URL + "/chat/completions?x=1"))
    with override_settings(**ALIBABA):
        client = OpenAICompatibleLLMClient(endpoint_config("alibaba"))
        assert client.chat([{"role": "user", "content": "hi"}]).content == "نعم"


# ── the retry matrix ─────────────────────────────────────────────


def _failing(status: int, *, headers: dict | None = None, then: dict | None = None):
    """Fail with `status` until the attempts run out, or succeed on the 2nd try."""
    state = {"calls": 0}

    def fake(request, timeout=None):  # noqa: ARG001
        state["calls"] += 1
        if then is not None and state["calls"] > 1:
            return _ok(then)(request)
        raise HTTPError(
            GOOD_URL, status, "err", headers or {}, io.BytesIO(b'{"error":{"code":"boom"}}')
        )

    fake.state = state
    return fake


@pytest.mark.parametrize(
    ("status", "expected", "attempts"),
    [
        (400, LLMBadRequest, 1),
        (401, LLMAuthenticationError, 1),
        (403, LLMAuthenticationError, 1),
        (429, LLMRateLimited, 3),
        (500, LLMUnavailable, 3),
        (502, LLMUnavailable, 3),
        (503, LLMUnavailable, 3),
    ],
)
def test_the_retry_matrix(monkeypatch, status, expected, attempts):
    """400 and 401/403 are decisions, not weather: retrying them wastes a
    student's wait and, for a 429, deepens the hole. 5xx and 429 are bounded."""
    fake = _failing(status)
    monkeypatch.setattr(llm_backend, "urlopen", fake)
    monkeypatch.setattr(llm_backend.time, "sleep", lambda _: None)
    with override_settings(**ALIBABA):
        client = OpenAICompatibleLLMClient(endpoint_config("alibaba"))
        with pytest.raises(expected):
            client.chat([{"role": "user", "content": "hi"}])
    assert fake.state["calls"] == attempts, f"HTTP {status} made {fake.state['calls']} attempts"


def test_a_timeout_is_retried_then_typed(monkeypatch):
    state = {"calls": 0}

    def fake(request, timeout=None):  # noqa: ARG001
        state["calls"] += 1
        raise TimeoutError

    monkeypatch.setattr(llm_backend, "urlopen", fake)
    monkeypatch.setattr(llm_backend.time, "sleep", lambda _: None)
    with override_settings(**ALIBABA):
        client = OpenAICompatibleLLMClient(endpoint_config("alibaba"))
        with pytest.raises(LLMTimeout):
            client.chat([{"role": "user", "content": "hi"}])
    assert state["calls"] == 3


def test_a_retry_that_succeeds_returns_the_answer(monkeypatch):
    fake = _failing(503, then=ANSWER)
    monkeypatch.setattr(llm_backend, "urlopen", fake)
    monkeypatch.setattr(llm_backend.time, "sleep", lambda _: None)
    with override_settings(**ALIBABA):
        client = OpenAICompatibleLLMClient(endpoint_config("alibaba"))
        assert client.chat([{"role": "user", "content": "hi"}]).content == "نعم"
    assert fake.state["calls"] == 2


def test_a_sane_retry_after_is_honoured(monkeypatch):
    slept: list[float] = []
    fake = _failing(429, headers={"Retry-After": "2"}, then=ANSWER)
    monkeypatch.setattr(llm_backend, "urlopen", fake)
    monkeypatch.setattr(llm_backend.time, "sleep", slept.append)
    with override_settings(**ALIBABA):
        OpenAICompatibleLLMClient(endpoint_config("alibaba")).chat(
            [{"role": "user", "content": "hi"}]
        )
    assert slept == [2.0]


@pytest.mark.parametrize("value", ["3600", "-1", "Wed, 21 Oct 2015 07:28:00 GMT", "banana"])
def test_an_unusable_retry_after_falls_back_to_the_bounded_schedule(monkeypatch, value):
    """A provider may send anything. An hour parks a student's question; a date
    string is unparseable. Honour it only when it is a small sane number."""
    slept: list[float] = []
    fake = _failing(429, headers={"Retry-After": value}, then=ANSWER)
    monkeypatch.setattr(llm_backend, "urlopen", fake)
    monkeypatch.setattr(llm_backend.time, "sleep", slept.append)
    with override_settings(**ALIBABA):
        OpenAICompatibleLLMClient(endpoint_config("alibaba")).chat(
            [{"role": "user", "content": "hi"}]
        )
    assert slept == [0.5]


def test_the_local_backend_does_not_retry(monkeypatch):
    """Local retried nothing before this refactor. Adding retries there would be a
    behaviour change smuggled in under a provider addition."""
    state = {"calls": 0}

    def fake(request, timeout=None):  # noqa: ARG001
        state["calls"] += 1
        raise URLError("refused")

    monkeypatch.setattr(llm_backend, "urlopen", fake)
    with override_settings(LLM_BACKEND="local"):
        client = OpenAICompatibleLLMClient(endpoint_config("local"))
        with pytest.raises(LLMUnavailable):
            client.chat([{"role": "user", "content": "hi"}])
    assert state["calls"] == 1


# ── diagnostics expose no host ───────────────────────────────────


def test_health_reports_a_region_and_never_a_hostname():
    """The first label of a Model Studio host IS the workspace identifier, so
    "hostname only" — which sounded like the safe half of a URL — discloses the
    very thing the rule was written to protect."""
    from core.services.llm_backend import check_llm_health

    with override_settings(**ALIBABA):
        health = check_llm_health("alibaba")
    body = json.dumps(health, ensure_ascii=False)

    assert health["region"] == REGION
    assert "endpoint_host" not in health
    assert WORKSPACE not in body, "the workspace identifier reached the health payload"
    assert "maas.aliyuncs.com" not in body
    assert KEY not in body
    assert health["provider"] == "alibaba-model-studio"
    assert health["model"] == "qwen3.7-max"


def test_a_retry_log_names_the_region_not_the_host(monkeypatch, caplog):
    """A retry storm is exactly when logs get shipped somewhere central."""
    import logging

    fake = _failing(503)
    monkeypatch.setattr(llm_backend, "urlopen", fake)
    monkeypatch.setattr(llm_backend.time, "sleep", lambda _: None)
    with override_settings(**ALIBABA), caplog.at_level(logging.WARNING):
        client = OpenAICompatibleLLMClient(endpoint_config("alibaba"))
        with pytest.raises(LLMUnavailable):
            client.chat([{"role": "user", "content": "hi"}])

    assert "region=ap-southeast-1" in caplog.text
    assert WORKSPACE not in caplog.text
    assert KEY not in caplog.text
