# python backfill_linkedin_urls.py [--limit N] [--dry-run]
"""One-off backfill: recover linkedin_url for compspro rows that have none,
by re-scraping each startup's own website and looking for a linkedin.com/
company/ link in the raw HTML.

Deliberately reuses main.scrape() -- not main.ingest() -- so this never calls
extract()/compare() and therefore never touches Mistral: scrape() only does
an HTTP fetch (or a Playwright fallback for JS-rendered sites) plus
deterministic regex/trafilatura parsing, and the LinkedIn link itself is
found by main._linkedin_url_from_html(), a plain regex scan, not an LLM call.

Safe to interrupt and rerun -- progress is the `linkedin_url is null` filter
itself, not a separate progress file.
"""

import argparse
import asyncio

from dotenv import load_dotenv

from main import scrape
from storage import _client

load_dotenv()


def fetch_pending(limit: int) -> list[dict]:
    client = _client()
    rows = (
        client.table("compspro")
        .select("id, name, website")
        .is_("linkedin_url", "null")
        .neq("website", "")
        .order("id")
        .limit(limit)
        .execute()
        .data or []
    )
    return rows


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=100, help="max rows to process")
    parser.add_argument("--dry-run", action="store_true", help="scrape and report, no writes")
    args = parser.parse_args()

    pending = fetch_pending(args.limit)
    print(f"{len(pending)} row(s) missing linkedin_url (limit {args.limit})\n")

    client = _client()
    found, not_found, errors = 0, 0, []

    for i, row in enumerate(pending, 1):
        name, website = row["name"], row["website"]
        print(f"[{i}/{len(pending)}] {name} ({website})", flush=True)
        try:
            _markdown, _logo_candidates, linkedin_url = await scrape(website)
        except Exception as e:
            print(f"    error: {e}")
            errors.append((name, str(e)))
            continue

        if not linkedin_url:
            print("    no linkedin link found")
            not_found += 1
            continue

        print(f"    found: {linkedin_url}")
        found += 1
        if not args.dry_run:
            client.table("compspro").update({"linkedin_url": linkedin_url}).eq("id", row["id"]).execute()

    print(f"\nDone — {found} found{' (dry run, not saved)' if args.dry_run else ' and saved'}, {not_found} not found, {len(errors)} error(s)")
    if errors:
        for name, msg in errors:
            print(f"  error: {name}: {msg}")


if __name__ == "__main__":
    asyncio.run(main())
