import json
import os
import httpx
from mistralai.client.sdk import Mistral
from mistralai.client.errors.sdkerror import SDKError
from dotenv import load_dotenv
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception
from storage import (
    BATCH_TIMEOUT_MS,
    INTERACTIVE_REQUEST,
    INTERACTIVE_TIMEOUT_MS,
    RETRY_INTERACTIVE_STOP,
    RETRY_INTERACTIVE_WAIT,
)
from taxonomy import TAXONOMY, SUBSECTOR_DEFINITIONS, HORIZONTAL_SUBSECTORS, validate_subsectors, demote_generic_erp_tag, remove_redundant_uncategorized


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, json.JSONDecodeError):
        return True
    return isinstance(exc, SDKError) and any(code in str(exc) for code in ("429", "503", "529"))


# Batch/backfill budget (reprocess_list.py's extract() calls) -- free-tier Mistral
# rate limits reset on a ~1min window, not seconds -- same fix as competitor.py's
# retry (a 30s ceiling gave up before the window cleared). See
# storage.RETRY_INTERACTIVE_WAIT/_STOP for the tighter interactive (/api/ingest,
# CLI) budget -- a single ingest chains several of these calls (Step 1, 2a, 2b,
# 2c), so each one must stay well short of the batch path's ~124s backoff +
# 7x120s timeout worst case (~16min) or the browser client stalls with no
# feedback. Shared with competitor.py via storage.py, not duplicated here.


@retry(retry=retry_if_exception(_is_retryable), wait=wait_exponential(multiplier=2, min=4, max=60), stop=stop_after_attempt(7), reraise=True)
def _chat_complete_core(client: Mistral, timeout_ms: int, **kwargs) -> dict:
    r = client.chat.complete(timeout_ms=timeout_ms, **kwargs)
    return json.loads(r.choices[0].message.content)


# Derived from _chat_complete_core via tenacity's own retry_with() rather than a
# second hand-written function -- inherits the same retry predicate and
# reraise=True, only wait/stop are overridden. Precomputed once, not re-wrapped
# on every call.
_chat_complete_core_interactive = _chat_complete_core.retry_with(
    wait=RETRY_INTERACTIVE_WAIT, stop=RETRY_INTERACTIVE_STOP
)


def _chat_complete(client: Mistral, **kwargs) -> dict:
    """Dispatch to the interactive or batch retry/timeout budget based on
    INTERACTIVE_REQUEST (set by main.ingest() for the duration of a single
    interactive ingest). Drop-in replacement for the old _chat_complete -- every
    existing call site is unchanged.
    """
    if INTERACTIVE_REQUEST.get():
        return _chat_complete_core_interactive(client, timeout_ms=INTERACTIVE_TIMEOUT_MS, **kwargs)
    return _chat_complete_core(client, timeout_ms=BATCH_TIMEOUT_MS, **kwargs)

load_dotenv()

_VALID_SECTORS = sorted(TAXONOMY.keys())
_SECTORS_LIST  = "\n".join(f'  "{s}"' for s in _VALID_SECTORS)

# ── Step 1 prompt: free extraction, no taxonomy ───────────────────────────────

_STEP1_SYSTEM = """
You are a startup intelligence analyst. Given a webpage in markdown,
extract structured information about the startup.

Return ONLY a valid JSON object:
{
  "name": string or null,
  "country": "the company's primary HQ country as its standard English short name
    (e.g. 'United States', 'United Kingdom', 'France') — exactly ONE country, no
    abbreviations (never 'USA', 'US', 'UK'), no extra text about offices or presence
    elsewhere. Return null if genuinely unclear.",
  "description": "2-3 sentence product description focusing on: what the product does, who the customer is, what problem it solves",
  "raw_sectors": ["free-form sector labels, e.g. 'AI video generation', 'cybersecurity for developers'"],
  "raw_subsectors": ["free-form subsector labels, e.g. 'AI avatar creation', 'code security automation'"],
  "logo_url": "the URL of the company's OWN logo, chosen from the LOGO CANDIDATES list
    at the top of the user message. Pick the candidate most likely to be the company's own
    logo — NOT a client/partner logo, NOT a social network icon. Prefer <img> logos over
    icons, and icons over og:image. Return the URL exactly as listed.
    Return null if the list is empty or no candidate is the company's own logo.",
  "linkedin_url": "The URL of the company's LinkedIn page. Look for linkedin.com/company/ links in the page. Return the full URL or null if not found."
}

Be precise and specific. Do not use generic labels.
If a field cannot be determined, set it to null.
""".strip()


# ── Step 2a: sector classification ───────────────────────────────────────────

def _step2a_sectors(client: Mistral, step1: dict) -> tuple[list[str], dict[str, float]]:
    name        = step1.get("name") or "Unknown"
    description = step1.get("description") or ""
    raw_sectors = json.dumps(step1.get("raw_sectors") or [], ensure_ascii=False)

    prompt = f"""You are a taxonomy classifier. Classify the startup into sectors from the list below.

Startup: {name}
Description: {description}
Raw sectors proposed: {raw_sectors}

VALID SECTORS (choose ONLY from this list):
{_SECTORS_LIST}

DISAMBIGUATION RULES — read before choosing:
- `Enterprise Software` = ONLY horizontal business tools: CRM, ERP, project management, customer support, productivity tools (email assistants, scheduling, note-taking, meeting tools). If the startup has a more specific sector, use that instead.
- `AI & Machine Learning` = AI infrastructure, foundation models, MLOps, AI agents, coding assistants, GPU cloud compute. Subsectors include: `MLOps & Infrastructure` (deploy/monitor models) vs `AI Compute & Cloud Infrastructure` (GPU cloud providers, serverless inference). NOT vertical AI applications (those go in their specific sector).
- `Cybersecurity` = any product whose primary value is security: threat detection, identity, compliance, data protection, SOC tools.
- `FinTech` = any product touching money, payments, financial compliance, treasury, crypto, insurance (B2B tools for financial industry).
- `Developer Tools & Infrastructure` = tools built FOR developers: CI/CD, observability, APIs, infrastructure, DevOps, code generation. NOT productivity tools for general professionals, NOT enterprise software that happens to have an API.
- `HRTech` = HR management, payroll, talent acquisition, workforce compliance, AND corporate/professional training or leadership development delivered to employees — whether software, content, or expert-delivered programs. NOT generic enterprise project management.
- `EdTech` = education for K-12 and higher-education institutions and students ONLY. NOT corporate training, professional upskilling, or leadership development for employees — those belong in `HRTech`, regardless of delivery format (software, content library, or expert-delivered program).
- `LegalTech` = legal compliance, contract management, regulatory automation. NOT generic enterprise risk tools.
- `InsurTech` = tools FOR insurance companies and brokers (B2B). NOT insurance products sold to consumers.
- `Consumer Tech` = any product sold directly to individual consumers for personal use (not business use).
- `Marketing Tech` = lead generation, SEO, ad tech, outbound sales, brand tools. NOT generic CRM.
- Software sold to restaurants or commercial kitchens (POS, back-office, ordering, food waste monitoring) belongs in `E-commerce & Retail`, NOT `FoodTech`. `FoodTech` is reserved for companies whose product IS food or food production technology: alternative proteins, fermentation, food ingredients, food science, and food supply chain.
- Software sold to hotels, accommodations, or travel/hospitality operators (revenue management, dynamic pricing, booking and reservation systems, channel management, property operations) belongs in `E-commerce & Retail`, NOT `Enterprise Software` — even when it functions as the operator's core day-to-day system, it is a vertical point solution for a single hospitality use case, not a horizontal, multi-function ERP.

- Sectors must reflect what the product IS, not the industries it SERVES. Only assign a vertical sector if the product itself is deeply embedded in that vertical — not just because it has customers there.
- A sector must represent the product's PRIMARY domain, not its target markets. If a product targets multiple industries (robotics, healthcare, automotive...), it belongs to the sector that describes WHAT IT IS, not WHO BUYS IT. Multi-industry targeting is a sign of a horizontal product — assign the core technology sector, not the customer verticals.

When in doubt between `Enterprise Software` and a more specific sector, ALWAYS prefer the more specific sector.
A startup can have 1-3 sectors. Only add `Enterprise Software` if the product is genuinely horizontal and not better described by another sector.

Return ONLY a valid JSON object:
{{
  "sectors": ["exact sector label from the list above"],
  "confidence": float between 0 and 1
}}

Rules:
- Choose EXACTLY from the provided list. Do not invent new labels.
- "Uncategorized" is a valid choice if nothing fits.
- Pick 1-3 sectors maximum.
- confidence: 1.0 = perfect match, 0.7-0.9 = good match, below 0.7 = uncertain.
"""
    data       = _chat_complete(
        client,
        model="mistral-medium-latest",
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
    )
    sectors    = data.get("sectors") or []
    confidence = float(data.get("confidence", 0.0))

    valid = [s for s in sectors if s in TAXONOMY]
    if not valid or confidence < 0.7:
        return ["Uncategorized"], {"Uncategorized": confidence}
    return valid, {s: confidence for s in valid}


# ── Step 2b: subsector classification ────────────────────────────────────────

def _step2b_subsectors(client: Mistral, description: str, sector: str) -> dict[str, float]:
    available = list(TAXONOMY.get(sector, {}).keys())
    if not available:
        return {"Uncategorized": 1.0}

    options = "\n".join(
        f'  "{s}" — {SUBSECTOR_DEFINITIONS[s]}' if s in SUBSECTOR_DEFINITIONS else f'  "{s}"'
        for s in available
    )
    prompt = f"""You are a taxonomy classifier. Classify the startup into subsectors for the sector "{sector}".

Description: {description}

VALID SUBSECTORS for "{sector}" (choose ONLY from this list):
{options}

Return ONLY a valid JSON object:
{{
  "subsectors": [
    {{"name": "exact subsector label from the list above", "confidence": float between 0 and 1}}
  ]
}}

Rules:
- Choose EXACTLY from the provided list. Do not invent new labels.
- "Uncategorized" is always a valid fallback.
- Pick 1-3 subsectors maximum. Only the most relevant ones.
- confidence: 1.0 = perfect match, 0.7-0.9 = good match, below 0.7 = uncertain.
"""
    data   = _chat_complete(
        client,
        model="mistral-medium-latest",
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
    )
    result = data.get("subsectors") or []
    valid  = {
        item["name"]: float(item.get("confidence", 0.0))
        for item in result
        if isinstance(item, dict) and item.get("name") in TAXONOMY.get(sector, {})
    }
    return valid if valid else {"Uncategorized": 1.0}


# ── Step 2c: sub-subsector classification ────────────────────────────────────

def _step2c_sub_subsectors(client: Mistral, description: str, sector: str, subsector: str) -> dict[str, float]:
    available = TAXONOMY.get(sector, {}).get(subsector, [])
    if not available:
        return {}

    options = "\n".join(f'  "{s}"' for s in available)
    prompt = f"""You are a taxonomy classifier. Select applicable sub-subsectors for a startup in "{subsector}".

Description: {description}

VALID SUB-SUBSECTORS for "{subsector}" (choose ONLY from this list):
{options}

Return ONLY a valid JSON object:
{{
  "sub_subsectors": [
    {{"label": "exact sub-subsector label from the list above", "confidence": float between 0 and 1}}
  ]
}}

Rules:
- Choose EXACTLY from the provided list. Do not invent new labels.
- Pick 0-4 sub-subsectors maximum. Only the most relevant ones. Return an empty list if nothing fits.
- confidence: 1.0 = perfect match, 0.7-0.9 = good match, below 0.7 = uncertain.
"""
    data   = _chat_complete(
        client,
        model="mistral-medium-latest",
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
    )
    result = data.get("sub_subsectors") or []
    return {
        item["label"]: float(item.get("confidence", 0.0))
        for item in result
        if isinstance(item, dict) and item.get("label") in available
    }


def _sector_exempt_from_removal(orig: set[str]) -> bool:
    """Step 5 (extract()) drops a sector once its subsectors are all filtered out --
    UNLESS every one of its *originally proposed* subsectors was only ever a
    horizontal capability tag (e.g. "General Purpose AI Models"), in which case the
    sector itself is still a legitimate classification even with zero surviving
    subsectors. This has needed re-deriving twice already (2026-08 code review),
    so it's a named, independently testable function rather than an inline
    condition -- get any of these three cases wrong and a real sector either
    vanishes or a company keeps a sector tag with nothing backing it:

    - orig = {"Uncategorized"} only (no real signal at all) -> NOT exempt. Its
      one signal was cross-sector redundant with a different sector's real
      classification (taxonomy.remove_redundant_uncategorized), not "removed
      because horizontal" -- the bug this function's very first version existed
      to fix.
    - orig = {"General Purpose AI Models"} (purely horizontal) -> exempt. Stripped
      by taxonomy.validate_subsectors' vertical-context rule, exactly the case
      the exemption is meant to protect.
    - orig = {"Speech & Audio AI", "Uncategorized"} (horizontal + no-info
      fallback) -> exempt. "Uncategorized" carries no information either way.
    - orig = {"Speech & Audio AI", "ERP & Business Operations"} (horizontal +
      a REAL but demoted signal) -> NOT exempt. "ERP & Business Operations" was
      dropped by taxonomy.demote_generic_erp_tag for the same cross-sector
      redundancy reason as the first case, not because it was horizontal --
      a horizontal tag merely coexisting with it doesn't launder that away.
    """
    real_signals = orig - {"Uncategorized"}
    return bool(real_signals) and real_signals.issubset(HORIZONTAL_SUBSECTORS)


# ── Main entry point ──────────────────────────────────────────────────────────

def extract(markdown: str, website: str = None, logo_candidates: list[dict] = None) -> dict:
    client = Mistral(api_key=os.environ["MISTRAL_API_KEY"], timeout_ms=BATCH_TIMEOUT_MS)

    markdown = markdown[:30000]

    if logo_candidates:
        candidates_txt = "\n".join(f"- {c['url']} ({c['hint']})" for c in logo_candidates)
    else:
        candidates_txt = "none"
    user_content = f"LOGO CANDIDATES:\n{candidates_txt}\n\n---\n\n{markdown}"

    # Step 1: free extraction
    step1 = _chat_complete(
        client,
        model="mistral-large-latest",
        messages=[
            {"role": "system", "content": _STEP1_SYSTEM},
            {"role": "user",   "content": user_content},
        ],
        response_format={"type": "json_object"},
    )
    description = step1.get("description") or ""

    # Guard against hallucinated URLs: only accept a listed candidate
    logo_url = step1.get("logo_url")
    if logo_candidates and logo_url not in {c["url"] for c in logo_candidates}:
        logo_url = None

    # Step 2a: sector classification
    sectors, sector_confidences = _step2a_sectors(client, step1)

    # Step 2b: subsector classification — individual confidence per subsector
    subsectors: list[str] = []
    sector_subsector_pairs: list[tuple[str, str]] = []
    subsector_confidences: dict[str, dict[str, float]] = {}
    for sector in sectors:
        subs = _step2b_subsectors(client, description, sector)
        subsector_confidences[sector] = subs
        for sub in subs:
            subsectors.append(sub)
            sector_subsector_pairs.append((sector, sub))
    subsectors = list(dict.fromkeys(subsectors))  # deduplicate, preserve order

    # Step 3: validate_subsectors — remove horizontal tags in vertical contexts
    subsectors = validate_subsectors(subsectors, sectors=sectors)
    subsectors = demote_generic_erp_tag(subsectors)
    subsectors = remove_redundant_uncategorized(subsectors)
    valid_subsector_set = set(subsectors)

    # Step 4: sync subsector_confidences and sector_subsector_pairs
    original_sector_subs: dict[str, set[str]] = {
        sec: set(subs.keys()) for sec, subs in subsector_confidences.items()
    }
    for sec in subsector_confidences:
        subsector_confidences[sec] = {
            sub: conf for sub, conf in subsector_confidences[sec].items()
            if sub in valid_subsector_set
        }
    sector_subsector_pairs = [
        (sec, sub) for sec, sub in sector_subsector_pairs
        if sub in valid_subsector_set
    ]

    # Step 5: threshold 0.9 — drop sectors with all valid subsectors below threshold
    # Sectors whose subsectors were removed only because horizontal are kept
    if len(sectors) >= 2:
        sectors_to_remove = []
        for sector in sectors:
            valid_subs = subsector_confidences.get(sector, {})
            scored_subs = {sub: conf for sub, conf in valid_subs.items() if sub != "Uncategorized"}
            if not valid_subs:
                orig = original_sector_subs.get(sector, set())
                # Exempt iff every one of orig's REAL (non-"Uncategorized") signals
                # is horizontal -- "Uncategorized" itself carries no information, so
                # it's excluded from the check either way. This is deliberately NOT
                # "orig has at least one horizontal tag" (too lenient: a sector whose
                # orig was e.g. {"Speech & Audio AI", "ERP & Business Operations"}
                # also lost a real, non-horizontal signal to demote_generic_erp_tag's
                # cross-sector redundancy rule -- that's a different, non-exempt
                # removal reason, not "removed only because horizontal", even though
                # a horizontal tag also happened to be present). It's also NOT "orig
                # is entirely horizontal-or-Uncategorized" (too strict: the original
                # bug this exemption exists to fix -- a sector whose orig was a lone
                # "Uncategorized" has no real signal at all, so it must NOT be
                # exempt just because "Uncategorized" is in the allowance set).
                real_signals = orig - {"Uncategorized"}
                if not (real_signals and real_signals.issubset(HORIZONTAL_SUBSECTORS)):
                    sectors_to_remove.append(sector)
            elif scored_subs and all(conf < 0.9 for conf in scored_subs.values()):
                sectors_to_remove.append(sector)

        # Guard: never remove all sectors — keep the highest-confidence one
        if set(sectors_to_remove) == set(sectors):
            best = max(sectors, key=lambda s: sector_confidences.get(s, 0.0))
            sectors_to_remove = [s for s in sectors_to_remove if s != best]

        for sector in sectors_to_remove:
            sectors.remove(sector)
        sector_subsector_pairs = [
            (sec, sub) for sec, sub in sector_subsector_pairs
            if sec not in sectors_to_remove
        ]
        remaining_subs = {sub for _, sub in sector_subsector_pairs}
        subsectors = [sub for sub in subsectors if sub in remaining_subs]
        for sector in sectors_to_remove:
            subsector_confidences.pop(sector, None)

    # Step 2c: sub-subsector classification — only for subsectors that actually
    # define sub-subsectors, to avoid a wasted LLM call in the common case
    raw_sub_subsectors: dict[str, float] = {}
    for sector, subsector in sector_subsector_pairs:
        if not TAXONOMY.get(sector, {}).get(subsector):
            continue
        for label, conf in _step2c_sub_subsectors(client, description, sector, subsector).items():
            if label not in raw_sub_subsectors or conf > raw_sub_subsectors[label]:
                raw_sub_subsectors[label] = conf
    sub_subsectors = [
        label for label, _ in sorted(raw_sub_subsectors.items(), key=lambda x: x[1], reverse=True)
    ][:4]

    # Last-mile guard: a sector can survive Step 5 with every one of its subsector
    # candidates filtered out upstream (confidence threshold, validate_subsectors,
    # demote_generic_erp_tag, remove_redundant_uncategorized), leaving subsectors
    # empty rather than ["Uncategorized"] -- invisible to ILIKE '%Uncategorized%'
    # audits, unlike a real Uncategorized tag. Catches this regardless of which
    # upstream filter caused it.
    if not subsectors:
        print("[extract] All subsector candidates filtered out — falling back to Uncategorized")
        subsectors = ["Uncategorized"]

    return {
        "name":                  step1.get("name"),
        "country":               step1.get("country"),
        "description":           description,
        "logo_url":              logo_url,
        "linkedin_url":          step1.get("linkedin_url"),
        "website":               website,
        "sectors":               sectors,
        "subsectors":            subsectors,
        "sub_subsectors":        sub_subsectors,
        "sector_confidences":    sector_confidences,
        "subsector_confidences": subsector_confidences,
    }


if __name__ == "__main__":
    import sys
    website = sys.argv[1] if len(sys.argv) > 1 else None
    markdown = sys.stdin.read()
    result = extract(markdown, website=website)
    print(json.dumps(
        {k: v for k, v in result.items() if k not in ("sector_confidences", "subsector_confidences")},
        ensure_ascii=False, indent=2,
    ))

    print("\n── Sector confidences ──")
    for sector, conf in result.get("sector_confidences", {}).items():
        print(f"  {conf:.2f}  {sector}")

    print("\n── Subsector confidences ──")
    for sector, subs in result.get("subsector_confidences", {}).items():
        for sub, conf in subs.items():
            print(f"  {conf:.2f}  {sector} > {sub}")
