# pip install fastapi uvicorn supabase python-dotenv
# python graph_app.py  →  open http://localhost:8000

import json
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from storage import _client
from main import ingest as ingest_startup

load_dotenv()

app = FastAPI()
app.mount("/assets", StaticFiles(directory="assets"), name="assets")

SECTOR_COLORS_JS = """
const SECTOR_COLORS = {
  "AI & Machine Learning":      "#4e79a7",
  "Cybersecurity":              "#e15759",
  "SaaS & Enterprise Software": "#59a14f",
  "FinTech":                    "#f28e2b",
  "HealthTech":                 "#ff9da7",
  "Developer Tools":            "#9c755f",
  "Robotics":                   "#bab0ac",
  "CleanTech":                  "#76b7b2",
  "EdTech":                     "#edc948",
  "E-commerce & Retail":        "#b07aa1",
  "Marketing Tech":             "#d4a6c8",
  "Deep Tech":                  "#499894",
  "Life Sciences":              "#86bcb6",
  "Aerospace":                  "#8cd17d",
  "Energy":                     "#f1ce63",
  "Mobility":                   "#a0cbe8",
  "SpaceTech":                  "#c7a8d8",
  "Defense":                    "#d7b5a6",
};
const DEFAULT_COLOR = "#777";
function sectorColor(sectors) {
  return SECTOR_COLORS[(sectors || [])[0]] || DEFAULT_COLOR;
}
"""


# ── API ───────────────────────────────────────────────────────────────────────

@app.get("/api/search")
def api_search(q: str = ""):
    q = q.strip()
    if len(q) < 2:
        return []
    db = _client()
    rows = (
        db.table("compspro")
        .select("name, sectors, subsectors, description, flaticon_url, website")
        .or_(f"name.ilike.%{q}%,website.ilike.%{q}%")
        .limit(10)
        .execute()
    )
    return rows.data or []


@app.post("/api/ingest")
async def api_ingest(url: str):
    url = url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL manquante.")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        result = await ingest_startup(url)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Échec du scraping ou de l'analyse : {e}")
    return result


@app.get("/api/graph/all")
def api_graph_all():
    db = _client()

    # Supabase caps responses at 1000 rows — paginate to fetch every startup
    startups: list[dict] = []
    page, size = 0, 1000
    while True:
        batch = (
            db.table("compspro")
            .select("name, sectors, flaticon_url, logo_url, description, website, linkedin_url")
            .range(page, page + size - 1)
            .execute()
            .data or []
        )
        startups.extend(batch)
        if len(batch) < size:
            break
        page += size

    # Same 1000-row cap as the startups query above — paginate or links get silently truncated
    links_raw: list[dict] = []
    page, size = 0, 1000
    while True:
        batch = (
            db.table("competitors")
            .select("company_a, company_b, score")
            .eq("active", True)
            .range(page, page + size - 1)
            .execute()
            .data or []
        )
        links_raw.extend(batch)
        if len(batch) < size:
            break
        page += size
    nodes = [
        {
            "name":        s["name"],
            "sectors":     s.get("sectors")     or [],
            "flaticon_url":    s.get("flaticon_url")    or "",
            "logo_url":    s.get("logo_url")    or "",
            "description": s.get("description") or "",
            "website":     s.get("website")     or "",
            "linkedin_url": s.get("linkedin_url") or "",
        }
        for s in startups
    ]
    # Drop links whose endpoints are missing from nodes — one bad link
    # would make d3.forceLink throw and blank the whole graph
    node_names = {n["name"] for n in nodes}
    links = [
        {"source": r["company_a"], "target": r["company_b"], "score": r.get("score") or 0}
        for r in links_raw
        if r["company_a"] in node_names and r["company_b"] in node_names
    ]
    return {"nodes": nodes, "links": links}


@app.get("/api/graph/{name}")
def api_graph(name: str):
    db = _client()

    as_a = db.table("competitors").select("company_a, company_b, score").eq("company_a", name).eq("active", True).execute().data or []
    as_b = db.table("competitors").select("company_a, company_b, score").eq("company_b", name).eq("active", True).execute().data or []

    links: list[dict] = []
    competitor_names: set[str] = set()
    seen: set[frozenset] = set()

    for row in as_a:
        b, score = row["company_b"], row.get("score", 0)
        pair = frozenset({name, b})
        if pair not in seen:
            seen.add(pair)
            links.append({"source": name, "target": b, "score": score})
        competitor_names.add(b)

    for row in as_b:
        a, score = row["company_a"], row.get("score", 0)
        pair = frozenset({name, a})
        if pair not in seen:
            seen.add(pair)
            links.append({"source": a, "target": name, "score": score})
        competitor_names.add(a)

    all_names = list(competitor_names | {name})
    startups = (
        db.table("compspro")
        .select("name, sectors, subsectors, description, website, flaticon_url, logo_url, linkedin_url")
        .in_("name", all_names)
        .execute()
        .data or []
    )
    sm = {s["name"]: s for s in startups}

    def node(n: str) -> dict:
        s = sm.get(n, {})
        return {
            "name":        n,
            "sectors":     s.get("sectors")     or [],
            "subsectors":  s.get("subsectors")  or [],
            "description": s.get("description") or "",
            "website":     s.get("website")     or "",
            "flaticon_url":    s.get("flaticon_url")    or "",
            "logo_url":    s.get("logo_url")    or "",
            "linkedin_url": s.get("linkedin_url") or "",
        }

    return {
        "center": node(name),
        "nodes":  [node(n) for n in competitor_names],
        "links":  links,
    }


# ── Pages ─────────────────────────────────────────────────────────────────────

SEARCH_HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>SM Project</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: #0f0f0f; color: #e0e0e0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      min-height: 100vh; display: flex; flex-direction: column;
      align-items: center; justify-content: center; padding: 40px 20px 40px;
    }}
    h1 {{ font-size: 2.4rem; font-weight: 700; letter-spacing: -0.02em; color: #fff; margin-bottom: 28px; }}
    .subtitle {{ color: #555; font-size: 0.95rem; margin-bottom: 48px; }}
    #search-wrap {{ width: 100%; max-width: 600px; display: flex; gap: 10px; }}
    #search {{
      flex: 1; min-width: 0; padding: 16px 20px; font-size: 16px;
      background: #1a1a1a; border: 1px solid #2e2e2e; border-radius: 12px;
      color: #eee; outline: none; transition: border-color 0.15s;
    }}
    #search::placeholder {{ color: #444; }}
    #search:focus {{ border-color: #555; }}
    #add-btn {{
      display: none; flex-shrink: 0; padding: 0 22px; font-size: 15px; font-weight: 600;
      background: #1a1a1a; border: 1px solid #2e2e2e; border-radius: 12px;
      color: #eee; cursor: pointer; transition: border-color 0.15s, background 0.15s, opacity 0.15s;
    }}
    #add-btn:hover {{ border-color: #555; background: #222; }}
    #add-btn:disabled {{ cursor: default; opacity: 0.6; }}
    .error {{ color: #d16565; font-size: 14px; text-align: center; padding: 24px 0; }}
    #results {{ width: 100%; max-width: 600px; margin-top: 12px; display: flex; flex-direction: column; gap: 8px; }}
    .card {{
      background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 10px;
      padding: 14px 16px; cursor: pointer; transition: border-color 0.15s, background 0.15s;
      display: flex; gap: 14px; align-items: flex-start;
    }}
    .card:hover {{ border-color: #444; background: #222; }}
    .card-logo {{
      width: 40px; height: 40px; border-radius: 50%; object-fit: cover; flex-shrink: 0;
    }}
    .card-logo-initial {{
      width: 40px; height: 40px; border-radius: 50%; flex-shrink: 0;
      display: flex; align-items: center; justify-content: center;
      font-size: 16px; font-weight: 700; color: #111;
    }}
    .card-body {{ flex: 1; min-width: 0; }}
    .card-name {{ font-size: 15px; font-weight: 600; color: #fff; margin-bottom: 8px; }}
    .badges {{ display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 8px; }}
    .badge {{ font-size: 11px; padding: 3px 9px; border-radius: 20px; font-weight: 500; color: #111; white-space: nowrap; }}
    .card-desc {{
      font-size: 12px; color: #888; line-height: 1.5;
      display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
    }}
    .empty {{ color: #555; font-size: 14px; text-align: center; padding: 24px 0; }}
    #graph-link {{ color: #555; font-size: 13px; margin-top: 10px; text-decoration: none; transition: color 0.15s; align-self: flex-end; }}
    #graph-link:hover {{ color: #aaa; }}
  </style>
</head>
<body>
  <h1>SM Project</h1>
  <div id="search-wrap">
    <input id="search" type="text" placeholder="Search a startup…" autocomplete="off">
    <button id="add-btn" type="button">Ajouter</button>
  </div>
  <a href="/graph" id="graph-link">View full graph →</a>
  <div id="results"></div>

  <script>
{SECTOR_COLORS_JS}

const searchEl  = document.getElementById("search");
const resultsEl = document.getElementById("results");
const addBtn    = document.getElementById("add-btn");
let timer;
let lastQuery = "";

searchEl.addEventListener("input", () => {{
  clearTimeout(timer);
  timer = setTimeout(() => doSearch(searchEl.value.trim()), 300);
}});

resultsEl.addEventListener("click", e => {{
  const card = e.target.closest(".card");
  if (card) go(card.dataset.name);
}});

function doSearch(q) {{
  lastQuery = q;
  if (q.length < 2) {{ resultsEl.innerHTML = ""; addBtn.style.display = "none"; return; }}
  fetch("/api/search?q=" + encodeURIComponent(q))
    .then(r => r.json())
    .then(render);
}}

function render(data) {{
  if (!data.length) {{
    resultsEl.innerHTML = '<div class="empty">No startup found</div>';
    addBtn.style.display = "inline-block";
    return;
  }}
  addBtn.style.display = "none";
  resultsEl.innerHTML = data.map(s => {{
    const badges = (s.sectors || []).map(sec =>
      `<span class="badge" style="background:${{SECTOR_COLORS[sec] || DEFAULT_COLOR}}">${{sec}}</span>`
    ).join("");
    const color   = sectorColor(s.sectors);
    const initial = (s.name || "?")[0].toUpperCase();
    const logoHtml = s.flaticon_url
      ? `<img class="card-logo" src="${{s.flaticon_url}}" alt="">`
      : `<div class="card-logo-initial" style="background:${{color}}">${{initial}}</div>`;
    return `<div class="card" data-name="${{s.name}}">
      ${{logoHtml}}
      <div class="card-body">
        <div class="card-name">${{s.name}}</div>
        <div class="badges">${{badges}}</div>
        <div class="card-desc">${{s.description || ""}}</div>
      </div>
    </div>`;
  }}).join("");
}}

function go(name) {{
  window.location.href = "/startup/" + encodeURIComponent(name);
}}

addBtn.addEventListener("click", () => {{
  const url = lastQuery;
  if (!url) return;
  addBtn.disabled = true;
  addBtn.textContent = "Ajout en cours…";
  fetch("/api/ingest?url=" + encodeURIComponent(url), {{ method: "POST" }})
    .then(async r => {{
      const body = await r.json();
      if (!r.ok) throw new Error(body.detail || "Erreur inconnue");
      return body;
    }})
    .then(result => {{ go(result.name); }})
    .catch(err => {{
      addBtn.disabled = false;
      addBtn.textContent = "Ajouter";
      resultsEl.innerHTML = `<div class="error">${{err.message}}</div>`;
    }});
}});
  </script>
</body>
</html>"""


GRAPH_HTML_TEMPLATE = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title id="page-title">Loading…</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: #0f0f0f; color: #e0e0e0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      height: 100vh; overflow: hidden; display: flex; flex-direction: column;
    }}

    #topbar {{
      height: 48px; background: #111; border-bottom: 1px solid #1e1e1e;
      display: flex; align-items: center; padding: 0 16px; gap: 14px; flex-shrink: 0;
    }}
    #back {{ color: #888; text-decoration: none; font-size: 20px; line-height: 1; transition: color 0.15s; }}
    #back:hover {{ color: #fff; }}
    #page-name {{ font-size: 14px; font-weight: 600; color: #ccc; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}

    #main {{ flex: 1; display: flex; overflow: hidden; }}

    #graph-col {{ flex: 0 0 100%; position: relative; }}
    svg#graph {{ width: 100%; height: 100%; display: block; }}
    #empty-msg {{
      position: absolute; top: 50%; left: 50%; transform: translate(-50%,-50%);
      color: #444; font-size: 14px; text-align: center;
    }}

    #panel {{
      flex: 0 0 30%; background: #111; border-left: 1px solid #1e1e1e;
      padding: 24px 20px; overflow-y: auto; display: none; flex-direction: column; gap: 16px;
    }}
    #panel-logo-row {{ display: flex; align-items: center; gap: 10px; align-self: flex-start; }}
    #panel-logo {{
      width: 80px; height: 80px; border-radius: 12px; object-fit: contain;
      background: #1a1a1a; border: 1px solid #222; padding: 6px;
    }}
    #panel-logo-download {{
      display: none; width: 28px; height: 28px; align-items: center; justify-content: center;
      background: #1a1a1a; border: 1px solid #2e2e2e; border-radius: 8px;
      color: #888; text-decoration: none; font-size: 15px; flex-shrink: 0;
      transition: background 0.15s, color 0.15s;
    }}
    #panel-logo-download:hover {{ background: #222; color: #fff; }}
    #panel-name {{ font-size: 20px; font-weight: 700; color: #fff; line-height: 1.3; }}
    #panel-link {{ font-size: 12px; color: #555; text-decoration: none; word-break: break-all; transition: color 0.15s; }}
    #panel-link:hover {{ color: #888; }}
    .badges {{ display: flex; flex-wrap: wrap; gap: 6px; }}
    .badge {{ font-size: 11px; padding: 3px 9px; border-radius: 20px; font-weight: 500; color: #111; white-space: nowrap; }}
    #panel-subsectors {{ display: flex; flex-direction: column; gap: 4px; }}
    .subsector-tag {{ font-size: 12px; color: #888; }}
    #panel-desc {{ font-size: 13px; color: #aaa; line-height: 1.6; }}
    #panel-view-btn {{
      display: none; margin-top: 8px;
      background: #1e1e1e; border: 1px solid #333; border-radius: 8px;
      color: #ccc; padding: 10px 16px; font-size: 13px; cursor: pointer;
      text-align: center; text-decoration: none; transition: background 0.15s, color 0.15s;
    }}
    #panel-view-btn:hover {{ background: #2a2a2a; color: #fff; }}

    .node circle {{ cursor: pointer; stroke-width: 2px; transition: stroke 0.15s; }}
    .node circle.selected {{ stroke: #fff !important; stroke-width: 3px; }}
    .node text {{ font-size: 11px; fill: #ccc; pointer-events: none; }}
    .link {{ stroke: #aaa; fill: none; }}
  </style>
</head>
<body>
  <div id="topbar">
    <a id="back" href="/">&#8592;</a>
    <span id="page-name">Loading…</span>
  </div>
  <div id="main">
    <div id="graph-col">
      <svg id="graph"></svg>
      <div id="empty-msg" style="display:none">No competitors found in the graph.</div>
    </div>
    <div id="panel">
      <div id="panel-logo-row">
        <img id="panel-logo" src="" alt="" style="display:none">
        <a id="panel-logo-download" href="#" download title="Télécharger le logo">&#8681;</a>
      </div>
      <div id="panel-name">—</div>
      <a id="panel-link" href="#" target="_blank" style="display:none"></a>
      <a id="panel-linkedin" href="#" target="_blank" style="display:none; font-size:12px; color:#0a66c2;">LinkedIn →</a>
      <div class="badges" id="panel-badges"></div>
      <div id="panel-subsectors"></div>
      <div id="panel-desc"></div>
      <a id="panel-view-btn">View graph →</a>
    </div>
  </div>

  <script src="https://d3js.org/d3.v7.min.js"></script>
  <script>
{SECTOR_COLORS_JS}

const STARTUP_NAME = __STARTUP_NAME_JSON__;

document.getElementById("page-title").textContent = STARTUP_NAME;
document.getElementById("page-name").textContent  = STARTUP_NAME;

function showPanel(node, isCenter) {{
  document.getElementById("panel").style.display = "flex";
  document.getElementById("graph-col").style.flex = "0 0 70%";
  const logoEl = document.getElementById("panel-logo");
  const logoDlEl = document.getElementById("panel-logo-download");
  const logoSrc = node.logo_url || node.flaticon_url;
  if (logoSrc) {{
    logoEl.src           = logoSrc;
    logoEl.style.display = "";
    logoDlEl.href        = logoSrc;
    logoDlEl.download    = node.name.replace(/[^a-z0-9]+/gi, "_") + "_logo" + logoSrc.slice(logoSrc.lastIndexOf("."));
    logoDlEl.style.display = "flex";
  }} else {{
    logoEl.style.display = "none";
    logoDlEl.style.display = "none";
  }}

  document.getElementById("panel-name").textContent = node.name;

  const linkEl = document.getElementById("panel-link");
  if (node.website) {{
    linkEl.href = node.website; linkEl.textContent = node.website; linkEl.style.display = "";
  }} else {{
    linkEl.style.display = "none";
  }}

  const linkedinEl = document.getElementById("panel-linkedin");
  if (node.linkedin_url) {{
    linkedinEl.href = node.linkedin_url; linkedinEl.style.display = "";
  }} else {{
    linkedinEl.style.display = "none";
  }}

  document.getElementById("panel-badges").innerHTML = (node.sectors || []).map(s =>
    `<span class="badge" style="background:${{sectorColor([s])}}; color:#111">${{s}}</span>`
  ).join("");

  document.getElementById("panel-subsectors").innerHTML = (node.subsectors || []).map(s =>
    `<span class="subsector-tag">· ${{s}}</span>`
  ).join("");

  document.getElementById("panel-desc").textContent = node.description || "";

  const btn = document.getElementById("panel-view-btn");
  if (!isCenter) {{
    btn.style.display = "";
    btn.onclick = () => {{ window.location.href = "/startup/" + encodeURIComponent(node.name); }};
  }} else {{
    btn.style.display = "none";
  }}
}}

fetch("/api/graph/" + encodeURIComponent(STARTUP_NAME))
  .then(r => r.json())
  .then(data => {{
    const {{ center, nodes, links }} = data;

    showPanel(center, true);

    if (!nodes.length) {{
      document.getElementById("empty-msg").style.display = "";
      return;
    }}

    const W   = document.getElementById("graph-col").clientWidth;
    const H   = document.getElementById("graph-col").clientHeight;
    const svg = d3.select("svg#graph").attr("width", W).attr("height", H);

    const defs    = svg.append("defs");
    const allNodes = [{{ ...center, _isCenter: true }}, ...nodes.map(n => ({{ ...n, _isCenter: false }}))];
    const allLinks = links;

    // Works before and after D3 resolves string refs to objects
    const nameOf = x => (typeof x === "object" ? x.name : x);

    const scoreOf = n => {{
      const link = allLinks.find(l =>
        (nameOf(l.source) === n.name || nameOf(l.target) === n.name) &&
        (nameOf(l.source) === center.name || nameOf(l.target) === center.name)
      );
      return link ? (link.score || 0) : 0;
    }};

    const sim = d3.forceSimulation(allNodes)
      .force("link",      d3.forceLink(allLinks).id(d => d.name).distance(160))
      .force("charge",    d3.forceManyBody().strength(-350))
      .force("center",    d3.forceCenter(W / 2, H / 2))
      .force("collision", d3.forceCollide().radius(d => d._isCenter ? 36 : 14 + scoreOf(d) * 14));

    const linkSel = svg.append("g")
      .selectAll("line")
      .data(allLinks)
      .join("line")
        .attr("class", "link")
        .attr("stroke-width",   d => 1.5 + d.score * 5)
        .attr("stroke-opacity", d => 0.15 + d.score * 0.45);

    const nodeSel = svg.append("g")
      .selectAll("g")
      .data(allNodes)
      .join("g")
        .attr("class", "node")
        .call(d3.drag()
          .on("start", (e, d) => {{ if (!e.active) sim.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; }})
          .on("drag",  (e, d) => {{ d.fx = e.x; d.fy = e.y; }})
          .on("end",   (e, d) => {{ if (!e.active) sim.alphaTarget(0); d.fx = null; d.fy = null; }})
        );

    // Circle — always rendered as click target and selection ring
    nodeSel.append("circle")
      .attr("r",      d => d._isCenter ? 30 : 10 + scoreOf(d) * 12)
      .attr("fill",   d => d.flaticon_url ? "none" : (d._isCenter ? "#ffffff" : sectorColor(d.sectors)))
      .attr("stroke", d => d._isCenter ? "#fff" : (d.flaticon_url ? sectorColor(d.sectors) : "#0f0f0f"))
      .on("click", (e, d) => {{
        nodeSel.selectAll("circle").classed("selected", false);
        d3.select(e.currentTarget).classed("selected", true);
        showPanel(d, d._isCenter);
      }});

    // Circular logo images for nodes that have flaticon_url
    nodeSel.each(function(d, i) {{
      if (!d.flaticon_url) return;
      const r = d._isCenter ? 30 : 10 + scoreOf(d) * 12;
      defs.append("clipPath")
        .attr("id", "logo-clip-" + i)
        .append("circle").attr("r", r);
      d3.select(this).append("image")
        .attr("href", d.flaticon_url)
        .attr("x", -r).attr("y", -r)
        .attr("width",  r * 2).attr("height", r * 2)
        .attr("clip-path", "url(#logo-clip-" + i + ")")
        .attr("preserveAspectRatio", "xMidYMid slice")
        .style("pointer-events", "none");
    }});

    nodeSel.append("text")
      .attr("x",            0)
      .attr("y",            d => (d._isCenter ? 30 : 10 + scoreOf(d) * 12) + 16)
      .attr("text-anchor",  "middle")
      .attr("font-weight",  d => d._isCenter ? "700" : "400")
      .style("fill",        "#ffffff")
      .style("font-size",   "11px")
      .style("stroke",         "#000000")
      .style("stroke-width",   "3px")
      .style("paint-order",    "stroke")
      .style("pointer-events", "none")
      .text(d => d.name.length > 20 ? d.name.slice(0, 18) + "…" : d.name);

    sim.on("tick", () => {{
      linkSel
        .attr("x1", d => d.source.x).attr("y1", d => d.source.y)
        .attr("x2", d => d.target.x).attr("y2", d => d.target.y);
      nodeSel.attr("transform", d => `translate(${{d.x}},${{d.y}})`);
    }});
  }});
  </script>
</body>
</html>"""


GLOBAL_GRAPH_HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Full Startup Graph</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: #0f0f0f; color: #e0e0e0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      height: 100vh; overflow: hidden; display: flex; flex-direction: column;
    }}
    #topbar {{
      height: 48px; background: #111; border-bottom: 1px solid #1e1e1e;
      display: flex; align-items: center; padding: 0 16px; gap: 14px; flex-shrink: 0;
    }}
    #back {{ color: #888; text-decoration: none; font-size: 20px; line-height: 1; transition: color 0.15s; }}
    #back:hover {{ color: #fff; }}
    #topbar-search {{ position: relative; margin-left: auto; width: 260px; }}
    #topbar-search-input {{
      width: 100%; padding: 8px 12px; font-size: 13px;
      background: #1a1a1a; border: 1px solid #2e2e2e; border-radius: 8px;
      color: #eee; outline: none; transition: border-color 0.15s;
    }}
    #topbar-search-input::placeholder {{ color: #444; }}
    #topbar-search-input:focus {{ border-color: #555; }}
    #topbar-search-results {{
      position: absolute; top: calc(100% + 6px); right: 0; width: 100%;
      max-height: 320px; overflow-y: auto; z-index: 10;
      background: #161616; border: 1px solid #2a2a2a; border-radius: 10px;
      display: none; flex-direction: column;
    }}
    #topbar-search-results.open {{ display: flex; }}
    .search-result {{
      padding: 9px 12px; cursor: pointer; display: flex; align-items: center; gap: 10px;
      border-bottom: 1px solid #222; transition: background 0.15s;
    }}
    .search-result:last-child {{ border-bottom: none; }}
    .search-result:hover {{ background: #222; }}
    .search-result-logo {{
      width: 24px; height: 24px; border-radius: 50%; object-fit: cover; flex-shrink: 0;
    }}
    .search-result-logo-initial {{
      width: 24px; height: 24px; border-radius: 50%; flex-shrink: 0;
      display: flex; align-items: center; justify-content: center;
      font-size: 11px; font-weight: 700; color: #111;
    }}
    .search-result-name {{ font-size: 13px; color: #eee; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .search-result-empty {{ padding: 12px; font-size: 12px; color: #555; text-align: center; }}
    #main {{ flex: 1; display: flex; overflow: hidden; }}
    #graph-col {{ flex: 0 0 100%; position: relative; }}
    svg#graph {{ width: 100%; height: 100%; display: block; cursor: grab; }}
    svg#graph:active {{ cursor: grabbing; }}
    #panel {{
      flex: 0 0 30%; background: #111; border-left: 1px solid #1e1e1e;
      padding: 24px 20px; overflow-y: auto; display: none; flex-direction: column; gap: 16px;
      position: relative;
    }}
    #panel-close {{
      position: absolute; top: 16px; right: 16px; width: 28px; height: 28px;
      display: flex; align-items: center; justify-content: center;
      background: transparent; border: none; border-radius: 50%;
      color: #666; font-size: 18px; line-height: 1; cursor: pointer;
      transition: background 0.15s, color 0.15s;
    }}
    #panel-close:hover {{ background: #222; color: #fff; }}
    #panel-logo-row {{ display: flex; align-items: center; gap: 10px; align-self: flex-start; }}
    #panel-logo {{
      width: 80px; height: 80px; border-radius: 12px; object-fit: contain;
      background: #1a1a1a; border: 1px solid #222; padding: 6px;
    }}
    #panel-logo-download {{
      display: none; width: 28px; height: 28px; align-items: center; justify-content: center;
      background: #1a1a1a; border: 1px solid #2e2e2e; border-radius: 8px;
      color: #888; text-decoration: none; font-size: 15px; flex-shrink: 0;
      transition: background 0.15s, color 0.15s;
    }}
    #panel-logo-download:hover {{ background: #222; color: #fff; }}
    #panel-name {{ font-size: 20px; font-weight: 700; color: #fff; line-height: 1.3; padding-right: 24px; }}
    #panel-link {{ font-size: 12px; color: #555; text-decoration: none; word-break: break-all; transition: color 0.15s; }}
    #panel-link:hover {{ color: #888; }}
    .badges {{ display: flex; flex-wrap: wrap; gap: 6px; }}
    .badge {{ font-size: 11px; padding: 3px 9px; border-radius: 20px; font-weight: 500; color: #111; white-space: nowrap; }}
    #panel-desc {{ font-size: 13px; color: #aaa; line-height: 1.6; }}
    #panel-hint {{ font-size: 13px; color: #444; }}
    #panel-view-btn {{
      display: none; margin-top: 8px;
      background: #1e1e1e; border: 1px solid #333; border-radius: 8px;
      color: #ccc; padding: 10px 16px; font-size: 13px;
      text-align: center; text-decoration: none; transition: background 0.15s, color 0.15s;
    }}
    #panel-view-btn:hover {{ background: #2a2a2a; color: #fff; }}
    .node circle {{ cursor: pointer; stroke-width: 2px; transition: stroke 0.15s; }}
    .node circle.selected {{ stroke: #fff !important; stroke-width: 3px; }}
    .node text {{ font-size: 11px; fill: #ccc; pointer-events: none; }}
    .link {{ stroke: #aaa; fill: none; }}
  </style>
</head>
<body>
  <div id="topbar">
    <a id="back" href="/">&#8592;</a>
    <div id="topbar-search">
      <input id="topbar-search-input" type="text" placeholder="Search a startup…" autocomplete="off">
      <div id="topbar-search-results"></div>
    </div>
  </div>
  <div id="main">
    <div id="graph-col">
      <svg id="graph"></svg>
    </div>
    <div id="panel">
      <button id="panel-close" aria-label="Close" title="Close">&#10005;</button>
      <div id="panel-logo-row">
        <img id="panel-logo" src="" alt="" style="display:none">
        <a id="panel-logo-download" href="#" download title="Télécharger le logo">&#8681;</a>
      </div>
      <div id="panel-name">Click a node</div>
      <a id="panel-link" href="#" target="_blank" style="display:none"></a>
      <a id="panel-linkedin" href="#" target="_blank" style="display:none; font-size:12px; color:#0a66c2;">LinkedIn →</a>
      <div class="badges" id="panel-badges"></div>
      <div id="panel-desc"></div>
      <a id="panel-view-btn">View graph →</a>
    </div>
  </div>
  <script src="https://d3js.org/d3.v7.min.js"></script>
  <script>
{SECTOR_COLORS_JS}

function showPanel(node) {{
  document.getElementById("panel").style.display = "flex";
  document.getElementById("graph-col").style.flex = "0 0 70%";
  const logoEl = document.getElementById("panel-logo");
  const logoDlEl = document.getElementById("panel-logo-download");
  const logoSrc = node.logo_url || node.flaticon_url;
  if (logoSrc) {{
    logoEl.src           = logoSrc;
    logoEl.style.display = "";
    logoDlEl.href        = logoSrc;
    logoDlEl.download    = node.name.replace(/[^a-z0-9]+/gi, "_") + "_logo" + logoSrc.slice(logoSrc.lastIndexOf("."));
    logoDlEl.style.display = "flex";
  }} else {{
    logoEl.style.display = "none";
    logoDlEl.style.display = "none";
  }}
  document.getElementById("panel-name").textContent = node.name;
  const linkEl = document.getElementById("panel-link");
  if (node.website) {{
    linkEl.href = node.website; linkEl.textContent = node.website; linkEl.style.display = "";
  }} else {{
    linkEl.style.display = "none";
  }}
  const linkedinEl = document.getElementById("panel-linkedin");
  if (node.linkedin_url) {{
    linkedinEl.href = node.linkedin_url; linkedinEl.style.display = "";
  }} else {{
    linkedinEl.style.display = "none";
  }}
  document.getElementById("panel-badges").innerHTML = (node.sectors || []).map(s =>
    `<span class="badge" style="background:${{sectorColor([s])}}; color:#111">${{s}}</span>`
  ).join("");
  document.getElementById("panel-desc").textContent = node.description || "";
  const btn = document.getElementById("panel-view-btn");
  btn.style.display = "";
  btn.onclick = () => {{ window.location.href = "/startup/" + encodeURIComponent(node.name); }};
}}

fetch("/api/graph/all")
  .then(r => r.json())
  .then(data => {{
    const nodes = data.nodes;
    const links = data.links;
    const nodeByName = new Map(nodes.map(n => [n.name, n]));

    // Compute degree before D3 resolves link source/target to objects
    const deg = {{}};
    links.forEach(l => {{
      deg[l.source] = (deg[l.source] || 0) + 1;
      deg[l.target] = (deg[l.target] || 0) + 1;
    }});
    const maxDeg = Math.max(...Object.values(deg), 1);
    const nodeR = n => 5 + ((deg[n.name] || 0) / maxDeg) * 15;

    const W = document.getElementById("graph-col").clientWidth;
    const H = document.getElementById("graph-col").clientHeight;
    const svg = d3.select("svg#graph").attr("width", W).attr("height", H);

    const g = svg.append("g");
    const defs = svg.append("defs");

    const zoom = d3.zoom().scaleExtent([0.05, 4]).on("zoom", e => g.attr("transform", e.transform));
    svg.call(zoom);

    const sim = d3.forceSimulation(nodes)
      .force("link",      d3.forceLink(links).id(d => d.name).distance(120))
      .force("charge",    d3.forceManyBody().strength(-300))
      .force("center",    d3.forceCenter(W / 2, H / 2))
      .force("collision", d3.forceCollide().radius(d => nodeR(d) + 4));

    const linkSel = g.append("g")
      .selectAll("line")
      .data(links)
      .join("line")
        .attr("class", "link")
        .attr("stroke-width",   d => 1 + (d.score || 0) * 4)
        .attr("stroke-opacity", d => 0.15 + (d.score || 0) * 0.45);

    const nodeSel = g.append("g")
      .selectAll("g")
      .data(nodes)
      .join("g")
        .attr("class", "node")
        .call(d3.drag()
          .on("start", (e, d) => {{ if (!e.active) sim.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; }})
          .on("drag",  (e, d) => {{ d.fx = e.x; d.fy = e.y; }})
          .on("end",   (e, d) => {{ if (!e.active) sim.alphaTarget(0); d.fx = null; d.fy = null; }})
        );

    function selectNode(d) {{
      nodeSel.selectAll("circle").classed("selected", false);
      nodeSel.filter(n => n.name === d.name).select("circle").classed("selected", true);
      showPanel(d);
    }}

    document.getElementById("panel-close").addEventListener("click", () => {{
      document.getElementById("panel").style.display = "none";
      document.getElementById("graph-col").style.flex = "0 0 100%";
      nodeSel.selectAll("circle").classed("selected", false);
    }});

    function centerOnNode(d) {{
      const scale = Math.max(d3.zoomTransform(svg.node()).k, 1.4);
      const transform = d3.zoomIdentity
        .translate(W / 2, H / 2)
        .scale(scale)
        .translate(-d.x, -d.y);
      svg.transition().duration(600).call(zoom.transform, transform);
    }}

    nodeSel.append("circle")
      .attr("r",      nodeR)
      .attr("fill",   d => d.flaticon_url ? "none" : sectorColor(d.sectors))
      .attr("stroke", d => d.flaticon_url ? sectorColor(d.sectors) : "#0f0f0f")
      .on("click", (e, d) => {{
        selectNode(d);
        e.stopPropagation();
      }});

    nodeSel.each(function(d, i) {{
      if (!d.flaticon_url) return;
      const r = nodeR(d);
      defs.append("clipPath")
        .attr("id", "logo-clip-" + i)
        .append("circle").attr("r", r);
      d3.select(this).append("image")
        .attr("href", d.flaticon_url)
        .attr("x", -r).attr("y", -r)
        .attr("width",  r * 2).attr("height", r * 2)
        .attr("clip-path", "url(#logo-clip-" + i + ")")
        .attr("preserveAspectRatio", "xMidYMid slice")
        .style("pointer-events", "none");
    }});

    nodeSel.append("text")
      .attr("x",           0)
      .attr("y",           d => nodeR(d) + 16)
      .attr("text-anchor", "middle")
      .style("fill",         "#ffffff")
      .style("font-size",    "11px")
      .style("stroke",       "#000000")
      .style("stroke-width", "3px")
      .style("paint-order",  "stroke")
      .style("pointer-events", "none")
      .text(d => d.name.length > 20 ? d.name.slice(0, 18) + "…" : d.name);

    sim.on("tick", () => {{
      linkSel
        .attr("x1", d => d.source.x).attr("y1", d => d.source.y)
        .attr("x2", d => d.target.x).attr("y2", d => d.target.y);
      nodeSel.attr("transform", d => `translate(${{d.x}},${{d.y}})`);
    }});

    // ── Search bar ──────────────────────────────────────────────────────────
    const searchInput   = document.getElementById("topbar-search-input");
    const searchResults = document.getElementById("topbar-search-results");
    let searchTimer;

    function jumpToNode(name) {{
      const d = nodeByName.get(name);
      if (!d) return;
      searchInput.value = "";
      searchResults.classList.remove("open");
      searchResults.innerHTML = "";
      selectNode(d);
      centerOnNode(d);
    }}

    function renderSearchResults(list) {{
      if (!list.length) {{
        searchResults.innerHTML = '<div class="search-result-empty">No startup found</div>';
        searchResults.classList.add("open");
        return;
      }}
      searchResults.innerHTML = list.map(s => {{
        const color   = sectorColor(s.sectors);
        const initial = (s.name || "?")[0].toUpperCase();
        const logoHtml = s.flaticon_url
          ? `<img class="search-result-logo" src="${{s.flaticon_url}}" alt="">`
          : `<div class="search-result-logo-initial" style="background:${{color}}">${{initial}}</div>`;
        return `<div class="search-result" data-name="${{s.name}}">
          ${{logoHtml}}
          <div class="search-result-name">${{s.name}}</div>
        </div>`;
      }}).join("");
      searchResults.classList.add("open");
    }}

    searchInput.addEventListener("input", () => {{
      clearTimeout(searchTimer);
      const q = searchInput.value.trim();
      if (q.length < 2) {{
        searchResults.classList.remove("open");
        searchResults.innerHTML = "";
        return;
      }}
      searchTimer = setTimeout(() => {{
        fetch("/api/search?q=" + encodeURIComponent(q))
          .then(r => r.json())
          .then(renderSearchResults);
      }}, 300);
    }});

    searchResults.addEventListener("click", e => {{
      const card = e.target.closest(".search-result");
      if (card) jumpToNode(card.dataset.name);
    }});

    document.addEventListener("click", e => {{
      if (!document.getElementById("topbar-search").contains(e.target)) {{
        searchResults.classList.remove("open");
      }}
    }});
  }})
  .catch(err => {{
    console.error("Erreur de chargement du graphe :", err);
    document.title = "Erreur de chargement — Full Startup Graph";
  }});
  </script>
</body>
</html>"""


@app.get("/")
def index():
    return HTMLResponse(content=SEARCH_HTML)


@app.get("/graph")
def graph_page():
    return HTMLResponse(content=GLOBAL_GRAPH_HTML)


@app.get("/startup/{name}")
def startup_page(name: str):
    html = GRAPH_HTML_TEMPLATE.replace("__STARTUP_NAME_JSON__", json.dumps(name))
    return HTMLResponse(content=html)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
