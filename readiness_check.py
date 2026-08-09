# python readiness_check.py

"""FR-4 start-trigger readiness check (Story 4.1).

Read-only report on whether the FR-3 fracture queue's evidence base is stable
enough to start FR-4's competitor-threshold recalibration (PRD Section 4.3):
>= 9 of the 11 queued subsectors diagnosed, and no new fracture type introduced
across the last 3 worked. Never writes anywhere -- Story 4.2 is where an actual
raised threshold gets applied, gated behind this script's own verdict.
"""

from storage import get_quality_reviews

# The FR-3 fracture queue (PRD Section 4.2) -- first place this list exists in
# code, previously only prose in the PRD/epics.md.
QUEUED_SUBSECTORS = (
    "AI Agent Platforms And Automation",
    "AI Data & Training Infrastructure",
    "Threat Detection & Intelligence",
    "Supply Chain & Logistics Automation",
    "Payment And Fraud Solutions",
    "Field & Industrial Operations",
    "Financial Compliance Automation",
    "Cybersecurity Risk Management",
    "Embedded Financial Services",
    "API Infrastructure",
    "MLOps & Model Serving",
)

MIN_DIAGNOSED = 9
STABILITY_WINDOW = 3


def _diagnosed_queue_entries() -> dict[str, dict]:
    """Most recent taxonomy_split row per in-queue subsector.

    Excludes any taxonomy_split row whose subject isn't in QUEUED_SUBSECTORS --
    Julien has diagnosed subsectors outside the official 11 (e.g. CRM & Sales),
    and the start trigger is specifically about this queue's own evidence
    stabilizing, not incidental extra diagnoses.

    A subsector can have more than one row over time (re-diagnosis after a fix,
    AD-7) -- only the most recent one reflects its current verdict.
    """
    rows = get_quality_reviews("taxonomy_split")
    in_queue = [r for r in rows if r["subject"] in QUEUED_SUBSECTORS]

    latest: dict[str, dict] = {}
    for row in in_queue:
        current = latest.get(row["subject"])
        if current is None or row["date_diagnosed"] > current["date_diagnosed"]:
            latest[row["subject"]] = row
    return latest


def check_readiness() -> dict:
    """Evaluate both FR-4 start-trigger conditions (PRD Section 4.3) and combine
    them into a single ready/not-ready verdict.
    """
    diagnosed = _diagnosed_queue_entries()
    count = len(diagnosed)
    count_ready = count >= MIN_DIAGNOSED

    chronological = sorted(diagnosed.values(), key=lambda r: r["date_diagnosed"])
    last_window = chronological[-STABILITY_WINDOW:]
    # Verdicts seen strictly BEFORE the last-3 window -- comparing the window
    # against the full diagnosed set (which always includes itself) would make
    # this trivially always "stable", defeating the check.
    cutoff = len(chronological) - len(last_window)
    seen_before = {r["verdict"] for r in chronological[:cutoff]}
    new_types = sorted({r["verdict"] for r in last_window} - seen_before)
    stability_ready = len(chronological) >= STABILITY_WINDOW and not new_types

    return {
        "diagnosed": diagnosed,
        "count": count,
        "count_ready": count_ready,
        "last_window": last_window,
        "new_types": new_types,
        "stability_ready": stability_ready,
        "ready": count_ready and stability_ready,
    }


def _print_report(result: dict) -> None:
    print(f"Queue: {result['count']}/{len(QUEUED_SUBSECTORS)} subsectors diagnosed (need >= {MIN_DIAGNOSED})\n")

    for subsector in QUEUED_SUBSECTORS:
        row = result["diagnosed"].get(subsector)
        if row:
            print(f"  [x] {subsector} -> {row['verdict']}")
        else:
            print(f"  [ ] {subsector} (not yet diagnosed)")

    print()
    if result["last_window"]:
        window_str = ", ".join(f"{r['subject']} -> {r['verdict']}" for r in result["last_window"])
        print(f"Last {len(result['last_window'])} worked (chronological): {window_str}")
        if result["new_types"]:
            print(f"  New fracture type(s) introduced: {', '.join(result['new_types'])}")
        else:
            print("  No new fracture type introduced.")
    else:
        print("Not enough diagnosed subsectors yet to assess stability.")

    print()
    if result["ready"]:
        print("READY -- both start-trigger conditions are met.")
    else:
        reasons = []
        if not result["count_ready"]:
            reasons.append(f"only {result['count']}/{MIN_DIAGNOSED} minimum diagnosed")
        if not result["stability_ready"]:
            if result["new_types"]:
                reasons.append(f"new fracture type(s) in the last {STABILITY_WINDOW}: {', '.join(result['new_types'])}")
            elif len(result["last_window"]) < STABILITY_WINDOW:
                reasons.append(f"fewer than {STABILITY_WINDOW} diagnosed subsectors so far")
        print(f"NOT READY -- {'; '.join(reasons)}.")


if __name__ == "__main__":
    _print_report(check_readiness())
