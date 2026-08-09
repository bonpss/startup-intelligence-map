# python backfill_competitors.py --dry-run              # estimate LLM calls, no writes
# python backfill_competitors.py --start 0 --end 100    # process a slice
"""Backfill competitor links for startups already in Supabase.

Startups are processed in alphabetical order. Each unordered pair is scored
only once — when processing its alphabetically first member — since pair
scores are symmetric. Pairs already in the competitors table are never
re-scored. Processed names are logged to backfill_progress.txt so an
interrupted run resumes without re-paying LLM calls.
"""

import argparse
import os
import time

from dotenv import load_dotenv
from competitor import CHUNK_SIZE, score_candidates, save_competitors
from storage import _client, get_by_subsectors, get_known_competitors

load_dotenv()

PROGRESS_FILE = "backfill_progress.txt"


def fetch_all_startups() -> list[dict]:
    client = _client()
    rows, page, size = [], 0, 1000
    while True:
        batch = (
            client.table("compspro")
            .select("name, description, sectors, subsectors, sub_subsectors")
            .order("name")
            .range(page, page + size - 1)
            .execute()
            .data or []
        )
        rows.extend(batch)
        if len(batch) < size:
            break
        page += size
    return rows


def load_progress() -> set[str]:
    if not os.path.exists(PROGRESS_FILE):
        return set()
    with open(PROGRESS_FILE, encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


def mark_done(name: str) -> None:
    with open(PROGRESS_FILE, "a", encoding="utf-8") as f:
        f.write(name + "\n")


def candidates_for(company: dict) -> list[dict]:
    name = company["name"]
    candidates = get_by_subsectors(
        company.get("subsectors") or [],
        company.get("sectors") or [],
        name,
        company.get("sub_subsectors") or [],
    )
    # Score each unordered pair once: only candidates after this company alphabetically
    candidates = [c for c in candidates if c["name"] > name]
    # Never re-score pairs already in the competitors table
    known = set(get_known_competitors(name))
    return [c for c in candidates if c["name"] not in known]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--end", type=int, default=None)
    parser.add_argument("--sleep", type=float, default=1.0, help="pause between startups (seconds)")
    parser.add_argument("--dry-run", action="store_true", help="count candidate pairs and LLM calls, no scoring")
    args = parser.parse_args()

    startups = fetch_all_startups()
    done = load_progress()
    batch = startups[args.start:args.end]
    total = len(batch)

    end_label = args.end if args.end is not None else len(startups)
    print(f"{len(startups)} startups in DB — processing {total} (indices {args.start}-{end_label})")
    print(f"{len(done)} already done (progress file)\n")

    if args.dry_run:
        pairs, calls = 0, 0
        for i, company in enumerate(batch, 1):
            if company["name"] in done:
                continue
            n = len(candidates_for(company))
            pairs += n
            calls += -(-n // CHUNK_SIZE)  # ceil
            print(f"[{i}/{total}] {company['name']}: {n} pair(s) to score")
        print(f"\nDry run — {pairs} pair(s) to score, ~{calls} LLM call(s)")
        return

    saved_total, errors = 0, []
    for i, company in enumerate(batch, 1):
        name = company["name"]
        if name in done:
            continue
        print(f"[{i}/{total}] {name}", flush=True)
        try:
            candidates = candidates_for(company)
            if candidates:
                results = score_candidates(company, candidates)
                saved = save_competitors(company, results)
                for rel in saved:
                    print(f"    ↔ {rel['company_b']} (score {rel['score']:.2f})")
                saved_total += len(saved)
            mark_done(name)
        except Exception as e:
            errors.append((name, str(e)))
            print(f"    ✗ ERROR — {e}")
        time.sleep(args.sleep)

    print(f"\n{'=' * 60}")
    print(f"Done — {saved_total} relationship(s) saved, {len(errors)} error(s)")
    for name, msg in errors:
        print(f"  {name}: {msg}")


if __name__ == "__main__":
    main()
