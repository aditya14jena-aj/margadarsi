let currentState = null;
let _operatorSecret = '';

async function initAuth() {
  try {
    const res = await fetch('/api/config/public');
    const cfg = await safeJson(res);
    if (cfg.operator_auth_required) {
      _operatorSecret = window.prompt('Operator secret required for Judge Mode:') || '';
    }
  } catch (e) {
    console.warn('initAuth failed:', e);
  }
}

function operatorHeaders() {
  const h = {'Content-Type': 'application/json'};
  if (_operatorSecret) h['X-Operator-Secret'] = _operatorSecret;
  return h;
}
let leafletMap = null;
let gateMarkers = {};
let recognizing = false;
let recognition = null;

/* ---- Safe fetch helpers ---- */
async function safeJson(res) {
  const text = await res.text();
  if (!text) throw new Error(`Empty response (HTTP ${res.status})`);
  try {
    return JSON.parse(text);
  } catch (e) {
    throw new Error(`Bad response (HTTP ${res.status}): ${text.slice(0, 120)}`);
  }
}

/* ---- State ---- */
async function fetchState() {
  try {
    const res = await fetch('/api/state');
    const state = await safeJson(res);
    currentState = state;
    renderTopbar(state);
    renderMap(state);
    document.getElementById('state-viewer').textContent = JSON.stringify(state, null, 2);
    return state;
  } catch (e) {
    document.getElementById('state-viewer').textContent = 'State unavailable: ' + e.message;
    console.error('fetchState failed:', e);
    return null;
  }
}

function renderTopbar(state) {
  document.getElementById('topbar-temp').textContent = state.ambient_temperature || '—';
  document.getElementById('topbar-zone').textContent = state.volunteer_location;
  document.getElementById('loc-display').textContent = state.volunteer_location;
}

/* ---- Real Leaflet map of MetLife Stadium ---- */
function loadPct(gate) {
  const v = parseInt((gate.capacity_load || '0').replace('%', ''), 10);
  return isNaN(v) ? 0 : v;
}
function colorForLoad(pct, hasIncident) {
  if (hasIncident) return '#FF4757';
  if (pct >= 80) return '#FF4757';
  if (pct >= 50) return '#F5A623';
  return '#3FCFB4';
}

async function initMap() {
  const venueRes = await fetch('/api/venue');
  const venue = await safeJson(venueRes);

  leafletMap = L.map('leaflet-map', { zoomControl: true, attributionControl: true })
    .setView([venue.lat, venue.lng], 16);

  L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
    attribution: '&copy; OpenStreetMap contributors &copy; CARTO',
    subdomains: 'abcd',
    maxZoom: 19
  }).addTo(leafletMap);

  L.marker([venue.lat, venue.lng], {
    icon: L.divIcon({ className: 'venue-pin', html: '◈', iconSize: [20, 20] })
  }).addTo(leafletMap).bindPopup(`<b>${venue.name}</b><br>WC26 Final Venue`);
}

function renderMap(state) {
  if (!leafletMap) return;
  const infra = state.infrastructure || {};

  Object.entries(infra).forEach(([key, gate]) => {
    if (typeof gate.lat !== 'number' || typeof gate.lng !== 'number') return;

    const isGate = key.startsWith('Gate_');
    const pct = isGate ? loadPct(gate) : null;
    const hasIncident = gate.incident && gate.incident !== 'none';
    const color = isGate ? colorForLoad(pct, hasIncident) : '#8FA8AC';

    const popupHtml = isGate
      ? `<b>${gate.label || key}</b><br>${gate.capacity_load || '—'} load · ${gate.avg_wait_minutes ?? '—'}m wait${hasIncident ? '<br><span style="color:#FF4757">INCIDENT: ' + gate.incident + '</span>' : ''}`
      : `<b>${gate.label || key}</b><br>${gate.distance_meters ?? '—'}m away · ${gate.status}`;

    if (gateMarkers[key]) {
      gateMarkers[key].setStyle({ color, fillColor: color });
      gateMarkers[key].setPopupContent(popupHtml);
    } else {
      const marker = L.circleMarker([gate.lat, gate.lng], {
        radius: isGate ? 12 : 8,
        color,
        fillColor: color,
        fillOpacity: 0.55,
        weight: 2
      }).addTo(leafletMap).bindPopup(popupHtml);
      gateMarkers[key] = marker;
    }

    if (hasIncident) {
      gateMarkers[key].setStyle({ className: 'pulse-marker' });
    }
  });
}

/* ---- Query flow ---- */
async function submitQuery() {
  const query = document.getElementById('query-input').value.trim();
  if (!query) return;

  const langSelect = document.getElementById('lang-select');
  const sourceLanguageHint = langSelect.value === 'auto' ? '' : langSelect.value;

  const btn = document.getElementById('analyze-btn');
  btn.disabled = true;
  btn.innerHTML = '<span class="btn-icon">…</span> Analyzing';

  const card = document.getElementById('triage-card');
  card.className = 'triage';
  document.getElementById('urgency-text').textContent = '···';
  document.getElementById('triage-flags').innerHTML = '';
  document.getElementById('script-box').textContent = 'Reading live conditions…';
  document.getElementById('fan-script-wrap').hidden = true;
  document.getElementById('speak-btn').disabled = true;
  document.getElementById('speak-fan-btn').disabled = true;

  renderPipeline(0);
  const stageTimers = [
    setTimeout(() => renderPipeline(1), 400),
    setTimeout(() => renderPipeline(2), 900),
    setTimeout(() => renderPipeline(3), 1400),
  ];

  try {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 25000);
    const res = await fetch('/api/query', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({query, source_language_hint: sourceLanguageHint}),
      signal: controller.signal
    });
    clearTimeout(timeoutId);
    const data = await safeJson(res);
    renderResult(data);
  } catch (e) {
    const msg = e.name === 'AbortError' ? 'Request timed out — try again.' : ('Connection error: ' + e.message);
    document.getElementById('script-box').textContent = msg;
    console.error('submitQuery failed:', e);
  }

  stageTimers.forEach(clearTimeout);
  clearPipeline();
  btn.disabled = false;
  btn.innerHTML = '<span class="btn-icon">▶</span> Analyze &amp; Generate Script';
}

const LANG_NAME_TO_BCP47 = {
  'spanish': 'es-ES', 'portuguese': 'pt-BR', 'french': 'fr-FR', 'arabic': 'ar-SA',
  'chinese': 'zh-CN', 'mandarin': 'zh-CN', 'hindi': 'hi-IN', 'german': 'de-DE',
  'italian': 'it-IT', 'japanese': 'ja-JP', 'korean': 'ko-KR', 'russian': 'ru-RU',
  'english': 'en-US'
};

function renderResult(data) {
  const card = document.getElementById('triage-card');
  const colorClass = (data.alert_color || 'green').toLowerCase();
  card.className = 'triage ' + colorClass;

  document.getElementById('urgency-text').textContent = (data.urgency || '—').toUpperCase();
  document.getElementById('script-box').textContent = data.volunteer_script || '(no script returned)';
  document.getElementById('intent-tag').textContent = 'Intent ' + (data.intent || '—');
  document.getElementById('color-tag').textContent = 'Alert ' + (data.alert_color || '—')
    + (data.guardrail_override ? ' · route corrected by engine' : '');

  const flags = [];
  if (data.needs_backup) flags.push('<span class="flag-badge backup">⚠ REQUEST BACKUP</span>');
  if (data.needs_clarification) flags.push('<span class="flag-badge clarify">? CLARIFY WITH FAN</span>');
  document.getElementById('triage-flags').innerHTML = flags.join('');

  const notifyBtn = document.getElementById('notify-btn');
  if (data.needs_backup) {
    notifyBtn.hidden = false;
    notifyBtn.disabled = false;
    notifyBtn.textContent = '🚨 Notify Supervisor';
    window._lastQueryResult = data;
  } else {
    notifyBtn.hidden = true;
  }

  const fanWrap = document.getElementById('fan-script-wrap');
  if (data.fan_facing_script && data.detected_language && data.detected_language.toLowerCase() !== 'english') {
    document.getElementById('fan-lang-label').textContent = data.detected_language +
      (data.translation_confidence ? ` · ${data.translation_confidence} confidence` : '');
    document.getElementById('fan-script-box').textContent = data.fan_facing_script;
    fanWrap.hidden = false;
  } else {
    fanWrap.hidden = true;
  }

  document.getElementById('speak-btn').disabled = !data.volunteer_script;
  document.getElementById('speak-fan-btn').disabled = !data.fan_facing_script;

  window._lastDetectedLang = data.detected_language || 'English';
  fetchRoutes();
}

function quickFill(text, lang) {
  document.getElementById('query-input').value = text;
  if (lang) document.getElementById('lang-select').value = lang;
}

/* ---- Real voice input (Web Speech API), language-aware ---- */
function initVoiceInput() {
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  const micBtn = document.getElementById('mic-btn');
  if (!SpeechRecognition) {
    micBtn.title = 'Voice input not supported in this browser';
    micBtn.style.opacity = '0.35';
    return;
  }
  recognition = new SpeechRecognition();
  recognition.continuous = false;
  recognition.interimResults = true;

  recognition.onresult = (event) => {
    let transcript = '';
    for (let i = 0; i < event.results.length; i++) {
      transcript += event.results[i][0].transcript;
    }
    document.getElementById('query-input').value = transcript;
  };
  recognition.onend = () => {
    recognizing = false;
    micBtn.classList.remove('recording');
  };
  recognition.onerror = (e) => {
    recognizing = false;
    micBtn.classList.remove('recording');
    console.error('speech recognition error:', e.error);
  };
}

function toggleMic() {
  if (!recognition) return;
  const micBtn = document.getElementById('mic-btn');
  const langSelect = document.getElementById('lang-select');
  const selected = langSelect.value;
  recognition.lang = selected === 'auto' ? 'en-US' : selected;

  if (recognizing) {
    recognition.stop();
    recognizing = false;
    micBtn.classList.remove('recording');
  } else {
    document.getElementById('query-input').value = '';
    recognition.start();
    recognizing = true;
    micBtn.classList.add('recording');
  }
}

/* ---- Pipeline stage indicator (reflects real request lifecycle) ---- */
const PIPELINE_STAGES = ['Transcribing', 'Translating', 'Routing', 'Generating'];

function renderPipeline(activeIndex) {
  const el = document.getElementById('pipeline-steps');
  el.innerHTML = PIPELINE_STAGES.map((stage, i) => {
    let cls = '';
    if (i < activeIndex) cls = 'done';
    if (i === activeIndex) cls = 'active';
    return `<span class="pipeline-step ${cls}">${stage}</span>`;
  }).join('');
}

function clearPipeline() {
  document.getElementById('pipeline-steps').innerHTML = '';
}

/* ---- Real text-to-speech playback, English or fan language ---- */
let _speakingMode = null; // 'volunteer' | 'fan' | null — tracks which button is actually driving playback

function resetSpeakButtonUI(mode) {
  const isEnglish = mode === 'volunteer';
  const btn = document.getElementById(isEnglish ? 'speak-btn' : 'speak-fan-btn');
  const label = document.getElementById(isEnglish ? 'speak-label' : 'speak-fan-label');
  btn.classList.remove('playing');
  label.textContent = isEnglish ? 'Play for volunteer' : 'Play for fan';
}

function speakScript(mode) {
  const isEnglish = mode === 'volunteer';
  const textEl = isEnglish ? document.getElementById('script-box') : document.getElementById('fan-script-box');
  const text = textEl.textContent.trim();
  if (!text || !window.speechSynthesis) return;

  const btn = document.getElementById(isEnglish ? 'speak-btn' : 'speak-fan-btn');
  const label = document.getElementById(isEnglish ? 'speak-label' : 'speak-fan-label');

  // If THIS button is the one currently playing, treat the click as "stop".
  if (_speakingMode === mode) {
    speechSynthesis.cancel();
    resetSpeakButtonUI(mode);
    _speakingMode = null;
    return;
  }

  // If a DIFFERENT script is currently playing, stop it first, then proceed
  // to start the newly requested one (previously this silently did nothing).
  if (_speakingMode !== null) {
    speechSynthesis.cancel();
    resetSpeakButtonUI(_speakingMode);
    _speakingMode = null;
  }

  const targetLang = isEnglish
    ? 'en-US'
    : (LANG_NAME_TO_BCP47[(window._lastDetectedLang || '').toLowerCase()] || 'en-US');

  const startUtterance = (lang, isRetry) => {
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.lang = lang;
    utterance.rate = 1.0;
    utterance.pitch = 1.0;
    utterance.onstart = () => { _speakingMode = mode; btn.classList.add('playing'); label.textContent = 'Stop'; };
    utterance.onend = () => { if (_speakingMode === mode) _speakingMode = null; resetSpeakButtonUI(mode); };
    utterance.onerror = (e) => {
      console.error('speechSynthesis error:', e.error, 'lang:', lang);
      if (!isRetry && lang !== 'en-US') {
        // Many browsers lack an installed voice for the fan's language —
        // retry once with the default voice rather than failing silently.
        startUtterance('en-US', true);
        return;
      }
      if (_speakingMode === mode) _speakingMode = null;
      resetSpeakButtonUI(mode);
    };
    // Cancel can be asynchronous in some browsers; a short delay avoids the
    // new utterance being dropped when one was just cancelled above.
    setTimeout(() => speechSynthesis.speak(utterance), 30);
  };

  startUtterance(targetLang, false);
}

/* ---- Simulation controls: publish real events to /api/events ---- */
async function publishEvent(event) {
  try {
    const res = await fetch('/api/events', {
      method: 'POST',
      headers: operatorHeaders(),
      body: JSON.stringify(event)
    });
    await safeJson(res);
  } catch (e) {
    console.error('publishEvent failed:', e);
  }
  fetchState();
}

function simulateOvercrowding() {
  publishEvent({
    node_id: 'Gate_A_Main_Metro',
    event_type: 'capacity_change',
    value: '94%',
    reason: 'Operator simulation: overcrowding'
  });
}
function simulateIncident() {
  publishEvent({
    node_id: 'Gate_A_Main_Metro',
    event_type: 'status_change',
    value: 'CLOSED',
    reason: 'Hardware Blockage'
  });
}

async function refreshWeather() {
  try {
    const res = await fetch('/api/weather/refresh', { method: 'POST', headers: operatorHeaders() });
    await safeJson(res);
  } catch (e) {
    console.error('refreshWeather failed:', e);
  }
  fetchState();
}

async function resetState() {
  try {
    const res = await fetch('/api/state/reset', { method: 'POST', headers: operatorHeaders() });
    await safeJson(res);
  } catch (e) {
    console.error('resetState failed:', e);
  }
  fetchState();
}

function pushCustomState() {
  const raw = document.getElementById('custom-json').value.trim();
  if (!raw) return;
  try {
    const parsed = JSON.parse(raw);
    fetch('/api/state', {
      method: 'POST',
      headers: operatorHeaders(),
      body: JSON.stringify(parsed)
    }).then(() => fetchState());
  } catch (e) {
    alert('Invalid JSON: ' + e.message);
  }
}

/* ---- Supervisor escalations ---- */
async function notifySupervisor() {
  const data = window._lastQueryResult;
  if (!data) return;
  const btn = document.getElementById('notify-btn');
  btn.disabled = true;
  btn.textContent = 'Notifying…';

  try {
    const res = await fetch('/api/escalations', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        query_id: data.query_id,
        intent: data.intent,
        urgency: data.urgency,
        query_text: document.getElementById('query-input').value,
        volunteer_script: data.volunteer_script,
        volunteer_location: currentState ? currentState.volunteer_location : ''
      })
    });
    await safeJson(res);
    btn.textContent = '✓ Supervisor Notified';
    fetchEscalations();
  } catch (e) {
    btn.disabled = false;
    btn.textContent = '🚨 Notify Supervisor (retry)';
    console.error('notifySupervisor failed:', e);
  }
}

async function fetchEscalations() {
  try {
    const res = await fetch('/api/escalations?limit=15');
    const data = await safeJson(res);
    renderEscalationsPanel(data.escalations);
  } catch (e) {
    document.getElementById('escalations-panel').textContent = 'Unavailable: ' + e.message;
    console.error('fetchEscalations failed:', e);
  }
}

function renderEscalationsPanel(escalations) {
  const panel = document.getElementById('escalations-panel');
  if (!escalations || !escalations.length) { panel.textContent = 'No escalations yet.'; return; }

  panel.innerHTML = escalations.slice().reverse().map(e => {
    const time = new Date(e.timestamp).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', second: '2-digit'});
    return `
      <div class="escalation-row status-${e.status}">
        <div class="escalation-top">
          <span class="escalation-time">${time}</span>
          <span class="escalation-intent">${e.intent} · ${e.urgency}</span>
          <span class="escalation-status">${e.status.toUpperCase()}</span>
        </div>
        <div class="escalation-query">"${e.query_text}"</div>
        <div class="escalation-actions">
          ${e.status !== 'acknowledged' ? `<button onclick="setEscalationStatus('${e.id}','acknowledged')">Acknowledge</button>` : ''}
          ${e.status !== 'resolved' ? `<button onclick="setEscalationStatus('${e.id}','resolved')">Resolve</button>` : ''}
        </div>
      </div>
    `;
  }).join('');
}

async function setEscalationStatus(id, status) {
  try {
    const res = await fetch(`/api/escalations/${id}`, {
      method: 'PATCH',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({status})
    });
    await safeJson(res);
  } catch (e) {
    console.error('setEscalationStatus failed:', e);
  }
  fetchEscalations();
}

/* ---- Shift log ---- */
async function fetchShiftLog() {
  try {
    const res = await fetch('/api/shift-log?limit=20');
    const data = await safeJson(res);
    renderShiftLogPanel(data.entries);
  } catch (e) {
    document.getElementById('shiftlog-panel').textContent = 'Unavailable: ' + e.message;
    console.error('fetchShiftLog failed:', e);
  }
}

function renderShiftLogPanel(entries) {
  const panel = document.getElementById('shiftlog-panel');
  if (!entries || !entries.length) { panel.textContent = 'No queries logged yet this session.'; return; }

  panel.innerHTML = entries.slice().reverse().map(e => {
    const time = new Date(e.timestamp).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', second: '2-digit'});
    return `
      <div class="shiftlog-row">
        <span class="shiftlog-time">${time}</span>
        <span class="shiftlog-intent">${e.intent || '—'}</span>
        <span class="shiftlog-source">${e.source}</span>
        <span class="shiftlog-text">${e.query_text}</span>
      </div>
    `;
  }).join('');
}

async function exportShiftLog() {
  try {
    const res = await fetch('/api/shift-log/export');
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `corridor_shift_log_${Date.now()}.csv`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (e) {
    console.error('exportShiftLog failed:', e);
    alert('Export failed: ' + e.message);
  }
}
async function fetchRoutes() {
  try {
    const res = await fetch('/api/routes');
    const data = await safeJson(res);
    renderRoutesPanel(data.safe_routes);
  } catch (e) {
    document.getElementById('routes-panel').textContent = 'Routing engine unavailable: ' + e.message;
    console.error('fetchRoutes failed:', e);
  }
}

function renderRoutesPanel(routes) {
  const panel = document.getElementById('routes-panel');
  if (!routes || !routes.length) { panel.textContent = 'No routes.'; return; }

  panel.innerHTML = routes.map(r => `
    <div class="route-row ${r.eligible ? 'eligible' : 'excluded'}">
      <div class="route-main">
        <span class="route-dot"></span>
        <span class="route-label">${r.label}</span>
        <span class="route-dist">${r.distance_m}m</span>
      </div>
      <div class="route-reason">${r.eligible ? `eligible · ${r.capacity_pct ?? '—'}% capacity` : r.reason}</div>
    </div>
  `).join('');
}

/* ---- Live events stream panel ---- */
async function fetchEvents() {
  try {
    const res = await fetch('/api/events?limit=15');
    const data = await safeJson(res);
    renderEventsPanel(data.events);
  } catch (e) {
    document.getElementById('events-panel').textContent = 'Event stream unavailable: ' + e.message;
    console.error('fetchEvents failed:', e);
  }
}

function renderEventsPanel(events) {
  const panel = document.getElementById('events-panel');
  if (!events || !events.length) { panel.textContent = 'No events yet. Trigger a simulation above.'; return; }

  panel.innerHTML = events.slice().reverse().map(e => {
    const time = new Date(e.timestamp).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', second: '2-digit'});
    return `
      <div class="event-row">
        <span class="event-time">${time}</span>
        <span class="event-type">${e.event_type}</span>
        <span class="event-detail">${e.node_id || 'ambient'} → ${e.value}${e.reason ? ' · ' + e.reason : ''}</span>
      </div>
    `;
  }).join('');
}

/* ---- Volunteer / Judge mode switch ---- */
function initModeSwitch() {
  const buttons = document.querySelectorAll('.mode-btn');
  buttons.forEach(btn => {
    btn.addEventListener('click', () => {
      const mode = btn.dataset.mode;
      document.body.dataset.mode = mode;
      buttons.forEach(b => b.setAttribute('aria-pressed', b === btn ? 'true' : 'false'));
      // Leaflet needs a nudge after its container becomes visible, or tiles render blank.
      if (mode === 'judge' && leafletMap) {
        setTimeout(() => leafletMap.invalidateSize(), 50);
      }
      window.scrollTo({top: 0, behavior: 'smooth'});
    });
  });
}

/* ---- Mobile tab bar (Judge Mode sub-navigation) ---- */
function initTabBar() {
  const tabs = document.querySelectorAll('.tab-btn');
  const main = document.querySelector('main.console');
  tabs.forEach(tab => {
    tab.addEventListener('click', () => {
      const target = tab.dataset.tab;
      main.dataset.activeTab = target;
      tabs.forEach(t => t.setAttribute('aria-selected', t === tab ? 'true' : 'false'));
      // Leaflet needs a nudge after becoming visible again, or tiles render blank.
      if (target === 'map' && leafletMap) {
        setTimeout(() => leafletMap.invalidateSize(), 50);
      }
      main.scrollTo({top: 0, behavior: 'instant' in document.documentElement.style ? 'instant' : 'auto'});
      window.scrollTo({top: 0, behavior: 'smooth'});
    });
  });
}

/* ---- Clock ---- */
function tickClock() {
  const now = new Date();
  document.getElementById('clock').textContent =
    now.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', hour12: false});
}
setInterval(tickClock, 1000 * 30);
tickClock();

/* ---- Boot ---- */
(async function boot() {
  await initAuth();
  initVoiceInput();
  initModeSwitch();
  initTabBar();
  await initMap();
  await fetchState();
  await fetchRoutes();
  await fetchEvents();
  await fetchEscalations();
  await fetchShiftLog();
  setInterval(fetchState, 6000);
  setInterval(fetchRoutes, 6000);
  setInterval(fetchEvents, 4000);
  setInterval(fetchEscalations, 5000);
  setInterval(fetchShiftLog, 5000);
})();
