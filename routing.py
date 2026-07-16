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
        reason = "eligible"

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
        under_preferred = 0 if (c.capacity_pct is not None and c.capacity_pct < CAPACITY_PREFERRED) else 1
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
    ("Security",      ["weapon", "gun", "knife", "fight", "fighting", "threat", "attack",
                       "assault", "stealing", "theft", "harassing", "aggressive"]),
    ("LostPerson",    ["lost my", "lost her", "lost him", "my child", "my daughter", "my son",
                       "missing child", "can't find", "cannot find", "separated from",
                       "her name is", "his name is", "lost child"]),
    # Weather before Medical so "too hot + faint" → Weather (cooling), not Medical (first aid)
    ("Weather",       ["too hot", "heat stroke", "overheating", "sunstroke", "dehydrated", "too cold"]),
    ("Medical",       ["chest pain", "can't breathe", "cannot breathe", "not breathing",
                       "unconscious", "seizure", "bleeding", "heart attack",
                       "allergic", "sick", "ambulance", "medic", "collapsed",
                       "dizzy", "faint", "hurt", "pain"]),
    ("Accessibility", ["wheelchair", "disability", "disabled", "mobility", "blind", "deaf",
                       "hearing impaired", "visually impaired"]),
    ("Transit",       ["gate", "exit", "train", "shuttle", "metro", "station", "leave", "way out"]),
    ("Crowd",         ["crowd", "crowded", "packed", "stuck", "not moving", "blocked", "trapped"]),
]


def guess_intent_from_text(query_text: str) -> str:
    lowered = query_text.lower()
    for intent, keywords in FALLBACK_KEYWORDS:
        if any(kw in lowered for kw in keywords):
            return intent
    return "Amenity"


def deterministic_fallback_script(intent: str, live_infrastructure: dict) -> str:
    """
    Template-based script used only when the LLM's output fails validation.
    Guarantees the fan is never sent toward a closed/overloaded node.
    """
    node_type = INTENT_TO_NODE_TYPE.get(intent, "transit_exit")
    route = best_route(live_infrastructure, node_type)
    if route is None:
        return ("All nearby routes are currently at capacity or closed. "
                "Please escort the fan to the nearest staffed command post for a manual reroute.")
    return (f"Please head to {route.label}, about {route.distance_m} meters away. "
            f"It is currently open with {route.capacity_pct if route.capacity_pct is not None else 'unknown'}% capacity.")
