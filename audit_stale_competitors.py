"""Read-only audit: which existing `competitors` rows would no longer pass
storage.get_by_subsectors()'s fine-subsector filter if recomputed today.

Context: that filter (Productivity Tools / AI Driven Developer Productivity /
AI Security And Guardrails today, generically any subsector TAXONOMY breaks
into sub_subsectors) only changes candidate-pool construction for FUTURE
scoring. The 5295 rows already saved in `competitors` were computed against
the old, broader pool, so some of them are false positives of the same shape
as Freestyle<->Fluidstack: one side has sub_subsectors=[] for a subsector the
other side filled in.

The old pool was a strict superset of the new one for these subsectors, so no
real competitor could have been missed by the old scoring -- this script does
NOT call Mistral and does NOT look for new pairs, only flags existing ones
that the current filter would now reject.

Zero writes. Deletion is a separate, human-approved step (not this script).

Usage: .venv/bin/python3 audit_stale_competitors.py
Output: console report (style matches audit_taxonomy.py) +
        audit_stale_competitors_report.json
"""

import json
from collections import Counter
from dotenv import load_dotenv
from storage import _client
from taxonomy import TAXONOMY

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
    dynamically, no subsector name hardcoded, so this covers today's three
    (Productivity Tools, AI Driven Developer Productivity, AI Security And
    Guardrails) and whatever taxonomy.py adds later.

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


def check_pair(a: dict, b: dict, fine_labels: dict[str, set[str]]) -> dict | None:
    """Mirrors get_by_subsectors()'s fine-subsector matching logic for one
    already-saved pair. Returns None if the pair would still pass today's
    filter (keep); otherwise a dict explaining why it would now be excluded.
    """
    a_subs = set(a.get("subsectors") or [])
    b_subs = set(b.get("subsectors") or [])
    shared = a_subs & b_subs

    if not shared:
        return {
            "exclusion_reason": "no_shared_subsector",
            "detail": "company_a and company_b no longer share any subsector at all "
                      "(taxonomy drift since this link was made -- unrelated to the "
                      "sub_subsector fix itself, but the pair wouldn't pass "
                      "get_by_subsectors' base overlap query either)",
        }

    # Rule 2: a coarse (no sub_subsectors defined) shared subsector alone is
    # enough to keep the pair, exactly like get_by_subsectors' short-circuit.
    if shared - set(fine_labels):
        return None

    a_sub_subs = set(a.get("sub_subsectors") or [])
    b_sub_subs = set(b.get("sub_subsectors") or [])

    per_subsector = []
    for sub in sorted(shared):  # every remaining shared subsector is fine
        labels = fine_labels[sub]
        a_own = sorted(a_sub_subs & labels)
        b_own = sorted(b_sub_subs & labels)
        per_subsector.append({"subsector": sub, "company_a_labels": a_own, "company_b_labels": b_own})
        if set(a_own) & set(b_own):
            return None  # genuine overlap on this subsector -- pair stays valid

    reasons = []
    for row in per_subsector:
        a_empty = not row["company_a_labels"]
        b_empty = not row["company_b_labels"]
        if a_empty and b_empty:
            why = "neither side has a sub_subsector for this subsector"
        elif a_empty:
            why = "company_a has no sub_subsector for this subsector"
        elif b_empty:
            why = "company_b has no sub_subsector for this subsector"
        else:
            why = "both sides have sub_subsectors for this subsector, but they don't overlap"
        reasons.append({**row, "why": why})

    return {"exclusion_reason": "fine_subsector_mismatch", "per_subsector": reasons}


def main() -> None:
    print(f"\n  Chargement des données Supabase…", end="", flush=True)
    companies = {r["name"]: r for r in _fetch_all("compspro", "name, sectors, subsectors, sub_subsectors")}
    pairs = _fetch_all("competitors", "id, company_a, company_b, score")
    print(f" {len(companies)} startups, {len(pairs)} liens competitors chargés.\n")

    fine_labels = _fine_subsector_labels()
    fine_names = set(fine_labels)

    touching_names = {
        name for name, c in companies.items()
        if set(c.get("subsectors") or []) & fine_names
    }
    relevant_pairs = [
        p for p in pairs
        if p["company_a"] in touching_names or p["company_b"] in touching_names
    ]

    stale, dangling = [], []
    for p in relevant_pairs:
        a, b = companies.get(p["company_a"]), companies.get(p["company_b"])
        if not a or not b:
            dangling.append(p)
            continue
        verdict = check_pair(a, b, fine_labels)
        if verdict is not None:
            stale.append({"company_a": p["company_a"], "company_b": p["company_b"], "score": p["score"], **verdict})

    # ── report ──────────────────────────────────────────────────────────────
    hr, sep = "═" * W, "─" * W
    print(hr)
    print(f"{BOLD}  STALE COMPETITORS AUDIT (post get_by_subsectors fix){RESET}")
    print(sep)
    print(f"  Subsectors 'fins' pris en compte : {', '.join(sorted(fine_names))}")
    print(f"  Total competitors rows           : {BOLD}{len(pairs)}{RESET}")
    print(f"  Pairs touchant un subsector fin   : {BOLD}{len(relevant_pairs)}{RESET}")
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
