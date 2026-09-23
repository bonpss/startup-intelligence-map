# python migrate_logos_to_r2.py [--dry-run]
"""Migrate favicon/logo images for every already-ingested compspro row from
local disk (assets/logos/) to Cloudflare R2, and rewrite flaticon_url/
logo_url to the new R2 public URL.

MUST BE RUN ON THE VPS, from the repo root -- the VPS is the only place
holding the actual current assets/logos/ files for startups ingested before
main.py's R2 migration (Step 2 of this chantier). assets/logos/ is
gitignored, so a dev machine never has these files; running this locally
would just report every row as missing_local_file.

Rows whose local file is missing are logged as "missing_local_file" rather
than failing the run -- those are genuinely-missing images (e.g.
bricklayer.ai) that need a real re-fetch afterward (Step 4: main.py's
fetch_and_save_favicon/fetch_and_save_real_logo, now R2-backed by default),
not a migration.

--dry-run: writes the report only -- no R2 uploads, no DB writes.
"""

import json
import os
import sys

from dotenv import load_dotenv

from main import _r2_upload, _EXT_TO_CONTENT_TYPE
from storage import _client

load_dotenv()

REPORT_PATH = "logo_migration_report.json"


def fetch_rows() -> list[dict]:
    """Every compspro row with a non-null flaticon_url or logo_url,
    .range()-paginated -- PostgREST caps a single response at 1000 rows
    (same pattern as audit_taxonomy.py::fetch_all).
    """
    client = _client()
    all_rows, page, size = [], 0, 1000
    while True:
        batch = (
            client.table("compspro")
            .select("id, name, flaticon_url, logo_url")
            .or_("flaticon_url.not.is.null,logo_url.not.is.null")
            .range(page, page + size - 1)
            .execute()
            .data or []
        )
        all_rows.extend(batch)
        if len(batch) < size:
            break
        page += size
    return all_rows


def _asset_entry(row_id: int, name: str, field: str, old_value: str | None, dry_run: bool) -> dict | None:
    """Migrate a single flaticon_url/logo_url value; returns a report entry,
    or None if the column was empty (nothing to do). Key convention matches
    main.py's _save_asset: the local file's own basename (slug.ext /
    slug_logo.ext) becomes logos/<basename> in R2, so this migration and any
    future fetch_and_save_favicon()/fetch_and_save_real_logo() call agree on
    the same key for the same startup.
    """
    if not old_value:
        return None
    if old_value.startswith(("http://", "https://")):
        # Already migrated (re-run of this script, or already R2-backed via
        # Step 2's write path) -- nothing to do.
        return {"id": row_id, "name": name, "field": field, "status": "already_migrated", "url": old_value}
    if not old_value.startswith("/assets/logos/"):
        return {"id": row_id, "name": name, "field": field, "old_path": old_value, "status": "unrecognized_format"}

    disk_path = old_value.lstrip("/")
    if not os.path.isfile(disk_path):
        return {"id": row_id, "name": name, "field": field, "old_path": old_value, "status": "missing_local_file"}

    if dry_run:
        return {"id": row_id, "name": name, "field": field, "old_path": old_value, "status": "would_migrate"}

    key = "logos/" + os.path.basename(disk_path)
    ext = key.rsplit(".", 1)[-1].lower()
    try:
        with open(disk_path, "rb") as f:
            content = f.read()
        new_url = _r2_upload(key, content, _EXT_TO_CONTENT_TYPE.get(ext))
    except Exception as e:
        return {"id": row_id, "name": name, "field": field, "old_path": old_value, "status": "upload_error", "error": str(e)}

    return {"id": row_id, "name": name, "field": field, "old_path": old_value, "new_url": new_url}


def migrate(dry_run: bool) -> None:
    rows = fetch_rows()
    total = len(rows)
    print(f"{total} row(s) with a favicon and/or logo\n")

    client = _client()
    report: list[dict] = []
    counts: dict[str, int] = {}

    for i, row in enumerate(rows, 1):
        row_id, name = row["id"], row.get("name", "unknown")
        updates = {}

        for field in ("flaticon_url", "logo_url"):
            entry = _asset_entry(row_id, name, field, row.get(field), dry_run)
            if entry is None:
                continue
            report.append(entry)
            status = entry.get("status", "migrated")
            counts[status] = counts.get(status, 0) + 1
            if status == "migrated":
                updates[field] = entry["new_url"]

        if updates and not dry_run:
            client.table("compspro").update(updates).eq("id", row_id).execute()

        if i % 200 == 0 or i == total:
            print(f"[{i}/{total}] processed", flush=True)

    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    print(f"\n{'=' * 60}")
    label = "DRY RUN" if dry_run else "Done"
    print(f"{label} — " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())))
    print(f"Report written to {REPORT_PATH}")


if __name__ == "__main__":
    migrate(dry_run="--dry-run" in sys.argv)
