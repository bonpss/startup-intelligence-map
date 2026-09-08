"""Read-only audit: which existing `competitors` rows would no longer pass
storage.get_by_subsectors()'s fine-subsector filter if recomputed today.

Context: that filter (currently 7 subsectors that TAXONOMY breaks into
sub_subsectors -- see _fine_subsector_labels() for the live list --
generically any subsector TAXONOMY breaks into sub_subsectors) only changes
candidate-pool construction for FUTURE
scoring. The 5295 rows already saved in `competitors` were computed against
the old, broader pool, so some of them are false positives of the same shape
as Freestyle<->Fluidstack: one side has sub_subsectors=[] for a subsector the
other side filled in.

The old pool was a strict superset of the new one for these subsectors, so no
real competitor could have been missed by the old scoring -- this script does
NOT call Mistral and does NOT look for new pairs, only flags existing ones
that the current filter would now reject.

Zero writes. Deletion is a separate, human-approved step (not this script).

Usage:
  .venv/bin/python3 audit_stale_competitors.py
      Default scope: pairs touching one of the fine subsectors, checked for
      BOTH no_shared_subsector and fine_subsector_mismatch (unchanged behavior).

  .venv/bin/python3 audit_stale_competitors.py --subsector "API Infrastructure"
      Targeted scope for reuse after a reprocess_list.py run: pairs touching
      any startup currently tagged with this subsector (resolved via
      reprocess_list.names_for_subsector() -- same resolution reprocess_list.py
      itself uses), checked ONLY for no_shared_subsector. The fine_subsector_
      mismatch check stays independent and unaffected -- it always runs against
      its own fixed fine-subsector scope, never merged with this one.

  .venv/bin/python3 audit_stale_competitors.py --companies names.txt
      Same targeted no_shared_subsector-only check, scoped to an explicit list
      of compspro.name values (one per line) instead of a subsector.

Output: console report (style matches audit_taxonomy.py) +
        audit_stale_competitors_report.json
"""

import argparse
import json
from collections import Counter
from dotenv import load_dotenv
from storage import _client
from taxonomy import TAXONOMY
from reprocess_list import names_for_subsector

load_dotenv()

# ── ANSI colours (matches audit_taxonomy.py) ────────────────────────────────
BOLD   = "\033[1m"
DIM    = "\033[2m"
RESET  = "\033[0m"
RED    = "\033[91m"
YELLOW = "\033[93m"
GREEN  = "\033[92m"
CYAN   = "\033[96m"

W = 64


def _fine_subsector_labels() -> dict[str, set[str]]:
    """subsector name -> its sub_subsectors set, for every subsector TAXONOMY
    breaks into sub_subsectors under at least one sector -- discovered
    dynamically, no subsector name or count hardcoded, so this covers
    whatever set taxonomy.py currently defines (and whatever it adds later).

    Flat name -> labels map (not scoped per sector) is safe here: verified
    2026-08-29 that no subsector name other than "Uncategorized" is reused
    across two different sectors in TAXONOMY.
    """
    result: dict[str, set[str]] = {}
    for subs in TAXONOMY.values():
        for sub, subsubs in subs.items():
            if subsubs:
                result[sub] = set(subsubs)
    return result


def _fetch_all(table: str, columns: str) -> list[dict]:
    client = _client()
    rows, offset, page = [], 0, 1000
    while True:
        batch = client.table(table).select(columns).range(offset, offset + page - 1).execute().data or []
        rows.extend(batch)
        if len(batch) < page:
            break
        offset += page
    return rows


def check_no_shared_subsector(a: dict, b: dict) -> dict | None:
    """Standalone no_shared_subsector check -- the only check run in --subsector/
    --companies scoped mode, and also the first thing check_pair() below checks
    for the default fine-subsector scope. Kept as its own function so scoped
    mode doesn't have to run (or care about) the fine_subsector_mismatch logic,
    per the ticket's "don't merge the two checks" requirement.
    """
    a_subs = set(a.get("subsectors") or [])
    b_subs = set(b.get("subsectors") or [])
    if a_subs & b_subs:
        return None
    return {
        "exclusion_reason": "no_shared_subsector",
        "detail": "company_a and company_b no longer share any subsector at all "
                  "(taxonomy drift since this link was made -- unrelated to the "
                  "sub_subsector fix itself, but the pair wouldn't pass "
                  "get_by_subsectors' base overlap query either)",
    }


def check_pair(a: dict, b: dict, fine_labels: dict[str, set[str]]) -> dict | None:
    """Mirrors get_by_subsectors()'s fine-subsector matching logic for one
    already-saved pair. Returns None if the pair would still pass today's
    filter (keep); otherwise a dict explaining why it would now be excluded.
    """
    no_shared = check_no_shared_subsector(a, b)
    if no_shared is not None:
        return no_shared

    a_subs = set(a.get("subsectors") or [])
    b_subs = set(b.get("subsectors") or [])
    shared = a_subs & b_subs

    # Rule 2: a coarse (no sub_subsectors defined) shared subsector alone is
    # enough to keep the pair, exactly like get_by_subsectors' short-circuit.
    if shared - set(fine_labels):
        return None

    a_sub_subs = set(a.get("sub_subsectors") or [])
    b_sub_subs = set(b.get("sub_subsectors") or [])

    # Only a subsector where BOTH sides have at least one sub_subsector label
    # is actually comparable -- an empty side is a data gap, not evidence of
    # divergence (e.g. "General Purpose AI Models" has no sub_subsectors
    # defined in TAXONOMY at all, so it must never reach this point flagged).
    reasons = []
    for sub in sorted(shared):  # every remaining shared subsector is fine
        labels = fine_labels[sub]
        a_own = sorted(a_sub_subs & labels)
        b_own = sorted(b_sub_subs & labels)
        if not a_own or not b_own:
            continue  # nothing to compare on this subsector -- skip, not a mismatch
        if set(a_own) & set(b_own):
            return None  # genuine overlap on this subsector -- pair stays valid
        reasons.append({
            "subsector": sub,
            "company_a_labels": a_own,
            "company_b_labels": b_own,
            "why": "both sides have sub_subsectors for this subsector, but they don't overlap",
        })

    if not reasons:
        return None  # no comparable subsector had data on both sides -- not a mismatch

    return {"exclusion_reason": "fine_subsector_mismatch", "per_subsector": reasons}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument(
        "--subsector",
        help='Restrict the audit to pairs touching a startup currently tagged with this '
             'subsector (e.g. "API Infrastructure"), for reuse after a reprocess_list.py '
             'run. Runs ONLY the no_shared_subsector check -- fine_subsector_mismatch stays '
             'scoped to its own fixed 3 subsectors regardless of this flag.',
    )
    scope.add_argument(
        "--companies",
        help="Path to a text file, one compspro.name per line -- same targeted "
             "no_shared_subsector-only check, scoped to this explicit list instead of a subsector.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    print(f"\n  Chargement des données Supabase…", end="", flush=True)
    companies = {r["name"]: r for r in _fetch_all("compspro", "name, sectors, subsectors, sub_subsectors")}
    pairs = _fetch_all("competitors", "id, company_a, company_b, score")
    print(f" {len(companies)} startups, {len(pairs)} liens competitors chargés.\n")

    fine_labels = _fine_subsector_labels()
    fine_names = set(fine_labels)

    if args.subsector or args.companies:
        if args.subsector:
            target_names = set(names_for_subsector(args.subsector))
            scope = f"subsector:{args.subsector}"
            scope_desc = f'startups tagged "{args.subsector}"'
        else:
            with open(args.companies, encoding="utf-8") as f:
                target_names = {line.strip() for line in f if line.strip()}
            scope = f"companies_file:{args.companies}"
            scope_desc = f"startups listed in {args.companies}"
        relevant_pairs = [
            p for p in pairs
            if p["company_a"] in target_names or p["company_b"] in target_names
        ]
        checker = check_no_shared_subsector
    else:
        target_names = {
            name for name, c in companies.items()
            if set(c.get("subsectors") or []) & fine_names
        }
        relevant_pairs = [
            p for p in pairs
            if p["company_a"] in target_names or p["company_b"] in target_names
        ]
        scope = "fine_subsectors_default"
        scope_desc = "subsector fin"
        checker = lambda a, b: check_pair(a, b, fine_labels)

    stale, dangling = [], []
    for p in relevant_pairs:
        a, b = companies.get(p["company_a"]), companies.get(p["company_b"])
        if not a or not b:
            dangling.append(p)
            continue
        verdict = checker(a, b)
        if verdict is not None:
            stale.append({"company_a": p["company_a"], "company_b": p["company_b"], "score": p["score"], **verdict})

    # ── report ──────────────────────────────────────────────────────────────
    hr, sep = "═" * W, "─" * W
    print(hr)
    print(f"{BOLD}  STALE COMPETITORS AUDIT (post get_by_subsectors fix){RESET}")
    print(sep)
    print(f"  Scope                             : {BOLD}{scope}{RESET}")
    print(f"  Subsectors 'fins' pris en compte : {', '.join(sorted(fine_names))}")
    print(f"  Total competitors rows           : {BOLD}{len(pairs)}{RESET}")
    print(f"  Pairs touchant {scope_desc:<17} : {BOLD}{len(relevant_pairs)}{RESET}")
    color = GREEN if not stale else (YELLOW if len(stale) < 50 else RED)
    print(f"  Stale (seraient exclues)          : {color}{len(stale)}{RESET}")
    if dangling:
        print(f"  {YELLOW}Références orphelines (nom absent de compspro) : {len(dangling)}{RESET}")
    print(f"{hr}\n")

    by_reason = Counter(s["exclusion_reason"] for s in stale)
    print(f"{BOLD}RÉPARTITION PAR RAISON{RESET}")
    for reason, cnt in by_reason.most_common():
        print(f"  {cnt:4d}  {reason}")

    print(f"\n{BOLD}PAIRES STALE{RESET}")
    for s in stale[:40]:
        print(f"  {RED}✗{RESET} {s['company_a']:<28} ↔ {s['company_b']:<28}  score={s['score']}")
        if s["exclusion_reason"] == "no_shared_subsector":
            print(f"      {DIM}{s['detail']}{RESET}")
        else:
            for row in s["per_subsector"]:
                a_lbl = ", ".join(row["company_a_labels"]) or "—"
                b_lbl = ", ".join(row["company_b_labels"]) or "—"
                print(f"      {DIM}[{row['subsector']}] a=[{a_lbl}] b=[{b_lbl}] — {row['why']}{RESET}")
    if len(stale) > 40:
        print(f"  {DIM}… et {len(stale) - 40} autres (voir le JSON){RESET}")

    print(f"\n{hr}\n")

    report = {
        "scope": scope,
        "summary": {
            "total_competitors_rows": len(pairs),
            "pairs_touching_fine_subsector": len(relevant_pairs),
            "stale_pairs": len(stale),
            "dangling_references": len(dangling),
        },
        "fine_subsectors": sorted(fine_names),
        "stale_pairs": stale,
        "dangling_references": dangling,
    }
    with open("audit_stale_competitors_report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"  Rapport exporté → {BOLD}audit_stale_competitors_report.json{RESET}\n")


if __name__ == "__main__":
    main()
