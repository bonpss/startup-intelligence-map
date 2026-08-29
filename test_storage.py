import httpx
import pytest
from postgrest.exceptions import APIError

import storage
from storage import normalize_domain, _normalize_subject, _validate_review, get_quality_reviews


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


# ── _normalize_subject: shared read/write contract (Story 5.3) ───────────────
# Regression coverage for the bug where get_quality_reviews() ran
# "redundant_uncategorized_cleanup"/"empty_subsectors_backfill" subjects through
# normalize_domain() (the write path's _validate_review() already special-cased
# them) -- a startup name like "Maisa" isn't a domain and would normalize to "".

def test_normalize_subject_leaves_redundant_uncategorized_cleanup_subject_unmangled():
    assert _normalize_subject("redundant_uncategorized_cleanup", "Maisa") == "Maisa"


def test_normalize_subject_leaves_empty_subsectors_backfill_subject_unmangled():
    assert _normalize_subject("empty_subsectors_backfill", "Maisa") == "Maisa"


def _raise_reached_db():
    raise RuntimeError("reached DB layer")


@pytest.mark.parametrize("review_type", ["redundant_uncategorized_cleanup", "empty_subsectors_backfill"])
def test_get_quality_reviews_accepts_startup_name_subjects_without_db_io(monkeypatch, review_type):
    # No live Supabase call and no swallowed exceptions (code-review finding,
    # Story 5.3): _client() is monkeypatched to raise a distinctive sentinel
    # right where the DB call would start, so this asserts precisely that
    # validation passed -- any other exception (e.g. a regression in
    # _normalize_subject) fails this test loudly instead of being masked.
    monkeypatch.setattr(storage, "_client", _raise_reached_db)
    with pytest.raises(RuntimeError, match="reached DB layer"):
        get_quality_reviews(review_type, subject="Maisa")


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


# ── ingestion_queue: list_ingestions / enqueue_ingestion / _set_ingestion_status
# Code review on story 6-2-en-attente-tab-with-per-row-status-badge (2026-08-29)
# added pytest coverage for the three bugs it found and fixed there. Same
# minimal-fake convention as above -- these fakes implement just enough of the
# postgrest chain (select/eq/in_/order/limit/insert/update/execute) for the
# functions under test to run against, with real filtering/sorting logic so a
# wrong column/operator in storage.py would actually fail the test.

class _FakeIngestionSelectQuery:
    def __init__(self, rows):
        self.request = type("FakeRequest", (), {"http_method": "GET"})()
        self._rows = list(rows)

    def select(self, *a, **k):
        return self

    def eq(self, col, val):
        self._rows = [r for r in self._rows if r.get(col) == val]
        return self

    def in_(self, col, vals):
        self._rows = [r for r in self._rows if r.get(col) in vals]
        return self

    def order(self, col, desc=False):
        self._rows = sorted(self._rows, key=lambda r: r[col], reverse=desc)
        return self

    def limit(self, n):
        self._rows = self._rows[:n]
        return self

    def execute(self):
        return type("FakeResponse", (), {"data": self._rows})()


def test_list_ingestions_orders_desc_by_created_at_and_respects_limit(monkeypatch):
    rows = [
        {"id": 1, "created_at": "2026-08-29T10:00:00+00:00"},
        {"id": 2, "created_at": "2026-08-29T10:02:00+00:00"},
        {"id": 3, "created_at": "2026-08-29T10:01:00+00:00"},
    ]
    fake_table = type("FakeTable", (), {"select": lambda self, *a, **k: _FakeIngestionSelectQuery(rows)})()
    fake_client = type("FakeClient", (), {"table": lambda self, name: fake_table})()
    monkeypatch.setattr(storage, "_client", lambda: fake_client)

    assert [r["id"] for r in storage.list_ingestions()] == [2, 3, 1]
    assert [r["id"] for r in storage.list_ingestions(limit=2)] == [2, 3]


def test_enqueue_ingestion_reuses_existing_active_row_without_inserting(monkeypatch):
    existing_row = {"id": 5, "url": "https://a.com", "domain": "a.com", "status": "queued"}

    def fail_if_called(*a, **k):
        raise AssertionError("insert should not be called when an active row already exists")

    fake_table = type("FakeTable", (), {
        "select": lambda self, *a, **k: _FakeIngestionSelectQuery([existing_row]),
        "insert": fail_if_called,
    })()
    fake_client = type("FakeClient", (), {"table": lambda self, name: fake_table})()
    monkeypatch.setattr(storage, "_client", lambda: fake_client)

    row, is_new = storage.enqueue_ingestion("https://a.com")
    assert row == existing_row
    assert is_new is False


def test_enqueue_ingestion_dedups_on_normalized_domain_not_raw_url(monkeypatch):
    """Code review (2026-08-29 #2): "acme.com", "https://acme.com/", and
    "http://acme.com" are the same startup and must all reuse the same row --
    dedup keys on normalize_domain(url), not an exact match of the url string.
    """
    existing_row = {"id": 6, "url": "https://acme.com/", "domain": "acme.com", "status": "queued"}

    def fail_if_called(*a, **k):
        raise AssertionError("insert should not be called when an active row for this domain already exists")

    fake_table = type("FakeTable", (), {
        "select": lambda self, *a, **k: _FakeIngestionSelectQuery([existing_row]),
        "insert": fail_if_called,
    })()
    fake_client = type("FakeClient", (), {"table": lambda self, name: fake_table})()
    monkeypatch.setattr(storage, "_client", lambda: fake_client)

    row, is_new = storage.enqueue_ingestion("http://acme.com")
    assert row == existing_row
    assert is_new is False


def test_enqueue_ingestion_reuses_row_on_concurrent_unique_violation(monkeypatch):
    """migrations/007's unique partial index on (domain) where status in
    ('queued','processing') closes enqueue_ingestion's check-then-act race
    (code review, 2026-08-29). This simulates the race: this call's own SELECT
    finds nothing (the other concurrent call hasn't committed yet), but its
    INSERT loses to the index and gets back a 23505 -- it must then re-fetch
    and reuse the row the other call just inserted, not raise or duplicate it.
    """
    winner_row = {"id": 9, "url": "https://race.com", "domain": "race.com", "status": "queued"}
    selects = iter([[], [winner_row]])

    class FakeTable:
        def select(self, *a, **k):
            return _FakeIngestionSelectQuery(next(selects))

        def insert(self, values):
            raise APIError({"message": "duplicate key value violates unique constraint", "code": "23505"})

    fake_client = type("FakeClient", (), {"table": lambda self, name: FakeTable()})()
    monkeypatch.setattr(storage, "_client", lambda: fake_client)

    row, is_new = storage.enqueue_ingestion("https://race.com")
    assert row == winner_row
    assert is_new is False


def test_enqueue_ingestion_reraises_non_unique_violation_api_errors(monkeypatch):
    class FakeTable:
        def select(self, *a, **k):
            return _FakeIngestionSelectQuery([])

        def insert(self, values):
            raise APIError({"message": "some other db error", "code": "42601"})

    fake_client = type("FakeClient", (), {"table": lambda self, name: FakeTable()})()
    monkeypatch.setattr(storage, "_client", lambda: fake_client)

    with pytest.raises(APIError):
        storage.enqueue_ingestion("https://boom.com")


def test_enqueue_ingestion_rejects_url_with_no_extractable_domain(monkeypatch):
    """Code review (2026-08-29): a host-less/malformed url (e.g. "https://")
    normalizes to an empty domain -- must be rejected, not silently collapsed
    onto a shared domain="" row with every other malformed submission. Raised
    before any DB call, so _client isn't even monkeypatched here."""
    with pytest.raises(ValueError):
        storage.enqueue_ingestion("https://")


class _FakeIngestionUpdateQuery:
    """Same http_method+fail_times shape as _FakeQuery above, but returns a
    postgrest-shaped response (.data) since _set_ingestion_status reads it,
    instead of the bare string the generic _execute tests use."""
    def __init__(self, fail_times: int = 0, data=None):
        self.request = type("FakeRequest", (), {"http_method": "PATCH"})()
        self._fail_times = fail_times
        self.calls = 0
        self._data = data if data is not None else [{"id": 1}]

    def eq(self, *a, **k):
        return self

    def execute(self):
        self.calls += 1
        if self.calls <= self._fail_times:
            raise httpx.RemoteProtocolError("dropped stream")
        return type("FakeResponse", (), {"data": self._data})()


def test_set_ingestion_status_retries_transient_error_unlike_a_generic_patch(monkeypatch):
    """Code review (2026-08-29): unlike a generic write, this update is safe to
    retry (keyed by row_id, reapplies the same values), so it goes through
    _execute_retryable instead of _execute's write-skips-retry default --
    without this, mark_done/mark_error failing transiently after
    ingest_startup succeeded left the row stuck at 'processing' forever.
    """
    query = _FakeIngestionUpdateQuery(fail_times=2)
    fake_table = type("FakeTable", (), {"update": lambda self, values: query})()
    fake_client = type("FakeClient", (), {"table": lambda self, name: fake_table})()
    monkeypatch.setattr(storage, "_client", lambda: fake_client)

    storage._set_ingestion_status(1, "done", result={"name": "X"})
    assert query.calls == 3


# ── retry_ingestion (Story 6-3-manual-retry-on-failure) ───────────────────────

def test_retry_ingestion_resets_error_row_to_queued(monkeypatch):
    calls = {}

    class Query:
        def __init__(self):
            self.request = type("FakeRequest", (), {"http_method": "PATCH"})()

        def eq(self, col, val):
            calls.setdefault("filters", []).append((col, val))
            return self

        def execute(self):
            return type("FakeResponse", (), {"data": [{"id": 7, "url": "https://retry.com", "status": "queued"}]})()

    class FakeTable:
        def update(self, values):
            calls["update_values"] = values
            return Query()

    fake_client = type("FakeClient", (), {"table": lambda self, name: FakeTable()})()
    monkeypatch.setattr(storage, "_client", lambda: fake_client)

    row = storage.retry_ingestion(7)

    assert calls["update_values"]["status"] == "queued"
    assert calls["update_values"]["error_message"] is None
    assert ("id", 7) in calls["filters"]
    assert ("status", "error") in calls["filters"]
    assert row == {"id": 7, "url": "https://retry.com", "status": "queued"}


def test_retry_ingestion_does_not_retry_transient_error(monkeypatch):
    """Code review (2026-08-29): the WHERE status='error' clause is state-
    dependent -- retrying after an in-doubt write (server commits, response
    lost) would re-check the now-false precondition against the row's new
    'queued' status, match zero rows, and raise a false 'not found' even
    though the retry actually succeeded. Must go through the unretried
    _execute(), not _execute_retryable, so a transient error surfaces as
    itself instead of a misleading ValueError."""
    query = _FakeIngestionUpdateQuery(fail_times=2, data=[{"id": 7, "url": "https://retry.com"}])
    fake_table = type("FakeTable", (), {"update": lambda self, values: query})()
    fake_client = type("FakeClient", (), {"table": lambda self, name: fake_table})()
    monkeypatch.setattr(storage, "_client", lambda: fake_client)

    with pytest.raises(httpx.RemoteProtocolError):
        storage.retry_ingestion(7)
    assert query.calls == 1


def test_retry_ingestion_raises_when_no_row_matches(monkeypatch):
    """Zero rows matched covers both 'no such id' and 'not currently error' --
    the caller (the retry endpoint) turns either case into a 404."""
    class Query:
        def __init__(self):
            self.request = type("FakeRequest", (), {"http_method": "PATCH"})()

        def eq(self, *a, **k):
            return self

        def execute(self):
            return type("FakeResponse", (), {"data": []})()

    fake_table = type("FakeTable", (), {"update": lambda self, values: Query()})()
    fake_client = type("FakeClient", (), {"table": lambda self, name: fake_table})()
    monkeypatch.setattr(storage, "_client", lambda: fake_client)

    with pytest.raises(ValueError):
        storage.retry_ingestion(99)


def test_retry_ingestion_propagates_unique_violation_api_errors(monkeypatch):
    """migrations/007's unique partial index can reject this UPDATE if a fresh
    queued/processing row for the same domain already exists. retry_ingestion()
    must not swallow it -- the endpoint decides it's a 409, not a 404/502."""
    class Query:
        def __init__(self):
            self.request = type("FakeRequest", (), {"http_method": "PATCH"})()

        def eq(self, *a, **k):
            return self

        def execute(self):
            raise APIError({"message": "duplicate key value violates unique constraint", "code": "23505"})

    fake_table = type("FakeTable", (), {"update": lambda self, values: Query()})()
    fake_client = type("FakeClient", (), {"table": lambda self, name: fake_table})()
    monkeypatch.setattr(storage, "_client", lambda: fake_client)

    with pytest.raises(APIError):
        storage.retry_ingestion(5)


# ── mark_done_rows_seen / get_ingestion_summary (Story 6-4-two-dot-notification-on-the-tab)

def test_mark_done_rows_seen_filters_status_done_and_seen_false(monkeypatch):
    calls = {}

    class Query:
        def __init__(self):
            self.request = type("FakeRequest", (), {"http_method": "PATCH"})()

        def eq(self, col, val):
            calls.setdefault("filters", []).append((col, val))
            return self

        def execute(self):
            return type("FakeResponse", (), {"data": [{"id": 1}, {"id": 2}]})()

    class FakeTable:
        def update(self, values):
            calls["update_values"] = values
            return Query()

    fake_client = type("FakeClient", (), {"table": lambda self, name: FakeTable()})()
    monkeypatch.setattr(storage, "_client", lambda: fake_client)

    count = storage.mark_done_rows_seen()

    assert calls["update_values"] == {"seen": True}
    assert ("status", "done") in calls["filters"]
    assert ("seen", False) in calls["filters"]
    assert count == 2


def test_mark_done_rows_seen_does_not_retry_transient_error(monkeypatch):
    """Code review (2026-08-29): unlike _set_ingestion_status()'s id-keyed
    update, this one filters on seen=false -- state-dependent, so a retry
    after an in-doubt write (server commits, response lost) would silently
    under-count instead of raising. Must go through the unretried _execute(),
    not _execute_retryable, so a transient error surfaces as an exception
    instead of a wrong count."""
    query = _FakeIngestionUpdateQuery(fail_times=2, data=[{"id": 1}])
    fake_table = type("FakeTable", (), {"update": lambda self, values: query})()
    fake_client = type("FakeClient", (), {"table": lambda self, name: fake_table})()
    monkeypatch.setattr(storage, "_client", lambda: fake_client)

    with pytest.raises(httpx.RemoteProtocolError):
        storage.mark_done_rows_seen()
    assert query.calls == 1


def test_get_ingestion_summary_returns_error_and_unseen_counts(monkeypatch):
    counts = iter([3, 5])  # error_count query first, then unseen_done_count query

    class Query:
        def __init__(self, count):
            self._count = count
            self.request = type("FakeRequest", (), {"http_method": "HEAD"})()

        def eq(self, *a, **k):
            return self

        def execute(self):
            return type("FakeResponse", (), {"data": [], "count": self._count})()

    class FakeTable:
        def select(self, *a, **k):
            assert k.get("count") == "exact"
            assert k.get("head") is True
            return Query(next(counts))

    fake_client = type("FakeClient", (), {"table": lambda self, name: FakeTable()})()
    monkeypatch.setattr(storage, "_client", lambda: fake_client)

    summary = storage.get_ingestion_summary()
    assert summary == {"error_count": 3, "unseen_done_count": 5}
