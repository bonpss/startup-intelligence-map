import json
import os
import threading
import time
import httpx
from mistralai.client.sdk import Mistral
from mistralai.client.errors.sdkerror import SDKError
from dotenv import load_dotenv
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception
from storage import (
    BATCH_TIMEOUT_MS,
    COMPETITOR_THRESHOLD,
    INTERACTIVE_REQUEST,
    INTERACTIVE_TIMEOUT_MS,
    RETRY_INTERACTIVE_STOP,
    RETRY_INTERACTIVE_WAIT,
    get_by_subsectors,
    get_company,
    get_known_competitors,
    relationship_exists,
    save_relationships,
)

load_dotenv()

CHUNK_SIZE = 20  # candidates per LLM call — keeps prompts short enough to score reliably


def _is_retryable(exc: BaseException) -> bool:
    # Covers ReadTimeout, ConnectTimeout, ConnectError, RemoteProtocolError, etc. —
    # any transient connection-level failure, not just the few subtypes we'd enumerate by hand
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, json.JSONDecodeError):
        return True
    return isinstance(exc, SDKError) and any(code in str(exc) for code in ("429", "503", "529"))


_retry = retry(
    retry=retry_if_exception(_is_retryable),
    # Free-tier Mistral rate limits reset on a ~1min window, not seconds —
    # a 30s ceiling gave up before the window cleared. Wait longer, try more.
    # This is the batch/backfill budget (reprocess_list.py, backfill_competitors.py) —
    # see storage.RETRY_INTERACTIVE_WAIT/_STOP for the tighter interactive budget
    # (shared with extractor.py, not duplicated here).
    wait=wait_exponential(multiplier=2, min=4, max=60),
    stop=stop_after_attempt(7),
    reraise=True,
)

# Free-tier Mistral is limited to ~1 req/s: pace chunk calls instead of
# bursting them and relying on retries to absorb the resulting 429s.
_CHUNK_PACING_SECONDS = 1.1

SYSTEM_PROMPT = """
These company pairs have already been filtered to share the same sector and subsector.
Your ONLY job is to determine if their descriptions confirm they are true competitors.
Judge each PAIR symmetrically — the score must be the same whichever company comes first.
Read the description field carefully and score based exclusively on what the product does,
who it serves, and what problem it solves.

Two companies are TRUE competitors ONLY if their descriptions show they:
1. Solve the exact same problem
2. Sell to the same type of customer
3. A customer would buy one INSTEAD OF the other

For each candidate assign a score:
- 0.9-1.0 : direct substitutes — a customer would choose one OR the other
- 0.75-0.89 : significant overlap, a customer might compare both
- below 0.75 : not competitors

Examples of NON-competitors despite shared subsector:
- An AI phone assistant (Sono) vs a CRM platform (Zero):
  different product, different buyer intent → score 0.2
- An LLM provider (Mistral) vs an AI governance tool (palma.ai):
  different layer in the stack → score 0.3
- A generic, individual-use tool for freelancers/small teams (e.g. an AI email
  assistant for Gmail) vs a governed enterprise platform with compliance/audit
  features targeting mid-to-large companies: different buyer (individual
  professional vs a compliance-conscious enterprise buying committee),
  different core value prop (personal convenience vs auditability/policy
  control) → NOT substitutes even if both are broadly "AI automation" →
  score 0.2-0.3. Company size/buyer sophistication implied by the description
  (individuals/startups vs mid-market/enterprise, self-serve vs governed
  rollout) is itself a strong signal of "different customer" — weigh it
  as heavily as the product category.

Examples of TRUE competitors:
- Two LLM providers (Anthropic vs Mistral):
  same product, same buyer → score 0.9
- Two IAM platforms targeting enterprise:
  same problem, same customer → score 0.85

Return ALL candidates with their score.
Return ONLY a JSON array of {name, score}. No explanation, no markdown.
""".strip()


_mistral: Mistral | None = None
_mistral_lock = threading.Lock()

# Per-call Mistral timeout, not baked into the client -- Chat.complete() accepts
# its own timeout_ms override (mistralai 2.4.9), so one shared client can serve
# both budgets below without constructing a second Mistral instance. Batch/
# interactive values shared with extractor.py via storage.py (Story 5.2 fix --
# not duplicated per module).


def _client() -> Mistral:
    """Lazily create and cache a single Mistral client. Locked for the same reason
    as storage.py's _client(): main.py's ingest() now runs its synchronous pipeline
    (including compare() -> this function) via asyncio.to_thread(), so concurrent
    /api/ingest requests can race on the first call, each constructing a client.
    """
    global _mistral
    if _mistral is None:
        with _mistral_lock:
            if _mistral is None:
                _mistral = Mistral(api_key=os.environ["MISTRAL_API_KEY"], timeout_ms=BATCH_TIMEOUT_MS)
    return _mistral


@_retry
def _chat_json_core(timeout_ms: int, **kwargs):
    r = _client().chat.complete(timeout_ms=timeout_ms, **kwargs)
    return json.loads(r.choices[0].message.content)


# Derived from _chat_json_core via tenacity's own retry_with() rather than a
# second hand-written function -- inherits the same retry predicate and
# reraise=True, only wait/stop are overridden. Precomputed once, not re-wrapped
# on every call.
_chat_json_core_interactive = _chat_json_core.retry_with(
    wait=RETRY_INTERACTIVE_WAIT, stop=RETRY_INTERACTIVE_STOP
)


def _chat_json(**kwargs):
    """Dispatch to the interactive or batch retry/timeout budget based on
    INTERACTIVE_REQUEST (set by main.ingest() for the duration of a single
    interactive ingest). Drop-in replacement for the old _chat_json -- every
    existing call site is unchanged.
    """
    if INTERACTIVE_REQUEST.get():
        return _chat_json_core_interactive(timeout_ms=INTERACTIVE_TIMEOUT_MS, **kwargs)
    return _chat_json_core(timeout_ms=BATCH_TIMEOUT_MS, **kwargs)


def _slim(company: dict) -> dict:
    """Keep only the fields the LLM needs — everything else is wasted tokens."""
    return {k: company.get(k) for k in ("name", "description", "sectors", "subsectors")}


def _score_chunk(company: dict, chunk: list[dict]) -> list[dict]:
    payload = json.dumps(
        {"new_company": _slim(company), "candidates": [_slim(c) for c in chunk]},
        ensure_ascii=False,
    )
    raw = _chat_json(
        model="mistral-large-latest",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": payload},
        ],
        response_format={"type": "json_object"},
    )
    if isinstance(raw, dict):
        raw = next((v for v in raw.values() if isinstance(v, list)), [])

    # Only accept scores for actual candidates, clamped to [0, 1]
    valid_names = {c["name"] for c in chunk}
    results = []
    for item in raw:
        if not isinstance(item, dict) or item.get("name") not in valid_names:
            continue
        try:
            score = float(item.get("score", 0))
        except (TypeError, ValueError):
            continue
        results.append({"name": item["name"], "score": min(max(score, 0.0), 1.0)})
    return results


def score_candidates(company: dict, candidates: list[dict]) -> list[dict]:
    """Score company against candidates, CHUNK_SIZE at a time.

    The pair score is symmetric: no separate reverse-direction call is needed.
    """
    results: list[dict] = []
    for i in range(0, len(candidates), CHUNK_SIZE):
        if i > 0:
            time.sleep(_CHUNK_PACING_SECONDS)
        results.extend(_score_chunk(company, candidates[i:i + CHUNK_SIZE]))
    return sorted(results, key=lambda r: r["score"], reverse=True)


def compare(new_company: dict) -> list[dict]:
    """Return all same-sector/subsector candidates with their competition score."""
    candidates = get_by_subsectors(
        new_company.get("subsectors") or [],
        new_company.get("sectors") or [],
        new_company.get("name"),
        new_company.get("sub_subsectors") or [],
    )
    if not candidates:
        return []
    return score_candidates(new_company, candidates)


def save_competitors(company_a: dict, scored_results: list[dict]) -> list[dict]:
    """Save A↔B for pair scores >= COMPETITOR_THRESHOLD, skipping existing links.

    Returns list of {company_a, company_b, score} rows inserted.
    """
    a_name = company_a.get("name")
    already_linked = set(get_known_competitors(a_name))
    to_save = [
        r for r in scored_results
        if r["score"] >= COMPETITOR_THRESHOLD and r["name"] not in already_linked
    ]
    return save_relationships(a_name, to_save)


def explore_transitive(company_a: dict, direct_competitor_names: list[str]) -> list[dict]:
    """Discover A's indirect competitors via A's direct competitors' known relationships.

    For each direct competitor U of A, looks at U's known competitors X.
    If A↔X not yet linked AND X shares sector+subsector with A → scores A vs X.
    Returns list of newly saved {company_a, company_b, score} rows.
    """
    a_name = company_a.get("name")
    a_sectors        = set(company_a.get("sectors")        or [])
    a_subsectors     = set(company_a.get("subsectors")     or [])
    a_sub_subsectors = set(company_a.get("sub_subsectors") or [])

    seen: set[str] = set(direct_competitor_names)
    seen.add(a_name)
    x_candidates: list[dict] = []

    for u_name in direct_competitor_names:
        for x_name in get_known_competitors(u_name):
            if x_name in seen:
                continue
            seen.add(x_name)

            if relationship_exists(a_name, x_name) or relationship_exists(x_name, a_name):
                continue

            x_data = get_company(x_name)
            if not x_data:
                continue

            x_sectors        = set(x_data.get("sectors")        or [])
            x_subsectors     = set(x_data.get("subsectors")     or [])
            x_sub_subsectors = set(x_data.get("sub_subsectors") or [])

            sector_match    = bool(a_sectors & x_sectors)
            subsector_match = bool(a_subsectors & x_subsectors)
            sub_sub_match   = (not a_sub_subsectors) or bool(a_sub_subsectors & x_sub_subsectors)

            if sector_match and subsector_match and sub_sub_match:
                x_candidates.append(x_data)

    if not x_candidates:
        return []

    results = score_candidates(company_a, x_candidates)
    return save_competitors(company_a, results)
