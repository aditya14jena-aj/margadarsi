import os
import json
import logging
import threading
import uuid
import csv
import io
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv
load_dotenv()  # reads .env in the working directory into os.environ, if present

from flask import Flask, request, jsonify, render_template, Response
from werkzeug.exceptions import HTTPException
from google import genai
from google.genai import types

import routing
import crowd_sim

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
class Config:
    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
    MODEL_NAME = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
    PORT = int(os.environ.get("PORT", 5000))
    DEBUG = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    STATE_FILE = os.environ.get(
        "STATE_FILE", os.path.join(os.path.dirname(__file__), "stadium_state.json")
    )
    MAX_QUERY_LEN = 500
    RATE_LIMIT_PER_MIN = int(os.environ.get("RATE_LIMIT_PER_MIN", 30))
    # Secret token required for state-mutation endpoints (/api/state POST,
    # /api/events POST, /api/state/reset). Set via .env or host env vars.
    # If unset (empty), mutations are still allowed but a warning is logged —
    # so dev/demo environments don't break, but production must set this.
    OPERATOR_SECRET = os.environ.get("OPERATOR_SECRET", "")

# Real venue: MetLife Stadium, East Rutherford, NJ — confirmed host of the
# 2026 FIFA World Cup Final (Match 104, July 19, 2026).
STADIUM_LAT = 40.8135
STADIUM_LNG = -74.0745
STADIUM_NAME = "MetLife Stadium — East Rutherford, NJ"


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("margadarshi")

app = Flask(__name__)
app.config.from_object(Config)

# ---------------------------------------------------------------------------
# Gemini client (lazy init so app boots even without a key set, e.g. CI/tests)
# ---------------------------------------------------------------------------
_client = None
_client_lock = threading.Lock()


def get_client():
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                if not app.config["GEMINI_API_KEY"]:
                    raise RuntimeError("GEMINI_API_KEY is not configured")
                _client = genai.Client(api_key=app.config["GEMINI_API_KEY"])
    return _client


# ---------------------------------------------------------------------------
# State store — thread-safe, file-backed with in-memory write-through cache.
# Every load_state() call hits the cache (a dict in RAM); only the initial
# boot and every save_state() touch disk. This eliminates the file read that
# was previously happening on every API call including the 6-second polling.
# Swap _state_cache for Redis (with a short TTL) in a multi-instance deploy.
# ---------------------------------------------------------------------------
_state_lock = threading.Lock()
_state_cache: Optional[dict] = None  # None means "not yet loaded"

DEFAULT_STATE = {
    "volunteer_location": "Corridor 3 - Sector B",
    "ambient_temperature": None,  # populated from live weather on first load
    "infrastructure": {
        "Gate_A_Main_Metro": {
            "status": "open", "capacity_load": "30%", "avg_wait_minutes": 5, "incident": "none",
            "lat": 40.8149, "lng": -74.0725, "label": "Gate A \u2014 NJ Transit Rail Link"
        },
        "Gate_B_West_Shuttle": {
            "status": "open", "capacity_load": "22%", "avg_wait_minutes": 3, "incident": "none",
            "lat": 40.8128, "lng": -74.0774, "label": "Gate B \u2014 West Shuttle Lot"
        },
        "First_Aid_Station_Zone_3": {
            "status": "operational", "capacity_load": "8%", "avg_wait_minutes": 2, "incident": "none",
            "lat": 40.8138, "lng": -74.0748, "label": "First Aid \u2014 Sector B Concourse"
        },
        "Cooling_Center_Sector_B": {
            "status": "operational", "capacity_load": "20%", "avg_wait_minutes": 1, "incident": "none",
            "lat": 40.8136, "lng": -74.0743, "label": "Cooling Center \u2014 Sector B"
        },
        "Security_Post_Sector_B": {
            "status": "operational", "capacity_load": "40%", "avg_wait_minutes": 3, "incident": "none",
            "lat": 40.8140, "lng": -74.0747, "label": "Security Post \u2014 Sector B"
        },
        "Lost_And_Found_Concourse": {
            "status": "operational", "capacity_load": "5%", "avg_wait_minutes": 2, "incident": "none",
            "lat": 40.8132, "lng": -74.0740, "label": "Lost & Found \u2014 Main Concourse"
        },
        "Family_Reunification_Point": {
            "status": "operational", "capacity_load": "8%", "avg_wait_minutes": 2, "incident": "none",
            "lat": 40.8134, "lng": -74.0751, "label": "Family Reunification Point \u2014 Gate C Plaza"
        },
        "Accessibility_Assist_Desk": {
            "status": "operational", "capacity_load": "15%", "avg_wait_minutes": 2, "incident": "none",
            "lat": 40.8137, "lng": -74.0745, "label": "Accessibility Assist Desk \u2014 Sector B"
        },
    },
}

WEATHER_URL = (
    "https://api.open-meteo.com/v1/forecast"
    f"?latitude={STADIUM_LAT}&longitude={STADIUM_LNG}"
    "&current=temperature_2m,relative_humidity_2m,weather_code"
    "&temperature_unit=fahrenheit&timezone=auto"
)


def fetch_live_weather():
    """Real live weather at MetLife Stadium via Open-Meteo (no API key required)."""
    try:
        with urllib.request.urlopen(WEATHER_URL, timeout=5) as resp:
            data = json.loads(resp.read().decode())
        current = data.get("current", {})
        temp_f = current.get("temperature_2m")
        humidity = current.get("relative_humidity_2m")
        if temp_f is None:
            return None
        return {
            "temperature_f": round(temp_f),
            "humidity_pct": humidity,
            "source": "open-meteo.com (live)",
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
        log.warning("weather_fetch_failed err=%s", e)
        return None


def load_state() -> dict:
    global _state_cache
    with _state_lock:
        if _state_cache is not None:
            return json.loads(json.dumps(_state_cache))  # return a deep copy; callers must not mutate cache directly
        path = app.config["STATE_FILE"]
        if not os.path.exists(path):
            seed = json.loads(json.dumps(DEFAULT_STATE))
            weather = fetch_live_weather()
            seed["ambient_temperature"] = f"{weather['temperature_f']}F" if weather else "82F"
            seed["weather_source"] = weather["source"] if weather else "fallback (weather API unreachable)"
            _write_state_unlocked(seed)
            _state_cache = json.loads(json.dumps(seed))
        else:
            with open(path, "r") as f:
                _state_cache = json.load(f)
        return json.loads(json.dumps(_state_cache))


def _write_state_unlocked(state: dict):
    """Write to disk atomically (POSIX rename) and update the in-memory cache."""
    global _state_cache
    path = app.config["STATE_FILE"]
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp_path, path)
    _state_cache = json.loads(json.dumps(state))  # keep cache in sync


def save_state(state: dict):
    with _state_lock:
        _write_state_unlocked(state)


# ---------------------------------------------------------------------------
# Event stream (in-memory stand-in for a Kafka topic). Each event is an
# append-only record of a real-world change (gate closure, capacity update,
# temp reading). Applying an event mutates live infrastructure state; the
# event itself is retained for the audit log / "Live Events Stream" panel.
# Swap _event_log for a real Kafka/Redpanda consumer in production — the
# apply_event() contract stays the same either way.
# ---------------------------------------------------------------------------
_event_lock = threading.Lock()
_event_log = []  # newest last
MAX_EVENT_LOG = 200

VALID_EVENT_TYPES = {"status_change", "capacity_change", "incident_flagged", "incident_cleared", "temp_change"}


def apply_event(event: dict) -> dict:
    """Validates and applies one event to live state; appends to the log."""
    node_id = event.get("node_id")
    event_type = event.get("event_type")
    value = event.get("value")
    reason = event.get("reason", "")

    if event_type not in VALID_EVENT_TYPES:
        raise ValueError(f"unknown event_type '{event_type}', must be one of {sorted(VALID_EVENT_TYPES)}")

    state = load_state()

    if event_type == "temp_change":
        state["ambient_temperature"] = str(value)
    else:
        if node_id not in state.get("infrastructure", {}):
            raise ValueError(f"unknown node_id '{node_id}'")
        node = state["infrastructure"][node_id]
        if event_type == "status_change":
            node["status"] = str(value)
        elif event_type == "capacity_change":
            node["capacity_load"] = str(value)
        elif event_type == "incident_flagged":
            node["incident"] = str(value)
            node["status"] = "restricted"
        elif event_type == "incident_cleared":
            node["incident"] = "none"
            node["status"] = "open"

    save_state(state)

    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "node_id": node_id,
        "event_type": event_type,
        "value": value,
        "reason": reason,
    }
    with _event_lock:
        _event_log.append(record)
        del _event_log[:-MAX_EVENT_LOG]

    log.info("event_applied node=%s type=%s value=%s reason=%s", node_id, event_type, value, reason)
    return {"state": state, "event": record}


def get_recent_events(limit: int = 50):
    with _event_lock:
        return list(_event_log[-limit:])


# ---------------------------------------------------------------------------
# Supervisor escalations — a real, queryable feed, not just a UI flag. A
# volunteer explicitly confirms "Notify Supervisor" for a specific answer;
# that creates a durable, timestamped, status-tracked record here.
# ---------------------------------------------------------------------------
_escalation_lock = threading.Lock()
_escalation_log = []
MAX_ESCALATIONS = 200
VALID_ESCALATION_STATUS = {"open", "acknowledged", "resolved"}


def create_escalation(payload: dict) -> dict:
    record = {
        "id": str(uuid.uuid4())[:8],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "query_id": payload.get("query_id"),
        "intent": payload.get("intent", "Unknown"),
        "urgency": payload.get("urgency", "Med"),
        "query_text": str(payload.get("query_text", ""))[:500],
        "volunteer_script": str(payload.get("volunteer_script", ""))[:500],
        "volunteer_location": payload.get("volunteer_location", ""),
        "status": "open",
    }
    with _escalation_lock:
        _escalation_log.append(record)
        del _escalation_log[:-MAX_ESCALATIONS]
    log.info("escalation_created id=%s intent=%s urgency=%s", record["id"], record["intent"], record["urgency"])
    return record


def get_escalations(limit: int = 50, status: Optional[str] = None):
    with _escalation_lock:
        items = list(_escalation_log)
    if status:
        items = [e for e in items if e["status"] == status]
    return items[-limit:]


def update_escalation_status(escalation_id: str, new_status: str) -> Optional[dict]:
    if new_status not in VALID_ESCALATION_STATUS:
        raise ValueError(f"status must be one of {sorted(VALID_ESCALATION_STATUS)}")
    with _escalation_lock:
        for record in _escalation_log:
            if record["id"] == escalation_id:
                record["status"] = new_status
                record["updated_at"] = datetime.now(timezone.utc).isoformat()
                return dict(record)
    return None


# ---------------------------------------------------------------------------
# Shift log — an append-only transcript of every query this console has
# handled, independent of escalation status. Exportable for post-event
# reporting. Every /api/query call (success or fallback) is recorded here.
# ---------------------------------------------------------------------------
_shift_lock = threading.Lock()
_shift_log = []
MAX_SHIFT_LOG = 1000


def record_shift_entry(entry: dict):
    record = {
        "query_id": entry.get("query_id"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "query_text": str(entry.get("query_text", ""))[:500],
        "detected_language": entry.get("detected_language", ""),
        "intent": entry.get("intent", ""),
        "urgency": entry.get("urgency", ""),
        "alert_color": entry.get("alert_color", ""),
        "volunteer_script": str(entry.get("volunteer_script", ""))[:500],
        "guardrail_override": bool(entry.get("guardrail_override", False)),
        "source": entry.get("source", "llm"),  # "llm" or "fallback"
    }
    with _shift_lock:
        _shift_log.append(record)
        del _shift_log[:-MAX_SHIFT_LOG]
    return record


def get_shift_log(limit: int = 100):
    with _shift_lock:
        return list(_shift_log[-limit:])


def shift_log_to_csv() -> str:
    with _shift_lock:
        rows = list(_shift_log)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["timestamp", "query_id", "detected_language", "intent", "urgency",
                      "alert_color", "source", "guardrail_override", "query_text", "volunteer_script"])
    for r in rows:
        writer.writerow([r["timestamp"], r["query_id"], r["detected_language"], r["intent"],
                          r["urgency"], r["alert_color"], r["source"], r["guardrail_override"],
                          r["query_text"], r["volunteer_script"]])
    return buf.getvalue()


SYSTEM_PROMPT = """You are Margadarshi, the AI reasoning core for a World Cup volunteer field console. Minimize output text. No conversational filler.

LANGUAGE PIPELINE:
The fan's question in <query> may be in any spoken language (it comes from
live speech-to-text). Your first job is translation, not just reasoning:
1. Detect the source language.
2. Silently translate it to English for your own reasoning — do not show
   your translation work, just use it.
3. Produce TWO output scripts (see schema): one in English for the
   volunteer's own understanding, and one in the fan's original detected
   language so the volunteer can play it back to the fan via text-to-speech.
   If you cannot confidently translate back into the fan's language, repeat
   the English script in fan_facing_script and set translation_confidence
   to "low".

CRITICAL — ROUTING IS NOT YOURS TO DECIDE:
A deterministic routing engine has already excluded closed/incident/
over-capacity nodes and ranked the safe candidates for you in
<safe_routes>. It is a closed set grouped by type (transit_exit, medical,
cooling, security, lost_and_found, family_reunification, accessibility).
You may only reference nodes that appear in it. Never invent, imply, or
route to a node absent from <safe_routes>, even if the fan names it
explicitly. If the fan's requested node is excluded, acknowledge their
request and redirect to the nearest eligible alternative of the same type,
briefly stating why.

INPUT DATA STRUCTURE:
<state>{live stadium state, including ambient_temperature}</state>
<safe_routes>{pre-vetted candidate nodes by type, ranked, closed set}</safe_routes>
<query>{fan's question, any language, transcribed from speech}</query>
<source_language_hint>{optional BCP-47 code the volunteer's app pre-selected, may be empty}</source_language_hint>

DECISION FRAMEWORK (apply in order):
1. TRANSLATE & CLASSIFY — detect language, translate internally, then classify:
   - intent: one of Transit, Medical, Crowd, Amenity, Security, LostPerson,
     Accessibility, Weather
   - urgency: Low, Med, Critical
2. ESCALATE ON COMPOUND RISK — urgency is not just about the words used.
   Escalate one level (e.g. Med -> Critical) when risk factors compound:
   - heat-related discomfort AND ambient_temperature in <state> is 95F or
     higher, or the fan mentions a vulnerable person (elderly, pregnant,
     small child, infant, disability, chronic condition)
   - any mention of chest pain, breathing difficulty, unconsciousness,
     seizure, severe bleeding, or a missing unaccompanied minor -> always
     Critical regardless of how calmly it's phrased
   - a security concern involving a weapon, fight, or credible threat ->
     always Critical, intent Security
3. MATCH INTENT TO NODE TYPE:
   - Medical / Critical medical -> medical
   - Transit / Crowd -> transit_exit
   - Weather (heat/cold advisory, no acute symptoms yet) -> cooling (or
     nearest shaded/climate node)
   - Security -> security
   - LostPerson (lost child, separated from group, lost companion) ->
     family_reunification (if searching for a person) or security (if a
     child is alone and distressed — prioritize adult supervision fastest)
   - Accessibility (wheelchair access, mobility assistance, sensory need) ->
     accessibility
   - Amenity (bathroom, food, merchandise, general info) -> no <safe_routes>
     lookup needed; give brief practical guidance from <state> context only
4. HANDLE AMBIGUITY — if the query is genuinely unclear or could mean two
   different things, set needs_clarification to true and phrase
   volunteer_script as a short clarifying question for the volunteer to ask
   the fan, rather than guessing.
5. PHRASE THE SCRIPTS — brief, warm, direct, spoken register (not written
   register). Explain the "why" only when redirecting away from a fan's
   explicit request or an excluded node.

OUTPUT FORMAT:
Return ONLY a valid JSON object matching this schema. No markdown, no code fences, no extra text.
{
  "detected_language": "Full language name, e.g. 'Spanish' or 'English'",
  "translation_confidence": "High | Med | Low",
  "translated_query": "The fan's question translated into English",
  "intent": "Transit | Medical | Crowd | Amenity | Security | LostPerson | Accessibility | Weather",
  "urgency": "Low | Med | Critical",
  "alert_color": "Green | Yellow | Red",
  "needs_clarification": false,
  "needs_backup": false,
  "volunteer_script": "English — what the volunteer should understand/do.",
  "fan_facing_script": "In the fan's detected language — what the volunteer reads aloud via text-to-speech so the fan understands them."
}

CRITICAL — EXACT WORDING ONLY: "volunteer_script" and "fan_facing_script" must
contain ONLY the literal words to be spoken aloud to the fan — nothing else.
No labels like "Volunteer:" or "Script:", no stage directions, no brackets,
no meta-commentary, no explanation of your reasoning, no restating the
question. If you were to hand these two strings directly to a person with
no other context, they must be able to read them aloud verbatim and have it
sound like a real volunteer talking to a real fan. You have to reply with
exactly what the volunteer has to say — nothing more, nothing less.
"""

VALID_INTENTS = {"Transit", "Medical", "Crowd", "Amenity", "Security", "LostPerson", "Accessibility", "Weather"}
VALID_URGENCY = {"Low", "Med", "Critical"}
VALID_COLOR = {"Green", "Yellow", "Red"}
VALID_CONFIDENCE = {"High", "Med", "Low"}


def validate_llm_result(result: dict) -> bool:
    if not isinstance(result, dict):
        return False
    if result.get("intent") not in VALID_INTENTS:
        return False
    if result.get("urgency") not in VALID_URGENCY:
        return False
    if result.get("alert_color") not in VALID_COLOR:
        return False
    if not isinstance(result.get("volunteer_script"), str) or not result["volunteer_script"].strip():
        return False
    # New fields are validated loosely (present + right type) rather than
    # required, so older/cheaper models that omit them don't hard-fail.
    if "fan_facing_script" in result and not isinstance(result["fan_facing_script"], str):
        return False
    if "translation_confidence" in result and result["translation_confidence"] not in VALID_CONFIDENCE:
        return False
    if "needs_clarification" in result and not isinstance(result["needs_clarification"], bool):
        return False
    if "needs_backup" in result and not isinstance(result["needs_backup"], bool):
        return False
    return True


def fallback_result(reason: str) -> dict:
    return {
        "intent": "Amenity",
        "urgency": "Low",
        "alert_color": "Green",
        "volunteer_script": "We're having a technical issue reading live conditions. "
                             "Please flag a supervisor nearby for immediate assistance.",
        "error": True,
        "reason": reason,
    }


def free_translate(text: str, target_lang_code: str) -> Optional[str]:
    """
    Translate text using the MyMemory free API (no API key required).
    Returns translated string or None on failure.
    Supports BCP-47 codes like 'es', 'fr', 'ar', 'hi', etc.
    Rate limit: 500 requests/day for anonymous use.
    """
    if not text or not target_lang_code or target_lang_code == "en":
        return None
    try:
        params = urllib.parse.urlencode({
            "q": text[:500],
            "langpair": f"en|{target_lang_code}",
        })
        url = f"https://api.mymemory.translated.net/get?{params}"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read().decode())
        translated = data.get("responseData", {}).get("translatedText", "")
        quality = float(data.get("responseData", {}).get("match", 0))
        if translated and quality > 0:
            return translated
        return None
    except Exception as e:
        log.warning("free_translate_failed lang=%s err=%s", target_lang_code, e)
        return None


# ---------------------------------------------------------------------------
# Deterministic keyword-based intent guess. Used ONLY when the LLM call
# itself fails/times out/is misconfigured — i.e. exactly the situation where
# we can least afford to silently default every fallback to "Transit". This
# is intentionally simple and conservative: medical/security/lost-person
# keywords are checked first since those are the highest-cost misses.
# ---------------------------------------------------------------------------
FALLBACK_KEYWORDS = routing.FALLBACK_KEYWORDS


def guess_intent_from_text(query_text: str) -> str:
    return routing.guess_intent_from_text(query_text)


# ---------------------------------------------------------------------------
# Minimal in-memory rate limiter (per IP, sliding window). Swap for
# Flask-Limiter + Redis in a multi-instance deployment.
# ---------------------------------------------------------------------------
_rate_state = {}
_rate_lock = threading.Lock()


def rate_limited(ip: str) -> bool:
    limit = app.config["RATE_LIMIT_PER_MIN"]
    now = datetime.now(timezone.utc).timestamp()
    window = 60
    with _rate_lock:
        bucket = _rate_state.setdefault(ip, [])
        bucket[:] = [t for t in bucket if now - t < window]
        if len(bucket) >= limit:
            return True
        bucket.append(now)
        return False


def require_operator_auth() -> Optional[tuple]:
    """
    Check for a valid operator secret on state-mutation requests.
    The secret is expected as the 'X-Operator-Secret' header.
    If OPERATOR_SECRET is unset (empty string), mutations are allowed but
    a one-time warning is logged so it's visible in server logs.
    Returns (response, status_code) if auth fails, None if auth passes.
    """
    secret = app.config.get("OPERATOR_SECRET", "")
    if not secret:
        log.warning("OPERATOR_SECRET not set — state mutations are unauthenticated. "
                    "Set OPERATOR_SECRET in .env before production deployment.")
        return None  # allow in dev/demo mode
    provided = request.headers.get("X-Operator-Secret", "")
    import hmac
    if not hmac.compare_digest(provided, secret):
        log.warning("operator_auth_failed ip=%s", request.remote_addr)
        return jsonify({"error": "unauthorized", "message": "Invalid or missing X-Operator-Secret header."}), 401
    return None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/healthz")
def healthz():
    """Liveness/readiness probe for load balancers & container orchestrators."""
    ok = True
    detail = {"state_store": "ok", "llm_key_configured": bool(app.config["GEMINI_API_KEY"])}
    try:
        load_state()
    except Exception as e:
        ok = False
        detail["state_store"] = f"error: {e}"
    return jsonify({"status": "ok" if ok else "degraded", **detail}), (200 if ok else 503)


@app.route("/api/venue")
def get_venue():
    return jsonify({"name": STADIUM_NAME, "lat": STADIUM_LAT, "lng": STADIUM_LNG})


@app.route("/api/config/public")
def public_config():
    """Non-sensitive config the frontend needs at boot time. Never exposes secrets."""
    return jsonify({"operator_auth_required": bool(app.config.get("OPERATOR_SECRET", ""))})


@app.route("/api/weather/refresh", methods=["POST"])
def refresh_weather():
    """Pull real current temperature from Open-Meteo and merge into state."""
    auth_error = require_operator_auth()
    if auth_error:
        return auth_error
    weather = fetch_live_weather()
    if weather is None:
        return jsonify({"error": "weather_unavailable", "message": "Could not reach weather provider."}), 502
    state = load_state()
    state["ambient_temperature"] = f"{weather['temperature_f']}F"
    state["weather_source"] = weather["source"]
    state["weather_humidity_pct"] = weather.get("humidity_pct")
    save_state(state)
    return jsonify(state)


@app.route("/api/state", methods=["GET"])
def get_state():
    state = load_state()
    state["crowd_sim"] = {
        "phase": crowd_sim.simulator.current_phase(),
        "elapsed_minutes": round(crowd_sim.simulator.elapsed_minutes(), 1),
    }
    return jsonify(state)


@app.route("/api/state", methods=["POST"])
def set_state():
    """Operator simulation controls: overwrite full state or merge partial
    patches. Requires X-Operator-Secret header when OPERATOR_SECRET is set."""
    auth_error = require_operator_auth()
    if auth_error:
        return auth_error

    payload = request.get_json(silent=True)
    if payload is None:
        return jsonify({"error": "invalid_json", "message": "Body must be valid JSON."}), 400

    state = load_state()

    if "full_state" in payload:
        new_state = payload["full_state"]
        if not isinstance(new_state, dict) or "infrastructure" not in new_state:
            return jsonify({"error": "invalid_state", "message": "full_state must include infrastructure."}), 400
        state = new_state
    else:
        if "ambient_temperature" in payload:
            if not isinstance(payload["ambient_temperature"], str):
                return jsonify({"error": "invalid_field", "message": "ambient_temperature must be a string."}), 400
            state["ambient_temperature"] = payload["ambient_temperature"]

        if "infrastructure_patch" in payload:
            patch = payload["infrastructure_patch"]
            if not isinstance(patch, dict):
                return jsonify({"error": "invalid_field", "message": "infrastructure_patch must be an object."}), 400
            for gate, fields in patch.items():
                if not isinstance(fields, dict):
                    return jsonify({"error": "invalid_field", "message": f"{gate} patch must be an object."}), 400
                state["infrastructure"].setdefault(gate, {})
                state["infrastructure"][gate].update(fields)

    save_state(state)
    log.info("state_updated by=%s", request.remote_addr)
    return jsonify(state)


@app.route("/api/state/reset", methods=["POST"])
def reset_state():
    """Reset gate/incident simulation to baseline, re-pull real live weather."""
    auth_error = require_operator_auth()
    if auth_error:
        return auth_error
    seed = json.loads(json.dumps(DEFAULT_STATE))
    weather = fetch_live_weather()
    seed["ambient_temperature"] = f"{weather['temperature_f']}F" if weather else "82F"
    seed["weather_source"] = weather["source"] if weather else "fallback (weather API unreachable)"
    save_state(seed)
    return jsonify(seed)


@app.route("/api/events", methods=["GET"])
def list_events():
    return jsonify({"events": get_recent_events(limit=int(request.args.get("limit", 50)))})


@app.route("/api/events", methods=["POST"])
def post_event():
    """Kafka-style event ingestion. Requires X-Operator-Secret header when OPERATOR_SECRET is set."""
    auth_error = require_operator_auth()
    if auth_error:
        return auth_error
    payload = request.get_json(silent=True)
    if payload is None:
        return jsonify({"error": "invalid_json", "message": "Body must be valid JSON."}), 400
    try:
        result = apply_event(payload)
    except ValueError as e:
        return jsonify({"error": "invalid_event", "message": str(e)}), 400
    return jsonify(result)


@app.route("/api/routes")
def get_routes():
    """Exposes the deterministic routing engine's output directly, independent
    of any LLM call — useful for debugging/demoing that safety rules are
    enforced in code, not by prompting."""
    node_type = request.args.get("type")  # transit_exit | medical | cooling
    state = load_state()
    ranked = routing.compute_safe_routes(state.get("infrastructure", {}), node_type=node_type)
    return jsonify({"safe_routes": [c.to_dict() for c in ranked]})


@app.route("/api/escalations", methods=["GET"])
def list_escalations():
    status = request.args.get("status")
    if status and status not in VALID_ESCALATION_STATUS:
        return jsonify({"error": "invalid_status", "message": f"status must be one of {sorted(VALID_ESCALATION_STATUS)}"}), 400
    return jsonify({"escalations": get_escalations(limit=int(request.args.get("limit", 50)), status=status)})


@app.route("/api/escalations", methods=["POST"])
def post_escalation():
    """
    Volunteer explicitly confirms a supervisor should be notified for a
    specific answer. Body: {query_id, intent, urgency, query_text,
    volunteer_script, volunteer_location}. This creates a durable record —
    it does not actually send an SMS/push in this MVP, but the contract
    (create -> list -> acknowledge -> resolve) is real and ready to wire to
    a real paging system (Twilio, PagerDuty, etc).
    """
    payload = request.get_json(silent=True)
    if payload is None:
        return jsonify({"error": "invalid_json", "message": "Body must be valid JSON."}), 400
    record = create_escalation(payload)
    return jsonify(record), 201


@app.route("/api/escalations/<escalation_id>", methods=["PATCH"])
def patch_escalation(escalation_id):
    payload = request.get_json(silent=True)
    if payload is None or "status" not in payload:
        return jsonify({"error": "invalid_json", "message": "Body must include 'status'."}), 400
    try:
        record = update_escalation_status(escalation_id, payload["status"])
    except ValueError as e:
        return jsonify({"error": "invalid_status", "message": str(e)}), 400
    if record is None:
        return jsonify({"error": "not_found", "message": f"No escalation with id {escalation_id}"}), 404
    return jsonify(record)


@app.route("/api/shift-log", methods=["GET"])
def get_shift_log_route():
    return jsonify({"entries": get_shift_log(limit=int(request.args.get("limit", 100)))})


@app.route("/api/shift-log/export", methods=["GET"])
def export_shift_log():
    csv_text = shift_log_to_csv()
    filename = f"margadarshi_shift_log_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv"
    return Response(
        csv_text,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/api/query", methods=["POST"])
def process_query():
    ip = request.remote_addr or "unknown"
    if rate_limited(ip):
        return jsonify({"error": "rate_limited", "message": "Too many requests, slow down."}), 429

    payload = request.get_json(silent=True)
    if payload is None or "query" not in payload:
        return jsonify({"error": "invalid_json", "message": "Body must include a 'query' string."}), 400

    query_text = str(payload["query"]).strip()
    if not query_text:
        return jsonify({"error": "empty_query", "message": "Query cannot be empty."}), 400
    if len(query_text) > app.config["MAX_QUERY_LEN"]:
        query_text = query_text[: app.config["MAX_QUERY_LEN"]]

    # Optional BCP-47 hint from the volunteer's language picker (e.g. "es-ES").
    # Purely advisory — the LLM still detects the language itself from the
    # transcript, since the volunteer's picker can be wrong or left on default.
    source_language_hint = str(payload.get("source_language_hint", "")).strip()[:20]

    state = load_state()
    infra = state.get("infrastructure", {})
    query_id = str(uuid.uuid4())[:8]

    # Step 1 — compute the safety-vetted candidate set in code, across every
    # node type the engine knows about. This is authoritative: closed nodes
    # and >85%-capacity nodes are excluded here, not left to the LLM to notice.
    node_types = ["transit_exit", "medical", "cooling", "security", "lost_and_found", "family_reunification", "accessibility"]
    safe_routes_payload = {
        t: [c.to_dict() for c in routing.compute_safe_routes(infra, node_type=t)]
        for t in node_types
    }

    prompt = (
        f"{SYSTEM_PROMPT}\n\n"
        f"<state>\n{json.dumps(state, indent=2)}\n</state>\n"
        f"<safe_routes>\n{json.dumps(safe_routes_payload, indent=2)}\n</safe_routes>\n"
        f"<query>\n\"{query_text}\"\n</query>\n"
        f"<source_language_hint>{source_language_hint}</source_language_hint>"
    )

    def respond_with_fallback(reason: str, intent_guess: Optional[str] = None):
        intent_guess = intent_guess or guess_intent_from_text(query_text)
        # Detect language from the actual query text for better fan-facing output
        detected_lang, lang_code = routing.detect_language(query_text)
        # Override with volunteer's language hint if detection came back English
        if detected_lang == "English" and source_language_hint and source_language_hint not in ("en", "en-US", "en-GB"):
            # Strip region subtag for translation API
            lang_code = source_language_hint.split("-")[0].lower()
            detected_lang = source_language_hint

        # Build a rich, context-aware script using live routing data
        smart = routing.smart_fallback_response(
            intent=intent_guess,
            live_infrastructure=infra,
            query_text=query_text,
            detected_language=detected_lang,
            language_code=lang_code,
        )

        # Attempt free translation for non-English fans
        if lang_code != "en":
            translated = free_translate(smart["volunteer_script"], lang_code)
            if translated:
                smart["fan_facing_script"] = translated
                smart["translation_confidence"] = "Med"

        smart["query_id"] = query_id
        smart["safe_routes"] = safe_routes_payload
        smart["volunteer_location"] = state.get("volunteer_location", "")
        smart["error"] = False
        smart["reason"] = reason
        record_shift_entry({**smart, "query_text": query_text, "source": "smart_fallback"})
        return jsonify(smart), 200

    import time as _time
    from google.genai import errors as _genai_errors

    _MAX_RETRIES = 4
    _BASE_DELAY  = 5  # seconds

    last_exc: Optional[Exception] = None
    for attempt in range(_MAX_RETRIES):
        try:
            response = get_client().models.generate_content(
                model=app.config["MODEL_NAME"],
                contents=prompt,
                config=types.GenerateContentConfig(response_mime_type="application/json"),
            )
            raw = (response.text or "").strip()
            result = json.loads(raw)

            if not validate_llm_result(result):
                log.warning("llm_schema_invalid raw=%s", raw[:300])
                guessed = result.get("intent") if isinstance(result, dict) and result.get("intent") in VALID_INTENTS else None
                return respond_with_fallback("schema_validation_failed", guessed)

            # Step 2 — independent guardrail: does the generated script name a
            # node that the routing engine excluded? If so, don't trust the
            # LLM's phrasing — fall back to a template built directly from
            # safe_routes.
            violation = routing.validate_script_against_routes(result["volunteer_script"], infra)
            if violation:
                log.warning("route_guardrail_triggered reason=%s query=%r", violation, query_text)
                fallback_script = routing.deterministic_fallback_script(result["intent"], infra)
                result["volunteer_script"] = fallback_script
                result["fan_facing_script"] = fallback_script
                result["guardrail_override"] = True

            result.pop("error", None)
            result["query_id"] = query_id
            result["safe_routes"] = safe_routes_payload  # transparency: show what the engine actually allowed
            result["volunteer_location"] = state.get("volunteer_location", "")
            record_shift_entry({**result, "query_text": query_text, "source": "llm"})
            return jsonify(result)

        except json.JSONDecodeError as e:
            log.error("llm_json_decode_error err=%s", e)
            return respond_with_fallback("json_decode_error")

        except (_genai_errors.ClientError, _genai_errors.ServerError) as e:
            status = getattr(e, "status_code", 0)
            # 429 RESOURCE_EXHAUSTED or 503 UNAVAILABLE — back off and retry
            if status in (429, 503):
                delay = _BASE_DELAY * (2 ** attempt)
                log.warning("llm_transient_error status=%d attempt=%d/%d retry_in=%.1fs", status, attempt + 1, _MAX_RETRIES, delay)
                last_exc = e
                _time.sleep(delay)
                continue
            # Any other client/server error is not retryable
            log.exception("llm_call_failed")
            return respond_with_fallback(str(e))

        except Exception as e:
            log.exception("llm_call_failed")
            return respond_with_fallback(str(e))

    # All retries exhausted due to rate limiting — use deterministic fallback
    log.error("llm_rate_limit_exhausted after %d attempts: %s", _MAX_RETRIES, last_exc)
    return respond_with_fallback("rate_limit_exhausted")


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------
@app.errorhandler(404)
def not_found(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "not_found"}), 404
    return render_template("index.html"), 200  # SPA-style fallback


@app.errorhandler(HTTPException)
def handle_http_exception(e):
    return jsonify({"error": e.name.lower().replace(" ", "_"), "message": e.description}), e.code


@app.errorhandler(Exception)
def handle_unexpected(e):
    log.exception("unhandled_exception")
    return jsonify({"error": "internal_error", "message": "Something went wrong."}), 500


# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------
@app.after_request
def set_security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


if __name__ == "__main__":
    load_state()
    crowd_sim.simulator.start(apply_event)
    app.run(debug=app.config["DEBUG"], host="0.0.0.0", port=app.config["PORT"])
