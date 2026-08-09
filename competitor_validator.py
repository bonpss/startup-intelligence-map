# pip install supabase python-dotenv mistralai tenacity httpx
# python competitor_validator.py

import json
import os
import random

import httpx
from dotenv import load_dotenv
from mistralai.client.sdk import Mistral
from mistralai.client.errors.sdkerror import SDKError
from storage import _client
from tenacity import retry, retry_if_exception, wait_exponential

load_dotenv()


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    return isinstance(exc, SDKError) and any(code in str(exc) for code in ("429", "503", "529"))


@retry(
    retry=retry_if_exception(_is_retryable),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    reraise=True,
)
def _chat_complete(client: Mistral, **kwargs):
    return client.chat.complete(**kwargs)


def _run_migration() -> None:
    """Add checked/validated columns to competitors. Safe to run multiple times."""
    url = os.environ["SUPABASE_URL"]
    key = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ["SUPABASE_KEY"]
    sql = (
        "ALTER TABLE competitors ADD COLUMN IF NOT EXISTS checked boolean DEFAULT false; "
        "ALTER TABLE competitors ADD COLUMN IF NOT EXISTS validated boolean DEFAULT null;"
    )
    try:
        resp = httpx.post(
            f"{url}/rest/v1/rpc/exec_sql",
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json={"sql": sql},
            timeout=15,
        )
        if resp.status_code not in (200, 204):
            print(f"  Migration skipped (HTTP {resp.status_code}) — run manually if first time:")
            print(f"    ALTER TABLE competitors ADD COLUMN IF NOT EXISTS checked boolean DEFAULT false;")
            print(f"    ALTER TABLE competitors ADD COLUMN IF NOT EXISTS validated boolean DEFAULT null;")
    except Exception as e:
        print(f"  Migration skipped ({e}) — run manually if first time.")


def main() -> None:
    _run_migration()

    db      = _client()
    mistral = Mistral(api_key=os.environ["MISTRAL_API_KEY"], timeout_ms=120_000)

    # ── Fetch unchecked rows ──────────────────────────────────────────────────
    rows = (
        db.table("competitors")
        .select("*")
        .eq("checked", False)
        .eq("active", True)
        .limit(100)
        .execute()
        .data or []
    )

    if not rows:
        print("All relationships validated.")
        return

    sample = random.sample(rows, min(5, len(rows)))

    # ── Build pairs with startup data ─────────────────────────────────────────
    pairs = []
    for row in sample:
        a = (
            db.table("compspro")
            .select("name, description, sectors, subsectors")
            .eq("name", row["company_a"])
            .single()
            .execute()
            .data or {}
        )
        b = (
            db.table("compspro")
            .select("name, description, sectors, subsectors")
            .eq("name", row["company_b"])
            .single()
            .execute()
            .data or {}
        )
        pairs.append({
            "company_a": a,
            "company_b": b,
            "score":     row.get("score"),
        })

    # ── Single LLM call for all pairs ─────────────────────────────────────────
    prompt = f"""For each pair below, determine if the two startups are true competitors —
i.e. they target the same customer with a similar product or solve the same problem.

{json.dumps(pairs, indent=2)}

Return ONLY a JSON array:
[
  {{
    "company_a": "name",
    "company_b": "name",
    "is_competitor": true,
    "reason": "one sentence"
  }}
]
"""

    response = _chat_complete(
        mistral,
        model="mistral-large-latest",
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
    )

    raw = json.loads(response.choices[0].message.content)
    # Model may return {"results": [...]} or a bare array wrapped in some key
    results: list[dict] = raw if isinstance(raw, list) else next(
        (v for v in raw.values() if isinstance(v, list)), []
    )

    # ── Update DB and print results ───────────────────────────────────────────
    validated_count   = 0
    invalidated_count = 0

    for result in results:
        ca  = result.get("company_a", "")
        cb  = result.get("company_b", "")
        ok  = bool(result.get("is_competitor"))
        why = result.get("reason", "")

        db.table("competitors").update(
            {"checked": True, "validated": ok}
        ).eq("company_a", ca).eq("company_b", cb).execute()

        orig = next((r for r in sample if r["company_a"] == ca and r["company_b"] == cb), {})

        if ok:
            validated_count += 1
            score = orig.get("score")
            score_str = f" (score: {score})" if score is not None else ""
            print(f"  ✓ {ca} ↔ {cb} → validated{score_str}")
        else:
            invalidated_count += 1
            print(f"  ✗ {ca} ↔ {cb} → invalidated — \"{why}\"")

    # ── Summary ───────────────────────────────────────────────────────────────
    remaining = (
        db.table("competitors")
        .select("company_a", count="exact")
        .eq("checked", False)
        .eq("active", True)
        .execute()
        .count or 0
    )

    print("\n── Validation complete ──")
    print(f"Validated: {validated_count} | Invalidated: {invalidated_count} | Remaining unchecked: {remaining}")


if __name__ == "__main__":
    main()
