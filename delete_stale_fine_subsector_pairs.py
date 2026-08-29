"""One-shot cleanup: delete the `competitors` rows flagged as
"fine_subsector_mismatch" in audit_stale_competitors_report.json -- the exact
false-positive pattern storage.get_by_subsectors()'s fix was built to
eliminate (one side has a real sub_subsector for a fine subsector, the other
side has none for that same subsector).

Deliberately does NOT touch the "no_shared_subsector" pairs in the same
report -- those are a separate taxonomy-drift issue (the companies no longer
share any subsector at all), out of scope for this cleanup.

Read-only until you type "y" at the confirmation prompt: it loads the report,
looks up the matching rows in `competitors`, prints them, and only issues
DELETEs after explicit interactive confirmation.

Usage: .venv/bin/python3 delete_stale_fine_subsector_pairs.py
"""

import json
from dotenv import load_dotenv
from storage import _client

load_dotenv()

REPORT_PATH = "audit_stale_competitors_report.json"
TARGET_REASON = "fine_subsector_mismatch"


def load_target_pairs() -> list[dict]:
    with open(REPORT_PATH, encoding="utf-8") as f:
        report = json.load(f)
    return [p for p in report["stale_pairs"] if p["exclusion_reason"] == TARGET_REASON]


def find_rows(client, company_a: str, company_b: str) -> list[dict]:
    """Both directions -- `competitors` doesn't guarantee a pair is stored
    with a fixed (company_a, company_b) order (storage.get_known_competitors
    checks both sides for the same reason)."""
    forward = (
        client.table("competitors").select("id, company_a, company_b, score")
        .eq("company_a", company_a).eq("company_b", company_b)
        .execute().data or []
    )
    backward = (
        client.table("competitors").select("id, company_a, company_b, score")
        .eq("company_a", company_b).eq("company_b", company_a)
        .execute().data or []
    )
    return forward + backward


def main() -> None:
    targets = load_target_pairs()
    if not targets:
        print(f"Aucune paire '{TARGET_REASON}' trouvée dans {REPORT_PATH} -- rien à faire.")
        return

    print(f"{len(targets)} paire(s) '{TARGET_REASON}' dans le rapport :")
    for p in targets:
        print(f"  {p['company_a']} <-> {p['company_b']}  (score={p['score']})")

    client = _client()
    rows_to_delete = []
    for pair in targets:
        rows = find_rows(client, pair["company_a"], pair["company_b"])
        if not rows:
            print(f"\n  ATTENTION: aucune ligne trouvée en base pour {pair['company_a']} <-> {pair['company_b']} (déjà supprimée ?)")
        rows_to_delete.extend(rows)

    if not rows_to_delete:
        print("\nAucune ligne à supprimer en base.")
        return

    print(f"\n{len(rows_to_delete)} ligne(s) vont être supprimées de `competitors` :\n")
    for r in rows_to_delete:
        print(f"  id={r['id']}  {r['company_a']} <-> {r['company_b']}  score={r['score']}")

    answer = input(f"\nConfirmer la suppression de ces {len(rows_to_delete)} ligne(s) ? [y/N] ").strip().lower()
    if answer != "y":
        print("Annulé -- aucune suppression effectuée.")
        return

    deleted = 0
    for r in rows_to_delete:
        client.table("competitors").delete().eq("id", r["id"]).execute()
        deleted += 1

    print(f"\n{deleted} ligne(s) supprimée(s).")


if __name__ == "__main__":
    main()
