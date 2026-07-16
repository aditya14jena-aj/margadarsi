let currentState = null;

async function fetchState() {
  const res = await fetch('/api/state');
  const state = await res.json();
  currentState = state;
  renderTopbar(state);
  renderGateMap(state);
  document.getElementById('state-viewer').textContent = JSON.stringify(state, null, 2);
  return state;
}

function renderTopbar(state) {
  document.getElementById('topbar-temp').textContent = state.ambient_temperature;
  document.getElementById('topbar-zone').textContent = state.volunteer_location;
  document.getElementById('loc-display').textContent = state.volunteer_location;
}

/* ---- Signature element: live gate map ---- */
function loadPct(gate) {
  const v = parseInt((gate.capacity_load || '0').replace('%', ''), 10);
  return isNaN(v) ? 0 : v;
}
function colorForLoad(pct, hasIncident) {
  if (hasIncident) return 'var(--red)';
  if (pct >= 80) return 'var(--red)';
  if (pct >= 50) return 'var(--amber)';
  return 'var(--teal)';
}
function pulseSpeed(pct) {
  // higher congestion = faster pulse
  const speed = 2.6 - (pct / 100) * 1.9;
  return Math.max(0.7, speed).toFixed(2) + 's';
}

function renderGateMap(state) {
  const svg = document.getElementById('gate-map');
  const infra = state.infrastructure || {};
  const gateA = infra.Gate_A_Main_Metro || {};
  const gateB = infra.Gate_B_West_Shuttle || {};
  const aid = infra.First_Aid_Station_Zone_3 || {};
  const cool = infra.Cooling_Center_Sector_B || {};

  const aPct = loadPct(gateA);
  const bPct = loadPct(gateB);
  const aHasIncident = gateA.incident && gateA.incident !== 'none';
  const aColor = colorForLoad(aPct, aHasIncident);
  const bColor = colorForLoad(bPct, false);

  svg.innerHTML = `
    <defs>
      <marker id="dotcap" markerWidth="4" markerHeight="4" refX="2" refY="2">
        <circle cx="2" cy="2" r="2" />
      </marker>
    </defs>

    <!-- background grid -->
    <g stroke="var(--line)" stroke-width="1" opacity="0.5">
      ${Array.from({length: 8}).map((_,i)=>`<line x1="${i*80}" y1="0" x2="${i*80}" y2="320" />`).join('')}
      ${Array.from({length: 5}).map((_,i)=>`<line x1="0" y1="${i*80}" x2="640" y2="${i*80}" />`).join('')}
    </g>

    <!-- volunteer node -->
    <circle cx="320" cy="160" r="9" fill="var(--text-primary)" />
    <text x="320" y="140" text-anchor="middle" fill="var(--text-primary)" font-family="IBM Plex Mono" font-size="11">YOU · Corridor 3</text>

    <!-- path to Gate A -->
    <path id="path-a" d="M320,160 C420,110 500,90 580,60" fill="none" stroke="${aColor}" stroke-width="3" opacity="0.85"/>
    <circle r="4" fill="${aColor}">
      <animateMotion dur="${pulseSpeed(aPct)}" repeatCount="indefinite">
        <mpath href="#path-a"/>
      </animateMotion>
    </circle>
    <circle cx="580" cy="60" r="10" fill="${aColor}" opacity="0.25"/>
    <circle cx="580" cy="60" r="6" fill="${aColor}"/>
    <text x="596" y="50" fill="var(--text-primary)" font-family="Barlow Condensed" font-weight="700" font-size="15">GATE A</text>
    <text x="596" y="66" fill="${aColor}" font-family="IBM Plex Mono" font-size="11">${gateA.capacity_load || '—'} load · ${gateA.avg_wait_minutes ?? '—'}m wait${aHasIncident ? ' · INCIDENT' : ''}</text>

    <!-- path to Gate B -->
    <path id="path-b" d="M320,160 C420,210 500,240 580,260" fill="none" stroke="${bColor}" stroke-width="3" opacity="0.85"/>
    <circle r="4" fill="${bColor}">
      <animateMotion dur="${pulseSpeed(bPct)}" repeatCount="indefinite">
        <mpath href="#path-b"/>
      </animateMotion>
    </circle>
    <circle cx="580" cy="260" r="10" fill="${bColor}" opacity="0.25"/>
    <circle cx="580" cy="260" r="6" fill="${bColor}"/>
    <text x="596" y="250" fill="var(--text-primary)" font-family="Barlow Condensed" font-weight="700" font-size="15">GATE B</text>
    <text x="596" y="266" fill="${bColor}" font-family="IBM Plex Mono" font-size="11">${gateB.capacity_load || '—'} load · ${gateB.avg_wait_minutes ?? '—'}m wait</text>

    <!-- path to First Aid -->
    <path d="M320,160 C260,120 160,100 90,70" fill="none" stroke="var(--text-dim)" stroke-width="2" stroke-dasharray="3 4" opacity="0.6"/>
    <circle cx="90" cy="70" r="6" fill="var(--text-dim)"/>
    <text x="20" y="55" fill="var(--text-muted)" font-family="Barlow Condensed" font-weight="600" font-size="13">FIRST AID</text>
    <text x="20" y="70" fill="var(--text-dim)" font-family="IBM Plex Mono" font-size="10">${aid.distance_meters ?? '—'}m</text>

    <!-- path to Cooling Center -->
    <path d="M320,160 C260,200 160,220 90,250" fill="none" stroke="var(--text-dim)" stroke-width="2" stroke-dasharray="3 4" opacity="0.6"/>
    <circle cx="90" cy="250" r="6" fill="var(--text-dim)"/>
    <text x="14" y="270" fill="var(--text-muted)" font-family="Barlow Condensed" font-weight="600" font-size="13">COOLING CENTER</text>
    <text x="14" y="285" fill="var(--text-dim)" font-family="IBM Plex Mono" font-size="10">${cool.distance_meters ?? '—'}m</text>
  `;
}

/* ---- Query flow ---- */
async function submitQuery() {
  const query = document.getElementById('query-input').value.trim();
  if (!query) return;

  const btn = document.getElementById('analyze-btn');
  btn.disabled = true;
  btn.innerHTML = '<span class="btn-icon">…</span> Analyzing';

  const card = document.getElementById('triage-card');
  card.className = 'triage';
  document.getElementById('urgency-text').textContent = '···';
  document.getElementById('script-box').textContent = 'Reading live corridor state…';

  try {
    const res = await fetch('/api/query', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({query})
    });
    const data = await res.json();
    renderResult(data);
  } catch (e) {
    document.getElementById('script-box').textContent = 'Connection error: ' + e.message;
  }

  btn.disabled = false;
  btn.innerHTML = '<span class="btn-icon">▶</span> Analyze &amp; Generate Script';
}

function renderResult(data) {
  const card = document.getElementById('triage-card');
  const colorClass = (data.alert_color || 'green').toLowerCase();
  card.className = 'triage ' + colorClass;

  document.getElementById('urgency-text').textContent = (data.urgency || '—').toUpperCase();
  document.getElementById('script-box').textContent = data.volunteer_script || '(no script returned)';
  document.getElementById('intent-tag').textContent = 'Intent ' + (data.intent || '—');
  document.getElementById('color-tag').textContent = 'Alert ' + (data.alert_color || '—');
}

function quickFill(text) {
  document.getElementById('query-input').value = text;
}

/* ---- God Mode ---- */
async function patchState(payload) {
  await fetch('/api/state', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  });
  fetchState();
}

function simulateOvercrowding() {
  patchState({ infrastructure_patch: { Gate_A_Main_Metro: { capacity_load: "94%", avg_wait_minutes: 35, status: "open" } } });
}
function simulateHeatSpike() {
  patchState({ ambient_temperature: "104F" });
}
function simulateIncident() {
  patchState({ infrastructure_patch: { Gate_A_Main_Metro: { incident: "crowd_crush_risk", status: "restricted" } } });
}
function resetState() {
  patchState({
    full_state: {
      volunteer_location: "Corridor 3 - Sector B",
      ambient_temperature: "89F",
      infrastructure: {
        Gate_A_Main_Metro: { status: "open", capacity_load: "40%", avg_wait_minutes: 5, incident: "none" },
        Gate_B_West_Shuttle: { status: "open", capacity_load: "22%", avg_wait_minutes: 3, incident: "none" },
        First_Aid_Station_Zone_3: { status: "operational", distance_meters: 45 },
        Cooling_Center_Sector_B: { status: "operational", distance_meters: 15 }
      }
    }
  });
}
function pushCustomState() {
  const raw = document.getElementById('custom-json').value.trim();
  if (!raw) return;
  try {
    patchState(JSON.parse(raw));
  } catch (e) {
    alert('Invalid JSON: ' + e.message);
  }
}

/* ---- Clock ---- */
function tickClock() {
  const now = new Date();
  document.getElementById('clock').textContent =
    now.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', hour12: false});
}
setInterval(tickClock, 1000 * 30);
tickClock();

setInterval(fetchState, 4000);
fetchState();
