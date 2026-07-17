"""
crowd_sim.py - Realistic stadium crowd simulation for FIFA World Cup 2026.

Runs as a background daemon thread. Every TICK_SECONDS it:
  1. Determines the current match phase (based on elapsed time since sim_start).
  2. Computes a realistic capacity load for every node using phase-specific
     base values + bounded Gaussian noise.
  3. Fires capacity_change events via apply_event() so the routing engine
     always has fresh, numeric data - no more "unknown%" in scripts.
  4. Probabilistically injects incidents (medical, security) at realistic rates
     and auto-clears them after a realistic hold window.
"""

import logging
import random
import threading
import time

log = logging.getLogger("margadarshi.crowd_sim")

TICK_SECONDS = 30
INCIDENT_CLEAR_TICKS = 6  # ~3 minutes

PHASES = [
    ("gates_open",        90),
    ("pre_kickoff_surge", 30),
    ("first_half",        45),
    ("half_time",         15),
    ("second_half",       45),
    ("final_whistle",      5),
    ("post_match_exit",   60),
]

TOTAL_DURATION_S = sum(d * 60 for _, d in PHASES)

NODE_PHASE_CAPS = {
    "Gate_A_Main_Metro": {
        "gates_open":        (30,  8),
        "pre_kickoff_surge": (82,  6),
        "first_half":        (18,  5),
        "half_time":         (25,  7),
        "second_half":       (20,  5),
        "final_whistle":     (55, 10),
        "post_match_exit":   (91,  5),
    },
    "Gate_B_West_Shuttle": {
        "gates_open":        (22,  7),
        "pre_kickoff_surge": (70,  8),
        "first_half":        (14,  5),
        "half_time":         (20,  6),
        "second_half":       (16,  5),
        "final_whistle":     (48, 10),
        "post_match_exit":   (87,  5),
    },
    "First_Aid_Station_Zone_3": {
        "gates_open":        ( 8,  3),
        "pre_kickoff_surge": (15,  5),
        "first_half":        (22,  6),
        "half_time":         (38,  8),
        "second_half":       (28,  7),
        "final_whistle":     (42, 10),
        "post_match_exit":   (55,  8),
    },
    "Cooling_Center_Sector_B": {
        "gates_open":        (20,  5),
        "pre_kickoff_surge": (45,  8),
        "first_half":        (30,  7),
        "half_time":         (68, 10),
        "second_half":       (40,  7),
        "final_whistle":     (35,  8),
        "post_match_exit":   (28,  6),
    },
    "Security_Post_Sector_B": {
        "gates_open":        (40,  6),
        "pre_kickoff_surge": (70,  8),
        "first_half":        (35,  5),
        "half_time":         (50,  7),
        "second_half":       (38,  5),
        "final_whistle":     (75, 10),
        "post_match_exit":   (88,  6),
    },
    "Lost_And_Found_Concourse": {
        "gates_open":        ( 5,  2),
        "pre_kickoff_surge": (10,  3),
        "first_half":        (12,  4),
        "half_time":         (22,  6),
        "second_half":       (15,  4),
        "final_whistle":     (30,  8),
        "post_match_exit":   (48, 10),
    },
    "Family_Reunification_Point": {
        "gates_open":        ( 8,  3),
        "pre_kickoff_surge": (18,  5),
        "first_half":        (12,  4),
        "half_time":         (28,  7),
        "second_half":       (15,  4),
        "final_whistle":     (40, 10),
        "post_match_exit":   (62,  8),
    },
    "Accessibility_Assist_Desk": {
        "gates_open":        (15,  4),
        "pre_kickoff_surge": (35,  6),
        "first_half":        (20,  4),
        "half_time":         (30,  6),
        "second_half":       (22,  4),
        "final_whistle":     (28,  6),
        "post_match_exit":   (35,  6),
    },
}

INCIDENT_PROBS = {
    "gates_open": {
        "Security_Post_Sector_B": (0.01, "ticket_dispute"),
    },
    "pre_kickoff_surge": {
        "Gate_A_Main_Metro":         (0.03, "crowd_crush_risk"),
        "Security_Post_Sector_B":    (0.04, "altercation"),
        "First_Aid_Station_Zone_3":  (0.02, "fainting"),
    },
    "first_half": {
        "First_Aid_Station_Zone_3":  (0.015, "heat_exhaustion"),
        "Cooling_Center_Sector_B":   (0.01,  "overcrowding"),
    },
    "half_time": {
        "Gate_A_Main_Metro":         (0.02, "crowd_surge"),
        "First_Aid_Station_Zone_3":  (0.04, "heat_exhaustion"),
        "Cooling_Center_Sector_B":   (0.03, "overcrowding"),
        "Security_Post_Sector_B":    (0.03, "altercation"),
    },
    "second_half": {
        "First_Aid_Station_Zone_3":  (0.02, "heat_exhaustion"),
        "Security_Post_Sector_B":    (0.015, "altercation"),
    },
    "final_whistle": {
        "Gate_A_Main_Metro":         (0.05, "crowd_crush_risk"),
        "Gate_B_West_Shuttle":       (0.04, "crowd_crush_risk"),
        "Security_Post_Sector_B":    (0.05, "altercation"),
    },
    "post_match_exit": {
        "Gate_A_Main_Metro":         (0.03, "crowd_crush_risk"),
        "Gate_B_West_Shuttle":       (0.03, "crowd_crush_risk"),
        "First_Aid_Station_Zone_3":  (0.03, "collapse"),
        "Security_Post_Sector_B":    (0.04, "altercation"),
        "Lost_And_Found_Concourse":  (0.02, "disturbance"),
    },
}

WAIT_MULTIPLIERS = {
    "gates_open":        1.0,
    "pre_kickoff_surge": 2.5,
    "first_half":        0.6,
    "half_time":         1.8,
    "second_half":       0.7,
    "final_whistle":     3.0,
    "post_match_exit":   4.0,
}

BASE_WAIT = {
    "Gate_A_Main_Metro":          5,
    "Gate_B_West_Shuttle":        3,
    "First_Aid_Station_Zone_3":   2,
    "Cooling_Center_Sector_B":    1,
    "Security_Post_Sector_B":     3,
    "Lost_And_Found_Concourse":   2,
    "Family_Reunification_Point": 2,
    "Accessibility_Assist_Desk":  2,
}


def _current_phase(elapsed_s):
    cursor = 0
    for name, duration_min in PHASES:
        cursor += duration_min * 60
        if elapsed_s < cursor:
            return name
    return PHASES[-1][0]


def _noisy_cap(base, sigma):
    raw = base + random.gauss(0, sigma)
    return max(5, min(99, round(raw)))


class CrowdSimulator:
    def __init__(self):
        self._started = False
        self._thread = None
        self._sim_start = time.monotonic()
        self._active_incidents = {}
        self._apply_event = None

    def start(self, apply_event_fn):
        if self._started:
            return
        self._started = True
        self._apply_event = apply_event_fn
        self._thread = threading.Thread(
            target=self._run, name="crowd-sim", daemon=True
        )
        self._thread.start()
        log.info("crowd_sim_started tick_s=%d phases=%d", TICK_SECONDS, len(PHASES))

    def _run(self):
        while True:
            try:
                self._tick()
            except Exception:
                log.exception("crowd_sim_tick_error")
            time.sleep(TICK_SECONDS)

    def _tick(self):
        elapsed = (time.monotonic() - self._sim_start) % TOTAL_DURATION_S
        phase = _current_phase(elapsed)
        wait_mult = WAIT_MULTIPLIERS.get(phase, 1.0)

        for node_id, phase_caps in NODE_PHASE_CAPS.items():
            base, sigma = phase_caps.get(phase, (50, 8))
            cap = _noisy_cap(base, sigma)
            wait = max(1, round(BASE_WAIT.get(node_id, 3) * wait_mult + random.gauss(0, 0.5)))

            try:
                self._apply_event({
                    "node_id": node_id,
                    "event_type": "capacity_change",
                    "value": f"{cap}%",
                    "reason": f"crowd_sim:{phase}",
                })
                self._patch_wait(node_id, wait)
            except Exception as e:
                log.warning("crowd_sim_cap_error node=%s err=%s", node_id, e)

        phase_incidents = INCIDENT_PROBS.get(phase, {})
        for node_id, (prob, desc) in phase_incidents.items():
            if node_id in self._active_incidents:
                continue
            if random.random() < prob:
                try:
                    self._apply_event({
                        "node_id": node_id,
                        "event_type": "incident_flagged",
                        "value": desc,
                        "reason": f"crowd_sim:{phase}",
                    })
                    self._active_incidents[node_id] = INCIDENT_CLEAR_TICKS
                    log.info("crowd_sim_incident node=%s desc=%s phase=%s", node_id, desc, phase)
                except Exception as e:
                    log.warning("crowd_sim_incident_error node=%s err=%s", node_id, e)

        to_clear = [nid for nid, rem in self._active_incidents.items() if rem <= 1]
        for node_id in to_clear:
            try:
                self._apply_event({
                    "node_id": node_id,
                    "event_type": "incident_cleared",
                    "value": "resolved",
                    "reason": "crowd_sim:auto_clear",
                })
                del self._active_incidents[node_id]
                log.info("crowd_sim_incident_cleared node=%s", node_id)
            except Exception as e:
                log.warning("crowd_sim_clear_error node=%s err=%s", node_id, e)

        for node_id in list(self._active_incidents):
            if node_id not in to_clear:
                self._active_incidents[node_id] -= 1

    def _patch_wait(self, node_id, wait_minutes):
        try:
            from app import load_state, save_state
            state = load_state()
            node = state.get("infrastructure", {}).get(node_id)
            if node is not None:
                node["avg_wait_minutes"] = wait_minutes
                save_state(state)
        except Exception as e:
            log.debug("crowd_sim_wait_patch_skip node=%s err=%s", node_id, e)

    def current_phase(self):
        elapsed = (time.monotonic() - self._sim_start) % TOTAL_DURATION_S
        return _current_phase(elapsed)

    def elapsed_minutes(self):
        return ((time.monotonic() - self._sim_start) % TOTAL_DURATION_S) / 60


simulator = CrowdSimulator()
