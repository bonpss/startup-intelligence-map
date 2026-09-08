import os
import threading

from mistralai.client.sdk import Mistral
from dotenv import load_dotenv

from retry import build_retry, is_mistral_retryable, log_retry_attempt
from storage import BATCH_TIMEOUT_MS, log_api_call

load_dotenv()

# Candidates per embed() call -- mistral-embed accepts a batch of inputs in one
# request, so backfill_embeddings.py can embed the whole DB in a handful of
# calls instead of one per row. Well under any request-size limit for
# description-length text (2-3 sentences each).
BATCH_SIZE = 50

_retry = build_retry(
    is_mistral_retryable,
    wait_multiplier=2, wait_min=4, wait_max=60, stop_attempts=7,
    before_sleep=log_retry_attempt,
)

_mistral: Mistral | None = None
_mistral_lock = threading.Lock()


def _client() -> Mistral:
    """Lazily create and cache a single Mistral client, same pattern as
    competitor.py's _client() -- avoids constructing a second client instance
    just because this module is imported separately.
    """
    global _mistral
    if _mistral is None:
        with _mistral_lock:
            if _mistral is None:
                _mistral = Mistral(api_key=os.environ["MISTRAL_API_KEY"], timeout_ms=BATCH_TIMEOUT_MS)
    return _mistral


@_retry
def _embed_batch(texts: list[str]) -> list[list[float]]:
    r = _client().embeddings.create(model="mistral-embed", inputs=texts, timeout_ms=BATCH_TIMEOUT_MS)
    usage = r.usage
    log_api_call("embedding", "mistral-embed", usage.prompt_tokens or 0, usage.completion_tokens or 0, item_count=len(texts))
    return [d.embedding for d in r.data]


def embed(texts: list[str]) -> list[list[float]]:
    """Embed a list of texts with mistral-embed, batching internally at
    BATCH_SIZE. Returns one 1024-float vector per input text, same order.
    """
    vectors: list[list[float]] = []
    for i in range(0, len(texts), BATCH_SIZE):
        vectors.extend(_embed_batch(texts[i:i + BATCH_SIZE]))
    return vectors


def embed_one(text: str) -> list[float]:
    """Embed a single text -- the ingestion-time call site (main.py), where
    there's only ever one description to embed per new startup.
    """
    return embed([text])[0]
