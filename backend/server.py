import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote

sys.path.insert(0, os.path.dirname(__file__))
import db
import agents

PORT = 8000
FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")


def json_body(handler):
    length = int(handler.headers.get("Content-Length", 0))
    if length == 0:
        return {}
    raw = handler.rfile.read(length)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[sentinel] {self.address_string()} - {fmt % args}")

    # ---------- helpers ----------
    def send_json(self, status, data):
        body = json.dumps(data, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, content_type):
        try:
            with open(path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except FileNotFoundError:
            self.send_json(404, {"error": "not found"})

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    # ---------- static frontend ----------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        if path == "/" or path == "/index.html":
            return self.send_file(os.path.join(FRONTEND_DIR, "index.html"), "text/html")

        if path == "/api/dashboard/sessions":
            sessions = db.list_sessions()
            return self.send_json(200, {"sessions": sessions})

        if path == "/api/audit/verify":
            ok, broken_at = db.verify_chain()
            return self.send_json(200, {"ok": ok, "broken_at": broken_at, "count": len(db.list_audit())})

        if path.startswith("/api/audit/"):
            session_id = path.split("/api/audit/")[1]
            return self.send_json(200, {"records": db.list_audit(session_id)})

        if path == "/api/audit":
            return self.send_json(200, {"records": db.list_audit()})

        self.send_json(404, {"error": "not found"})

    # ---------- API actions ----------
    def do_POST(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        body = json_body(self)

        if path == "/api/sessions":
            session_id = db.new_session_id()
            db.create_session(session_id)
            return self.send_json(200, {"session_id": session_id})

        if path.startswith("/api/sessions/") and path.endswith("/messages"):
            session_id = path.split("/api/sessions/")[1].split("/messages")[0]
            return self.handle_message(session_id, body)

        if path == "/api/actions/confirm":
            return self.handle_confirm(body)

        if path == "/api/audit/tamper":
            audit_id = body.get("audit_id")
            if audit_id:
                db.tamper_demo(audit_id)
            return self.send_json(200, {"tampered": audit_id})

        self.send_json(404, {"error": "not found"})

    # ---------- core orchestration ----------
    def handle_message(self, session_id, body):
        text = body.get("text", "")
        if not db.get_session(session_id):
            db.create_session(session_id)
        db.add_message(session_id, "user", text)

        classification = agents.classify_intent(text)
        intent = classification["intent"]
        db.update_session(session_id, flow=classification["label"])

        if intent == "HUMAN_REQUEST":
            return self.escalate(session_id, text, 1.0, "Explicit human request",
                                  agents.explain("ESCALATION_HUMAN_REQUEST"))

        if intent == "UNKNOWN":
            return self.escalate(session_id, text, 0.31, "Low-confidence intent classification",
                                  agents.explain("ESCALATION_LOW_CONFIDENCE",
                                                 threshold=agents.AUTONOMY_CONFIDENCE_THRESHOLD))

        risk = agents.risk_assessment(intent)

        if intent == "LOST_STOLEN":
            resolution = agents.resolve_lost_stolen()
            rationale = agents.explain("CARD_REISSUE", policy=resolution["policy"])
            return self.propose(session_id, intent, resolution, rationale, risk)

        if intent == "FEE_REVERSAL":
            amount = agents.extract_amount(text)
            resolution = agents.resolve_fee_reversal(amount)
            if resolution["autonomous"]:
                rationale = agents.explain("FEE_REVERSAL", amount=resolution["payload"]["amount"],
                                            limit=agents.FEE_WAIVER_LIMIT, policy=resolution["policy"])
                return self.propose(session_id, intent, resolution, rationale, risk)
            else:
                rationale = agents.explain("FEE_REVERSAL_DENIED", amount=resolution["payload"]["amount"],
                                            limit=agents.FEE_WAIVER_LIMIT, policy=resolution["policy"])
                return self.escalate(session_id, text, resolution["confidence"],
                                      "Fee amount exceeds autonomous threshold", rationale)

        if intent == "CREDIT_LIMIT":
            resolution = agents.resolve_credit_limit()
            rationale = agents.explain("CREDIT_LIMIT_PREVIEW", **resolution["payload"])
            return self.propose(session_id, intent, resolution, rationale, risk)

        if intent == "DISPUTE":
            amount = agents.extract_amount(text)
            resolution = agents.resolve_dispute(amount)
            if resolution["autonomous"]:
                rationale = agents.explain("DISPUTE_PROVISIONAL_CREDIT", amount=resolution["payload"]["amount"],
                                            limit=agents.DISPUTE_AUTONOMOUS_LIMIT, policy=resolution["policy"])
                return self.propose(session_id, intent, resolution, rationale, risk)
            else:
                rationale = agents.explain("DISPUTE_DENIED", limit=agents.DISPUTE_AUTONOMOUS_LIMIT)
                return self.escalate(session_id, text, resolution["confidence"],
                                      "Dispute amount exceeds always-human threshold", rationale)

        return self.send_json(400, {"error": "unhandled intent"})

    def propose(self, session_id, intent, resolution, rationale, risk):
        gate = agents.policy_gate(resolution["action"], resolution["payload"])
        if not gate["passed"]:
            return self.escalate(session_id, "", resolution["confidence"],
                                  "Policy Gateway rejected proposed action: " + gate["reason"], rationale)

        db.update_session(session_id, status="Awaiting Confirmation", confidence=resolution["confidence"])
        db.add_message(session_id, "agent", resolution["summary"])
        response = {
            "type": "proposal",
            "session_id": session_id,
            "intent": intent,
            "action": resolution["action"],
            "summary": resolution["summary"],
            "confidence": resolution["confidence"],
            "policy": resolution["policy"],
            "rationale": rationale,
            "payload": resolution["payload"],
            "step_up_required": risk["step_up_required"],
            "autonomous": resolution["autonomous"],
        }
        return self.send_json(200, response)

    def escalate(self, session_id, text, confidence, reason, rationale):
        context = agents.build_escalation_context(session_id, text, confidence, reason)
        db.update_session(session_id, status="Escalated", confidence=confidence)
        entry = db.write_audit(session_id, "ESCALATION", context, confidence,
                                policy="ESCALATION-POLICY", rationale=rationale)
        return self.send_json(200, {
            "type": "escalation",
            "session_id": session_id,
            "reason": reason,
            "rationale": rationale,
            "context": context,
            "audit": entry,
        })

    def handle_confirm(self, body):
        session_id = body.get("session_id")
        action = body.get("action")
        payload = body.get("payload", {})
        confidence = body.get("confidence", 0.0)
        policy = body.get("policy", "")
        rationale = body.get("rationale", "")

        gate = agents.policy_gate(action, payload)
        if not gate["passed"]:
            return self.send_json(403, {"error": "Policy Gateway rejected execution: " + gate["reason"]})

        entry = db.write_audit(session_id, action, payload, confidence, policy=policy, rationale=rationale)
        db.update_session(session_id, status="Resolved")
        db.add_message(session_id, "system", f"Action {action} executed and logged as {entry['id']}")
        return self.send_json(200, {"executed": True, "audit": entry})


def main():
    db.init_db()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Sentinel backend running at http://localhost:{PORT}")
    print(f"Serving frontend from: {os.path.abspath(FRONTEND_DIR)}")
    print(f"Database: {db.DB_PATH}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
