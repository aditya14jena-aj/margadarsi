import json
import os
import threading

# Blueprint definitions
STANDARD_BLUEPRINT = {
    "volunteer_location": "Corridor 3 - Sector B",
    "ambient_temperature": "82F",
    "infrastructure": {
        "Gate_A_Main_Metro": {
            "status": "open",
            "capacity_load": "94%",
            "avg_wait_minutes": 35,
            "incident": "none",
            "lat": 40.8149,
            "lng": -74.0725,
            "label": "Gate A — NJ Transit Rail Link"
        },
        "Gate_B_West_Shuttle": {
            "status": "open",
            "capacity_load": "22%",
            "avg_wait_minutes": 3,
            "incident": "none",
            "lat": 40.8128,
            "lng": -74.0774,
            "label": "Gate B — West Shuttle Lot"
        },
        "First_Aid_Station_Zone_3": {
            "status": "operational",
            "distance_meters": 45,
            "lat": 40.8138,
            "lng": -74.0748,
            "label": "First Aid — Sector B Concourse"
        },
        "Cooling_Center_Sector_B": {
            "status": "operational",
            "distance_meters": 15,
            "lat": 40.8136,
            "lng": -74.0743,
            "label": "Cooling Center — Sector B"
        }
    }
}

CRISIS_BLUEPRINT = {
    "volunteer_location": "Corridor 3 - Sector B",
    "ambient_temperature": "104F",
    "infrastructure": {
        "Gate_A_Main_Metro": {
            "status": "closed",
            "capacity_load": "100%",
            "avg_wait_minutes": 999,
            "incident": "structural blockage",
            "lat": 40.8149,
            "lng": -74.0725,
            "label": "Gate A — NJ Transit Rail Link"
        },
        "Gate_B_West_Shuttle": {
            "status": "open",
            "capacity_load": "22%",
            "avg_wait_minutes": 3,
            "incident": "none",
            "lat": 40.8128,
            "lng": -74.0774,
            "label": "Gate B — West Shuttle Lot"
        },
        "First_Aid_Station_Zone_3": {
            "status": "operational",
            "distance_meters": 45,
            "lat": 40.8138,
            "lng": -74.0748,
            "label": "First Aid — Sector B Concourse"
        },
        "Cooling_Center_Sector_B": {
            "status": "operational",
            "distance_meters": 15,
            "lat": 40.8136,
            "lng": -74.0743,
            "label": "Cooling Center — Sector B"
        }
    }
}

_state_lock = threading.Lock()
_current_state = json.loads(json.dumps(STANDARD_BLUEPRINT))

def get_current_state():
    with _state_lock:
        return json.loads(json.dumps(_current_state))

def set_mode(mode: str):
    global _current_state
    with _state_lock:
        if mode == "standard":
            _current_state = json.loads(json.dumps(STANDARD_BLUEPRINT))
        elif mode == "crisis":
            _current_state = json.loads(json.dumps(CRISIS_BLUEPRINT))
        else:
            raise ValueError(f"Unknown mode: {mode}")
    return get_current_state()

def update_field(field_path: list, value):
    global _current_state
    with _state_lock:
        target = _current_state
        for segment in field_path[:-1]:
            target = target[segment]
        target[field_path[-1]] = value
    return get_current_state()
