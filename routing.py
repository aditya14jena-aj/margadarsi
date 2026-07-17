"""
Deterministic routing engine.

This module is the source of truth for "which node is it safe to send a fan
to." It is intentionally NOT the LLM's job to invent routes — an LLM can
hallucinate a route through a closed gate under time pressure. Instead:

  1. STATIC_MAP defines the fixed graph (which corridor connects to which
     node, and the distance) — this basically never changes mid-event.
  2. Live status/capacity/incident fields on each node are mutated by the
     event stream (see /api/events) or by the simulation panel.
  3. compute_safe_routes() applies the safety rules below in code, not in a
     prompt, and returns a ranked, pre-vetted candidate list.
  4. The LLM is only allowed to phrase what's already been decided — see
     build_prompt() in app.py, which passes safe_routes as a closed set.
  5. validate_script_against_routes() is a second, independent guardrail:
     if the LLM's generated script still names a node code that
     is excluded, callers should not trust that field.

Safety rules (mirrors real-world routing logic):
  - A node with status CLOSED or an active incident is never eligible,
    regardless of capacity.
  - If the nearest eligible node's capacity_load > 85%, prefer the next
    closest node with capacity_load < 50% over sending more people into
    an already-overloaded gate.
  - Ties broken by distance.
"""

import random
from dataclasses import dataclass, field
from typing import Optional

CAPACITY_HARD_LIMIT = 85   # never route into a node at/above this load
CAPACITY_PREFERRED = 50    # prefer rerouting to a node below this load


# ---------------------------------------------------------------------------
# Static infrastructure map — fixed topology (edges + distances).
# Mirrors: "Node_Gate_A connected to Corridor_3, Distance: 100m"
# ---------------------------------------------------------------------------
STATIC_MAP = {
    "Gate_A_Main_Metro": {
        "type": "transit_exit",
        "connected_corridor": "Corridor_3",
        "distance_m": 100,
        "label": "Gate A — NJ Transit Rail Link",
    },
    "Gate_B_West_Shuttle": {
        "type": "transit_exit",
        "connected_corridor": "Corridor_4",
        "distance_m": 180,
        "label": "Gate B — West Shuttle Lot",
    },
    "First_Aid_Station_Zone_3": {
        "type": "medical",
        "connected_corridor": "Corridor_3",
        "distance_m": 45,
        "label": "First Aid — Sector B Concourse",
    },
    "Cooling_Center_Sector_B": {
        "type": "cooling",
        "connected_corridor": "Corridor_3",
        "distance_m": 15,
        "label": "Cooling Center — Sector B",
    },
    "Security_Post_Sector_B": {
        "type": "security",
        "connected_corridor": "Corridor_3",
        "distance_m": 60,
        "label": "Security Post — Sector B",
    },
    "Lost_And_Found_Concourse": {
        "type": "lost_and_found",
        "connected_corridor": "Corridor_3",
        "distance_m": 90,
        "label": "Lost & Found — Main Concourse",
    },
    "Family_Reunification_Point": {
        "type": "family_reunification",
        "connected_corridor": "Corridor_3",
        "distance_m": 70,
        "label": "Family Reunification Point — Gate C Plaza",
    },
    "Accessibility_Assist_Desk": {
        "type": "accessibility",
        "connected_corridor": "Corridor_3",
        "distance_m": 50,
        "label": "Accessibility Assist Desk — Sector B",
    },
}


@dataclass
class RouteCandidate:
    node_id: str
    label: str
    node_type: str
    distance_m: int
    capacity_pct: Optional[int]
    status: str
    incident: str
    eligible: bool
    reason: str

    def to_dict(self):
        return {
            "node_id": self.node_id,
            "label": self.label,
            "type": self.node_type,
            "distance_m": self.distance_m,
            "capacity_pct": self.capacity_pct,
            "status": self.status,
            "incident": self.incident,
            "eligible": self.eligible,
            "reason": self.reason,
        }


def _capacity_pct(node_state: dict) -> Optional[int]:
    raw = node_state.get("capacity_load")
    if raw is None:
        return None
    try:
        return int(str(raw).replace("%", "").strip())
    except ValueError:
        return None


def _is_closed(node_state: dict) -> bool:
    status = str(node_state.get("status", "")).lower()
    incident = str(node_state.get("incident", "none")).lower()
    return status in ("closed", "restricted", "blocked") or incident != "none"


def evaluate_node(node_id: str, live_state: dict) -> RouteCandidate:
    static = STATIC_MAP.get(node_id, {})
    live = live_state.get(node_id, {})
    capacity = _capacity_pct(live)
    status = live.get("status", "unknown")
    incident = live.get("incident", "none")
    closed = _is_closed(live)

    if closed:
        reason = f"excluded: status={status}" + (f", incident={incident}" if incident != "none" else "")
        eligible = False
    elif capacity is not None and capacity >= CAPACITY_HARD_LIMIT:
        reason = f"excluded: capacity {capacity}% exceeds {CAPACITY_HARD_LIMIT}% safety threshold"
        eligible = False
    else:
        eligible = True
        if capacity is None:
            reason = "eligible (capacity unknown — treated as moderate)"
        else:
            reason = "eligible"

    # For routing score: treat None as 50 (moderate, not preferred, not excluded)
    effective_capacity = capacity if capacity is not None else 50

    return RouteCandidate(
        node_id=node_id,
        label=static.get("label", node_id),
        node_type=static.get("type", "unknown"),
        distance_m=static.get("distance_m", 9999),
        capacity_pct=capacity,
        status=status,
        incident=incident,
        eligible=eligible,
        reason=reason,
    )


def compute_safe_routes(live_infrastructure: dict, node_type: Optional[str] = None) -> list:
    """
    Returns every node evaluated against the safety rules, ranked so that:
      1. Eligible nodes come before excluded ones.
      2. Among eligible nodes, ones under CAPACITY_PREFERRED are preferred.
      3. Ties broken by distance (closest first).
    If node_type is given (e.g. "transit_exit", "medical", "cooling"),
    only nodes of that type are returned.
    """
    candidates = [
        evaluate_node(node_id, live_infrastructure)
        for node_id in STATIC_MAP
        if node_type is None or STATIC_MAP[node_id]["type"] == node_type
    ]

    def sort_key(c: RouteCandidate):
        # None capacity treated as 50 (moderate) — not in preferred band, not excluded
        effective = c.capacity_pct if c.capacity_pct is not None else 50
        under_preferred = 0 if effective < CAPACITY_PREFERRED else 1
        return (not c.eligible, under_preferred, c.distance_m)

    candidates.sort(key=sort_key)
    return candidates


def best_route(live_infrastructure: dict, node_type: str) -> Optional[RouteCandidate]:
    ranked = compute_safe_routes(live_infrastructure, node_type=node_type)
    eligible = [c for c in ranked if c.eligible]
    return eligible[0] if eligible else None


def validate_script_against_routes(script: str, live_infrastructure: dict) -> Optional[str]:
    """
    Second-pass guardrail: flags a script that sends someone toward an
    excluded node. A script is allowed to *mention* a closed node while
    explaining the closure (e.g. "Gate A is closed, use Gate B instead") —
    that's the correct behavior. It's only a violation if the excluded node
    is named and no eligible alternative of the same type is also named,
    which suggests the fan is being routed there rather than away from it.
    Returns a warning string, or None if the script is clean.
    """
    if not script:
        return None
    lowered = script.lower()

    excluded_mentioned = None
    for node_id, static in STATIC_MAP.items():
        live = live_infrastructure.get(node_id, {})
        if _is_closed(live):
            label_key = static["label"].lower().split(" — ")[0]  # e.g. "gate a"
            if label_key in lowered:
                excluded_mentioned = (node_id, static)
                break

    if excluded_mentioned is None:
        return None

    node_id, static = excluded_mentioned
    node_type = static["type"]

    # does the script also mention an eligible node of the same type?
    ranked = compute_safe_routes(live_infrastructure, node_type=node_type)
    eligible_labels = [c.label.lower().split(" — ")[0] for c in ranked if c.eligible]
    mentions_alternative = any(label in lowered for label in eligible_labels)

    if mentions_alternative:
        return None  # explaining the closure while redirecting — fine

    return f"script references excluded node {node_id} ({static['label']}) without naming an eligible alternative"


INTENT_TO_NODE_TYPE = {
    "Medical": "medical",
    "Transit": "transit_exit",
    "Crowd": "transit_exit",
    "Security": "security",
    "LostPerson": "family_reunification",
    "Accessibility": "accessibility",
    "Weather": "cooling",
}

# ---------------------------------------------------------------------------
# Fallback intent classifier — pure keyword matching, no LLM dependency.
# Used when the Gemini call fails/times out so the safety fallback is always
# correctly typed regardless of LLM availability.
# Priority order matters: Medical/Security/LostPerson checked first because
# those are the highest-cost classification errors.
# ---------------------------------------------------------------------------
FALLBACK_KEYWORDS = [
    # ---- English ----
    ("Security",      ["weapon", "gun", "knife", "fight", "fighting", "threat", "attack",
                       "assault", "stealing", "theft", "harassing", "aggressive",
                       # Spanish
                       "arma", "cuchillo", "pistola", "pelea", "amenaza", "ataque", "robo",
                       # Arabic
                       "سلاح", "مشادة", "تهديد", "هجوم",
                       # French
                       "arme", "couteau", "bagarre", "menace", "attaque",
                       # Portuguese
                       "arma", "faca", "briga", "ameaça",
                       # German
                       "waffe", "messer", "schlägerei", "bedrohung"]),
    ("LostPerson",    ["lost my", "lost her", "lost him", "my child", "my daughter", "my son",
                       "missing child", "can't find", "cannot find", "separated from",
                       "her name is", "his name is", "lost child",
                       # Spanish
                       "perdido", "perdida", "hijo", "hija", "niño", "niña", "busco", "buscar",
                       "separado", "separada", "no encuentro", "me perdí",
                       # Arabic
                       "ضاع", "طفل", "ابن", "ابنة", "مفقود",
                       # French
                       "perdu", "perdue", "enfant", "fils", "fille", "cherche",
                       # Portuguese
                       "perdido", "criança", "filho", "filha", "procuro",
                       # German
                       "verloren", "kind", "suche", "vermisst"]),
    # Weather before Medical
    ("Weather",       ["too hot", "heat stroke", "overheating", "sunstroke", "dehydrated", "too cold",
                       # Spanish
                       "calor", "fiebre", "deshidratado", "hace mucho calor", "frío",
                       # Arabic
                       "حر", "حرارة", "ضربة شمس",
                       # French
                       "chaud", "coup de chaleur", "déshydraté",
                       # Portuguese
                       "calor", "insolação", "desidratado",
                       # German
                       "heiß", "sonnenstich", "überhitzt"]),
    ("Medical",       ["chest pain", "can't breathe", "cannot breathe", "not breathing",
                       "unconscious", "seizure", "bleeding", "heart attack",
                       "allergic", "sick", "ambulance", "medic", "collapsed",
                       "dizzy", "faint", "hurt", "pain",
                       # Spanish
                       "dolor", "sangre", "médico", "ambulancia", "desmayo", "respirar",
                       "inconsciente", "corazón", "alergia", "enfermo",
                       # Arabic
                       "ألم", "إسعاف", "طبيب", "نزيف", "إغماء",
                       # French
                       "douleur", "ambulance", "médecin", "saigne", "évanoui",
                       # Portuguese
                       "dor", "ambulância", "médico", "sangue", "desmaiou",
                       # German
                       "schmerz", "krankenwagen", "arzt", "blutung", "ohnmacht"]),
    ("Accessibility", ["wheelchair", "disability", "disabled", "mobility", "blind", "deaf",
                       "hearing impaired", "visually impaired",
                       # Spanish
                       "silla de ruedas", "discapacidad", "movilidad",
                       # French
                       "fauteuil roulant", "handicap", "mobilité",
                       # Arabic
                       "كرسي متحرك", "إعاقة",
                       # Portuguese
                       "cadeira de rodas", "deficiência",
                       # German
                       "rollstuhl", "behinderung"]),
    ("Transit",       ["gate", "exit", "train", "shuttle", "metro", "station", "leave", "way out",
                       # Spanish
                       "salida", "puerta", "tren", "estación", "irme", "salir",
                       # Arabic
                       "مخرج", "بوابة", "محطة", "قطار",
                       # French
                       "sortie", "porte", "gare", "train", "partir",
                       # Portuguese
                       "saída", "portão", "trem", "metrô", "sair",
                       # German
                       "ausgang", "tor", "bahnhof", "zug", "verlassen",
                       # Hindi
                       "निकास", "गेट", "ट्रेन",
                       # Japanese
                       "出口", "ゲート", "電車",
                       # Korean
                       "출구", "게이트",
                       # Chinese
                       "出口", "大门", "地铁"]),
    ("Crowd",         ["crowd", "crowded", "packed", "stuck", "not moving", "blocked", "trapped",
                       # Spanish
                       "multitud", "atascado", "bloqueado", "aglomeración",
                       # French
                       "foule", "coincé", "bloqué",
                       # Arabic
                       "زحام", "مكتظ",
                       # Portuguese
                       "multidão", "preso", "bloqueado",
                       # German
                       "gedränge", "stau", "blockiert"]),
]


def guess_intent_from_text(query_text: str) -> str:
    lowered = query_text.lower()
    for intent, keywords in FALLBACK_KEYWORDS:
        if any(kw in lowered for kw in keywords):
            return intent
    return "Amenity"


# ---------------------------------------------------------------------------
# Language detection — lightweight heuristic for common FIFA World Cup
# attendee languages. Used to tag detected_language in fallback mode.
# ---------------------------------------------------------------------------
LANGUAGE_SIGNATURES = [
    # (language_name, bcp47_code, unique_trigrams_or_words)
    ("Spanish",    "es", ["¿", "¡", " el ", " la ", " es ", " no ", "por favor", "gracias", "dónde", "donde", "ayuda", "qué", "que "]),
    ("Arabic",     "ar", ["ال", "في", "من", "إلى", "على", "مساعدة", "أين", "طريق"]),
    ("French",     "fr", ["le ", "la ", "les ", "un ", "une ", "des ", "est ", "que ", "s'il vous", "merci", "où", "aide"]),
    ("Portuguese", "pt", ["por favor", "obrigado", "obrigada", "onde", "ajuda", "está", "para", "com "]),
    ("German",     "de", ["der ", "die ", "das ", "ich ", "sie ", "bitte", "hilfe", "wo ist", "können"]),
    ("Italian",    "it", ["il ", "la ", "per favore", "grazie", "dove", "aiuto", "che ", "una "]),
    ("Japanese",   "ja", ["の", "は", "が", "を", "に", "で", "ください", "どこ"]),
    ("Korean",     "ko", ["이", "가", "를", "에서", "도움", "어디", "주세요"]),
    ("Chinese",    "zh", ["的", "是", "在", "了", "我", "你", "他", "她", "这", "那", "哪里", "帮助"]),
    ("Hindi",      "hi", ["है", "में", "का", "को", "के", "और", "यह", "मदद", "कहाँ"]),
    ("Russian",    "ru", ["пожалуйста", "помогите", "где", "это", "как ", "я ", "не "]),
]


def detect_language(query_text: str) -> tuple:
    """Returns (language_name, bcp47_code). Defaults to ('English', 'en')."""
    lowered = query_text.lower()
    scores = {}
    for lang, code, markers in LANGUAGE_SIGNATURES:
        score = sum(1 for m in markers if m in lowered or m in query_text)
        if score > 0:
            scores[lang] = (score, code)
    if not scores:
        return ("English", "en")
    best = max(scores, key=lambda k: scores[k][0])
    return (best, scores[best][1])


# ---------------------------------------------------------------------------
# Smart intent-specific script templates — varied, context-aware, realistic
# volunteer language. Used when LLM is unavailable (API quota exhausted).
# ---------------------------------------------------------------------------

_TRANSIT_TEMPLATES = [
    "Head to {label} — that's about {dist} meters from here, down {corridor}. "
    "It's currently open with {cap}% crowd load and roughly {wait} minutes wait. "
    "Follow the green signs and I'll radio ahead.",
    "The best exit right now is {label}. Walk about {dist} meters along {corridor} — "
    "you can't miss the green overhead signs. Currently {cap}% busy, wait's around {wait} min.",
    "Take {corridor} toward {label}. It's {dist} meters out and sitting at {cap}% capacity. "
    "Should be about {wait} minutes to board. I'll alert the gate team.",
    "Your fastest way out is {label}, about {dist}m down {corridor}. "
    "It's at {cap}% right now so flow should be smooth. Estimated wait: {wait} minutes.",
]

_CROWD_TEMPLATES = [
    "This area is congested — best path out is {label} via {corridor}, about {dist} meters. "
    "At {cap}% capacity it's your clearest option. Follow the overhead signs and stay to the right.",
    "I know it feels crowded here. Head to {label} — {dist} meters via {corridor}. "
    "It's at {cap}% load, less congested than other exits. Wait's about {wait} min.",
    "For the crowd, I'm routing you to {label} through {corridor}. "
    "That's {dist}m and currently {cap}% busy — the least congested transit option available.",
]

_MEDICAL_TEMPLATES = [
    "MEDICAL: Take them to {label} right now — {dist} meters, straight along {corridor}. "
    "I'm radioing the medics. Keep them calm and walking slowly if possible.",
    "Get to {label} immediately — that's {dist} meters via {corridor}. "
    "Medical staff are on standby. I'm alerting them now. Walk steadily, don't run.",
    "We need medical assistance at {label}. It's {dist} meters along {corridor}. "
    "Medics are there now. I'm calling this in as priority. Move carefully.",
]

_SECURITY_TEMPLATES = [
    "Please come with me to {label} — {dist} meters along {corridor}. "
    "Security officers are stationed there and I'm radioing them right now.",
    "Head to {label} immediately — security post, {dist} meters via {corridor}. "
    "I've already alerted the officers. This is being escalated.",
    "Security: proceed to {label}, {dist}m along {corridor}. "
    "I'm flagging this to the supervisor on duty.",
]

_WEATHER_TEMPLATES = [
    "It's hot out here — the {label} is just {dist} meters away via {corridor}. "
    "Air conditioning and cold water are available. Take it slow.",
    "Head to {label} to cool down — {dist} meters along {corridor}. "
    "They have water, shade, and climate control. Currently at {cap}% so there's room.",
    "You can cool off at {label}, only {dist} meters via {corridor}. "
    "Stay hydrated, walk slowly. I'll meet you there if needed.",
]

_LOST_PERSON_TEMPLATES = [
    "Go to {label} — our family reunification point, {dist} meters along {corridor}. "
    "Staff there have the lost-person register and PA system access. We'll find them.",
    "Head to {label} right away — {dist} meters via {corridor}. "
    "Reunification staff are already looking for separated guests. Describe the person to them.",
    "The {label} handles all separated-person cases. It's {dist}m via {corridor}. "
    "They have a PA system and coordination with all sector posts.",
]

_ACCESSIBILITY_TEMPLATES = [
    "The {label} is {dist} meters along {corridor} — they have full mobility support, "
    "wheelchair assist, and dedicated staff ready to help.",
    "Head to {label} for accessibility support — {dist}m via {corridor}. "
    "Wheelchair access, hearing loop, and visual aids are all available there.",
    "I'll walk you to {label} — {dist} meters along {corridor}. "
    "Accessibility team is fully staffed and ready.",
]

_AMENITY_TEMPLATES = [
    "Happy to help! Restrooms are along Corridor 3 near Sector B. "
    "Food concessions are on the east concourse. Merchandise is at the main atrium.",
    "Nearest restrooms: 30 meters east on Corridor 3. Concessions: 50 meters north. "
    "Guest services can assist with anything else at the main information desk.",
    "You'll find restrooms, food, and merch along the main concourse. "
    "Follow the blue overhead signs. Anything specific I can help narrow down?",
]

_ALL_CLOSED_TEMPLATES = {
    "Transit":       "All exits in this sector are currently at capacity or restricted. "
                     "Please follow staff to the overflow routing point at the east concourse.",
    "Medical":       "URGENT: All medical posts are busy. Call 911 now and keep the person still. "
                     "I'm flagging this as a critical escalation to the sector supervisor.",
    "Security":      "Radio priority: all posts occupied. Supervisor is being paged immediately. "
                     "Stay calm, maintain visual contact with the situation.",
    "Weather":       "Cooling centers at capacity. Seek shade here along the concourse wall. "
                     "I'm requesting a mobile water distribution team.",
    "LostPerson":    "Reunification point is staffed — proceed along Corridor 3 and look for "
                     "the yellow FAMILY REUNIFICATION banner. Staff will assist.",
    "Accessibility": "Accessibility desk is busy. I'll escort you directly — please stay with me.",
    "Crowd":         "Area is heavily congested. Hold position, follow my lead. "
                     "Do not push forward. I'm coordinating with crowd management now.",
    "Amenity":       "Guest services desk on the east concourse can help — about 80 meters east.",
}

_INTENT_TEMPLATES = {
    "Transit":       _TRANSIT_TEMPLATES,
    "Crowd":         _CROWD_TEMPLATES,
    "Medical":       _MEDICAL_TEMPLATES,
    "Security":      _SECURITY_TEMPLATES,
    "Weather":       _WEATHER_TEMPLATES,
    "LostPerson":    _LOST_PERSON_TEMPLATES,
    "Accessibility": _ACCESSIBILITY_TEMPLATES,
    "Amenity":       _AMENITY_TEMPLATES,
}

_URGENCY_MAP = {
    "Medical": ("Critical", "Red"),
    "Security": ("Critical", "Red"),
    "Crowd": ("Med", "Yellow"),
    "Weather": ("Med", "Yellow"),
    "LostPerson": ("Critical", "Red"),
    "Transit": ("Low", "Green"),
    "Accessibility": ("Med", "Yellow"),
    "Amenity": ("Low", "Green"),
}


def deterministic_fallback_script(intent: str, live_infrastructure: dict) -> str:
    """
    Template-based script used only when the LLM's output fails validation.
    Guarantees the fan is never sent toward a closed/overloaded node.
    Simple string version — for full dict use smart_fallback_response().
    """
    result = smart_fallback_response(intent, live_infrastructure, query_text="")
    return result["volunteer_script"]


def smart_fallback_response(
    intent: str,
    live_infrastructure: dict,
    query_text: str = "",
    detected_language: str = "English",
    language_code: str = "en",
) -> dict:
    """
    Full smart fallback that produces a complete, context-aware response dict
    (matching the LLM output schema) without any LLM call.

    Selects a template, fills in live crowd/routing data, randomises
    phrasing variation, and fills all required schema fields.
    """
    node_type = INTENT_TO_NODE_TYPE.get(intent, "transit_exit")
    route = best_route(live_infrastructure, node_type)
    urgency, alert_color = _URGENCY_MAP.get(intent, ("Low", "Green"))

    templates = _INTENT_TEMPLATES.get(intent, _AMENITY_TEMPLATES)

    if route is None:
        volunteer_script = _ALL_CLOSED_TEMPLATES.get(
            intent,
            "All nearby routes are at capacity or closed. "
            "Please escort the fan to the nearest staffed command post for a manual reroute."
        )
    else:
        cap = route.capacity_pct if route.capacity_pct is not None else "moderate"
        wait_raw = live_infrastructure.get(route.node_id, {}).get("avg_wait_minutes", 3)
        try:
            wait = int(wait_raw)
        except (TypeError, ValueError):
            wait = 3

        # Determine corridor name from STATIC_MAP
        corridor = STATIC_MAP.get(route.node_id, {}).get("connected_corridor", "the main corridor")

        template = random.choice(templates)
        try:
            volunteer_script = template.format(
                label=route.label,
                dist=route.distance_m,
                cap=cap,
                wait=wait,
                corridor=corridor,
            )
        except KeyError:
            # Some templates (e.g. Amenity) don't use route fields — that's fine
            volunteer_script = template

    # For fan_facing_script: we default to English (caller will translate async)
    fan_facing_script = volunteer_script

    needs_backup = intent in ("Medical", "Security", "LostPerson")

    return {
        "detected_language": detected_language,
        "translation_confidence": "Low",
        "translated_query": query_text,
        "intent": intent,
        "urgency": urgency,
        "alert_color": alert_color,
        "needs_clarification": False,
        "needs_backup": needs_backup,
        "volunteer_script": volunteer_script,
        "fan_facing_script": fan_facing_script,
        "guardrail_override": False,
        "_source": "smart_fallback",
    }
