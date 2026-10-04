# Sentinel — Autonomous Servicing Intelligence Platform

![CI](https://github.com/kumarshivesh465-hue/sentinel-amex-codestreet/actions/workflows/ci.yml/badge.svg)

> 🚀 Built for **American Express CodeStreet 2026**  
> Theme: **End-to-End Servicing Agent**

Sentinel is an AI-powered servicing platform designed to automate high-frequency customer support requests for American Express cardmembers. Instead of relying on a single chatbot, Sentinel coordinates multiple specialized AI agents that collaborate to understand customer intent, assess risk, execute policy-compliant actions, explain every decision, and maintain a tamper-proof audit trail for complete transparency.

The project demonstrates how autonomous AI agents can safely resolve real customer servicing requests while keeping humans in the loop whenever confidence is low or additional verification is required.

---

## ✅ Project Status

> **Complete and fully tested for American Express CodeStreet 2026.**

Every feature on the roadmap is implemented: multi-agent servicing, explainable decisions, a hash-chained audit trail, real authentication with roles, CSAT prediction, timeline replay, and voice input/output. The backend is deliberately dependency-free (Python standard library only) and is covered by an automated test suite plus a live smoke test.

---

# ✨ Current Features

### 🤖 Multi-Agent Architecture

- Intent Agent
- Risk Assessment Agent
- Policy Gateway
- Explainability Agent
- Escalation Agent
- Four Specialized Domain Resolver Agents

---

### 💳 Supported Servicing Requests

- Lost / Stolen Card
- Fee Reversal
- Credit Limit Increase
- Billing Dispute

---

### 🔍 Explainable AI

Every automated decision includes:

- Confidence Score
- Policy References
- Human-readable reasoning
- Decision trace

---

### 🔐 Secure Audit Trail

- SHA-256 hash-chained audit logs
- Tamper detection
- Audit verification
- Immutable event history

---

### 📊 Supervisor Dashboard

- Live servicing sessions
- Confidence monitoring
- Escalation tracking
- Case overview

---

### 🕓 Timeline Replay

- Chronological replay of every message and audit event per session
- Served by `GET /api/sessions/<id>/timeline`

---

### 🔄 Escalation Workflow

Low-confidence requests are automatically routed to a human agent together with:

- Conversation history
- AI reasoning
- Confidence score
- Suggested resolution

---

### 🔐 Authentication & Roles

- Register / sign in with email + password (PBKDF2-SHA256, per-user salt)
- Stateless HMAC-SHA256 signed bearer tokens with expiry
- Every data endpoint requires a token; sessions are scoped to their owner
- Two roles: **cardmember** (own cases only) and **supervisor** (all cases, audit, tamper demo)
- Enable/disable with `AUTH_ENABLED` (on by default)

---

### 📈 CSAT Prediction

- Predicts the star rating a customer is likely to leave (1.0-5.0)
- Deterministic and explainable: each contributing signal is listed as a driver
- Surfaced on proposals, escalations, and every dashboard session

---

### 🎙 Voice Channel

- Browser speech-to-text (Web Speech API) transcribes the cardmember's request
- Optional spoken replies via speech synthesis
- Degrades gracefully in browsers without support

---

### 📝 LLM-powered Intent Classification (optional)

- Optional Gemini refinement of the deterministic rule-based classifier
- Off unless `LLM_ENABLED=true` and an API key are set; the rule-based result is always the fallback

---

# 🛠 Tech Stack

## Backend

- Python 3
- http.server
- sqlite3
- hashlib / hmac / secrets (auth)
- JSON REST APIs

> No external backend frameworks or third-party packages are used.

### Frontend

- HTML5
- CSS3
- JavaScript (ES6)

### Database

- SQLite

---

# 📂 Project Structure

```
sentinel-amex-codestreet/
│
├── backend/
│   ├── agents.py      # intent, sentiment, policy, explainability, CSAT
│   ├── auth.py        # password hashing + signed tokens
│   ├── config.py      # environment-driven settings
│   ├── db.py          # SQLite persistence + hash-chained audit log
│   ├── intent.py      # optional LLM intent refinement
│   └── server.py      # stdlib HTTP server, routing, orchestration
│
├── frontend/
│   └── index.html     # single-page app (chat, dashboard, audit, timeline, voice)
│
├── scripts/
│   └── smoke_test.py  # live end-to-end HTTP smoke test
│
├── tests/
│   ├── test_agents.py
│   ├── test_api.py
│   ├── test_auth.py
│   └── test_db.py
│
├── run_tests.py
├── README.md
└── .gitignore
```

---

# ⚙️ Getting Started

## Clone the Repository

```bash
git clone https://github.com/kumarshivesh465-hue/sentinel-amex-codestreet.git

cd sentinel-amex-codestreet
```

---

## Requirements

- Python 3.8+
- Modern Web Browser

---

## Run Locally

```bash
python backend/server.py
```

Open:

```
http://localhost:8000
```

The first screen is a sign-in / create-account form. Register a **supervisor**
account to see the dashboard and audit explorer, or a **cardmember** account for
the customer-scoped experience. To run the open, auth-free demo instead:

```bash
AUTH_ENABLED=false python backend/server.py
```

### Key environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `PORT` | `8000` | HTTP port |
| `DEMO_MODE` | `true` | Enables the audit-tamper simulation endpoint |
| `AUTH_ENABLED` | `true` | Require login for all data endpoints |
| `AUTH_SECRET` | random per run | Signing key; set it to keep tokens valid across restarts |
| `LLM_ENABLED` / `GEMINI_API_KEY` | off | Optional LLM intent refinement |

---

## Testing

```bash
python run_tests.py            # unit + API tests
python scripts/smoke_test.py   # boots the real server and exercises it over HTTP
python scripts/load_test.py    # concurrency + p50/p95/p99 latency
```

CI (`.github/workflows/ci.yml`) runs the compile check, the full test suite, the
live smoke test and a secret scan on every push and pull request.

---

# 📸 Screenshots

Screenshots and demo GIFs will be added as development progresses.

---

# 🗺 Roadmap

- [x] Multi-Agent Architecture
- [x] Explainability Engine
- [x] Policy Gateway
- [x] Audit Trail
- [x] Supervisor Dashboard
- [x] Escalation Workflow

### Upcoming

- [x] Gemini-powered Intent Agent (optional refinement)
- [x] Real Authentication
- [x] Sentiment Analysis
- [x] Timeline Replay
- [x] AI Quality Score
- [x] CSAT Prediction
- [x] Voice Channel
- [x] Automated Testing

> **Note on the FastAPI migration:** intentionally not pursued. The project is
designed as a zero-dependency standard-library backend, and rewriting it on
FastAPI/uvicorn would add third-party packages and invalidate the existing
test architecture for no functional gain.

---

# 🔐 Security

- Real authentication (PBKDF2-SHA256 + HMAC-signed tokens) with server-side
  roles and per-user data isolation. Cross-user access returns **404**, not 403.
- Security headers on every response (CSP, HSTS, nosniff, frame-deny, referrer
  and permissions policies).
- Rate limiting on login/register (per IP and per email) and the general API,
  returning `429` with `Retry-After`.
- Structured JSON logs with a per-request `X-Request-ID`; passwords, tokens and
  emails are redacted before logging. Clients never see stack traces.
- Money handled in integer cents; parameterised SQL; path-traversal-safe static
  serving; account data export and deletion.
- Threat model and residual risks: [`docs/security/threat-model.md`](docs/security/threat-model.md).

---

# 📚 Documentation

| Doc | Contents |
| --- | --- |
| [`docs/system-design.md`](docs/system-design.md) | Problem, users, flows, requirements, out-of-scope |
| [`docs/architecture.md`](docs/architecture.md) | Components, diagram, risks |
| [`docs/permissions.md`](docs/permissions.md) | Role × action matrix and authorization rules |
| [`docs/api-conventions.md`](docs/api-conventions.md) | Error shape, pagination, status codes, headers |
| [`docs/testing.md`](docs/testing.md) | Test strategy and how to run |
| [`docs/security/threat-model.md`](docs/security/threat-model.md) | Threats, mitigations, residual risks |
| [`docs/hosting.md`](docs/hosting.md) | Environments, env vars, production checklist |
| [`docs/monitoring.md`](docs/monitoring.md) | Health checks, metrics, alert rules |
| [`docs/caching.md`](docs/caching.md) | Cache rules and headers |
| [`docs/performance.md`](docs/performance.md) | Measured load test + bottleneck |
| [`docs/runbooks/`](docs/runbooks/) | Deploy, rollback, incident, backups |
| [`docs/decisions/`](docs/decisions/) | Architecture Decision Records |

---

# ⚖️ Legal

- [Privacy Policy](frontend/privacy.html) — what is stored and your export/delete rights.
- [Terms of Service](frontend/terms.html) — demo-only, no real financial actions.

---

# 🤝 Contributing

This repository is currently maintained by me as part of the American Express CodeStreet 2026 Hackathon.

External contributions will be considered after the competition concludes.

---

# 📄 License

This project is currently **not licensed** while the hackathon is in progress.

All rights reserved.

---

## 👨‍💻 Author

**Shivesh Kumar**
