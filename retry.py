import json
import sys

import httpx
from mistralai.client.errors.sdkerror import SDKError
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential


def is_mistral_retryable(exc: BaseException) -> bool:
    """Shared retryable-error predicate for every Mistral-backed retry
    (competitor.py, extractor.py, competitor_validator.py). Story 5.3, AD-8 --
    one canonical definition instead of three independently-drifting copies
    (competitor_validator.py's own copy was missing the json.JSONDecodeError
    branch the other two already had).
    """
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, json.JSONDecodeError):
        return True
    return isinstance(exc, SDKError) and any(code in str(exc) for code in ("429", "503", "529"))


def build_retry(
    predicate,
    *,
    wait_multiplier: float,
    wait_min: float,
    wait_max: float,
    stop_attempts: int | None,
    reraise: bool = True,
    before_sleep=None,
):
    """Construct a tenacity retry decorator from its wait/stop numbers. Story 5.3,
    AD-8 -- factors the *construction pattern* shared by storage.py's postgrest
    retry and the three Mistral-backed retries, not a single shared policy value:
    each call site still supplies its own predicate and numbers, since the
    failure classes and backoff budgets genuinely differ per site.

    stop_attempts=None means unbounded -- tenacity's own behavior when no stop=
    is passed -- making an unbounded retry an explicit, visible parameter at the
    call site instead of a silent omission.
    """
    kwargs = dict(
        retry=retry_if_exception(predicate),
        wait=wait_exponential(multiplier=wait_multiplier, min=wait_min, max=wait_max),
        reraise=reraise,
    )
    if stop_attempts is not None:
        kwargs["stop"] = stop_after_attempt(stop_attempts)
    if before_sleep is not None:
        kwargs["before_sleep"] = before_sleep
    return retry(**kwargs)


def log_retry_attempt(retry_state) -> None:
    """tenacity before_sleep hook for competitor.py's and extractor.py's
    Mistral retry decorators (passed as build_retry(..., before_sleep=...)).
    Without this, a run stuck retrying a slow/503 Mistral looks identical to a
    hung process for minutes at a time -- print each failed attempt so it's
    visible instead of silent. Lives here, not storage.py, since it's a
    Mistral-retry concern like the rest of this module (Story 5.3 review).
    """
    exc = retry_state.outcome.exception()
    print(
        f"  [Mistral] attempt {retry_state.attempt_number} failed "
        f"({type(exc).__name__}: {exc}) -- retrying...",
        file=sys.stderr,
    )
