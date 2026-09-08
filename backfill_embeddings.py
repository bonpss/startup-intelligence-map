# python backfill_embeddings.py --dry-run   # count rows needing an embedding, no writes
# python backfill_embeddings.py              # backfill them
"""One-off backfill: compute a mistral-embed vector for every compspro row that
doesn't have one yet (embedding pre-filter chantier, 2026-09-01 conversation).

Safe to interrupt and rerun -- progress is the `embedding is null` filter
itself, not a separate progress file: every run only ever picks up rows still
missing a vector, so a partial run just resumes where it left off.
"""

import argparse

from dotenv import load_dotenv

from embeddings import BATCH_SIZE, embed
from storage import _client

load_dotenv()


def fetch_pending() -> list[dict]:
    client = _client()
    rows, page, size = [], 0, 1000
    while True:
        batch = (
            client.table("compspro")
            .select("id, name, description")
            .is_("embedding", "null")
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
    parser.add_argument("--dry-run", action="store_true", help="count rows needing an embedding, no writes")
    args = parser.parse_args()

    pending = fetch_pending()
    skipped, todo = [], []
    for r in pending:
        (skipped if not (r.get("description") or "").strip() else todo).append(r)

    print(f"{len(pending)} row(s) missing an embedding — {len(skipped)} have no description (skipped), {len(todo)} to embed")
    if skipped:
        for r in skipped:
            print(f"  skip (no description): {r['name']}")

    if args.dry_run:
        calls = -(-len(todo) // BATCH_SIZE)  # ceil
        print(f"\nDry run — ~{calls} embed call(s) for {len(todo)} row(s)")
        return

    client = _client()
    done = 0
    for i in range(0, len(todo), BATCH_SIZE):
        chunk = todo[i:i + BATCH_SIZE]
        vectors = embed([r["description"] for r in chunk])
        for row, vector in zip(chunk, vectors):
            client.table("compspro").update({"embedding": vector}).eq("id", row["id"]).execute()
            done += 1
        print(f"[{done}/{len(todo)}] embedded", flush=True)

    print(f"\nDone — {done} row(s) embedded, {len(skipped)} skipped (no description)")


if __name__ == "__main__":
    main()
