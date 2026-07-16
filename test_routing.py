"""
test_routing.py — Safety-critical unit tests for routing.py

Run:  pytest test_routing.py -v
These tests must pass before any deployment. They verify the core safety
invariants: no fan is ever routed to a closed, incident-flagged, or
over-capacity node regardless of query type or node ordering.
"""
import pytest
from routing import (
    compute_safe_routes,
    best_route,
    evaluate_node,
    validate_script_against_routes,
    deterministic_fallback_script,
    guess_intent_from_text,
    STATIC_MAP,
    CAPACITY_HARD_LIMIT,
    CAPACITY_PREFERRED,
    INTENT_TO_NODE_TYPE,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def baseline_infra():
    """All nodes open, low capacity, no incidents."""
    return {
        "Gate_A_Main_Metro":        {"status": "open",         "capacity_load": "30%", "avg_wait_minutes": 4,  "incident": "none"},
        "Gate_B_West_Shuttle":      {"status": "open",         "capacity_load": "22%", "avg_wait_minutes": 3,  "incident": "none"},
        "First_Aid_Station_Zone_3": {"status": "operational",  "distance_meters": 45},
        "Cooling_Center_Sector_B":  {"status": "operational",  "distance_meters": 15},
        "Security_Post_Sector_B":   {"status": "operational",  "distance_meters": 60},
        "Lost_And_Found_Concourse": {"status": "operational",  "distance_meters": 90},
        "Family_Reunification_Point":{"status": "operational", "distance_meters": 70},
        "Accessibility_Assist_Desk":{"status": "operational",  "distance_meters": 50},
    }


def gate_a_closed():
    infra = baseline_infra()
    infra["Gate_A_Main_Metro"]["status"] = "CLOSED"
    return infra


def gate_a_incident():
    infra = baseline_infra()
    infra["Gate_A_Main_Metro"]["incident"] = "crowd_crush_risk"
    return infra


def gate_a_over_capacity():
    infra = baseline_infra()
    infra["Gate_A_Main_Metro"]["capacity_load"] = f"{CAPACITY_HARD_LIMIT + 1}%"
    return infra


def all_transit_blocked():
    infra = baseline_infra()
    infra["Gate_A_Main_Metro"]["status"] = "CLOSED"
    infra["Gate_B_West_Shuttle"]["status"] = "CLOSED"
    return infra


# ── Core safety invariants ────────────────────────────────────────────────────

class TestCoreRoutingSafety:

    def test_closed_node_is_never_eligible(self):
        candidate = evaluate_node("Gate_A_Main_Metro", gate_a_closed())
        assert not candidate.eligible, "Closed node must never be eligible"

    def test_incident_node_is_never_eligible(self):
        candidate = evaluate_node("Gate_A_Main_Metro", gate_a_incident())
        assert not candidate.eligible, "Node with active incident must never be eligible"

    def test_over_capacity_node_is_never_eligible(self):
        candidate = evaluate_node("Gate_A_Main_Metro", gate_a_over_capacity())
        assert not candidate.eligible, f"Node at >{CAPACITY_HARD_LIMIT}% must never be eligible"

    def test_exactly_at_limit_is_never_eligible(self):
        infra = baseline_infra()
        infra["Gate_A_Main_Metro"]["capacity_load"] = f"{CAPACITY_HARD_LIMIT}%"
        candidate = evaluate_node("Gate_A_Main_Metro", infra)
        assert not candidate.eligible, f"Node at exactly {CAPACITY_HARD_LIMIT}% must be excluded"

    def test_one_below_limit_is_eligible(self):
        infra = baseline_infra()
        infra["Gate_A_Main_Metro"]["capacity_load"] = f"{CAPACITY_HARD_LIMIT - 1}%"
        candidate = evaluate_node("Gate_A_Main_Metro", infra)
        assert candidate.eligible

    def test_open_node_is_eligible(self):
        candidate = evaluate_node("Gate_A_Main_Metro", baseline_infra())
        assert candidate.eligible

    def test_restricted_status_is_never_eligible(self):
        infra = baseline_infra()
        infra["Gate_A_Main_Metro"]["status"] = "restricted"
        candidate = evaluate_node("Gate_A_Main_Metro", infra)
        assert not candidate.eligible

    def test_blocked_status_is_never_eligible(self):
        infra = baseline_infra()
        infra["Gate_A_Main_Metro"]["status"] = "blocked"
        candidate = evaluate_node("Gate_A_Main_Metro", infra)
        assert not candidate.eligible


class TestRoutingOrdering:

    def test_eligible_nodes_rank_before_excluded(self):
        routes = compute_safe_routes(gate_a_closed(), node_type="transit_exit")
        eligible = [r for r in routes if r.eligible]
        excluded = [r for r in routes if not r.eligible]
        if eligible and excluded:
            # all eligible must appear before any excluded
            last_eligible_idx = max(routes.index(r) for r in eligible)
            first_excluded_idx = min(routes.index(r) for r in excluded)
            assert last_eligible_idx < first_excluded_idx

    def test_preferred_capacity_nodes_rank_above_high_capacity(self):
        infra = baseline_infra()
        infra["Gate_A_Main_Metro"]["capacity_load"] = "70%"   # over preferred, still eligible
        infra["Gate_B_West_Shuttle"]["capacity_load"] = "20%"  # under preferred
        routes = compute_safe_routes(infra, node_type="transit_exit")
        eligible = [r for r in routes if r.eligible]
        assert eligible[0].node_id == "Gate_B_West_Shuttle", \
            "Node under preferred capacity should rank first"

    def test_closest_eligible_wins_when_capacity_equal(self):
        infra = baseline_infra()
        # both under preferred capacity
        infra["Gate_A_Main_Metro"]["capacity_load"] = "10%"
        infra["Gate_B_West_Shuttle"]["capacity_load"] = "10%"
        routes = compute_safe_routes(infra, node_type="transit_exit")
        eligible = [r for r in routes if r.eligible]
        # Gate A distance 100m < Gate B 180m
        assert eligible[0].node_id == "Gate_A_Main_Metro"


class TestBestRoute:

    def test_best_route_skips_closed_gate(self):
        result = best_route(gate_a_closed(), "transit_exit")
        assert result is not None
        assert result.node_id != "Gate_A_Main_Metro", \
            "SAFETY BUG: best_route returned a closed gate"

    def test_best_route_skips_incident_gate(self):
        result = best_route(gate_a_incident(), "transit_exit")
        assert result is not None
        assert result.node_id != "Gate_A_Main_Metro"

    def test_best_route_returns_none_when_all_blocked(self):
        result = best_route(all_transit_blocked(), "transit_exit")
        assert result is None, "Should return None when no eligible routes exist"

    def test_best_route_covers_all_node_types(self):
        infra = baseline_infra()
        for node_type in ["transit_exit", "medical", "cooling", "security",
                           "lost_and_found", "family_reunification", "accessibility"]:
            result = best_route(infra, node_type)
            assert result is not None, f"No best_route for type={node_type}"
            assert result.eligible


# ── Guardrail validation ──────────────────────────────────────────────────────

class TestGuardrail:

    def test_script_naming_only_closed_node_is_flagged(self):
        infra = gate_a_closed()
        bad = "Head straight to Gate A for the fastest exit."
        violation = validate_script_against_routes(bad, infra)
        assert violation is not None, "Should flag script routing to closed node"

    def test_script_explaining_closure_and_redirecting_is_clean(self):
        infra = gate_a_closed()
        good = "Gate A is currently closed due to a hardware issue. Please use Gate B — West Shuttle Lot instead."
        violation = validate_script_against_routes(good, infra)
        assert violation is None, "Should allow script that mentions closure while giving an alternative"

    def test_script_not_mentioning_excluded_node_is_clean(self):
        infra = gate_a_closed()
        good = "Please head to Gate B — West Shuttle Lot, about 180 meters from here."
        violation = validate_script_against_routes(good, infra)
        assert violation is None

    def test_empty_script_does_not_raise(self):
        violation = validate_script_against_routes("", gate_a_closed())
        assert violation is None

    def test_open_nodes_never_trigger_guardrail(self):
        infra = baseline_infra()
        for script in ["Gate A is fine", "Use Gate B", "Go to Gate A"]:
            violation = validate_script_against_routes(script, infra)
            assert violation is None, f"Should not flag open node: {script!r}"


# ── Fallback intent classifier ────────────────────────────────────────────────

class TestFallbackIntentClassifier:

    @pytest.mark.parametrize("query,expected", [
        ("my child is lost and her name is angelina", "LostPerson"),
        ("my daughter is missing", "LostPerson"),
        ("chest pain cant breathe",  "Medical"),
        ("someone is bleeding badly", "Medical"),
        ("there is a man with a knife", "Security"),
        ("there is a fight breaking out", "Security"),
        ("wheelchair ramp", "Accessibility"),
        ("it is too hot and I feel faint", "Weather"),
        ("which gate is fastest to exit", "Transit"),
        ("the crowd is not moving and I am stuck", "Crowd"),
        ("where is the nearest bathroom", "Amenity"),
    ])
    def test_keyword_classifier(self, query, expected):
        result = guess_intent_from_text(query)
        assert result == expected, f"Query '{query}': expected {expected}, got {result}"

    def test_unknown_query_returns_amenity(self):
        assert guess_intent_from_text("hello how are you") == "Amenity"


# ── Deterministic fallback script ─────────────────────────────────────────────

class TestFallbackScript:

    def test_fallback_script_never_routes_to_closed_node(self):
        infra = gate_a_closed()
        for intent in ["Transit", "Medical", "Security", "LostPerson",
                       "Accessibility", "Weather", "Crowd"]:
            script = deterministic_fallback_script(intent, infra)
            assert "Gate A" not in script or "closed" in script.lower(), \
                f"Fallback script for intent={intent} named a closed gate without flagging closure"

    def test_fallback_script_handles_all_blocked(self):
        script = deterministic_fallback_script("Transit", all_transit_blocked())
        assert "command post" in script.lower() or "supervisor" in script.lower(), \
            "Should escalate to manual command post when all transit routes blocked"

    def test_all_intents_covered_in_mapping(self):
        for intent in ["Transit", "Medical", "Crowd", "Security",
                       "LostPerson", "Accessibility", "Weather"]:
            assert intent in INTENT_TO_NODE_TYPE, f"Intent {intent} missing from INTENT_TO_NODE_TYPE"


# ── Static map integrity ──────────────────────────────────────────────────────

class TestStaticMapIntegrity:

    def test_all_nodes_have_required_fields(self):
        required = {"type", "connected_corridor", "distance_m", "label"}
        for node_id, attrs in STATIC_MAP.items():
            missing = required - set(attrs.keys())
            assert not missing, f"Node {node_id} missing fields: {missing}"

    def test_no_duplicate_labels(self):
        labels = [v["label"] for v in STATIC_MAP.values()]
        assert len(labels) == len(set(labels)), "Duplicate node labels found in STATIC_MAP"

    def test_all_distances_positive(self):
        for node_id, attrs in STATIC_MAP.items():
            assert attrs["distance_m"] > 0, f"Node {node_id} has non-positive distance"
