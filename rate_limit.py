"""Tiny in-process sliding-window rate limiter -- backs /api/login's
per-IP and per-(email+IP) throttling (graph_app.py). In-memory, not Redis/
DB-backed: matches this project's existing in-process-state convention for
demo/solo-tool scale (main.py's _domain_locks, graph_app.py's
_ingestion_queue/_tracked_ingestion_ids) rather than adding new
infrastructure for a single-process app. Resets on process restart --
acceptable for a login-attempt throttle, unlike the ingestion daily quota
(auth.ingestion_quota_reached), which is backed by the DB precisely because
it must survive a restart.
"""

import time
from collections import deque


class SlidingWindowRateLimiter:
    """Per-key sliding window: at most `max_calls` calls per `window_seconds`
    for a given key. Each distinct key gets its own deque of call
    timestamps, trimmed to the window on every access.

    Two ways to use a key:
      - allow(key): check-and-record in one call -- the key is charged for
        every access regardless of outcome. Use this when every attempt
        (successful or not) should count, e.g. a plain per-IP login throttle.
      - check(key) / record(key): split apart -- check() is read-only (peek
        at whether the key is currently within budget, without charging it),
        record() unconditionally charges it. Use this when only some calls
        should count, e.g. a per-account login throttle that must only count
        *failed* attempts (counting successes too would let a legitimate
        user's own repeated logins burn their own budget, and worse, counting
        *unauthenticated* attempts against an email-only key would let an
        attacker lock a victim out just by submitting wrong passwords for
        their address -- see graph_app.py's api_login for how this is used).

    Bounded memory: every access opportunistically evicts keys whose window
    has gone fully idle (via _evict_stale), and a hard cap (max_keys) drops
    the least-recently-touched key if the table is still oversized after
    that -- without this, an attacker cycling through distinct emails/IPs
    (each one only ever hit once or twice) could otherwise grow this
    structure without bound, since a low-traffic key's single leftover
    timestamp doesn't get trimmed by the deque's own cutoff logic until
    something touches that exact key again.
    """

    # Bounds worst-case memory at a few hundred KB even under sustained
    # abuse across many distinct keys -- generous for this app's real
    # traffic (a handful of concurrent users), a hard backstop against a
    # single attacker inflating the table with disposable keys.
    _MAX_KEYS = 10_000
    # How many accesses between opportunistic sweeps for keys whose entire
    # window has expired -- amortizes the O(n) scan instead of paying it on
    # every single call.
    _EVICT_EVERY = 500

    def __init__(self, max_calls: int, window_seconds: float, max_keys: int | None = None):
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self.max_keys = max_keys if max_keys is not None else self._MAX_KEYS
        self._hits: dict[str, deque[float]] = {}
        self._access_count = 0

    def _trim(self, key: str) -> tuple[deque[float], float]:
        now = time.monotonic()
        hits = self._hits.setdefault(key, deque())
        cutoff = now - self.window_seconds
        while hits and hits[0] < cutoff:
            hits.popleft()
        return hits, now

    def _evict_stale(self) -> None:
        """Drop every key whose deque emptied out (all hits aged past the
        window). Only the CURRENT key gets trimmed by _trim() on each call --
        every OTHER idle key keeps its now-stale, already-emptied-by-nothing
        deque sitting in the dict forever unless something sweeps it. Run
        periodically (every _EVICT_EVERY accesses), not on every call, since
        it's an O(n) scan over every known key.
        """
        now = time.monotonic()
        cutoff = now - self.window_seconds
        stale = [k for k, hits in self._hits.items() if not hits or hits[-1] < cutoff]
        for k in stale:
            del self._hits[k]

    def _enforce_hard_cap(self) -> None:
        """Backstop for the case _evict_stale() can't help with: a burst of
        distinct keys all created within the SAME window, so none of them
        look stale yet. Drops the least-recently-touched keys (oldest last
        timestamp first) until back under max_keys.
        """
        if len(self._hits) <= self.max_keys:
            return
        by_recency = sorted(self._hits.items(), key=lambda kv: kv[1][-1] if kv[1] else 0.0)
        overflow = len(self._hits) - self.max_keys
        for k, _ in by_recency[:overflow]:
            del self._hits[k]

    def _maybe_evict(self) -> None:
        self._access_count += 1
        if self._access_count % self._EVICT_EVERY == 0:
            self._evict_stale()
        if len(self._hits) > self.max_keys:
            self._enforce_hard_cap()

    def check(self, key: str) -> bool:
        """Read-only: True if `key` is currently within budget. Does NOT
        record a hit -- pair with record() for a "only count some calls"
        limiter (see class docstring).
        """
        hits, _ = self._trim(key)
        allowed = len(hits) < self.max_calls
        self._maybe_evict()
        return allowed

    def record(self, key: str) -> None:
        """Unconditionally charge one hit against `key`."""
        hits, now = self._trim(key)
        hits.append(now)
        self._maybe_evict()

    def allow(self, key: str) -> bool:
        """Check-and-record in one call: charges `key` regardless of the
        result. Use when every attempt (not just some subset) should count.
        """
        hits, now = self._trim(key)
        allowed = len(hits) < self.max_calls
        hits.append(now)
        self._maybe_evict()
        return allowed
