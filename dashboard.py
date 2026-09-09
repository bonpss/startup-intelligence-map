"""Aggregation for the owner-only /admin dashboard (2026-09-04 conversation).

Every number here is computed in Python from plain storage.py fetches -- no
server-side SQL aggregation exists in this project (Postgrest has none to
offer beyond count="exact"), and table sizes (thousands of rows) make that a
non-issue for a page an admin checks occasionally.
"""

import os
from collections import Counter
from datetime import datetime, timezone

import storage

TOP_SECTORS_LIMIT = 12


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _pct(part: int, total: int) -> float:
    return round(100 * part / total, 1) if total else 0.0


def _max_users() -> int:
    """Same parsing as auth.py's private _max_users() (unset/blank/non-numeric
    all read as 0) -- duplicated rather than imported since it's a one-line
    env lookup and auth.py's version is private to that module by convention.
    """
    raw = os.environ.get("MAX_USERS", "").strip()
    try:
        return int(raw) if raw else 0
    except ValueError:
        return 0


def get_overview() -> dict:
    total_users = storage.count_users()
    non_owner_users = storage.count_non_owner_users()
    max_users = _max_users()
    return {
        "total_startups": storage.count_compspro(),
        "total_competitor_links": storage.count_competitors(),
        "total_users": total_users,
        "non_owner_users": non_owner_users,
        "max_users": max_users,
        "signup_slots_left": max(max_users - non_owner_users, 0) if max_users else None,
    }


def get_completeness() -> dict:
    data = storage.get_data_completeness()
    total = data["total"]
    fields = ["linkedin_url", "logo_url", "flaticon_url", "description", "country", "sub_subsectors", "embedding"]
    return {
        "total": total,
        "fields": [
            {"field": f, "missing": data[f], "missing_pct": _pct(data[f], total)}
            for f in fields
        ],
    }


def get_sector_breakdown() -> list[dict]:
    all_sectors = storage.get_all_compspro_sectors()
    total = len(all_sectors)
    counts = Counter(s for sectors in all_sectors for s in sectors)
    return [
        {"sector": sector, "count": count, "pct": _pct(count, total)}
        for sector, count in counts.most_common(TOP_SECTORS_LIMIT)
    ]


def get_ingestion_health() -> dict:
    rows = storage.get_ingestion_health_rows()
    by_status = Counter(r["status"] for r in rows)
    total = len(rows)
    terminal = by_status.get("done", 0) + by_status.get("error", 0)

    durations = []
    for r in rows:
        if r["status"] != "done":
            continue
        start, end = _parse_ts(r["created_at"]), _parse_ts(r["updated_at"])
        if start and end:
            durations.append((end - start).total_seconds())
    avg_seconds = round(sum(durations) / len(durations), 1) if durations else None

    return {
        "total": total,
        "queued": by_status.get("queued", 0),
        "processing": by_status.get("processing", 0),
        "done": by_status.get("done", 0),
        "error": by_status.get("error", 0),
        "success_rate_pct": _pct(by_status.get("done", 0), terminal),
        "avg_processing_seconds": avg_seconds,
    }


def get_cost_summary() -> dict:
    rows = storage.get_api_call_log_rows()
    total_cost = sum(r["cost_usd"] for r in rows)
    total_tokens = sum(r["prompt_tokens"] + r["completion_tokens"] for r in rows)

    by_call_type: dict[str, dict] = {}
    by_model: dict[str, dict] = {}
    for r in rows:
        for key, bucket in ((r["call_type"], by_call_type), (r["model"], by_model)):
            entry = bucket.setdefault(key, {"cost_usd": 0.0, "calls": 0, "tokens": 0})
            entry["cost_usd"] += r["cost_usd"]
            entry["calls"] += 1
            entry["tokens"] += r["prompt_tokens"] + r["completion_tokens"]

    by_day: dict[str, float] = {}
    for r in rows:
        ts = _parse_ts(r["created_at"])
        if not ts:
            continue
        day = ts.date().isoformat()
        by_day[day] = by_day.get(day, 0.0) + r["cost_usd"]
    last_14_days = sorted(by_day.items())[-14:]

    per_ingestion_cost: dict[int, float] = {}
    per_ingestion_candidates: dict[int, int] = {}
    for r in rows:
        qid = r["ingestion_queue_id"]
        if qid is None:
            continue
        per_ingestion_cost[qid] = per_ingestion_cost.get(qid, 0.0) + r["cost_usd"]
        if r["call_type"] == "competitor_score" and r["item_count"]:
            per_ingestion_candidates[qid] = per_ingestion_candidates.get(qid, 0) + r["item_count"]

    attributed_costs = list(per_ingestion_cost.values())
    avg_cost_per_startup = round(sum(attributed_costs) / len(attributed_costs), 4) if attributed_costs else None

    return {
        "total_cost_usd": round(total_cost, 4),
        "total_tokens": total_tokens,
        "total_calls": len(rows),
        "avg_cost_per_startup_usd": avg_cost_per_startup,
        "by_call_type": {k: {**v, "cost_usd": round(v["cost_usd"], 4)} for k, v in sorted(by_call_type.items())},
        "by_model": {k: {**v, "cost_usd": round(v["cost_usd"], 4)} for k, v in sorted(by_model.items())},
        "cost_by_day": [{"day": day, "cost_usd": round(cost, 4)} for day, cost in last_14_days],
        "_per_ingestion_cost": per_ingestion_cost,
        "_per_ingestion_candidates": per_ingestion_candidates,
    }


def get_recent_additions(cost_summary: dict, limit: int = 25) -> list[dict]:
    """cost_summary is get_cost_summary()'s return value -- passed in rather
    than recomputed here so a dashboard page load fetches api_call_log's rows
    once, not once per section that needs them.
    """
    rows = storage.get_recent_done_ingestions(limit)
    ids = [(r["result"] or {}).get("id") for r in rows]
    ids = [i for i in ids if i is not None]
    compspro_by_id = storage.get_compspro_by_ids(ids)
    # Fallback for ingestion_queue rows completed before this deploy, whose
    # stored result has no "id" key yet -- avoids blank sector/logo badges
    # for the handful of rows still in the window right after rollout. Kept
    # name-keyed (get_compspro_by_names' collision caveat) only as a
    # temporary bridge for that legacy data.
    legacy_names = [(r["result"] or {}).get("name") for r in rows if not (r["result"] or {}).get("id")]
    compspro_by_name = storage.get_compspro_by_names([n for n in legacy_names if n])
    users_by_id = storage.get_users_by_ids([r["requested_by_user_id"] for r in rows])

    per_ingestion_cost = cost_summary["_per_ingestion_cost"]
    per_ingestion_candidates = cost_summary["_per_ingestion_candidates"]

    out = []
    for r in rows:
        result = r["result"] or {}
        name = result.get("name")
        cs = compspro_by_id.get(result.get("id")) or compspro_by_name.get(name, {})
        out.append({
            "ingestion_queue_id": r["id"],
            "name": name or r["url"],
            "url": r["url"],
            "sectors": cs.get("sectors") or [],
            "flaticon_url": cs.get("flaticon_url"),
            "competitors_found": result.get("competitors_found"),
            "candidates_scored": per_ingestion_candidates.get(r["id"]),
            "cost_usd": round(per_ingestion_cost.get(r["id"], 0.0), 4) if r["id"] in per_ingestion_cost else None,
            "added_by": users_by_id.get(r["requested_by_user_id"]),
            "completed_at": r["updated_at"],
        })
    return out


def get_dashboard() -> dict:
    cost = get_cost_summary()
    recent = get_recent_additions(cost)
    cost = {k: v for k, v in cost.items() if not k.startswith("_")}
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "overview": get_overview(),
        "completeness": get_completeness(),
        "sectors": get_sector_breakdown(),
        "ingestion_health": get_ingestion_health(),
        "cost": cost,
        "recent": recent,
    }
