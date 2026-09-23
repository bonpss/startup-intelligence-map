"""Model pricing, for api_call_log.cost_usd (admin dashboard, storage.log_api_call).

Mistral source: https://docs.mistral.ai/inference/pricing, checked 2026-09-04. The
"-latest" aliases used throughout this project (mistral-large-latest,
mistral-medium-latest) currently resolve to Mistral Large 3 and Mistral
Medium 3.5 respectively -- if Mistral repoints an alias to a different model
generation, its price can change silently; there is no API to read current
pricing at request time, so this table needs a manual update when that happens.

Jev pricing: $0.042/M input tokens, the rate given for this project's
TypeSafe account (no public pricing page found). Output priced at $0.00 --
no output rate was ever published or confirmed anywhere; Jev's answers are a
handful of tokens (~20-25 observed), so the omission is a rounding error at
worst, not a hidden cost. Keyed on the EXACT versioned model string the API
returns (e.g. "jev-1.13.0", not an alias like "jev-latest" -- TypeSafe has no
stable-alias convention the way Mistral's "-latest" does) -- a version bump
silently falls through to compute_cost_usd()'s $0.0 fail-open default below,
same risk already documented for Mistral above, needs a manual update when
TypeSafe ships a new Jev version.
"""

# USD per 1,000,000 tokens.
MODEL_PRICING_PER_1M_TOKENS = {
    "mistral-large-latest":  {"input": 0.50, "output": 1.50},
    "mistral-medium-latest": {"input": 1.50, "output": 7.50},
    "mistral-embed":         {"input": 0.10, "output": 0.00},
    "jev-1.13.0":            {"input": 0.042, "output": 0.00},
}


def compute_cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Estimated USD cost of one call. Returns 0.0 for a model not in the
    pricing table above (fails open -- an unpriced call is logged with a
    visible $0 rather than raising and losing the token counts entirely).
    """
    pricing = MODEL_PRICING_PER_1M_TOKENS.get(model)
    if pricing is None:
        return 0.0
    return (prompt_tokens * pricing["input"] + completion_tokens * pricing["output"]) / 1_000_000
