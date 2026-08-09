# python fix_redundant_uncategorized.py --dry-run   # preview, no writes
# python fix_redundant_uncategorized.py              # apply fixes
"""One-off retroactive cleanup for rows saved before remove_redundant_uncategorized()
existed in the extract() pipeline: drops the 'Uncategorized' subsector tag wherever
it coexists with a real subsector in compspro. Pure data cleanup -- no LLM calls.

Logs every correction to quality_review_log (review_type='redundant_uncategorized_cleanup')
for audit, mirroring the pattern already used for taxonomy_split/scraping_diagnostic.
"""

import argparse
import json

from dotenv import load_dotenv
from storage import _client, save_quality_review
from taxonomy import remove_redundant_uncategorized

load_dotenv()


def fetch_candidates() -> list[dict]:
    """compspro rows whose subsectors contain 'Uncategorized' alongside >=1 other tag."""
    rows = (
        _client()
        .table("compspro")
        .select("id, name, subsectors")
        .contains("subsectors", ["Uncategorized"])
        .execute()
        .data or []
    )
    return [r for r in rows if len(r.get("subsectors") or []) > 1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="print what would change, no writes")
    args = parser.parse_args()

    candidates = fetch_candidates()
    print(f"{len(candidates)} row(s) with 'Uncategorized' alongside real subsector(s)\n")

    fixed, skipped, errors = 0, 0, []
    for row in candidates:
        name = row["name"]
        before = row["subsectors"]
        after = remove_redundant_uncategorized(before)

        if after == before:
            # Defensive guard -- shouldn't trigger given fetch_candidates()'s filter,
            # but a row whose real subsector(s) don't survive removal here would
            # otherwise be silently mis-logged as "fixed".
            print(f"  SKIP  {name}: {before} unchanged")
            skipped += 1
            continue

        print(f"  FIX   {name}: {before} -> {after}")
        if args.dry_run:
            fixed += 1
            continue

        try:
            _client().table("compspro").update({"subsectors": after}).eq("id", row["id"]).execute()
            save_quality_review(
                review_type="redundant_uncategorized_cleanup",
                subject=name,
                verdict="fixed",
                source_snapshot={"subsectors": before},
                resolution=json.dumps(after, ensure_ascii=False),
                notes="Removed redundant Uncategorized tag alongside real subsector(s)",
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
