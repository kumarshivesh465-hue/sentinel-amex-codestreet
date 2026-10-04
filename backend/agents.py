"""Sentinel agent layer.

Pure, deterministic functions: no I/O and no network. Given the same input and
session seed, every agent returns the same result, so a decision recorded in
the audit trail can be reproduced and re-verified later.

The only component that may touch the network is the optional LLM intent
backend in ``intent.py``, which stays disabled unless explicitly configured.
"""

from __future__ import annotations

import hashlib
import re

try:
    from . import money
except ImportError:  # executed as a top-level script
    import money

# ---------------------------------------------------------------------------
# Policy constants. The README and the UI quote these numbers, so they live in
# exactly one place.
# ---------------------------------------------------------------------------
AUTONOMY_CONFIDENCE_THRESHOLD = 0.65
FEE_WAIVER_LIMIT = 150.00
DISPUTE_AUTONOMOUS_LIMIT = 100.00

FRUSTRATION_ESCALATION_THRESHOLD = 0.70

DEFAULT_FEE_AMOUNT = 95.00
DEFAULT_DISPUTE_AMOUNT = 84.00

MAX_PLAUSIBLE_AMOUNT = 250_000.00


# ---------------------------------------------------------------------------
# Intent & Entity Agent
# ---------------------------------------------------------------------------

# Explicit handoff language only. A bare "agent" or "person" is NOT enough:
# "the agent on the phone charged me a fee" is a fee complaint, not a request
# for a human. Excluding bare nouns fixes that false positive.
_HUMAN_REQUEST = re.compile(
    r"(?:\b(?:speak|talk|connect|transfer|escalate|chat)\s+(?:me\s+)?(?:to|with)\s+"
    r"(?:a\s+|an\s+|the\s+|someone\s+|any(?:one)?\s+)?"
    r"(?:human|person|representative|rep|agent|supervisor|manager|someone)\b)"
    r"|(?:\b(?:human|real|live)\s+(?:agent|being|person|representative|rep)\b)"
    r"|(?:\breal\s+person\b)"
    r"|(?:\bcustomer\s+(?:service|care)\s+(?:rep|representative|agent)\b)",
    re.I,
)

# Ordered most-specific / highest-risk first. Card security outranks billing,
# billing outranks fee reversal.
_INTENT_RULES = (
    (
        "LOST_STOLEN",
        "Lost / Stolen Card",
        0.96,
        re.compile(
            r"\b(lost|stolen|stole|missing|misplaced|snatched|can'?t find|cannot find|went missing)\b",
            re.I,
        ),
    ),
    (
        "DISPUTE",
        "Billing Dispute",
        0.93,
        re.compile(
            r"\b(dispute|unauthori[sz]ed|fraud(?:ulent)?|don'?t recognize|do not recognize|"
            r"didn'?t make|did not make|wrong charge|double charge|duplicate charge|"
            r"never (?:made|authorized|authorised)|chargeback)\b",
            re.I,
        ),
    ),
    (
        "FEE_REVERSAL",
        "Fee Reversal",
        0.90,
        re.compile(
            r"\b(fee|fees|waive|waiver|reverse|reversal|overcharge[d]?|annual charge|"
            r"late charge|interest charge|service charge)\b",
            re.I,
        ),
    ),
    (
        "CREDIT_LIMIT",
        "Credit Limit Increase",
        0.94,
        re.compile(
            r"\b(credit limit|limit increase|raise (?:my )?limit|increase (?:my |the )?limit|"
            r"higher limit|spending limit|more credit)\b",
            re.I,
        ),
    ),
)


# Canonical display names for every intent, so any layer (rule-based or LLM)
# renders the same label for the same intent.
INTENT_LABELS = {
    "LOST_STOLEN": "Lost / Stolen Card",
    "DISPUTE": "Billing Dispute",
    "FEE_REVERSAL": "Fee Reversal",
    "CREDIT_LIMIT": "Credit Limit Increase",
    "HUMAN_REQUEST": "Explicit Human Request",
    "UNKNOWN": "Unclassified",
}


def classify_intent(text: str) -> dict:
    """Classify a customer message.

    Returns the primary intent plus every other intent that also matched, so a
    multi-intent message ("lost my card and was charged a fee") is visible in
    the decision payload instead of being silently flattened to one category.
    """
    t = text or ""

    if _HUMAN_REQUEST.search(t):
        return {
            "intent": "HUMAN_REQUEST",
            "label": "Explicit Human Request",
            "confidence": 1.0,
            "secondary_intents": [],
            "ambiguous": False,
        }

    matches = [
        {"intent": name, "label": label, "confidence": conf}
        for name, label, conf, pattern in _INTENT_RULES
        if pattern.search(t)
    ]

    if not matches:
        return {
            "intent": "UNKNOWN",
            "label": "Unclassified",
            "confidence": 0.31,
            "secondary_intents": [],
            "ambiguous": False,
        }

    primary = matches[0]
    secondary = matches[1:]

    confidence = primary["confidence"]
    if secondary:
        # More than one servicing category matched. Do not pretend to be sure.
        confidence = round(confidence - 0.12, 2)

    return {
        "intent": primary["intent"],
        "label": primary["label"],
        "confidence": confidence,
        "secondary_intents": [m["intent"] for m in secondary],
        "ambiguous": bool(secondary),
    }


# ---------------------------------------------------------------------------
# Entity extraction
# ---------------------------------------------------------------------------

_CURRENCY_AMOUNT = re.compile(r"\$\s*(\d{1,6}(?:,\d{3})*(?:\.\d{1,2})?)")
_CONTEXT_AMOUNT = re.compile(
    r"\b(?:charged|charge|fee|fees|amount|bill(?:ed)?|purchase|transaction|payment|"
    r"refund|cost|paid|paying|owe|owing|debit(?:ed)?|total)\b[^0-9$]{0,24}?"
    r"\$?\s*(\d{1,6}(?:,\d{3})*(?:\.\d{1,2})?)",
    re.I,
)
_DECIMAL_AMOUNT = re.compile(r"\b(\d{1,6}\.\d{1,2})\b")


def _to_amount(raw: str):
    try:
        value = float(str(raw).replace(",", ""))
    except (TypeError, ValueError):
        return None
    if 0.01 <= value <= MAX_PLAUSIBLE_AMOUNT:
        return round(value, 2)
    return None


def extract_amount(text: str):
    """Best-effort money extraction, or ``None``.

    Order matters. A dollar sign is the strongest signal; otherwise we require
    money-ish wording right next to the number; otherwise we accept a decimal
    number, which is almost always a price.

    A bare integer is deliberately NOT accepted. That is what previously made
    "my card ending 1234" and "since 2019" parse as amounts.
    """
    if not text:
        return None

    for pattern in (_CURRENCY_AMOUNT, _CONTEXT_AMOUNT, _DECIMAL_AMOUNT):
        match = pattern.search(text)
        if match:
            value = _to_amount(match.group(1))
            if value is not None:
                return value
    return None


# ---------------------------------------------------------------------------
# Sentiment & frustration detection
# ---------------------------------------------------------------------------

# Deliberately a small, transparent lexicon rather than a model: it needs no
# network, no weights, and no API key, and it is auditable line by line. The
# tradeoff is that it misses sarcasm and unusual phrasing.
_NEGATIVE_TERMS = {
    "angry": 0.90,
    "furious": 1.00,
    "ridiculous": 0.80,
    "unacceptable": 0.90,
    "terrible": 0.85,
    "awful": 0.85,
    "disgusted": 0.90,
    "frustrated": 0.85,
    "frustrating": 0.85,
    "annoyed": 0.70,
    "annoying": 0.70,
    "upset": 0.70,
    "disappointed": 0.60,
    "unhappy": 0.60,
    "useless": 0.80,
    "incompetent": 0.90,
    "scam": 0.90,
    "sue": 0.95,
    "lawyer": 0.90,
    "legal action": 1.00,
    "complaint": 0.60,
    "sick of": 0.80,
    "fed up": 0.90,
    "never again": 0.90,
    "cancel my card": 0.80,
    "close my account": 0.80,
    "worst": 0.90,
    "horrible": 0.85,
    "appalling": 0.90,
    "pathetic": 0.85,
}
_INTENSIFIERS = ("very", "really", "extremely", "so", "absolutely", "completely", "totally")


def detect_sentiment(text: str) -> dict:
    """Estimate customer frustration on a 0-1 scale.

    Used to decide whether an otherwise-resolvable case should go to a human
    anyway, and surfaced in the supervisor dashboard.
    """
    if not text:
        return {"score": 0.0, "label": "neutral", "signals": []}

    lowered = text.lower()
    score = 0.0
    signals = []

    for term, weight in _NEGATIVE_TERMS.items():
        if term in lowered:
            boost = 0.10 if any(f"{q} {term}" in lowered for q in _INTENSIFIERS) else 0.0
            score = max(score, min(1.0, weight + boost))
            signals.append(term)

    letters = [c for c in text if c.isalpha()]
    if letters:
        caps_ratio = sum(1 for c in letters if c.isupper()) / len(letters)
        if caps_ratio > 0.60 and len(letters) > 12:
            score = min(1.0, score + 0.15)
            signals.append("shouting")

    if re.search(r"!{2,}", text):
        score = min(1.0, score + 0.10)
        signals.append("multiple_exclamations")

    if score >= 0.75:
        label = "high"
    elif score >= 0.45:
        label = "elevated"
    else:
        label = "neutral"

    return {"score": round(score, 2), "label": label, "signals": signals[:6]}


# ---------------------------------------------------------------------------
# Risk & Identity Agent
# ---------------------------------------------------------------------------

def risk_assessment(intent: str, sentiment: dict = None, ambiguous: bool = False) -> dict:
    sentiment = sentiment or {"score": 0.0, "label": "neutral", "signals": []}
    step_up_required = intent in ("LOST_STOLEN", "CREDIT_LIMIT")

    elevated = (
        step_up_required
        or ambiguous
        or sentiment.get("score", 0.0) >= FRUSTRATION_ESCALATION_THRESHOLD
    )

    return {
        "step_up_required": step_up_required,
        "risk_tier": "elevated" if elevated else "standard",
        "sentiment": sentiment.get("label", "neutral"),
    }


# ---------------------------------------------------------------------------
# Deterministic pseudo-randomness
# ---------------------------------------------------------------------------

def stable_int(seed: str, low: int, high: int) -> int:
    """Deterministic integer in [low, high] derived from a string seed.

    Replaces ``random.randint`` so that re-opening the same case shows the same
    numbers. A demo about explainability cannot show a different utilization on
    every page refresh.
    """
    digest = hashlib.sha256((seed or "sentinel").encode("utf-8")).hexdigest()
    span = high - low + 1
    return low + (int(digest[:12], 16) % span)


# ---------------------------------------------------------------------------
# Domain Resolver Agents
# ---------------------------------------------------------------------------

def resolve_lost_stolen() -> dict:
    return {
        "action": "CARD_REISSUE",
        "confidence": 0.97,
        "policy": "CARD-REISSUE-01",
        "autonomous": True,
        "payload": {"virtual_card_issued": True, "physical_card_frozen": True},
        "summary": "Freeze card & issue instant virtual replacement",
    }


def resolve_fee_reversal(amount) -> dict:
    assumed = amount is None
    value = DEFAULT_FEE_AMOUNT if assumed else amount
    # Compare in integer cents so a boundary amount is never misjudged.
    eligible = money.to_cents(value) <= money.to_cents(FEE_WAIVER_LIMIT)

    if assumed:
        confidence = 0.74 if eligible else 0.55
    else:
        confidence = 0.91 if eligible else 0.58

    return {
        "action": "FEE_REVERSAL",
        "confidence": confidence,
        "policy": "FEE-WAIVER-02",
        "autonomous": eligible,
        "payload": {
            "amount": value,
            "amount_cents": money.to_cents(value),
            "amount_assumed": assumed,
        },
        "summary": (
            f"Reverse ${value:.2f} fee"
            if eligible
            else f"${value:.2f} exceeds autonomous limit"
        ),
    }


def resolve_credit_limit(seed: str = "") -> dict:
    utilization = stable_int(seed + ":util", 15, 55)
    on_time_payments = stable_int(seed + ":otp", 12, 48)

    likely = utilization < 35 and on_time_payments >= 12
    confidence = 0.89 if likely else 0.62

    return {
        "action": "CREDIT_LIMIT_PREVIEW",
        "confidence": confidence,
        "policy": "CREDIT-LIMIT-PREVIEW-01",
        "autonomous": likely,
        "payload": {"utilization": utilization, "on_time_payments": on_time_payments},
        "summary": (
            "Likely autonomous approval" if likely else "Likely requires underwriter review"
        ),
    }


def resolve_dispute(amount) -> dict:
    assumed = amount is None
    value = DEFAULT_DISPUTE_AMOUNT if assumed else amount
    eligible = money.to_cents(value) <= money.to_cents(DISPUTE_AUTONOMOUS_LIMIT)

    if assumed:
        confidence = 0.72 if eligible else 0.40
    else:
        confidence = 0.88 if eligible else 0.40

    return {
        "action": "DISPUTE_PROVISIONAL_CREDIT",
        "confidence": confidence,
        "policy": "DISPUTE-PROVISIONAL-01",
        "autonomous": eligible,
        "payload": {
            "amount": value,
            "amount_cents": money.to_cents(value),
            "amount_assumed": assumed,
        },
        "summary": (
            f"Issue ${value:.2f} provisional credit"
            if eligible
            else f"${value:.2f} exceeds always-human threshold"
        ),
    }


# ---------------------------------------------------------------------------
# Explainability Agent
# ---------------------------------------------------------------------------

RATIONALE_TEMPLATES = {
    "CARD_REISSUE": (
        "No fraud signals on the account, identity verified via step-up authentication, "
        "and this matches the standard reissue policy ({policy}). Sentinel can act autonomously."
    ),
    "FEE_REVERSAL": (
        "Fee amount ${amount:.2f} is under the ${limit:.2f} autonomous-approval threshold "
        "under {policy} and no prior waiver exists in the last 12 months."
    ),
    "FEE_REVERSAL_ASSUMED": (
        "No fee amount was stated, so Sentinel is working from the typical fee of "
        "${amount:.2f} for this account type. That is still under the ${limit:.2f} "
        "autonomous threshold ({policy}), but because the amount was assumed rather than "
        "confirmed, confidence is reduced."
    ),
    "FEE_REVERSAL_DENIED": (
        "Requested amount ${amount:.2f} exceeds the ${limit:.2f} autonomous-approval limit "
        "under {policy}. Requires human review - not because the AI is unsure, but because "
        "policy reserves this size of waiver for a person."
    ),
    "CREDIT_LIMIT_PREVIEW": (
        "Preview based on {on_time_payments} consecutive on-time payments and current "
        "utilization of {utilization}%. This is a non-binding estimate - no hard inquiry "
        "has been made."
    ),
    "DISPUTE_PROVISIONAL_CREDIT": (
        "Charge amount ${amount:.2f} is under the ${limit:.2f} provisional-credit "
        "autonomous threshold ({policy}). Sentinel can issue provisional credit now while "
        "the merchant investigation proceeds in the background."
    ),
    "DISPUTE_ASSUMED": (
        "No charge amount was stated, so Sentinel is working from the typical disputed "
        "amount of ${amount:.2f}. That is under the ${limit:.2f} provisional-credit "
        "threshold ({policy}), but the assumed amount lowers confidence."
    ),
    "DISPUTE_DENIED": (
        "Disputes over ${limit:.2f} are an always-human category regardless of AI "
        "confidence - these often involve merchant negotiation and potential chargeback "
        "litigation that requires human judgment."
    ),
    "ESCALATION_LOW_CONFIDENCE": (
        "Sentinel could not confidently match this request to a known servicing category "
        "(confidence below the {threshold:.0%} autonomy threshold). Rather than guess on a "
        "financial action, it hands off to a human with full context."
    ),
    "ESCALATION_HUMAN_REQUEST": (
        "Customer explicitly asked to speak with a human agent - Sentinel honors this "
        "immediately, no confidence threshold applies."
    ),
    "ESCALATION_FRUSTRATION": (
        "Frustration signals were detected ({signals}) even though the request itself "
        "matched a servicable category. Sentinel escalates rather than risk training a "
        "high-emotion customer on a fully automated path."
    ),
}


def explain(action: str, **kwargs) -> str:
    template = RATIONALE_TEMPLATES.get(action, "No rationale template available for this action.")
    try:
        return template.format(**kwargs)
    except (KeyError, ValueError, IndexError):
        return template


# ---------------------------------------------------------------------------
# Policy Gateway
# ---------------------------------------------------------------------------

def _payload_cents(payload: dict):
    """Read the amount from a payload as integer cents, or ``None``."""
    if payload.get("amount_cents") is not None:
        try:
            return int(payload["amount_cents"])
        except (TypeError, ValueError):
            return None
    if payload.get("amount") is not None:
        try:
            return money.to_cents(payload["amount"])
        except (TypeError, ValueError):
            return None
    return None


def policy_gate(action: str, payload: dict) -> dict:
    """Hard limits that no confidence score can override.

    Amounts are compared as integer cents (see ``money``) so the boundary is
    exact, including for amounts a caller supplies directly.
    """
    payload = payload or {}
    amount_cents = _payload_cents(payload)

    if action == "FEE_REVERSAL" and amount_cents is not None and amount_cents > money.to_cents(FEE_WAIVER_LIMIT):
        return {
            "passed": False,
            "reason": f"amount exceeds FEE-WAIVER-02 hard limit ${FEE_WAIVER_LIMIT:.2f}",
        }

    if (
        action == "DISPUTE_PROVISIONAL_CREDIT"
        and amount_cents is not None
        and amount_cents > money.to_cents(DISPUTE_AUTONOMOUS_LIMIT)
    ):
        return {
            "passed": False,
            "reason": f"amount exceeds DISPUTE-PROVISIONAL-01 hard limit ${DISPUTE_AUTONOMOUS_LIMIT:.2f}",
        }

    return {"passed": True, "reason": "within policy bounds"}


# ---------------------------------------------------------------------------
# AI Quality Score
# ---------------------------------------------------------------------------

def quality_score(
    confidence: float,
    policy_passed: bool = True,
    escalated: bool = False,
    sentiment: dict = None,
    ambiguous: bool = False,
    assumed_amount: bool = False,
) -> dict:
    """A single 0-100 score for how well Sentinel handled a case.

    Weighted: confidence 60, policy compliance 25, sentiment handling 15, minus
    explicit penalties for ambiguity and assumed values. It is a transparency
    device, not a regulatory metric.
    """
    sentiment = sentiment or {"score": 0.0, "label": "neutral", "signals": []}

    components = {
        "confidence": round(max(0.0, min(1.0, confidence)) * 60.0, 1),
        "policy": 25.0 if policy_passed else 0.0,
        "sentiment_handling": (
            15.0
            if escalated or sentiment.get("score", 0.0) < FRUSTRATION_ESCALATION_THRESHOLD
            else 0.0
        ),
    }

    penalties = 0.0
    if ambiguous:
        penalties += 8.0
    if assumed_amount:
        penalties += 7.0

    score = max(0.0, min(100.0, sum(components.values()) - penalties))

    if score >= 85:
        band = "excellent"
    elif score >= 70:
        band = "good"
    elif score >= 55:
        band = "fair"
    else:
        band = "needs_review"

    return {
        "score": round(score, 1),
        "band": band,
        "components": components,
        "penalties": penalties,
    }


# ---------------------------------------------------------------------------
# CSAT Prediction Agent
# ---------------------------------------------------------------------------

def predict_csat(
    confidence: float,
    sentiment: dict = None,
    escalated: bool = False,
    resolved: bool = False,
    quality: dict = None,
    assumed_amount: bool = False,
) -> dict:
    """Predict the star rating a customer is likely to leave (1.0-5.0).

    Deterministic and explainable, like every other agent here: each signal
    contributes a visible amount, so the prediction can be justified rather
    than presented as a black box. It is a heuristic estimate, not a trained
    model - the dashboard labels it as such.
    """
    sentiment = sentiment or {"score": 0.0, "label": "neutral"}
    drivers = []
    score = 4.0
    drivers.append("baseline 4.0")

    if resolved and not escalated:
        score += 0.6
        drivers.append("+0.6 resolved autonomously")
    if escalated:
        score -= 0.7
        drivers.append("-0.7 handed to a human")

    s = float(sentiment.get("score", 0.0) or 0.0)
    if s >= FRUSTRATION_ESCALATION_THRESHOLD:
        score -= 1.3
        drivers.append("-1.3 high frustration")
    elif s >= 0.45:
        score -= 0.5
        drivers.append("-0.5 elevated frustration")

    if confidence >= 0.9:
        score += 0.3
        drivers.append("+0.3 high confidence")
    elif confidence < 0.65:
        score -= 0.3
        drivers.append("-0.3 low confidence")

    q = (quality or {}).get("score")
    if q is not None:
        if q >= 85:
            score += 0.3
            drivers.append("+0.3 excellent quality")
        elif q < 55:
            score -= 0.3
            drivers.append("-0.3 poor quality")

    if assumed_amount:
        score -= 0.2
        drivers.append("-0.2 assumed amount")

    score = max(1.0, min(5.0, round(score, 1)))

    if score >= 4.0:
        band = "likely_promoter"
    elif score >= 3.0:
        band = "neutral"
    else:
        band = "at_risk"

    return {"predicted": score, "band": band, "drivers": drivers}


# ---------------------------------------------------------------------------
# Escalation Agent
# ---------------------------------------------------------------------------

def build_escalation_context(
    session_id: str,
    transcript_last: str,
    confidence: float,
    reason: str,
    sentiment: dict = None,
    quality: dict = None,
) -> dict:
    sentiment = sentiment or {"score": 0.0, "label": "neutral", "signals": []}
    context = {
        "session": session_id,
        "transcript_last_message": transcript_last,
        "confidence": confidence,
        "reason": reason,
        "sentiment": sentiment,
        "handoff_note": "No re-authentication required. Full transcript attached.",
    }
    if quality:
        context["quality_score"] = quality["score"]
        context["quality_band"] = quality["band"]
    return context
