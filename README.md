# Sentinel — Autonomous Servicing Intelligence Platform

> 🚀 Built for **American Express CodeStreet 2026**  
> Theme: **End-to-End Servicing Agent**

Sentinel is an AI-powered servicing platform designed to automate high-frequency customer support requests for American Express cardmembers. Instead of relying on a single chatbot, Sentinel coordinates multiple specialized AI agents that collaborate to understand customer intent, assess risk, execute policy-compliant actions, explain every decision, and maintain a tamper-proof audit trail for complete transparency.

The project demonstrates how autonomous AI agents can safely resolve real customer servicing requests while keeping humans in the loop whenever confidence is low or additional verification is required.

---

## 🚧 Project Status

> **This project is currently under active development as part of American Express CodeStreet 2026.**

The core architecture and servicing workflows are functional, while several advanced AI capabilities are still being implemented. The repository will continue to receive frequent updates throughout the hackathon.

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

### 🔄 Escalation Workflow

Low-confidence requests are automatically routed to a human agent together with:

- Conversation history
- AI reasoning
- Confidence score
- Suggested resolution

---

# 🚀 Currently Being Developed

- LLM-powered Intent Classification
- Real Authentication Flow
- Sentiment & Frustration Detection
- Timeline Replay
- AI Quality Score
- CSAT Prediction
- Voice Support

---

# 🛠 Tech Stack

## Backend

- Python 3
- http.server
- sqlite3
- hashlib
- JSON REST APIs

> No external backend frameworks are used.

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
│   ├── agents.py
│   ├── db.py
│   ├── server.py
│   └── ...
│
├── frontend/
│   ├── index.html
│   ├── css/
│   ├── js/
│   └── assets/
│
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

- [ ] Gemini-powered Intent Agent
- [ ] Real Authentication
- [ ] Sentiment Analysis
- [ ] Timeline Replay
- [ ] AI Quality Score
- [ ] Voice Channel
- [ ] Automated Testing
- [ ] FastAPI Migration

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
