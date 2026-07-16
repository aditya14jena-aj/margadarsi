# Corridor — WC26 Volunteer Field Console

AI-orchestrated wayfinding, medical, and crowd-guidance assistant for World Cup
volunteers. A fan asks a translated question; Corridor cross-references live
stadium state (gate load, incidents, temperature) and returns a strict-schema
JSON directive plus a script the volunteer reads aloud.

## Stack
- **Backend:** Flask + Gemini (`google-genai`), gunicorn for production
- **Frontend:** server-rendered HTML/CSS/JS, no build step
- **State:** file-backed JSON (swap for Redis/Postgres at scale — see `load_state`/`save_state` in `app.py`)

## Local development
```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in GEMINI_API_KEY
export $(cat .env | xargs)
python app.py
```
Visit `http://localhost:5000`.

## Production run (gunicorn)
```bash
export GEMINI_API_KEY=...
gunicorn --bind 0.0.0.0:8080 --workers 3 --threads 2 --timeout 60 app:app
```

## Docker
```bash
docker build -t corridor .
docker run -p 8080:8080 -e GEMINI_API_KEY=your_key corridor
```

## Deploy targets
- **Render / Railway / Heroku:** push repo, set `GEMINI_API_KEY` env var, `Procfile` is picked up automatically.
- **Fly.io / any container host:** use the `Dockerfile` directly.
- **Behind a load balancer:** point health checks at `GET /healthz` (returns `200` when the state store is reachable, `503` otherwise).

## API
| Method | Path | Purpose |
|---|---|---|
| GET | `/api/state` | Current stadium state JSON |
| POST | `/api/state` | Jury God Mode — `{full_state:{...}}` or `{ambient_temperature, infrastructure_patch}` |
| POST | `/api/query` | `{query: "translated fan question"}` → orchestrator JSON |
| GET | `/healthz` | Liveness/readiness probe |

All `/api/*` errors return `{error, message}` with an appropriate HTTP status
(`400` bad input, `429` rate-limited, `500` unexpected, `503` unhealthy).

## Production notes / what to change before scaling past a demo
- **State store:** the JSON file is fine for a single instance; move to Redis or a DB for multi-instance deployments so God Mode updates are shared.
- **Rate limiting:** in-memory per-process limiter (`RATE_LIMIT_PER_MIN`, default 30/min/IP). Replace with `Flask-Limiter` + Redis behind a load balancer.
- **Secrets:** never commit `.env`; `GEMINI_API_KEY` is read from the environment only.
- **Logging:** structured stdout logs (`corridor` logger) — wire to your platform's log aggregator.
- **CORS:** not enabled; add `flask-cors` if the frontend is served from a different origin.
- **Auth:** the console currently has no login — add volunteer auth before real deployment with real fan data.
