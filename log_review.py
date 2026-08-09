# python log_review.py <subsector>

"""Human-in-the-loop taxonomy-fracture reading rubric (F/FR-5).

Guides Julien through FR-3's reading rubric for one subsector flagged as split
across multiple Louvain communities by graph_analysis.py, then records his
verdict to quality_review_log. Never modifies taxonomy.py, compspro, or
competitors -- it captures a decision, it doesn't execute one.
"""

import json
import os
import random
import sys

from storage import _client, get_quality_reviews, normalize_domain, save_quality_review
from taxonomy import TAXONOMY

REPORT_PATH = "graph_analysis_report.json"

# Local recovery file for a verdict that's been chosen but not yet confirmed saved
# to Supabase. Written just before the save attempt, deleted right after it
# succeeds. source_snapshot captures a precise slice of subsector_splits that
# can't be reliably reconstructed from memory after a crash -- this is a plain
# local file, not a queue, consistent with this project's flat-script convention.
PENDING_FILE = ".log_review_pending.json"

# Revisable -- how many of this subsector's members to show per community.
SAMPLE_SIZE_PER_COMMUNITY = 8

# Local flattened set for a friendly early check -- same pattern audit_taxonomy.py
# already uses (audit_taxonomy.py:71-72). The real write-time enforcement stays in
# storage.py (AD-1/AD-2/AD-4); this doesn't duplicate that authority.
_TAXONOMY_SUBSECTORS = {sub for subs in TAXONOMY.values() for sub in subs}

# Byte-for-byte match with storage.py's _TAXONOMY_SPLIT_VERDICTS (AD-4) -- a
# reworded string here would be silently rejected by save_quality_review().
_VERDICT_MENU = [
    "isolated mis-tag",
    "structural gap",
    "ambiguous",
    "scraping artifact",
]

_RUBRIC_TEXT = """
Reading rubric (PRD Section 4.2):
  1. Read the sample of descriptions per community above.
  2. Assess whether the communities reflect substantially different activities,
     or just wording variation around the same positioning.
  3. Verdict "isolated mis-tag": the majority are correctly classified, only
     1-2 startups are mistagged -- fixable with a targeted cleanup rule.
  4. Verdict "structural gap": the communities correspond to genuinely
     distinct activities -- needs a new subsector or a re-split.
  5. If genuinely unclear, verdict "ambiguous" rather than forcing a binary
     call -- a recurring "ambiguous" outcome is itself a signal worth watching.
"""


def _load_report() -> dict:
    try:
        with open(REPORT_PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"{REPORT_PATH} not found -- run `python graph_analysis.py` first.", file=sys.stderr)
        sys.exit(1)


def _find_split(report: dict, subsector: str) -> dict | None:
    for entry in report.get("subsector_splits", []):
        if entry.get("subsector") == subsector:
            return entry
    return None


def _find_community(report: dict, community_id: int) -> dict:
    # Search by id field, not list position -- graph_analysis.py's filtering/sorting
    # coincides with index order today, but the id field is the actual contract.
    for c in report.get("communities", []):
        if c.get("id") == community_id:
            return c
    raise ValueError(f"community_id {community_id} not found in {REPORT_PATH}")


def _fetch_subsector_members(subsector: str) -> dict[str, str]:
    """Name -> description for every compspro row currently tagged with this subsector."""
    rows = (
        _client()
        .table("compspro")
        .select("name, description")
        .contains("subsectors", [subsector])
        .execute()
        .data or []
    )
    return {r["name"]: r.get("description") or "" for r in rows}


def _fetch_subsector_websites(subsector: str) -> dict[str, str]:
    """Name -> website for every compspro row currently tagged with this subsector.

    Deliberately a separate query from _fetch_subsector_members() (Story 3.1) rather
    than extending it -- avoids touching/risking regression in an already
    live-verified function for the sake of one extra lightweight query.
    """
    rows = (
        _client()
        .table("compspro")
        .select("name, website")
        .contains("subsectors", [subsector])
        .execute()
        .data or []
    )
    return {r["name"]: r.get("website") or "" for r in rows}


def _scraping_guardrail_check(websites: dict[str, str]) -> tuple[dict[str, dict], dict[str, dict]]:
    """Check this subsector's startup domains against known scraping_diagnostic
    findings (AD-2's FR-3 Precondition 1 guardrail). Returns (covered, problems):
      - covered: members whose normalized domain has *any* scraping_diagnostic row
        (any verdict, including "ok") -- i.e. FR-1 has actually looked at this site.
      - problems: the subset of covered whose most recent row's verdict != "ok" --
        a known scraping gap, not just "checked and found nothing wrong".
    "ok" is Story 2.1's characterize()'s signal for "nothing wrong" -- everything
    else (today's known labels, or any future free-text one, per AD-4's open
    vocabulary) is treated as a match via the negative condition, not a hardcoded
    positive list that would silently stop matching a label nobody wrote yet.
    """
    all_rows = get_quality_reviews("scraping_diagnostic")

    latest_by_domain: dict[str, dict] = {}
    for row in sorted(all_rows, key=lambda r: r["date_diagnosed"], reverse=True):
        latest_by_domain.setdefault(row["subject"], row)

    covered: dict[str, dict] = {}
    for name, website in websites.items():
        if not website:
            continue
        domain = normalize_domain(website)
        if domain in latest_by_domain:
            covered[name] = latest_by_domain[domain]

    problems = {name: row for name, row in covered.items() if row["verdict"] != "ok"}
    return covered, problems


def _display_samples(report: dict, split: dict, members: dict[str, str]) -> None:
    member_names = set(members)
    print(f"\n{'=' * 70}")
    print(f"Subsector: {split['subsector']}  ({split['total_members']} members total in the report snapshot)")
    print(f"{'=' * 70}")

    for slice_info in split["split_across"]:
        community = _find_community(report, slice_info["community_id"])
        in_subsector = sorted(set(community.get("members", [])) & member_names)

        print(f"\n--- Community #{community['id']} -- {len(in_subsector)} of this subsector's members here ---")
        if not in_subsector:
            print("    (none of this subsector's current members are in this community anymore --")
            print("     the report snapshot may be stale; consider re-running graph_analysis.py)")
            continue

        dom = ", ".join(f"{sub} ({cnt})" for sub, cnt in community.get("dominant_subsectors", [])[:5])
        print(f"    dominant subsectors in this community: {dom}")

        sample = random.sample(in_subsector, min(SAMPLE_SIZE_PER_COMMUNITY, len(in_subsector)))
        for name in sample:
            desc = members.get(name) or "(no description)"
            print(f"    - {name}: {desc}")

    print(_RUBRIC_TEXT)


def _prompt_verdict() -> str:
    while True:
        print("Verdict:")
        for i, v in enumerate(_VERDICT_MENU, start=1):
            print(f"  {i}. {v}")
        choice = input("Choose 1-4: ").strip()
        if choice in ("1", "2", "3", "4"):
            return _VERDICT_MENU[int(choice) - 1]
        print("Invalid choice, try again.\n")


def _prompt_notes() -> str | None:
    notes = input("Notes (optional, press Enter to skip): ").strip()
    return notes or None


def _apply_scraping_guardrail(subsector: str, verdict: str) -> str:
    """FR-1 guardrail (AD-2's Precondition 1) -- only called when verdict is
    "structural gap" (the Given clause's scope; other verdicts skip this entirely).
    Returns the verdict to actually record: unchanged unless Julien explicitly
    opts to switch to "scraping artifact" after seeing a known scraping gap.
    """
    websites = _fetch_subsector_websites(subsector)
    covered, problems = _scraping_guardrail_check(websites)

    if not covered:
        print("\nFR-1 guardrail: none of this subsector's current startup domains have been")
        print("diagnosed by diagnose_scraping.py yet -- nothing to check against.")
        print("Proceeding with 'structural gap' as selected.")
        return verdict

    if not problems:
        print(f"\nFR-1 guardrail: checked {len(covered)} of this subsector's already-diagnosed")
        print("sites -- no scraping issue found. Proceeding with 'structural gap'.")
        return verdict

    print(f"\nFR-1 guardrail: {len(problems)} of this subsector's startups have a known scraping issue:")
    for name, row in problems.items():
        print(f"  - {name} ({row['subject']}): {row['verdict']} -- {row.get('notes') or '(no notes)'} [{row['date_diagnosed']}]")
    choice = input("\nSwitch verdict to 'scraping artifact' instead of 'structural gap'? [y/N]: ").strip().lower()
    if choice in ("y", "yes"):
        return "scraping artifact"
    return verdict


def _save_verdict(payload: dict) -> dict:
    """Write the pending file first, attempt the Supabase save, then delete the
    pending file on success. On failure the pending file is left in place and the
    caller is responsible for telling the user how to resume -- this function
    itself doesn't decide that (used identically by a fresh save and a --resume).
    """
    if os.path.exists(PENDING_FILE):
        with open(PENDING_FILE, encoding="utf-8") as f:
            existing = json.load(f)
        if existing.get("subject") != payload["subject"]:
            print(
                f"Note: a not-yet-saved review for {existing.get('subject')!r} is being replaced "
                f"by this one -- run `python log_review.py --resume` first if you want to save it."
            )

    with open(PENDING_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    result = save_quality_review(
        review_type="taxonomy_split",
        subject=payload["subject"],
        verdict=payload["verdict"],
        source_snapshot=payload.get("source_snapshot"),
        resolution=payload.get("resolution"),
        notes=payload.get("notes"),
    )

    os.remove(PENDING_FILE)
    return result


def _resume() -> None:
    try:
        with open(PENDING_FILE, encoding="utf-8") as f:
            payload = json.load(f)
    except FileNotFoundError:
        print(f"No pending review found ({PENDING_FILE} doesn't exist) -- nothing to resume.", file=sys.stderr)
        sys.exit(1)

    print(f"Resuming pending review: {payload['subject']!r} -> {payload['verdict']!r}")
    try:
        result = _save_verdict(payload)
    except Exception as e:
        print(f"\nSave failed again: {e}", file=sys.stderr)
        print(f"Verdict is still saved locally in {PENDING_FILE} -- run `python log_review.py --resume` again later.", file=sys.stderr)
        sys.exit(1)

    print(f"\nSaved (resumed): {payload['subject']!r} -> {payload['verdict']!r} (id={result.get('id')}, date_diagnosed={result.get('date_diagnosed')})")


def main() -> None:
    if len(sys.argv) == 2 and sys.argv[1] == "--resume":
        _resume()
        return

    if len(sys.argv) != 2:
        print("Usage: python log_review.py <subsector>", file=sys.stderr)
        print("       python log_review.py --resume", file=sys.stderr)
        sys.exit(1)

    subsector = sys.argv[1]

    if subsector not in _TAXONOMY_SUBSECTORS:
        print(f"{subsector!r} is not a known TAXONOMY subsector.", file=sys.stderr)
        sys.exit(1)

    report = _load_report()
    split = _find_split(report, subsector)
    if split is None:
        print(f"{subsector!r} is not currently flagged as split in {REPORT_PATH} -- nothing to review.")
        sys.exit(0)

    members = _fetch_subsector_members(subsector)
    _display_samples(report, split, members)

    verdict = _prompt_verdict()
    if verdict == "structural gap":
        verdict = _apply_scraping_guardrail(subsector, verdict)
    notes = _prompt_notes()

    payload = {
        "subject": subsector,
        "verdict": verdict,
        "resolution": None,
        "source_snapshot": split,
        "notes": notes,
    }

    try:
        result = _save_verdict(payload)
    except Exception as e:
        print(f"\nSave failed: {e}", file=sys.stderr)
        print(f"Your verdict was saved locally to {PENDING_FILE} -- run `python log_review.py --resume` to retry the write.", file=sys.stderr)
        sys.exit(1)

    print(f"\nSaved: {subsector!r} -> {verdict!r} (id={result.get('id')}, date_diagnosed={result.get('date_diagnosed')})")


if __name__ == "__main__":
    main()
