# python backfill_specific.py websites.txt
"""Backfill competitor links for an explicit list of startups (by website).

Unlike backfill_competitors.py's alphabetical-sweep dedup (each pair scored
once, when processing the alphabetically-first member), this scores every
listed startup against its FULL current candidate pool, in both directions —
appropriate right after a targeted reprocess where subsectors changed and
existing links were wiped, since candidates outside the list may already
have been swept past in the main alphabetical backfill.
"""

import sys
import time

from competitor import score_candidates, save_competitors
from storage import _client, get_by_subsectors


def fetch_by_websites(websites: list[str]) -> list[dict]:
    client = _client()
    rows = (
        client.table("compspro")
        .select("name, description, sectors, subsectors, sub_subsectors, website")
        .in_("website", websites)
        .execute()
        .data or []
    )
    return rows


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python backfill_specific.py <file with one website per line>", file=sys.stderr)
        sys.exit(1)

    with open(sys.argv[1], encoding="utf-8") as f:
        websites = [line.strip() for line in f if line.strip()]

    companies = fetch_by_websites(websites)
    total = len(companies)
    print(f"{total} startup(s) loaded\n")

    saved_total, errors = 0, []

    for i, company in enumerate(companies, 1):
        name = company["name"]
        print(f"[{i}/{total}] {name}", flush=True)
        try:
            candidates = get_by_subsectors(
                company.get("subsectors") or [],
                company.get("sectors") or [],
                name,
                company.get("sub_subsectors") or [],
            )
            if candidates:
                results = score_candidates(company, candidates)
                saved = save_competitors(company, results)
                for rel in saved:
                    print(f"    ↔ {rel['company_b']} (score {rel['score']:.2f})")
                saved_total += len(saved)
        except Exception as e:
            errors.append((name, str(e)))
            print(f"    ✗ ERROR — {e}")
        if i < total:
            time.sleep(1)

    print(f"\n{'=' * 60}")
    print(f"Done — {saved_total} relationship(s) saved, {len(errors)} error(s)")
    for name, msg in errors:
        print(f"  {name}: {msg}")


if __name__ == "__main__":
    main()
