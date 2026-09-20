# python backfill_compspro_domain.py --dry-run   # report only, no writes
# python backfill_compspro_domain.py              # write domain for every unambiguous row
"""One-off backfill for migrations/013 (compspro.domain).

Computes storage.normalize_domain(website) for every compspro row missing a
domain and writes it. Any domain shared by two or more rows is a genuine
name/website collision (the Corma-style bug this whole fix targets, or a
pre-existing one nobody noticed) -- those rows are reported and left
domain = NULL rather than picking one arbitrarily. Run this, resolve any
reported collision by hand, re-run until the report is clean, THEN apply
migrations/014 (the unique index) -- a dirty backfill would make that
migration fail outright.
"""

import argparse
from collections import defaultdict

from storage import _client, normalize_domain


def fetch_rows() -> list[dict]:
    client = _client()
    rows, page, size = [], 0, 1000
    while True:
        batch = (
            client.table("compspro")
            .select("id, name, website, domain")
            .order("id")
            .range(page, page + size - 1)
            .execute()
            .data or []
        )
        rows.extend(batch)
        if len(batch) < size:
            break
        page += size
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="report only, no writes")
    parser.add_argument("--recompute-all", action="store_true", help="recompute domain even for rows that already have one")
    args = parser.parse_args()

    rows = fetch_rows()
    print(f"{len(rows)} row(s) in compspro")

    no_website = [r for r in rows if not r.get("website")]
    candidates = [r for r in rows if r.get("website") and (args.recompute_all or not r.get("domain"))]

    by_domain: dict[str, list[dict]] = defaultdict(list)
    for r in candidates:
        d = normalize_domain(r["website"])
        if d:
            by_domain[d].append(r)
        else:
            no_website.append(r)

    collisions = {d: rs for d, rs in by_domain.items() if len(rs) > 1}
    clean = {d: rs[0] for d, rs in by_domain.items() if len(rs) == 1}

    if no_website:
        print(f"\n{len(no_website)} row(s) with no usable website (left domain = NULL):")
        for r in no_website:
            print(f"  id={r['id']} name={r['name']!r} website={r.get('website')!r}")

    if collisions:
        print(f"\n{len(collisions)} domain collision(s) -- left domain = NULL, resolve by hand before migrations/014:")
        for d, rs in collisions.items():
            print(f"  domain={d!r}:")
            for r in rs:
                print(f"    id={r['id']} name={r['name']!r} website={r['website']!r}")

    print(f"\n{len(clean)} row(s) ready to write" + (" (dry run, nothing written)" if args.dry_run else ""))

    if args.dry_run:
        return

    client = _client()
    written = 0
    for d, r in clean.items():
        client.table("compspro").update({"domain": d}).eq("id", r["id"]).execute()
        written += 1
    print(f"Wrote domain for {written} row(s).")

    if collisions:
        print(f"\n{len(collisions)} collision(s) still need manual resolution -- do not apply migrations/014 yet.")


if __name__ == "__main__":
    main()
