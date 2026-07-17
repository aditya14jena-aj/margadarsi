<div align="center">

# 🏟️ Margadarshi — WC26 Volunteer Field Console

### AI-Orchestrated Wayfinding & Crowd Guidance (Because tourists get lost easily) 🧭

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![Python](https://img.shields.io/badge/Python-3.10+-blue?logo=python)](https://python.org/)
[![Flask](https://img.shields.io/badge/Flask-Backend-black?logo=flask)](https://flask.palletsprojects.com/)
[![Gemini](https://img.shields.io/badge/AI-Google_Gemini-orange?logo=google)](https://ai.google.dev/)

Listen, World Cup fans are chaotic. **Margadarshi** is a real-time, AI-powered field console that helps volunteers tell lost, overheated, or confused fans exactly where to go—without sending them into a closed gate or a medical emergency. 

[🚀 Live Demo (Probably)](#) · [📖 Read The Code](#) · [🐛 Cry About a Bug](../../issues) 

</div>

---

## ✨ Features (What it actually does)


| Feature                              | Description                                                                               |
| ------------------------------------ | ----------------------------------------------------------------------------------------- |
| 🧠 **AI Orchestrator (Gemini)**      | Takes frantic fan questions, cross-references stadium state, and spits out a script.      |
| 🗣️ **Auto-Translation Magic**        | Detects the fan's language, translates for you, and gives you a script to read back.      |
| 🏟️ **Live Stadium State**            | Real-time tracking of gate loads, incidents, and wait times so you don't route people to closed doors. |
| 🌡️ **Weather Integration**           | Pulls live weather. If it's 95°F and a fan is dizzy, the AI *will* escalate it to Critical. |
| 🚨 **Supervisor Escalations**        | A real queue for when things hit the fan. Medical? Security? It logs and pages.           |
| 🛡️ **Deterministic Routing Guardrails** | We don't let the LLM hallucinate routes. Safe routes are computed in strict Python code first. |
| 🕹️ **Jury God Mode**                 | Operator APIs to inject incidents, close gates, or simulate chaos on the fly.             |
| 📋 **Shift Logs (Receipts)**         | Logs every single query and response. Because accountability matters.                     |

---
## 🤖 The AI Volunteer Brain (Margadarshi)

Our LLM prompt isn't just a basic chatbot. It runs a strict decision framework:
1. **Translate & Classify:** Figures out what the fan is yelling, silently translates to English, and classifies the intent (Medical, Transit, Crowd, etc.).
2. **Escalate on Compound Risk:** Mentions chest pain? Boom, Critical. Heat-related issue on a 95°F day? Escalated. 
3. **Match Intent to Node:** Uses the deterministic `safe_routes` to find the actual closest open spot.
4. **Generate Strict Scripts:** Returns a `volunteer_script` (for you to understand) and a `fan_facing_script` (for you to read aloud via TTS in their language).

---

## 🛠 Tech Stack (The Good Stuff)

### Backend (Where the magic happens)
- **[Flask](https://flask.palletsprojects.com/)** — Because sometimes you just need a reliable Python server.
- **[Gemini API](https://ai.google.dev/)** — `google-genai` powering the reasoning core.
- **State Store** — File-backed JSON (swap for Redis/Postgres at scale, obviously).

### Frontend (Where things look pretty)
- **Vanilla HTML/CSS/JS** — Server-rendered. No build step. No Webpack. Just pure, unadulterated web standards. 

### Deployment (Production Ready)
- **Gunicorn** — Handling the heat in prod.
- **Docker** — Containerized because it's 2026.

---

## 📁 Project Structure

```text
FIFA-solution/
├── app.py             # The massive brains of the operation (Routes & Logic)
├── routing.py         # Hardcoded safety rules (Don't trust the AI with safety)
├── crowd_sim.py       # Simulates people moving around (Because we need data)
├── templates/         # Where the HTML lives
└── stadium_state.json # The live state database (for now)
```

---

## 🚀 Local Development (Don't break it)

### Prerequisites

- Python 3.10+ (Update your Python, seriously)
- A Google Gemini API Key

---

### ⚙️ Setup Instructions

```bash
# Create and activate a virtual environment. Do not skip this unless you enjoy dependency hell.
python -m venv venv

# Mac/Linux
source venv/bin/activate
# Windows
.\venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Setup your secrets
cp .env.example .env
```

#### Environment Variables

Edit your `.env` file and drop your Gemini API key in there:
> ⚠️ **Never hardcode credentials in source code. We will judge you.**

```env
GEMINI_API_KEY=your_actual_key_here
```

#### Run the Thing

```bash
# Load env vars and run
python app.py
```

Visit `http://localhost:5000` and pretend you're managing a stadium.

---

## 🏭 Production (Because localhost isn't real life)

### Gunicorn Run
```bash
export GEMINI_API_KEY=...
gunicorn --bind 0.0.0.0:8080 --workers 3 --threads 2 --timeout 60 app:app
```

### Docker Run
```bash
docker build -t Margadarshi .
docker run -p 8080:8080 -e GEMINI_API_KEY=your_key Margadarshi
```

### Deploy Targets
- **Render / Railway / Heroku:** Just push the repo, set `GEMINI_API_KEY`, and the `Procfile` takes over.
- **Fly.io:** Use the provided `Dockerfile`.

---

## 🔥 API Reference

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/state` | Grabs the current stadium state JSON. |
| POST | `/api/state` | **Jury God Mode** — Inject chaos into the stadium. |
| POST | `/api/query` | The main brain endpoint. Feed it a fan query, get back JSON. |
| GET | `/healthz` | Liveness/readiness probe so your load balancer stops crying. |

All `/api/*` errors return `{error, message}` with actual appropriate HTTP status codes (`400` bad input, `429` rate-limited, `500` unexpected, `503` unhealthy). We have standards.

---

## 🔐 Production Notes (Read before scaling)

- **State store:** The JSON file is cute for a single instance. Move to Redis or a DB for multi-instance deployments.
- **Rate limiting:** In-memory right now. Replace with `Flask-Limiter` + Redis behind a load balancer.
- **Secrets:** Never commit `.env`. Just don't do it.
- **Auth:** There's no login yet. Add volunteer auth before real deployment unless you want rogue fans answering themselves.

---

<div align="center">

Built to keep the World Cup running smoothly.

*Margadarshi — Don't get lost.*

</div>
