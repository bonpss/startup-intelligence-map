"""Placeholder competitor-scoring logic for the public repo.

The real competitor.py — the tuned scoring prompt and matching rules built up
over many corrections — is proprietary and kept out of this repo (see
README's "Scope of this repo"). This file exists so the pipeline is
importable and runnable end-to-end for anyone cloning the repo: same
function signatures every other module expects (compare, score_candidates,
save_competitors, explore_transitive, compare_jev, score_candidates_jev,
save_competitors_jev, explore_transitive_jev, CHUNK_SIZE), same
chunking/pacing/retry architecture as the rest of the pipeline, but with a
simplified example scoring prompt instead of the real one.

Swap this file out for your own scoring logic — nothing else in the
pipeline needs to change.
"""

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv
from mistralai.client.sdk import Mistral
from typesafe_sdk import (
    Noul,
    TypeSafeAPIConnectionError,
    TypeSafeAPITimeoutError,
    TypeSafeClient,
    TypeSafeInternalServerError,
    TypeSafeRateLimitError,
)

from retry import build_retry, is_mistral_retryable, log_retry_attempt
from storage import (
    BATCH_TIMEOUT_MS,
    JEV_ACCEPT_THRESHOLD,
    JEV_REVIEW_FLOOR,
    get_by_subsectors,
    get_company,
    get_company_by_id,
    get_known_competitors,
    log_api_call,
    relationship_exists,
    save_relationships,
    save_review_queue,
)

load_dotenv()

CHUNK_SIZE = 20  # candidates per LLM call, same budget as the real pipeline
_CHUNK_PACING_SECONDS = 1.1  # stay under free-tier ~1 req/s

SYSTEM_PROMPT = """
You are scoring whether pairs of companies are competitors, based only on
their descriptions. For each candidate, score from 0.0 (not a competitor) to
1.0 (direct competitor) how closely its product and target customer overlap
with the reference company's.

Return ONLY a valid JSON object:
{"scores": [{"name": "<candidate name>", "score": <float between 0 and 1>}]}
"""

_retry = build_retry(
    is_mistral_retryable,
    wait_multiplier=2, wait_min=4, wait_max=60, stop_attempts=7,
    before_sleep=log_retry_attempt,
)

_mistral: Mistral | None = None
_mistral_lock = threading.Lock()


def _client() -> Mistral:
    """Lazily create and cache a single Mistral client (same pattern as
    embeddings.py's _client())."""
    global _mistral
    if _mistral is None:
        with _mistral_lock:
            if _mistral is None:
                _mistral = Mistral(api_key=os.environ["MISTRAL_API_KEY"], timeout_ms=BATCH_TIMEOUT_MS)
    return _mistral


@_retry
def _score_chunk(company: dict, chunk: list[dict]) -> list[dict]:
    model = "mistral-large-latest"
    payload = {
        "reference": {"name": company.get("name"), "description": company.get("description")},
        "candidates": [{"name": c["name"], "description": c.get("description")} for c in chunk],
    }
    r = _client().chat.complete(
        model=model,
        timeout_ms=BATCH_TIMEOUT_MS,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        response_format={"type": "json_object"},
    )
    usage = r.usage
    log_api_call("competitor_scoring", model, usage.prompt_tokens or 0, usage.completion_tokens or 0, item_count=len(chunk))

    data = json.loads(r.choices[0].message.content)
    valid_names = {c["name"] for c in chunk}
    return [
        {"name": item["name"], "score": max(0.0, min(1.0, float(item.get("score", 0.0))))}
        for item in data.get("scores") or []
        if isinstance(item, dict) and item.get("name") in valid_names
    ]


def score_candidates(company: dict, candidates: list[dict]) -> list[dict]:
    """Score every candidate against `company`, chunked at CHUNK_SIZE and
    paced to stay under Mistral's free-tier rate limit."""
    results: list[dict] = []
    for i in range(0, len(candidates), CHUNK_SIZE):
        chunk = candidates[i:i + CHUNK_SIZE]
        results.extend(_score_chunk(company, chunk))
        if i + CHUNK_SIZE < len(candidates):
            time.sleep(_CHUNK_PACING_SECONDS)
    return results


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0


def _prefilter_by_embedding(company: dict, candidates: list[dict], top_n: int = 40) -> list[dict]:
    """Narrow the candidate pool by embedding similarity before the (more
    expensive) LLM scoring pass, same idea as the real pipeline's embedding
    pre-filter chantier. No-op if either side has no embedding yet."""
    ref = company.get("embedding")
    scored = [(c, _cosine_similarity(ref, c["embedding"])) for c in candidates if ref and c.get("embedding")]
    if len(scored) != len(candidates):
        return candidates  # some rows missing an embedding -- skip prefiltering rather than drop them
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return [c for c, _ in scored[:top_n]]


def compare(company: dict) -> list[dict]:
    """Fetch same-subsector candidates and score them against `company`."""
    candidates = get_by_subsectors(
        company.get("subsectors") or [],
        company.get("sectors") or [],
        company.get("name", ""),
        company.get("sub_subsectors") or [],
        include_embedding=True,
    )
    candidates = _prefilter_by_embedding(company, candidates)
    return score_candidates(company, candidates)


def save_competitors(company: dict, results: list[dict]) -> list[dict]:
    """Persist competitor relationships scoring above threshold (storage.py
    handles the threshold check and dedup)."""
    return save_relationships(company["name"], results)


def explore_transitive(company: dict, direct_names: list[str]) -> list[dict]:
    """For each direct competitor, check its own known competitors for anyone
    not yet linked to `company` -- score and save those too."""
    seen = {company.get("name"), *direct_names}
    saved: list[dict] = []
    for name in direct_names:
        for candidate_name in get_known_competitors(name):
            if candidate_name in seen:
                continue
            seen.add(candidate_name)
            candidate = get_company(candidate_name)
            if not candidate:
                continue
            scored = score_candidates(company, [candidate])
            saved.extend(save_relationships(company["name"], scored))
    return saved


# ---------------------------------------------------------------------------
# Jev path: an alternate scorer (typesafe_sdk's Noul primitive) used instead
# of the Mistral chunked-scoring path above. Same idea as SYSTEM_PROMPT: the
# real pipeline's tuned instructions/criteria are proprietary and kept out of
# this repo -- jev_prompts/baseline.json here holds a simplified example
# rubric instead. Get your own TypeSafe API key the same way you'd get a
# Mistral one (see README) to run this path.
# ---------------------------------------------------------------------------

_JEV_PROMPT_PATH = "jev_prompts/baseline.json"
_JEV_MAX_CONCURRENCY = 10  # Jev scores one pair per call, no batching -- keep this modest absent a documented rate limit to size it against

with open(_JEV_PROMPT_PATH, encoding="utf-8") as _jev_prompt_file:
    _jev_prompt = json.load(_jev_prompt_file)
NOUL_INSTRUCTIONS_JEV = _jev_prompt["instructions"]
NOUL_CRITERIA_JEV = {"true": _jev_prompt["criteria_true"], "false": _jev_prompt["criteria_false"]}

_typesafe: TypeSafeClient | None = None
_typesafe_lock = threading.Lock()


def _jev_client() -> TypeSafeClient:
    """Lazily create and cache a single TypeSafeClient (same pattern as _client() above)."""
    global _typesafe
    if _typesafe is None:
        with _typesafe_lock:
            if _typesafe is None:
                _typesafe = TypeSafeClient(api_key=os.environ["TYPESAFE_API_KEY"])
    return _typesafe


def _is_typesafe_retryable(exc: BaseException) -> bool:
    return isinstance(exc, (
        TypeSafeAPIConnectionError,
        TypeSafeAPITimeoutError,
        TypeSafeRateLimitError,
        TypeSafeInternalServerError,
    ))


_jev_retry = build_retry(
    _is_typesafe_retryable,
    wait_multiplier=2, wait_min=4, wait_max=60, stop_attempts=5,
)


def _jev_state(company_a: dict, company_b: dict) -> str:
    return (
        f"Company A: {company_a['name']}\n"
        f"Description: {company_a.get('description') or '(no description available)'}\n\n"
        f"Company B: {company_b['name']}\n"
        f"Description: {company_b.get('description') or '(no description available)'}"
    )


@_jev_retry
def _score_pair_jev(company_a: dict, candidate: dict) -> dict:
    """Score one pair with Jev. Returns {id, name, score, zone} -- zone is
    one of "accept"/"review"/"reject" per JEV_ACCEPT_THRESHOLD/JEV_REVIEW_FLOOR
    in storage.py."""
    state = _jev_state(company_a, candidate)
    response = _jev_client().system_one(
        state=state,
        questions={"are_competitors": Noul(instructions=NOUL_INSTRUCTIONS_JEV, criteria=NOUL_CRITERIA_JEV)},
    )
    log_api_call(
        "competitor_score_jev", response.model,
        response.usage.input_tokens, response.usage.output_tokens,
        item_count=1,
    )
    score = response.answers["are_competitors"].noul
    if score >= JEV_ACCEPT_THRESHOLD:
        zone = "accept"
    elif score < JEV_REVIEW_FLOOR:
        zone = "reject"
    else:
        zone = "review"
    return {"id": candidate["id"], "name": candidate["name"], "score": score, "zone": zone}


def score_candidates_jev(company: dict, candidates: list[dict]) -> list[dict]:
    """Score `company` against every candidate with Jev, in parallel
    (ThreadPoolExecutor, _JEV_MAX_CONCURRENCY workers) instead of
    score_candidates()'s sequential Mistral chunking -- Jev has no batching
    equivalent. Returns the same shape as score_candidates(), plus "zone"
    per result, sorted by score descending."""
    if not candidates:
        return []
    results = []
    with ThreadPoolExecutor(max_workers=_JEV_MAX_CONCURRENCY) as pool:
        futures = {pool.submit(_score_pair_jev, company, c): c for c in candidates}
        for future in as_completed(futures):
            results.append(future.result())
    return sorted(results, key=lambda r: r["score"], reverse=True)


def compare_jev(new_company: dict) -> list[dict]:
    """Jev equivalent of compare() -- same candidate pool, scored with Jev
    instead of Mistral (no embedding prefilter: that exists to cut Mistral's
    chunk-call volume, and doesn't apply to Jev's per-pair calls)."""
    candidates = get_by_subsectors(
        new_company.get("subsectors") or [],
        new_company.get("sectors") or [],
        new_company.get("name"),
        new_company.get("sub_subsectors") or [],
        exclude_id=new_company.get("id"),
    )
    if not candidates:
        return []
    return score_candidates_jev(new_company, candidates)


def save_competitors_jev(company_a: dict, scored_results: list[dict]) -> dict:
    """Saves "accept"-zone results to `competitors` (via save_relationships,
    with JEV_ACCEPT_THRESHOLD and scorer="jev" so these rows are
    distinguishable from Mistral's). "review"-zone results go to
    competitor_review_queue via save_review_queue() instead -- they are not
    confirmed competitors.

    Returns {"saved": [...], "review_queued": [...]}.
    """
    a_id, a_name = company_a["id"], company_a.get("name")
    already_linked_ids = {c["id"] for c in get_known_competitors(a_id)}

    accepted = [r for r in scored_results if r["zone"] == "accept" and r["id"] not in already_linked_ids]
    review = [r for r in scored_results if r["zone"] == "review" and r["id"] not in already_linked_ids]

    saved = save_relationships(a_id, a_name, accepted, threshold=JEV_ACCEPT_THRESHOLD, scorer="jev")
    review_queued = save_review_queue(a_id, a_name, review, scorer="jev")
    return {"saved": saved, "review_queued": review_queued}


def explore_transitive_jev(company_a: dict, direct_competitors: list[dict]) -> dict:
    """Jev equivalent of explore_transitive() -- for each direct competitor
    U of A, look at U's own known competitors X; if A-X isn't already linked
    and X shares sector+subsector with A, it's a transitive candidate, scored
    via Jev and saved via the same three-zone split as save_competitors_jev().

    direct_competitors: rows shaped like save_competitors_jev()'s "saved"
    list (carrying company_b_id).

    Returns the same {"saved": [...], "review_queued": [...]} shape as
    save_competitors_jev().
    """
    a_id = company_a["id"]
    a_sectors = set(company_a.get("sectors") or [])
    a_subsectors = set(company_a.get("subsectors") or [])
    a_sub_subsectors = set(company_a.get("sub_subsectors") or [])

    seen_ids = {c["company_b_id"] for c in direct_competitors}
    seen_ids.add(a_id)

    x_candidates = []
    for u in direct_competitors:
        for x in get_known_competitors(u["company_b_id"]):
            x_id = x["id"]
            if x_id in seen_ids:
                continue
            seen_ids.add(x_id)

            if relationship_exists(a_id, x_id) or relationship_exists(x_id, a_id):
                continue

            x_data = get_company_by_id(x_id)
            if not x_data:
                continue

            x_sectors = set(x_data.get("sectors") or [])
            x_subsectors = set(x_data.get("subsectors") or [])
            x_sub_subsectors = set(x_data.get("sub_subsectors") or [])

            sector_match = bool(a_sectors & x_sectors)
            subsector_match = bool(a_subsectors & x_subsectors)
            sub_sub_match = not a_sub_subsectors or bool(a_sub_subsectors & x_sub_subsectors)

            if sector_match and subsector_match and sub_sub_match:
                x_candidates.append(x_data)

    if not x_candidates:
        return {"saved": [], "review_queued": []}

    results = score_candidates_jev(company_a, x_candidates)
    return save_competitors_jev(company_a, results)
