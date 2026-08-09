# python fix_empty_subsectors.py --dry-run   # preview, no writes
# python fix_empty_subsectors.py              # apply fixes
"""One-off retroactive backfill for rows saved before extract()'s empty-subsectors
guard existed: a sector can survive Step 5 with every subsector candidate filtered
out downstream, leaving subsectors = [] rather than ["Uncategorized"] -- invisible
to ILIKE '%Uncategorized%' audits, unlike a real Uncategorized tag. Pure data
cleanup -- no LLM calls.

Logs every correction to quality_review_log (review_type='empty_subsectors_backfill'),
mirroring fix_redundant_uncategorized.py's pattern.
"""

import argparse

from dotenv import load_dotenv
from storage import _client, save_quality_review

load_dotenv()


def fetch_candidates() -> list[dict]:
    """compspro rows whose subsectors array is empty or null.

    Paginates explicitly (same pattern as backfill_competitors.py's
    fetch_all_startups()) -- an unfiltered select() otherwise silently truncates
    at PostgREST's default 1000-row cap, well under compspro's actual row count.
    """
    client = _client()
    rows, page, size = [], 0, 1000
    while True:
        batch = (
            client.table("compspro")
            .select("id, name, subsectors")
            .range(page, page + size - 1)
            .execute()
            .data or []
        )
        rows.extend(batch)
        if len(batch) < size:
            break
        page += size
    return [r for r in rows if not r.get("subsectors")]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="print what would change, no writes")
    args = parser.parse_args()

    candidates = fetch_candidates()
    print(f"{len(candidates)} row(s) with an empty subsectors array\n")

    fixed, skipped, errors = 0, 0, []
    for row in candidates:
        name = row["name"]
        before = row.get("subsectors") or []

        if before:
            # Defensive guard -- shouldn't trigger given fetch_candidates()'s filter.
            print(f"  SKIP  {name}: subsectors not actually empty ({before})")
            skipped += 1
            continue

        print(f"  FIX   {name}: {before} -> ['Uncategorized']")
        if args.dry_run:
            fixed += 1
            continue

        try:
            _client().table("compspro").update({"subsectors": ["Uncategorized"]}).eq("id", row["id"]).execute()
            save_quality_review(
                review_type="empty_subsectors_backfill",
                subject=name,
                verdict="fixed",
                source_snapshot={"subsectors": []},
                resolution='["Uncategorized"]',
                notes="Backfilled empty subsectors array to Uncategorized for visibility",
            )
            fixed += 1
        except Exception as e:
            errors.append((name, str(e)))
            print(f"    ✗ ERROR — {e}")

    print(f"\n{'=' * 60}")
    label = "Would fix" if args.dry_run else "Fixed"
    print(f"{label} {fixed} row(s), skipped {skipped}, {len(errors)} error(s)")
    for name, msg in errors:
        print(f"  {name}: {msg}")


if __name__ == "__main__":
    main()
