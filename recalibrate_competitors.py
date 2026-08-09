# python recalibrate_competitors.py

"""Retroactive competitor-threshold recalibration (Story 4.2).

Re-evaluates every existing `competitors` row against the current
storage.COMPETITOR_THRESHOLD and marks rows that no longer qualify as
active=false -- never deletes them (AD-6, FR-4(b)). One-directional by
construction: only ever moves true -> false, never the reverse (lowering the
threshold would need fresh LLM rescoring of previously-rejected pairs, never
persisted -- explicitly out of scope, epics.md Story 4.2).

Reads whatever value COMPETITOR_THRESHOLD currently holds in storage.py --
bumping it to the actual new number is a separate, deliberate code change
Julien makes himself before running this script for real.
"""

from storage import COMPETITOR_THRESHOLD, _client


def _fetch_all_rows() -> list[dict]:
    """All competitors rows, paginated -- same pattern already established in
    graph_app.py's api_graph_all and graph_analysis.py's fetch_edges (Supabase
    caps a single response at 1000 rows).
    """
    client = _client()
    rows, page, size = [], 0, 1000
    while True:
        batch = (
            client.table("competitors")
            .select("id, company_a, company_b, score, active")
            .range(page, page + size - 1)
            .execute()
            .data or []
        )
        rows.extend(batch)
        if len(batch) < size:
            break
        page += size
    return rows


def recalibrate() -> dict:
    """Mark active=false on every row whose score no longer meets
    COMPETITOR_THRESHOLD. Only ever moves true -> false: an already-inactive row
    is left untouched (idempotent re-runs don't re-write it), and a row that
    already meets the threshold is left untouched (already active=true by the
    migration's own default -- no write needed).
    """
    rows = _fetch_all_rows()
    client = _client()

    newly_inactive = 0
    already_inactive = 0
    still_active = 0

    for row in rows:
        below_threshold = (row.get("score") or 0) < COMPETITOR_THRESHOLD
        if below_threshold and row.get("active"):
            client.table("competitors").update({"active": False}).eq("id", row["id"]).execute()
            newly_inactive += 1
        elif below_threshold:
            already_inactive += 1
        else:
            still_active += 1

    return {
        "total": len(rows),
        "newly_inactive": newly_inactive,
        "already_inactive": already_inactive,
        "still_active": still_active,
        "threshold_used": COMPETITOR_THRESHOLD,
    }


if __name__ == "__main__":
    summary = recalibrate()
    print(f"Threshold used: {summary['threshold_used']}")
    print(f"Total rows evaluated: {summary['total']}")
    print(f"Newly marked inactive: {summary['newly_inactive']}")
    print(f"Already inactive (unchanged): {summary['already_inactive']}")
    print(f"Still active (score >= threshold): {summary['still_active']}")
