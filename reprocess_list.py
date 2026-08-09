import asyncio
import os
import sys
import time

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
]


def urls_for_subsector(subsector: str) -> list[str]:
    """Websites of every startup currently tagged with a given subsector."""
    rows = (
        _db_client()
        .table("compspro")
        .select("website")
        .contains("subsectors", [subsector])
        .execute()
        .data or []
    )
    return [r["website"] for r in rows if r.get("website")]


def process(url: str) -> dict:
    markdown, logo_candidates = asyncio.run(scrape(url))
    data = extract(markdown, website=url, logo_candidates=logo_candidates)

    if not data.get("name"):
        raise ValueError("Could not extract startup name")

    data.setdefault("sub_subsectors", [])

    data.pop("sector_confidences", None)
    data.pop("subsector_confidences", None)

    extracted_logo_url = data.pop("logo_url", None)

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
