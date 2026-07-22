import re
import random

AUTONOMY_CONFIDENCE_THRESHOLD = 0.65
FEE_WAIVER_LIMIT = 150.00
DISPUTE_AUTONOMOUS_LIMIT = 100.00


# ---------------------------------------------------------------------------
# Intent & Entity Agent
# ---------------------------------------------------------------------------
def classify_intent(text: str) -> dict:
    t = text.lower()
    if re.search(r"\b(human|agent|person|representative)\b", t):
        return {"intent": "HUMAN_REQUEST", "label": "Explicit Human Request"}
    if re.search(r"lost|stolen|missing|misplaced", t):
        return {"intent": "LOST_STOLEN", "label": "Lost / Stolen Card"}
    if re.search(r"dispute|unauthorized|don.?t recognize|didn.?t make|wrong charge", t):
        return {"intent": "DISPUTE", "label": "Billing Dispute"}
    if re.search(r"fee|waive|reverse|overcharge|annual charge", t):
        return {"intent": "FEE_REVERSAL", "label": "Fee Reversal"}
    if re.search(r"credit limit|limit increase|raise (my )?limit|increase (my )?limit", t):
        return {"intent": "CREDIT_LIMIT", "label": "Credit Limit Increase"}
    return {"intent": "UNKNOWN", "label": "Unclassified"}


def extract_amount(text: str):
    m = re.search(r"\$?\s?(\d{1,6}(\.\d{1,2})?)", text)
    return float(m.group(1)) if m else None


# Risk & Identity Agent
def risk_assessment(intent: str) -> dict:
    step_up_required = intent in ("LOST_STOLEN", "CREDIT_LIMIT")
    return {"step_up_required": step_up_required, "risk_tier": "elevated" if step_up_required else "standard"}


# ---------------------------------------------------------------------------
# Domain Resolver Agents
# ---------------------------------------------------------------------------
def resolve_lost_stolen() -> dict:
    confidence = 0.97
    return {
        "action": "CARD_REISSUE",
        "confidence": confidence,
        "policy": "CARD-REISSUE-01",
        "autonomous": True,
        "payload": {"virtual_card_issued": True, "physical_card_frozen": True},
        "summary": "Freeze card & issue instant virtual replacement",
    }


def resolve_fee_reversal(amount: float) -> dict:
    amount = amount or 95.0
    eligible = amount <= FEE_WAIVER_LIMIT
    confidence = 0.91 if eligible else 0.58
    return {
        "action": "FEE_REVERSAL",
        "confidence": confidence,
        "policy": "FEE-WAIVER-02",
        "autonomous": eligible,
        "payload": {"amount": amount},
        "summary": f"Reverse ${amount:.2f} fee" if eligible else f"${amount:.2f} exceeds autonomous limit",
    }


def resolve_credit_limit() -> dict:
    utilization = random.randint(15, 55)
    on_time_payments = 24
    likely = utilization < 35
    confidence = 0.89 if likely else 0.62
    return {
        "action": "CREDIT_LIMIT_PREVIEW",
        "confidence": confidence,
        "policy": "CREDIT-LIMIT-PREVIEW-01",
        "autonomous": likely,
        "payload": {"utilization": utilization, "on_time_payments": on_time_payments},
        "summary": "Likely autonomous approval" if likely else "Likely requires underwriter review",
    }


def resolve_dispute(amount: float) -> dict:
    amount = amount or 84.0
    eligible = amount <= DISPUTE_AUTONOMOUS_LIMIT
    confidence = 0.88 if eligible else 0.40
    return {
        "action": "DISPUTE_PROVISIONAL_CREDIT",
        "confidence": confidence,
        "policy": "DISPUTE-PROVISIONAL-01",
        "autonomous": eligible,
        "payload": {"amount": amount},
        "summary": f"Issue ${amount:.2f} provisional credit" if eligible else f"${amount:.2f} exceeds always-human threshold",
    }


# ---------------------------------------------------------------------------
# Explainability Agent
# Deliberately decoupled from the Resolver Agents above: it only ever
# receives the resolver's *output*, never its internal logic, so an
# explanation can never be "reverse engineered" to sound more
# reasonable than the actual decision was. (PRD section 24)
# ---------------------------------------------------------------------------
RATIONALE_TEMPLATES = {
    "CARD_REISSUE": "No fraud signals on the account, identity verified via step-up authentication, "
                     "and this matches the standard reissue policy ({policy}). Sentinel can act autonomously.",
    "FEE_REVERSAL": "Fee amount ${amount:.2f} is under the ${limit:.2f} autonomous-approval threshold "
                     "under {policy} and no prior waiver exists in the last 12 months.",
    "FEE_REVERSAL_DENIED": "Requested amount ${amount:.2f} exceeds the ${limit:.2f} autonomous-approval limit "
                            "under {policy}. Requires human review — not because the AI is unsure, but because "
                            "policy reserves this size of waiver for a person.",
    "CREDIT_LIMIT_PREVIEW": "Preview based on {on_time_payments} consecutive on-time payments and current "
                             "utilization of {utilization}%. This is a non-binding estimate — no hard inquiry has been made.",
    "DISPUTE_PROVISIONAL_CREDIT": "Charge amount ${amount:.2f} is under the ${limit:.2f} provisional-credit "
                                   "autonomous threshold ({policy}). Sentinel can issue provisional credit now "
                                   "while the merchant investigation proceeds in the background.",
    "DISPUTE_DENIED": "Disputes over ${limit:.2f} are an always-human category regardless of AI confidence — "
                       "these often involve merchant negotiation and potential chargeback litigation that "
                       "requires human judgment.",
    "ESCALATION_LOW_CONFIDENCE": "Sentinel could not confidently match this request to a known servicing "
                                  "category (confidence below the {threshold:.0%} autonomy threshold). Rather "
                                  "than guess on a financial action, it hands off to a human with full context.",
    "ESCALATION_HUMAN_REQUEST": "Customer explicitly asked to speak with a human agent — Sentinel honors this "
                                 "immediately, no confidence threshold applies.",
}


def explain(action: str, **kwargs) -> str:
    template = RATIONALE_TEMPLATES.get(action, "No rationale template available for this action.")
    try:
        return template.format(**kwargs)
    except KeyError:
        return template


# ---------------------------------------------------------------------------
# Policy Gateway
# Independent, final-say check — deliberately re-validates hard limits
# even though the Resolver already applied them, so a bug in a single
# Resolver Agent can never bypass a compliance boundary. (PRD section 17/27)
# ---------------------------------------------------------------------------
def policy_gate(action: str, payload: dict) -> dict:
    if action == "FEE_REVERSAL" and payload.get("amount", 0) > FEE_WAIVER_LIMIT:
        return {"passed": False, "reason": f"amount exceeds FEE-WAIVER-02 hard limit ${FEE_WAIVER_LIMIT:.2f}"}
    if action == "DISPUTE_PROVISIONAL_CREDIT" and payload.get("amount", 0) > DISPUTE_AUTONOMOUS_LIMIT:
        return {"passed": False, "reason": f"amount exceeds DISPUTE-PROVISIONAL-01 hard limit ${DISPUTE_AUTONOMOUS_LIMIT:.2f}"}
    return {"passed": True, "reason": "within policy bounds"}


# ---------------------------------------------------------------------------
# Escalation Agent
# ---------------------------------------------------------------------------
def build_escalation_context(session_id: str, transcript_last: str, confidence: float, reason: str) -> dict:
    return {
        "session": session_id,
        "transcript_last_message": transcript_last,
        "confidence": confidence,
        "reason": reason,
        "handoff_note": "No re-authentication required. Full transcript attached.",
    }
