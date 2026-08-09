import httpx
import pytest

import storage
from storage import normalize_domain, _validate_review, get_quality_reviews


# ── normalize_domain ──────────────────────────────────────────────────────────

def test_normalize_domain_strips_scheme_www_and_trailing_slash():
    assert normalize_domain("https://Example.com/") == "example.com"
    assert normalize_domain("http://www.example.com") == "example.com"
    assert normalize_domain("example.com") == "example.com"


def test_normalize_domain_lowercases():
    assert normalize_domain("HTTPS://WWW.EXAMPLE.COM/") == "example.com"


def test_normalize_domain_strips_path_query_and_port():
    assert normalize_domain("https://example.com/about") == "example.com"
    assert normalize_domain("https://example.com/about/team/") == "example.com"
    assert normalize_domain("https://example.com:8080") == "example.com"
    assert normalize_domain("https://example.com/search?q=1") == "example.com"


def test_normalize_domain_preserves_subdomains():
    assert normalize_domain("https://sub.example.com/") == "sub.example.com"


def test_normalize_domain_accepts_bare_domain_without_scheme():
    assert normalize_domain("example.com/about") == "example.com"


def test_normalize_domain_handles_bare_host_port_without_scheme():
    assert normalize_domain("example.com:8080") == "example.com"
    assert normalize_domain("example.com:8080/path") == "example.com"


def test_normalize_domain_strips_userinfo():
    assert normalize_domain("http://user:pass@example.com/path") == "example.com"


def test_normalize_domain_handles_ipv6_literal():
    assert normalize_domain("http://[::1]:8080/") == "::1"


def test_normalize_domain_handles_protocol_relative_url():
    assert normalize_domain("//example.com/path") == "example.com"
    assert normalize_domain("//www.example.com") == "example.com"


def test_normalize_domain_strips_trailing_dot_fqdn():
    assert normalize_domain("example.com.") == "example.com"
    assert normalize_domain("https://example.com./") == "example.com"


# ── _validate_review: review_type ─────────────────────────────────────────────

def test_validate_review_rejects_unknown_review_type():
    with pytest.raises(ValueError):
        _validate_review("not_a_real_type", "AI & Machine Learning", "ambiguous")


def test_validate_review_accepts_taxonomy_split():
    subject = _validate_review("taxonomy_split", "IAM / PAM", "ambiguous")
    assert subject == "IAM / PAM"


def test_validate_review_rejects_sector_name_as_subject():
    # "AI & Machine Learning" is a SECTOR, not a subsector -- taxonomy_split
    # subjects must be exact subsector names (AD-7). Regression test for the
    # bug where subject was checked against TAXONOMY's top-level (sector) keys.
    with pytest.raises(ValueError):
        _validate_review("taxonomy_split", "AI & Machine Learning", "ambiguous")


def test_validate_review_rejects_blank_verdict():
    with pytest.raises(ValueError):
        _validate_review("taxonomy_split", "IAM / PAM", "")
    with pytest.raises(ValueError):
        _validate_review("scraping_diagnostic", "example.com", "   ")


def test_validate_review_rejects_scraping_diagnostic_subject_that_normalizes_empty():
    with pytest.raises(ValueError):
        _validate_review("scraping_diagnostic", "https:///path", "blocking page")


def test_validate_review_accepts_scraping_diagnostic():
    subject = _validate_review("scraping_diagnostic", "https://Example.com/", "blocking page")
    assert subject == "example.com"


# ── _validate_review: taxonomy_split subject/verdict contract ────────────────

def test_validate_review_taxonomy_split_rejects_non_taxonomy_subject():
    with pytest.raises(ValueError):
        _validate_review("taxonomy_split", "Not A Real Sector", "ambiguous")


def test_validate_review_taxonomy_split_rejects_invalid_verdict():
    with pytest.raises(ValueError):
        _validate_review("taxonomy_split", "IAM / PAM", "not a real verdict")


@pytest.mark.parametrize(
    "verdict", ["isolated mis-tag", "structural gap", "scraping artifact", "ambiguous"]
)
def test_validate_review_taxonomy_split_accepts_all_four_verdicts(verdict):
    assert _validate_review("taxonomy_split", "IAM / PAM", verdict) == "IAM / PAM"


# ── _validate_review: scraping_diagnostic has no verdict constraint ──────────

def test_validate_review_scraping_diagnostic_accepts_any_verdict_text():
    subject = _validate_review("scraping_diagnostic", "example.com", "anything goes here")
    assert subject == "example.com"


# ── get_quality_reviews: validation raises before any DB call ────────────────

def test_get_quality_reviews_rejects_unknown_review_type():
    with pytest.raises(ValueError):
        get_quality_reviews("not_a_real_type")


def test_get_quality_reviews_rejects_sector_name_as_taxonomy_split_subject():
    with pytest.raises(ValueError):
        get_quality_reviews("taxonomy_split", subject="AI & Machine Learning")


def test_get_quality_reviews_rejects_scraping_diagnostic_subject_that_normalizes_empty():
    with pytest.raises(ValueError):
        get_quality_reviews("scraping_diagnostic", subject="https:///path")


# ── _execute: retry scoped to idempotent (GET/HEAD) requests only ────────────
# No mocking convention exists in this project (see Story 1.2's Testability
# Design note) -- a minimal fake query object with the one attribute/method
# _execute() actually touches (request.http_method, execute()) is enough here.

class _FakeQuery:
    def __init__(self, http_method: str, fail_times: int = 0):
        self.request = type("FakeRequest", (), {"http_method": http_method})()
        self._fail_times = fail_times
        self.calls = 0

    def execute(self):
        self.calls += 1
        if self.calls <= self._fail_times:
            raise httpx.RemoteProtocolError("dropped stream")
        return "ok"


def test_execute_retries_get_on_transient_error():
    query = _FakeQuery("GET", fail_times=2)
    assert storage._execute(query) == "ok"
    assert query.calls == 3


def test_execute_does_not_retry_post_on_transient_error():
    query = _FakeQuery("POST", fail_times=1)
    with pytest.raises(httpx.RemoteProtocolError):
        storage._execute(query)
    assert query.calls == 1


def test_execute_does_not_retry_patch_on_transient_error():
    query = _FakeQuery("PATCH", fail_times=1)
    with pytest.raises(httpx.RemoteProtocolError):
        storage._execute(query)
    assert query.calls == 1
