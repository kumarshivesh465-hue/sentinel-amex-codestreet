"""Optional LLM-backed intent classification.

Design contract
---------------
1. This module never raises. On any failure - missing key, network error,
   timeout, malformed response, unexpected label - it returns ``None`` and the
   caller keeps the rule-based result.
2. It only *refines* classification. It cannot approve financial actions, and
   it cannot raise a confidence above the ceiling below.
3. It uses the standard library only, so it adds no dependency to the project.

Enable with:

    set LLM_ENABLED=true
    set GEMINI_API_KEY=<your key>

Nothing is sent anywhere unless both are set.
"""

from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.request

try:
    from . import config
except ImportError:  # executed as a top-level script
    import config

# An LLM may refine a classification, but must never be able to claim more
# certainty than this. Financial actions still pass through the Policy Gateway.
LLM_CONFIDENCE_CEILING = 0.92

VALID_INTENTS = {
    "LOST_STOLEN",
    "DISPUTE",
    "FEE_REVERSAL",
    "CREDIT_LIMIT",
    "HUMAN_REQUEST",
    "UNKNOWN",
}

_ENDPOINTS = {
    "gemini": "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
}

_PROMPT = (
    "You are an intent classifier for a credit-card servicing system.\n"
    "Classify the customer message into exactly one intent.\n\n"
    "Allowed intents:\n"
    "LOST_STOLEN   - card lost, stolen, missing, unusable\n"
    "DISPUTE       - unrecognized, fraudulent, duplicate, or wrong charge\n"
    "FEE_REVERSAL  - asking to waive or reverse a fee\n"
    "CREDIT_LIMIT  - asking for a higher credit limit\n"
    "HUMAN_REQUEST - explicitly asking for a human\n"
    "UNKNOWN       - none of the above\n\n"
    "Reply with JSON only, no prose:\n"
    '{"intent": "<ONE OF THE ABOVE>", "confidence": <float 0-1>, '
    '"reason": "<max 12 words>"}\n\n'
    "Customer message:\n---\n{text}\n---"
)

_state_lock = threading.Lock()
_disabled_reason = None


def status() -> dict:
    with _state_lock:
        return {
            "enabled": config.settings.llm_ready,
            "provider": config.settings.llm_provider if config.settings.llm_ready else None,
            "usable": config.settings.llm_ready and _disabled_reason is None,
            "disabled_reason": _disabled_reason,
        }


def _disable(reason: str) -> None:
    """Latch the backend off after a permanent failure so we stop retrying."""
    global _disabled_reason
    with _state_lock:
        if _disabled_reason is None:
            _disabled_reason = reason


def _extract_json(raw: str):
    if not raw:
        return None
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, flags=re.S)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return None


def _call_gemini(text: str):
    endpoint = _ENDPOINTS["gemini"].format(model=config.settings.llm_model)
    url = f"{endpoint}?key={config.settings.llm_api_key}"
    body = json.dumps(
        {
            "contents": [{"parts": [{"text": _PROMPT.format(text=text[:2000])}]}],
            "generationConfig": {"temperature": 0.0, "maxOutputTokens": 128},
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=config.settings.llm_timeout_seconds) as response:
        payload = json.loads(response.read().decode("utf-8"))

    candidates = payload.get("candidates") or []
    if not candidates:
        return None
    parts = (candidates[0].get("content") or {}).get("parts") or []
    if not parts:
        return None
    return parts[0].get("text", "")


def classify(text: str):
    """Return a refinement dict, or ``None`` to keep the rule-based result."""
    if not config.settings.llm_ready or _disabled_reason is not None or not text.strip():
        return None

    try:
        if config.settings.llm_provider == "gemini":
            raw = _call_gemini(text)
        else:
            _disable(f"unsupported provider '{config.settings.llm_provider}'")
            return None
    except urllib.error.HTTPError as exc:
        # 401/403/400 mean the key or model is wrong; retrying will not help.
        if exc.code in (400, 401, 403, 404):
            _disable(f"http {exc.code} from provider")
            return None
        return None
    except Exception:
        return None

    parsed = _extract_json(raw)
    if not isinstance(parsed, dict):
        return None

    intent = str(parsed.get("intent", "")).strip().upper()
    if intent not in VALID_INTENTS:
        return None

    try:
        confidence = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0

    confidence = max(0.0, min(LLM_CONFIDENCE_CEILING, confidence))

    return {
        "intent": intent,
        "label": intent.replace("_", " ").title(),
        "confidence": round(confidence, 2),
        "reason": str(parsed.get("reason", ""))[:120],
        "source": "llm",
    }
