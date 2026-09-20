"""Placeholder competitor-scoring logic for the public repo.

The real competitor.py — the tuned scoring prompt and matching rules built up
over many corrections — is proprietary and kept out of this repo (see
README's "Scope of this repo"). This file exists so the pipeline is
importable and runnable end-to-end for anyone cloning the repo: same
function signatures every other module expects (compare, score_candidates,
save_competitors, explore_transitive, CHUNK_SIZE), same chunking/pacing/retry
architecture as the rest of the pipeline, but with a simplified example
scoring prompt instead of the real one.

Swap this file out for your own scoring logic — nothing else in the
pipeline needs to change.
"""

import os
import threading
import time

from dotenv import load_dotenv
from mistralai.client.sdk import Mistral

from retry import build_retry, is_mistral_retryable, log_retry_attempt
from storage import (
    BATCH_TIMEOUT_MS,
    get_by_subsectors,
    get_company,
    get_known_competitors,
    log_api_call,
    save_relationships,
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
    import json

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
