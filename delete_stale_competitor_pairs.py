"""One-shot cleanup: delete `competitors` rows flagged "no_shared_subsector" in
an audit_stale_competitors.py report -- pairs whose two companies no longer
share ANY subsector at all (taxonomy drift since the link was made).

Deliberately does NOT touch "fine_subsector_mismatch" pairs from the same
report -- that pattern is already covered by delete_stale_fine_subsector_pairs.py,
not duplicated here.

Per-pair interactive confirmation (y/n), no bulk --yes-to-all flag: each
candidate is shown with FRESH subsectors fetched live from Supabase via
storage.get_company() (not the report's frozen JSON values, in case the data
changed again since the audit ran) before you decide.

Usage:
  .venv/bin/python3 delete_stale_competitor_pairs.py --dry-run [--report path.json]
      Shows every candidate pair and its live subsectors, deletes nothing,
      never prompts.

  .venv/bin/python3 delete_stale_competitor_pairs.py [--report path.json]
      Same display, then asks y/n per pair before deleting.

Default --report: audit_stale_competitors_report.json (repo root).
"""

import argparse
import json
from dotenv import load_dotenv
from storage import _client, get_company

load_dotenv()

DEFAULT_REPORT = "audit_stale_competitors_report.json"
TARGET_REASON = "no_shared_subsector"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--report", default=DEFAULT_REPORT, help=f"Path to an audit_stale_competitors.py JSON report (default: {DEFAULT_REPORT})")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be deleted without prompting or deleting anything.")
    return parser.parse_args()


def load_target_pairs(report_path: str) -> list[dict]:
    with open(report_path, encoding="utf-8") as f:
        report = json.load(f)
    return [p for p in report["stale_pairs"] if p["exclusion_reason"] == TARGET_REASON]


def find_rows(client, company_a: str, company_b: str) -> list[dict]:
    """Both directions -- `competitors` doesn't guarantee a pair is stored with
    a fixed (company_a, company_b) order (same reasoning as
    delete_stale_fine_subsector_pairs.py's find_rows -- not duplicated logic,
    just the same small query shape, since there's no shared helper to import
    for it in storage.py)."""
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


def _fmt_subsectors(name: str) -> str:
    """Live lookup via storage.get_company(), not the report's frozen values --
    the whole point of re-checking before a destructive action."""
    company = get_company(name)
    if not company:
        return f"{name}: (introuvable dans compspro)"
    subs = company.get("subsectors") or []
    return f"{name}: {', '.join(subs) if subs else '(aucun subsector)'}"


def main() -> None:
    args = parse_args()
    targets = load_target_pairs(args.report)
    if not targets:
        print(f"Aucune paire '{TARGET_REASON}' trouvée dans {args.report} -- rien à faire.")
        return

    mode = "[DRY RUN] " if args.dry_run else ""
    print(f"{mode}{len(targets)} paire(s) '{TARGET_REASON}' candidates dans {args.report}.\n")

    client = _client()
    deleted, kept, missing = 0, 0, 0

    for i, pair in enumerate(targets, 1):
        a_name, b_name = pair["company_a"], pair["company_b"]
        print(f"[{i}/{len(targets)}] {a_name} <-> {b_name}  (score={pair['score']})")
        print(f"    {_fmt_subsectors(a_name)}")
        print(f"    {_fmt_subsectors(b_name)}")

        rows = find_rows(client, a_name, b_name)
        if not rows:
            print("    (aucune ligne trouvée en base -- déjà supprimée ?)\n")
            missing += 1
            continue

        if args.dry_run:
            for r in rows:
                print(f"    [dry-run] supprimerait id={r['id']}")
            print()
            continue

        answer = input(f"    Supprimer cette paire ({len(rows)} ligne(s)) ? [y/N] ").strip().lower()
        if answer == "y":
            for r in rows:
                client.table("competitors").delete().eq("id", r["id"]).execute()
            deleted += len(rows)
            print(f"    → supprimé ({len(rows)} ligne(s))\n")
        else:
            kept += 1
            print("    → conservé\n")

    print("=" * 60)
    if args.dry_run:
        print(f"[DRY RUN] {len(targets)} paire(s) candidates -- rien n'a été supprimé.")
    else:
        print(
            f"Terminé -- {deleted} ligne(s) supprimée(s), {kept} paire(s) conservée(s), "
            f"{missing} introuvable(s), {len(targets)} candidat(s) au total."
        )


if __name__ == "__main__":
    main()
