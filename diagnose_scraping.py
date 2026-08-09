# python diagnose_scraping.py

"""Read-only scraping-heterogeneity diagnostic (FR-1).

Samples sites already present in compspro, re-runs scrape() on each, and
characterizes the result. Findings are written directly to quality_review_log
(review_type='scraping_diagnostic') -- never to compspro/competitors (AD-5).
"""

import asyncio
import random

from main import BLOCKING_MARKERS, _noise_ratio, scrape
from storage import get_quality_reviews, normalize_domain, save_quality_review
from storage import _client

# Stop once this many consecutive *newly-characterized* sites in a row repeat an
# already-seen failure type -- revisable, same "stability window" pattern as
# Story 4.1's own signal, but a separate constant (AC #1).
STABILITY_WINDOW = 5

# markdown shorter than this is treated as incomplete_content.
_MIN_CONTENT_LENGTH = 500


def _candidate_websites() -> list[str]:
    """All non-null compspro websites, in random order -- unbiased sampling across
    different site types, not insertion order (AC #1). Queries compspro directly via
    storage.py's shared _client(), matching the existing project convention
    (reprocess_list.py, main.py, competitor_validator.py) rather than adding a new
    storage.py wrapper for a one-off read (AD-1: storage.py is the sole place the
    Supabase client is instantiated, not the sole place every query is written).
    """
    rows = _client().table("compspro").select("website").execute().data or []
    websites = [r["website"] for r in rows if r.get("website")]
    random.shuffle(websites)
    return websites


def characterize(markdown: str) -> tuple[str, str]:
    """Heuristic characterization of a scrape() result. Returns (verdict, notes).

    The failure-type vocabulary is intentionally open (AD-4) -- this is a starting
    heuristic set, not a fixed contract. The one rule that matters: reuse the SAME
    label for the SAME kind of failure across sites in a run, since AC #1's stopping
    condition depends on label consistency, not prose creativity.
    """
    length = len(markdown)
    lowered = markdown.lower()

    for marker in BLOCKING_MARKERS:
        if marker in lowered:
            return "blocking_page", f"matched trigger phrase: {marker!r} (length={length})"

    if length < _MIN_CONTENT_LENGTH:
        return "incomplete_content", f"markdown length={length} < {_MIN_CONTENT_LENGTH}"

    # Noise ratio: short lines are typical of nav/footer boilerplate rather than
    # substantive product content. Shared with main.py's _fetch_light fallback
    # check via _noise_ratio() so the two heuristics can't silently drift apart.
    noise_ratio = _noise_ratio(markdown)
    if noise_ratio > 0.85:
        lines = [line.strip() for line in markdown.splitlines() if line.strip()]
        return "content_drowned_in_noise", f"noise_ratio={noise_ratio:.2f} over {len(lines)} lines (length={length})"

    return "ok", f"length={length}"


def diagnose_one(website: str) -> tuple[str, str]:
    """Scrape one site and characterize the result. A scrape() exception is itself
    diagnostic signal (AD-5's automated-characterization framing), not a reason to
    crash the run -- caught and recorded as its own verdict.
    """
    try:
        markdown, _logo_candidates = asyncio.run(scrape(website))
    except Exception as e:
        return "scrape_exception", f"{type(e).__name__}: {e}"
    return characterize(markdown)


def run(stability_window: int = STABILITY_WINDOW) -> dict:
    """Sample compspro sites, scrape + characterize each not-yet-diagnosed one, and
    persist findings to quality_review_log until no new failure type has appeared
    across `stability_window` consecutive newly-characterized sites, or the
    candidate pool is exhausted (AC #1-#5).
    """
    candidates = _candidate_websites()

    seen_labels: set[str] = set()
    consecutive_no_new = 0
    characterized = 0
    skipped_known = 0
    skipped_invalid = 0
    stop_reason = "sample_exhausted"

    for website in candidates:
        # Dedup against an already-known diagnosis for this domain (AC #4, AD-7).
        # Skipped sites don't count toward the stability window -- they produced no
        # new characterization. A website value normalize_domain() can't resolve to
        # a domain (e.g. a protocol-relative URL) raises ValueError here -- that's a
        # malformed candidate, not a reason to abort the whole diagnostic sweep.
        try:
            already_known = bool(get_quality_reviews("scraping_diagnostic", subject=website))
        except ValueError as e:
            skipped_invalid += 1
            print(f"  {website} -> skipped (unresolvable domain: {e})")
            continue
        if already_known:
            skipped_known += 1
            continue

        verdict, notes = diagnose_one(website)
        save_quality_review(
            review_type="scraping_diagnostic",
            subject=website,
            verdict=verdict,
            notes=notes,
        )
        characterized += 1

        if verdict in seen_labels:
            consecutive_no_new += 1
        else:
            seen_labels.add(verdict)
            consecutive_no_new = 0

        print(f"  {normalize_domain(website)} -> {verdict} ({notes})")

        if consecutive_no_new >= stability_window:
            stop_reason = "stability_reached"
            break

    return {
        "characterized": characterized,
        "skipped_known": skipped_known,
        "skipped_invalid": skipped_invalid,
        "labels_seen": sorted(seen_labels),
        "stop_reason": stop_reason,
    }


if __name__ == "__main__":
    summary = run()
    print()
    print(f"Characterized: {summary['characterized']} sites")
    print(f"Skipped (already known): {summary['skipped_known']} sites")
    print(f"Skipped (unresolvable domain): {summary['skipped_invalid']} sites")
    print(f"Failure types seen: {', '.join(summary['labels_seen']) or 'none'}")
    reason = "stability window reached" if summary["stop_reason"] == "stability_reached" else "candidate sample exhausted"
    print(f"Stopped because: {reason}")
