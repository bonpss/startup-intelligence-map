import json
from collections import Counter
import networkx as nx
from dotenv import load_dotenv
from storage import _client

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

TOP_N_BETWEENNESS   = 20
TOP_N_COMMUNITIES   = 15
MIN_COMMUNITY_SIZE  = 3   # communities smaller than this are noise, not a real cluster
MIN_SPLIT_MEMBERS   = 3   # a subsector's slice of a community must be at least this big to count


def fetch_nodes() -> list[dict]:
    client = _client()
    rows, page, size = [], 0, 1000
    while True:
        batch = (
            client.table("compspro")
            .select("name, sectors, subsectors")
            .range(page, page + size - 1)
            .execute()
            .data or []
        )
        rows.extend(batch)
        if len(batch) < size:
            break
        page += size
    return rows


def fetch_edges() -> list[dict]:
    client = _client()
    rows, page, size = [], 0, 1000
    while True:
        batch = (
            client.table("competitors")
            .select("company_a, company_b, score")
            .eq("active", True)
            .range(page, page + size - 1)
            .execute()
            .data or []
        )
        rows.extend(batch)
        if len(batch) < size:
            break
        page += size
    return rows


def build_graph(nodes: list[dict], edges: list[dict]) -> nx.Graph:
    G = nx.Graph()
    for n in nodes:
        G.add_node(
            n["name"],
            sectors=n.get("sectors") or [],
            subsectors=n.get("subsectors") or [],
        )
    for e in edges:
        a, b, score = e["company_a"], e["company_b"], e.get("score") or 0
        if a not in G or b not in G:
            continue  # dangling reference, same guard as graph_app.py
        # betweenness treats "weight" as distance (shorter = more likely on a
        # shortest path); our score is a similarity (higher = closer), so invert it
        G.add_edge(a, b, weight=score, distance=max(1e-6, 1 - score))
    return G


def _bar(n: int, max_width: int = 30) -> str:
    return "█" * min(n, max_width)


def main() -> None:
    print(f"\n  Chargement des données Supabase…", end="", flush=True)
    nodes, edges = fetch_nodes(), fetch_edges()
    print(f" {len(nodes)} startups, {len(edges)} liens chargés.\n")

    G = build_graph(nodes, edges)
    connected_nodes = [n for n in G.nodes if G.degree(n) > 0]
    isolates = len(G.nodes) - len(connected_nodes)

    # ── Betweenness centrality ────────────────────────────────────────────────
    print(f"  Calcul du betweenness centrality…", end="", flush=True)
    betweenness = nx.betweenness_centrality(G, weight="distance")
    print(" fait.")

    # ── Louvain communities ───────────────────────────────────────────────────
    print(f"  Détection de communautés (Louvain)…", end="", flush=True)
    communities = nx.community.louvain_communities(G, weight="weight", seed=42)
    communities = [c for c in communities if len(c) >= MIN_COMMUNITY_SIZE]
    communities.sort(key=len, reverse=True)
    print(f" {len(communities)} communautés (>= {MIN_COMMUNITY_SIZE} membres).\n")

    subsector_of = {n["name"]: (n.get("subsectors") or []) for n in nodes}

    def dominant_subsectors(members: set, limit: int = 5) -> list[tuple[str, int]]:
        c = Counter()
        for m in members:
            for s in subsector_of.get(m, []):
                c[s] += 1
        return c.most_common(limit)

    # ── Subsector-split detection ─────────────────────────────────────────────
    # For each existing subsector tag, see how its members are scattered across
    # the detected communities. Spread across >=2 communities with a real
    # foothold in each = same pattern as the "ERP & Business Operations" case.
    subsector_members: dict[str, set] = {}
    for n in nodes:
        for s in n.get("subsectors") or []:
            subsector_members.setdefault(s, set()).add(n["name"])

    community_of: dict[str, int] = {}
    for i, c in enumerate(communities):
        for m in c:
            community_of[m] = i

    splits = []
    for sub, members in subsector_members.items():
        counts = Counter(community_of[m] for m in members if m in community_of)
        real_slices = {cid: cnt for cid, cnt in counts.items() if cnt >= MIN_SPLIT_MEMBERS}
        if len(real_slices) >= 2:
            splits.append((sub, len(members), real_slices))
    splits.sort(key=lambda x: len(x[2]), reverse=True)

    # ══════════════════════════════════════════════════════════════════════════
    # PRINT REPORT
    # ══════════════════════════════════════════════════════════════════════════
    hr  = "═" * W
    sep = "─" * W

    print(f"{hr}")
    print(f"{BOLD}  GRAPH ANALYSIS REPORT{RESET}")
    print(f"{sep}")
    print(f"  Nœuds             : {BOLD}{len(G.nodes)}{RESET}")
    print(f"  Liens             : {BOLD}{len(G.edges)}{RESET}")
    print(f"  Nœuds isolés      : {(GREEN if isolates == 0 else YELLOW)}{isolates}{RESET}")
    print(f"  Communautés       : {BOLD}{len(communities)}{RESET} (>= {MIN_COMMUNITY_SIZE} membres)")
    print(f"{hr}\n")

    # ── Section 1: betweenness ────────────────────────────────────────────────
    print(f"{BOLD}1. TOP {TOP_N_BETWEENNESS} — BETWEENNESS CENTRALITY (nœuds-pont entre marchés){RESET}")
    print(f"{DIM}   Score élevé = fait le lien entre plusieurs clusters de marché,{RESET}")
    print(f"{DIM}   souvent le signe d'un chemin de dérive comme Alma→...→Airweave.{RESET}\n")
    top_between = sorted(betweenness.items(), key=lambda x: x[1], reverse=True)[:TOP_N_BETWEENNESS]
    max_b = top_between[0][1] if top_between else 1
    for name, score in top_between:
        subs = ", ".join(subsector_of.get(name, [])[:2]) or "—"
        bar = _bar(int(30 * score / max_b)) if max_b else ""
        print(f"  {CYAN}{score:.4f}{RESET}  {bar}")
        print(f"    {DIM}{name:<30}{RESET} deg={G.degree(name):<3} {DIM}{subs}{RESET}")
    print()

    # ── Section 2: communities ────────────────────────────────────────────────
    print(f"{BOLD}2. TOP {TOP_N_COMMUNITIES} COMMUNAUTÉS DÉTECTÉES{RESET}\n")
    for i, c in enumerate(communities[:TOP_N_COMMUNITIES]):
        dom = dominant_subsectors(c)
        dom_str = ", ".join(f"{s} ({n})" for s, n in dom)
        print(f"  {BOLD}#{i}{RESET} — {len(c)} membres")
        print(f"    {DIM}subsectors dominants:{RESET} {dom_str}")
        sample = sorted(c)[:6]
        print(f"    {DIM}ex: {', '.join(sample)}{'…' if len(c) > 6 else ''}{RESET}\n")

    # ── Section 3: subsector splits ───────────────────────────────────────────
    print(f"{BOLD}3. SUBSECTORS QUI ÉCLATENT EN PLUSIEURS COMMUNAUTÉS{RESET}")
    print(f"{DIM}   Signal automatique du pattern trouvé sur \"ERP & Business Operations\" —{RESET}")
    print(f"{DIM}   candidats à un audit / split de taxonomy.{RESET}\n")
    if not splits:
        print(f"  {GREEN}✓ Aucun subsector significativement éclaté{RESET}\n")
    else:
        for sub, total, real_slices in splits[:20]:
            slices_str = ", ".join(f"#{cid} ({cnt})" for cid, cnt in sorted(real_slices.items(), key=lambda x: -x[1]))
            color = RED if len(real_slices) >= 3 else YELLOW
            print(f"  {color}▸ {sub}{RESET} ({total} membres au total)")
            print(f"    {DIM}répartition: {slices_str}{RESET}")
        print()

    # ══════════════════════════════════════════════════════════════════════════
    # JSON EXPORT
    # ══════════════════════════════════════════════════════════════════════════
    report = {
        "summary": {
            "nodes": len(G.nodes),
            "edges": len(G.edges),
            "isolates": isolates,
            "communities": len(communities),
        },
        "betweenness_top": [
            {"name": n, "score": s, "degree": G.degree(n), "subsectors": subsector_of.get(n, [])}
            for n, s in top_between
        ],
        "betweenness_all": {n: s for n, s in betweenness.items()},
        "communities": [
            {
                "id": i,
                "size": len(c),
                "members": sorted(c),
                "dominant_subsectors": dominant_subsectors(c, limit=10),
            }
            for i, c in enumerate(communities)
        ],
        "subsector_splits": [
            {
                "subsector": sub,
                "total_members": total,
                "split_across": [{"community_id": cid, "members": cnt} for cid, cnt in real_slices.items()],
            }
            for sub, total, real_slices in splits
        ],
    }

    with open("graph_analysis_report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"  Rapport exporté → {BOLD}graph_analysis_report.json{RESET}\n")


if __name__ == "__main__":
    main()
