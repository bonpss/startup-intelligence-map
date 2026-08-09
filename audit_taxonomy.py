import json
from collections import Counter
from dotenv import load_dotenv
from storage import _client
from taxonomy import TAXONOMY, HORIZONTAL_SUBSECTORS

load_dotenv()

# ── ANSI colours ──────────────────────────────────────────────────────────────
BOLD   = "\033[1m"
DIM    = "\033[2m"
RESET  = "\033[0m"
RED    = "\033[91m"
YELLOW = "\033[93m"
GREEN  = "\033[92m"
CYAN   = "\033[96m"

W = 64  # report width


def fetch_all() -> list[dict]:
    client = _client()
    all_rows, page, size = [], 0, 1000
    while True:
        batch = (
            client.table("compspro")
            .select("id, name, sectors, subsectors, website")
            .range(page, page + size - 1)
            .execute()
            .data or []
        )
        all_rows.extend(batch)
        if len(batch) < size:
            break
        page += size
    return all_rows


def _bar(n: int, max_width: int = 30) -> str:
    return "█" * min(n, max_width)


def _show_list(label: str, items: list[dict], color: str = RED, limit: int = 15) -> None:
    if not items:
        print(f"\n  {GREEN}✓ {label} : aucune{RESET}")
        return
    print(f"\n  {color}▸ {label} ({len(items)}){RESET}")
    for r in items[:limit]:
        secs  = ", ".join(r.get("sectors")    or []) or "—"
        subs  = ", ".join(r.get("subsectors") or []) or "—"
        extra = f"  {DIM}[{r.get('_extra', '')}]{RESET}" if r.get("_extra") else ""
        print(f"    {DIM}{r['name']!s:<35}{RESET}  sectors={secs}")
        print(f"    {'':35}  subsectors={subs}{extra}")
    if len(items) > limit:
        print(f"    {DIM}… et {len(items) - limit} autres{RESET}")


def main() -> None:
    print(f"\n  Chargement des données Supabase…", end="", flush=True)
    rows = fetch_all()
    print(f" {len(rows)} startups chargées.\n")

    total = len(rows)

    # ── Valid subsector sets ──────────────────────────────────────────────────
    taxonomy_subs: set[str] = {
        sub
        for sector_subs in TAXONOMY.values()
        for sub in sector_subs
    }
    # "Uncategorized" is always valid; add it explicitly in case TAXONOMY omits it
    taxonomy_subs.add("Uncategorized")

    # ── 1. Sector distribution ────────────────────────────────────────────────
    sector_counter: Counter = Counter()
    for r in rows:
        for s in (r.get("sectors") or []):
            sector_counter[s] += 1

    # ── 2. Subsector distribution ─────────────────────────────────────────────
    subsector_counter: Counter = Counter()
    for r in rows:
        for s in (r.get("subsectors") or []):
            subsector_counter[s] += 1
    singletons = [sub for sub, cnt in subsector_counter.items() if cnt == 1]

    # ── 3. Anomalies ──────────────────────────────────────────────────────────
    empty_sub_with_sec = [r for r in rows if not r.get("subsectors") and r.get("sectors")]
    empty_sectors_list = [r for r in rows if not r.get("sectors")]
    has_uncategorized  = [r for r in rows if "Uncategorized" in (r.get("subsectors") or [])]
    over_tagged        = [r for r in rows if len(r.get("subsectors") or []) > 3]

    # ── 4. Horizontal conflicts ───────────────────────────────────────────────
    # Check against the 3 subsectors explicitly named by the user
    check_horizontals = {
        "Horizontal Workflow Automation",
        "AI World Models",
        "General Purpose AI Models",
    }
    conflicts = []
    for r in rows:
        subs = set(r.get("subsectors") or [])
        h_tags = subs & check_horizontals
        has_vertical = any(
            s not in HORIZONTAL_SUBSECTORS and s != "Uncategorized"
            for s in subs
        )
        if h_tags and has_vertical:
            r = dict(r)  # copy to avoid mutating original
            r["_extra"] = f"horizontal: {', '.join(sorted(h_tags))}"
            conflicts.append(r)

    # ── 5. Taxonomy drift ─────────────────────────────────────────────────────
    drift = []
    for r in rows:
        unknown = [s for s in (r.get("subsectors") or []) if s not in taxonomy_subs]
        if unknown:
            row_copy = dict(r)
            row_copy["_extra"] = f"inconnus: {', '.join(unknown)}"
            drift.append(row_copy)

    # ── Summary metrics ───────────────────────────────────────────────────────
    classified = sum(
        1 for r in rows
        if r.get("sectors") and r.get("subsectors")
    )
    anomaly_ids = set()
    for r in (
        empty_sub_with_sec + empty_sectors_list + has_uncategorized
        + over_tagged + conflicts + drift
    ):
        anomaly_ids.add(r["id"])

    pct_classified = 100 * classified / total if total else 0
    pct_anomalies  = 100 * len(anomaly_ids) / total if total else 0

    # ══════════════════════════════════════════════════════════════════════════
    # PRINT REPORT
    # ══════════════════════════════════════════════════════════════════════════
    hr  = "═" * W
    sep = "─" * W

    print(f"{hr}")
    print(f"{BOLD}  TAXONOMY AUDIT REPORT{RESET}")
    print(f"{sep}")
    classified_color = GREEN if pct_classified >= 80 else YELLOW
    anomaly_color    = GREEN if pct_anomalies < 5  else (YELLOW if pct_anomalies < 20 else RED)
    print(f"  Total startups    : {BOLD}{total}{RESET}")
    print(f"  Classifiées       : {classified_color}{classified} ({pct_classified:.1f}%){RESET}")
    print(f"  Avec anomalies    : {anomaly_color}{len(anomaly_ids)} ({pct_anomalies:.1f}%){RESET}")
    print(f"{hr}\n")

    # ── Section 1 ─────────────────────────────────────────────────────────────
    print(f"{BOLD}1. DISTRIBUTION PAR SECTEUR{RESET}")
    max_count = max(sector_counter.values(), default=1)
    for sector, cnt in sector_counter.most_common():
        bar = _bar(cnt, max_width=int(30 * cnt / max_count))
        print(f"  {cnt:4d}  {CYAN}{bar:<30}{RESET}  {sector}")

    # ── Section 2 ─────────────────────────────────────────────────────────────
    print(f"\n{BOLD}2. DISTRIBUTION PAR SOUS-SECTEUR{RESET}")
    max_sub = max(subsector_counter.values(), default=1)
    for sub, cnt in subsector_counter.most_common():
        flag = f"  {YELLOW}⚠ singleton{RESET}" if cnt == 1 else ""
        bar  = _bar(cnt, max_width=int(30 * cnt / max_sub))
        print(f"  {cnt:4d}  {CYAN}{bar:<30}{RESET}  {sub}{flag}")
    if singletons:
        print(f"\n  {YELLOW}Singletons ({len(singletons)}) :{RESET} {', '.join(sorted(singletons))}")

    # ── Section 3 ─────────────────────────────────────────────────────────────
    print(f"\n{BOLD}3. ANOMALIES DE CLASSIFICATION{RESET}")
    _show_list("Sectors non vides mais subsectors vides",     empty_sub_with_sec)
    _show_list("Sectors vides",                               empty_sectors_list)
    _show_list("Subsectors contenant 'Uncategorized'",        has_uncategorized, YELLOW)
    _show_list("Plus de 3 sous-secteurs (sur-taggées)",       over_tagged,       YELLOW)

    # ── Section 4 ─────────────────────────────────────────────────────────────
    print(f"\n{BOLD}4. CONFLITS HORIZONTAL_SUBSECTORS{RESET}")
    _show_list(
        "Tag horizontal coexistant avec un tag vertical",
        conflicts,
        RED,
    )

    # ── Section 5 ─────────────────────────────────────────────────────────────
    print(f"\n{BOLD}5. SOUS-SECTEURS HORS TAXONOMY (taxonomy drift){RESET}")
    _show_list("Sous-secteurs absents de la taxonomy courante", drift, RED)

    print(f"\n{hr}\n")

    # ══════════════════════════════════════════════════════════════════════════
    # JSON EXPORT
    # ══════════════════════════════════════════════════════════════════════════
    def _strip(rows: list[dict]) -> list[dict]:
        return [
            {k: v for k, v in r.items() if not k.startswith("_")}
            for r in rows
        ]

    report = {
        "summary": {
            "total": total,
            "classified": classified,
            "pct_classified": round(pct_classified, 1),
            "with_anomalies": len(anomaly_ids),
            "pct_anomalies": round(pct_anomalies, 1),
        },
        "sector_distribution":    dict(sector_counter.most_common()),
        "subsector_distribution": dict(subsector_counter.most_common()),
        "singletons":             sorted(singletons),
        "anomalies": {
            "empty_subsectors_with_sectors": [
                {"name": r["name"], "website": r.get("website"), "sectors": r.get("sectors")}
                for r in empty_sub_with_sec
            ],
            "empty_sectors": [{"name": r["name"]} for r in empty_sectors_list],
            "uncategorized":  [
                {"name": r["name"], "subsectors": r.get("subsectors")}
                for r in has_uncategorized
            ],
            "over_tagged": [
                {"name": r["name"], "subsectors": r.get("subsectors")}
                for r in over_tagged
            ],
        },
        "horizontal_conflicts": [
            {
                "name":       r["name"],
                "sectors":    r.get("sectors"),
                "subsectors": r.get("subsectors"),
            }
            for r in conflicts
        ],
        "taxonomy_drift": [
            {
                "name":               r["name"],
                "website":            r.get("website"),
                "unknown_subsectors": [
                    s for s in (r.get("subsectors") or [])
                    if s not in taxonomy_subs
                ],
            }
            for r in drift
        ],
    }

    with open("audit_report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"  Rapport exporté → {BOLD}audit_report.json{RESET}\n")


if __name__ == "__main__":
    main()
