import asyncio
import os
import sys
import time

from embeddings import embed_one
from main import scrape, fetch_and_save_favicon, fetch_and_save_real_logo, slugify, LOGO_EXTENSIONS
from extractor import extract
from storage import save_startup, _client as _db_client

URLS = [
    "https://maisa.ai",
    "https://www.mimica.ai",
    "https://pit.com",
    "https://www.sola.ai",
    "https://www.tekst.com",
    "https://www.usefini.com",
    "https://poetic.com",

    # API Infrastructure -- SUBSECTOR_DEFINITIONS tightened (2026-08-30) to the
    # "litmus test" wording (is the API/integration layer itself the product,
    # or just the delivery mechanism for something else). All 60 startups
    # currently tagged "API Infrastructure" as of that change.
    "https://actualyze.ai",
    "https://airweave.ai",
    "https://aleno.ai",
    "https://www.allium.so",
    "https://alpic.ai",
    "https://arcjet.com",
    "https://www.assemblyai.com",
    "https://www.birdi.io",
    "https://bondio.co",
    "https://bota.dev",
    "https://www.chift.eu",
    "https://www.context.dev",
    "https://www.deepseek.com",
    "https://deepgram.com",
    "https://didit.me",
    "https://www.edgee.ai",
    "https://exa.ai",
    "https://fireworks.ai",
    "https://www.flumes.ai",
    "https://www.gladia.io",
    "https://insforge.dev",
    "https://www.kugelaudio.com/en",
    "https://www.linkup.so",
    "https://www.lissi.id",
    "https://livekit.com",
    "https://paywithlocus.com",
    "https://www.merge.dev",
    "https://moonlakeai.com",
    "https://naftiko.io",
    "https://www.natural.co",
    "https://nevermined.ai",
    "https://nexos.ai",
    "https://octen.ai",
    "https://onecli.sh",
    "https://www.orthogonal.com",
    "https://oxylabs.io",
    "https://www.parasail.io",
    "https://pinata.cloud",
    "https://polar.sh",
    "https://www.project-q.ai",
    "https://www.revenuecat.com",
    "https://www.rutter.com",
    "https://www.sailresearch.com",
    "https://www.sanity.io",
    "https://seltz.ai",
    "https://simplehash.com",
    "https://smallest.ai",
    "https://www.smooth.sh",
    "https://stacklok.com",
    "https://www.stacksync.com",
    "https://supabase.com",
    "https://www.tabs.com",
    "https://www.withterminal.com",
    "https://www.theneo.io",
    "https://www.together.ai",
    "https://tracerml.ai",
    "https://you.com/home",
    "https://www.zama.org",
    "https://www.zerolook.com",
    "https://www.pyannote.ai",
]


def _rows_for_subsector(subsector: str, columns: str) -> list[dict]:
    """compspro rows currently tagged with `subsector`, selecting only `columns`.
    Shared resolution query -- urls_for_subsector() and audit_stale_competitors.py's
    names_for_subsector() import both build on this so "which startups are in this
    subsector" is answered identically everywhere, not duplicated per script.
    """
    return (
        _db_client()
        .table("compspro")
        .select(columns)
        .contains("subsectors", [subsector])
        .execute()
        .data or []
    )


def urls_for_subsector(subsector: str) -> list[str]:
    """Websites of every startup currently tagged with a given subsector."""
    return [r["website"] for r in _rows_for_subsector(subsector, "website") if r.get("website")]


def names_for_subsector(subsector: str) -> list[str]:
    """compspro.name of every startup currently tagged with a given subsector --
    used by audit_stale_competitors.py's --subsector flag so both tools resolve
    "startups in this subsector" the same way.
    """
    return [r["name"] for r in _rows_for_subsector(subsector, "name") if r.get("name")]


def process(url: str) -> dict:
    markdown, logo_candidates, linkedin_url = asyncio.run(scrape(url))
    data = extract(markdown, website=url, logo_candidates=logo_candidates, linkedin_url=linkedin_url)

    if not data.get("name"):
        raise ValueError("Could not extract startup name")

    data.setdefault("sub_subsectors", [])

    data.pop("sector_confidences", None)
    data.pop("subsector_confidences", None)

    extracted_logo_url = data.pop("logo_url", None)

    if data.get("description"):
        data["embedding"] = embed_one(data["description"])

    action = save_startup(data)

    name    = data.get("name", "unknown")
    website = data.get("website", "")
    slug    = slugify(name)

    # Favicon — displayed in graph circles
    flaticon_url = None
    for ext in LOGO_EXTENSIONS:
        if os.path.exists(f"assets/logos/{slug}.{ext}"):
            flaticon_url = f"/assets/logos/{slug}.{ext}"
            break
    if not flaticon_url:
        flaticon_url = fetch_and_save_favicon(name, website)

    # Real logo — for market maps
    logo_url = None
    for ext in LOGO_EXTENSIONS:
        if os.path.exists(f"assets/logos/{slug}_logo.{ext}"):
            logo_url = f"/assets/logos/{slug}_logo.{ext}"
            break
    if not logo_url:
        logo_url = fetch_and_save_real_logo(name, extracted_logo_url)

    updates = {}
    if flaticon_url:
        updates["flaticon_url"] = flaticon_url
    if logo_url:
        updates["logo_url"] = logo_url
    if updates:
        _db_client().table("compspro").update(updates).eq("name", name).execute()

    return {
        "name":           name,
        "sectors":        data.get("sectors", []),
        "subsectors":     data.get("subsectors", []),
        "sub_subsectors": data.get("sub_subsectors", []),
        "action":         action,
    }


if __name__ == "__main__":
    target_subsector = sys.argv[1] if len(sys.argv) > 1 else None
    urls = urls_for_subsector(target_subsector) if target_subsector else URLS

    total     = len(urls)
    ok        = 0
    errors    = []
    moved_out = []

    label = f'subsector "{target_subsector}"' if target_subsector else "hardcoded list"
    print(f"Reprocessing {total} startups from {label}...\n")

    for i, url in enumerate(urls, 1):
        print(f"[{i}/{total}] {url}")
        try:
            result = process(url)
            print(f"  ✓ {result['action']:7s}  {result['name']}")
            print(f"           sectors:        {result['sectors']}")
            print(f"           subsectors:     {result['subsectors']}")
            print(f"           sub_subsectors: {result['sub_subsectors']}")
            if target_subsector and target_subsector not in result["subsectors"]:
                moved_out.append(result)
                print(f"           → moved out of \"{target_subsector}\"")
            ok += 1
        except Exception as e:
            print(f"  ✗ ERROR — {e}")
            errors.append((url, str(e)))

        if i < total:
            time.sleep(2)

    print(f"\n{'='*60}")
    print(f"Done — {ok}/{total} OK   {len(errors)} error(s)   {len(moved_out)} moved out")
    if moved_out:
        print("\nMoved out:")
        for r in moved_out:
            print(f"  {r['name']}: sectors={r['sectors']} subsectors={r['subsectors']}")
    if errors:
        print("\nErrors:")
        for url, msg in errors:
            print(f"  {url}")
            print(f"    {msg}")
