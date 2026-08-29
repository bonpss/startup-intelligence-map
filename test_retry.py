import json

import httpx
import pytest
from mistralai.client.errors.sdkerror import SDKError

from retry import build_retry, is_mistral_retryable, log_retry_attempt


# ── is_mistral_retryable ───────────────────────────────────────────────────────

def test_is_mistral_retryable_accepts_httpx_transport_error():
    assert is_mistral_retryable(httpx.ReadTimeout("timed out"))


def test_is_mistral_retryable_accepts_json_decode_error():
    try:
        json.loads("not json")
    except json.JSONDecodeError as exc:
        assert is_mistral_retryable(exc)
    else:
        pytest.fail("expected JSONDecodeError")


def _sdk_error(status_code: int) -> SDKError:
    resp = httpx.Response(status_code, request=httpx.Request("POST", "https://api.mistral.ai/v1/chat/completions"))
    return SDKError("API error occurred", resp)


@pytest.mark.parametrize("code", [429, 503, 529])
def test_is_mistral_retryable_accepts_specific_sdk_error_codes(code):
    assert is_mistral_retryable(_sdk_error(code))


def test_is_mistral_retryable_rejects_other_sdk_error_codes():
    assert not is_mistral_retryable(_sdk_error(400))


def test_is_mistral_retryable_rejects_unrelated_exception():
    assert not is_mistral_retryable(ValueError("not retryable"))


# ── build_retry ────────────────────────────────────────────────────────────────

def test_build_retry_bounded_stops_after_given_attempts():
    calls = {"n": 0}

    @build_retry(is_mistral_retryable, wait_multiplier=0, wait_min=0, wait_max=0, stop_attempts=3)
    def always_fails():
        calls["n"] += 1
        raise httpx.ReadTimeout("timed out")

    with pytest.raises(httpx.ReadTimeout):
        always_fails()
    assert calls["n"] == 3


def test_build_retry_unbounded_has_no_stop_condition():
    decorator = build_retry(is_mistral_retryable, wait_multiplier=0, wait_min=0, wait_max=0, stop_attempts=None)

    @decorator
    def f():
        return "ok"

    assert f.retry.stop(None) is False  # never signals "stop" -- unbounded


def test_build_retry_does_not_retry_non_matching_exception():
    calls = {"n": 0}

    @build_retry(is_mistral_retryable, wait_multiplier=0, wait_min=0, wait_max=0, stop_attempts=5)
    def fails_with_value_error():
        calls["n"] += 1
        raise ValueError("not retryable")

    with pytest.raises(ValueError):
        fails_with_value_error()
    assert calls["n"] == 1


# ── log_retry_attempt ────────────────────────────────────────────────────────
# No mocking convention exists in this project -- a minimal fake retry_state
# with the two attributes log_retry_attempt actually reads is enough here.

class _FakeOutcome:
    def __init__(self, exc: BaseException):
        self._exc = exc

    def exception(self):
        return self._exc


class _FakeRetryState:
    def __init__(self, attempt_number: int, exc: BaseException):
        self.attempt_number = attempt_number
        self.outcome = _FakeOutcome(exc)


def test_log_retry_attempt_prints_attempt_number_and_exception(capsys):
    log_retry_attempt(_FakeRetryState(2, ValueError("boom")))
    captured = capsys.readouterr()
    assert "attempt 2" in captured.err
    assert "ValueError" in captured.err
    assert "boom" in captured.err
