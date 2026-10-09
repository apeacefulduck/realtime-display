const socketUrl = "wss://realtime-display.onrender.com/ws?role=browser";
const itemInput = document.querySelector("#itemInput");
const sendButton = document.querySelector("#sendButton");
const connectionStatus = document.querySelector("#connectionStatus");
const notesPreview = document.querySelector("#notesPreview");
const spotifyPreview = document.querySelector("#spotifyPreview");
const spotifyTrack = document.querySelector("#spotifyTrack");
const spotifyArtist = document.querySelector("#spotifyArtist");
const spotifyAlbum = document.querySelector("#spotifyAlbum");
const itemCount = document.querySelector("#itemCount");
const previewTitle = document.querySelector("#previewTitle");
const notesTab = document.querySelector("#notesTab");
const spotifyTab = document.querySelector("#spotifyTab");
const notesControls = document.querySelector("#notesControls");
const spotifyControls = document.querySelector("#spotifyControls");
const spotifySwitch = document.querySelector("#spotifySwitch");
const refreshSpotifyButton = document.querySelector("#refreshSpotifyButton");
const weatherTab = document.querySelector("#weatherTab");
const weatherControls = document.querySelector("#weatherControls");
const weatherPreview = document.querySelector("#weatherPreview");
const weatherIcon = document.querySelector("#weatherIcon");
const weatherTemp = document.querySelector("#weatherTemp");
const weatherLabel = document.querySelector("#weatherLabel");
const weatherForecast = document.querySelector("#weatherForecast");
const weatherSwitch = document.querySelector("#weatherSwitch");
const refreshWeatherButton = document.querySelector("#refreshWeatherButton");
const weatherLat = document.querySelector("#weatherLat");
const weatherLon = document.querySelector("#weatherLon");

let socket;
let reconnectTimer;
let spotifyTimer;
let weatherTimer;
let activePanel = "notes";
let previewPanel = "notes";
let lastDataAt = 0;
let connectionAttempted = false;
let toastTimer;
let spotifyLoading = false;
let weatherLoading = false;
const deviceTab = document.querySelector("#deviceTab");
const deviceControls = document.querySelector("#deviceControls");
const panels = {indoor: document.querySelector("#indoorControls"), notes: notesControls, spotify: spotifyControls, weather: weatherControls, device: deviceControls};
const tabs = {indoor: document.querySelector("#indoorTab"), notes: notesTab, spotify: spotifyTab, weather: weatherTab, device: deviceTab};
const ui = id => document.querySelector(`#${id}`);
function notify(message) {
  ui("toast").textContent = message;
  ui("toast").hidden = false;
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => { ui("toast").hidden = true; }, 3500);
}
function feedback(id, message, error = false) {
  const element = ui(id);
  element.hidden = !message;
  element.textContent = message;
  element.classList.toggle("error", error);
}
function relativeTime(timestamp) {
  if (!timestamp) return "Henüz veri alınmadı";
  const seconds = Math.max(0, Math.floor((Date.now() - timestamp) / 1000));
  return seconds < 5 ? "Az önce" : seconds < 60 ? `${seconds} sn önce` : seconds < 3600 ? `${Math.floor(seconds / 60)} dk önce` : `${Math.floor(seconds / 3600)} sa önce`;
}
function updateSyncLabels() {
  const label = lastDataAt ? `Son veri: ${relativeTime(lastDataAt)}` : relativeTime(0);
  ui("lastSync").textContent = label;
  ui("previewUpdated").textContent = label;
  ui("deviceSync").textContent = relativeTime(lastDataAt);
}
function received(kind) {
  lastDataAt = Date.now();
  updateSyncLabels();
}
const STORAGE_PREFIX = "homeflow:";
function loadSaved(name, fallback) {
  try { const raw = localStorage.getItem(STORAGE_PREFIX + name); return raw === null ? fallback : JSON.parse(raw); }
  catch { return fallback; }
}
function save(name, value) {
  try { localStorage.setItem(STORAGE_PREFIX + name, JSON.stringify(value)); } catch { /* Storage may be disabled. */ }
}
let spotifySession = loadSaved("spotifySession", "");
let deviceCount = 0;
let heartbeatTimer;
let lastPongAt = 0;
const pendingPayloads = new Map();
const authFragment = new URLSearchParams(window.location.hash.slice(1));
if (authFragment.has("spotify_session")) {
  spotifySession = authFragment.get("spotify_session");
  save("spotifySession", spotifySession);
  save("spotifyEnabled", true);
  window.history.replaceState(null, "", window.location.pathname + window.location.search);
}
spotifySwitch.checked = loadSaved("spotifyEnabled", Boolean(spotifySession));
weatherSwitch.checked = loadSaved("weatherEnabled", false);
weatherLat.value = loadSaved("weatherLat", "41.0082");
weatherLon.value = loadSaved("weatherLon", "28.9784");
const apiBaseUrl = "https://realtime-display.onrender.com";

function setConnectionState(isConnected) {
  const state = isConnected ? deviceCount > 0 ? "connected" : "connecting" : connectionAttempted ? "disconnected" : "connecting";
  connectionStatus.classList.remove("connected", "disconnected", "connecting");
  connectionStatus.classList.add(state);
  connectionStatus.textContent = isConnected ? deviceCount > 0 ? "ESP32 çevrimiçi" : "ESP32 bekleniyor" : connectionAttempted ? "Bağlantı kesildi" : "Bağlantı kuruluyor";
  ui("deviceServer").textContent = isConnected ? "Bağlı" : connectionAttempted ? "Yeniden bağlanılıyor" : "Bağlantı kuruluyor";
  ui("deviceConnections").textContent = isConnected ? String(deviceCount) : "Bilinmiyor";
  ui("connectionNotice").hidden = isConnected && deviceCount > 0;
  ui("connectionNotice").textContent = isConnected ? "Sunucuya bağlısınız. ESP32 bağlantısı bekleniyor." : "Sunucuya bağlanılıyor. Notlarınızı kaydetmek için sunucu bağlantısı gerekir.";
  ui("previewLive").textContent = isConnected && deviceCount > 0 ? "Canlı veri" : isConnected ? "Cihaz bekleniyor" : "Bağlantı bekleniyor";
  ui("previewLive").classList.toggle("online", isConnected && deviceCount > 0);
  sendButton.disabled = false;
  spotifySwitch.disabled = false;
  refreshSpotifyButton.disabled = !isConnected || spotifyLoading;
  weatherSwitch.disabled = false;
  refreshWeatherButton.disabled = !isConnected || weatherLoading;
  updateSyncLabels();
  if (!isConnected || deviceCount === 0) clearIndoor();
}

function setActivePanel(panel) {
  activePanel = panel;
  for (const [name, tab] of Object.entries(tabs)) {
    const selected = name === panel;
    tab.classList.toggle("active", selected);
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
    panels[name].hidden = !selected;
  }
  if (panel !== "device") previewPanel = panel;
  ui("indoorPreview").hidden = previewPanel !== "indoor";
  notesPreview.hidden = previewPanel !== "notes";
  spotifyPreview.hidden = previewPanel !== "spotify";
  weatherPreview.hidden = previewPanel !== "weather";
  previewTitle.textContent = previewPanel === "indoor" ? "EV" : previewPanel === "weather" ? "WEATHER" : previewPanel === "spotify" ? "SPOTIFY" : "NOTLAR";
  itemCount.textContent = previewPanel === "indoor" ? "" : previewPanel === "weather" ? (weatherSwitch.checked ? "ON" : "OFF") : previewPanel === "spotify" ? (spotifySwitch.checked ? "ON" : "OFF") : String(noteGroups.length);
  if (previewPanel === "notes") renderNotesPreview();
}

function sendSocketPayload(payload) {
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    pendingPayloads.set(payload.type, payload);
    return false;
  }

  socket.send(JSON.stringify(payload));
  return true;
}

function connectSocket() {
  const connection = new WebSocket(socketUrl);
  socket = connection;
  connection.addEventListener("open", () => {
    if (socket !== connection) return;
    window.clearTimeout(reconnectTimer);
    lastPongAt = Date.now();
    connectionAttempted = true;
    setConnectionState(true);
    for (const payload of pendingPayloads.values()) sendSocketPayload(payload);
    pendingPayloads.clear();
    refreshNotes();
    if (spotifySwitch.checked) setSpotifyPolling(true);
    if (weatherSwitch.checked) setWeatherPolling(true);
    window.clearInterval(heartbeatTimer);
    heartbeatTimer = window.setInterval(() => {
      if (Date.now() - lastPongAt > 45000) { connection.close(); return; }
      if (connection.readyState === WebSocket.OPEN) connection.send(JSON.stringify({type: "ping"}));
    }, 20000);
  });
  connection.addEventListener("message", event => {
    if (socket !== connection) return;
    let data;
    try { data = JSON.parse(event.data); } catch { return; }
    if (data.type === "pong") { lastPongAt = Date.now(); return; }
    if (data.type === "status") {
      deviceCount = Number(data.devices) || 0;
      setConnectionState(connection.readyState === WebSocket.OPEN);
    } else if (data.type === "indoor") {
      received("indoor");
      applyIndoor(data);
    } else if (data.type === "notes_changed") {
      received("notes");
      refreshNotes();
    } else if (data.type === "weather") {
      received("weather");
      weatherSwitch.checked = Boolean(data.enabled);
      save("weatherEnabled", weatherSwitch.checked);
      renderWeather(data.enabled ? data : null);
    } else if (data.type === "weather_error") {
      const minutes = Math.max(1, Math.ceil((Number(data.retryAfter) || 60) / 60));
      const reason = data.error === "rate_limited"
        ? "Hava sağlayıcısının istek sınırına ulaşıldı."
        : "Hava sağlayıcısına erişilemiyor.";
      feedback("weatherFeedback", `${reason} ${minutes} dakika sonra otomatik tekrar denenecek.`, true);
    } else if (data.type === "spotify") {
      received("spotify");
      // Display the shared device state without sharing the browser's OAuth credential.
      spotifySwitch.checked = Boolean(data.enabled);
      renderSpotify(data);
    }
  });
  connection.addEventListener("close", () => {
    if (socket !== connection) return;
    window.clearInterval(heartbeatTimer);
    deviceCount = 0;
    connectionAttempted = true;
    setConnectionState(false);
    reconnectTimer = window.setTimeout(connectSocket, 1500);
  });
  connection.addEventListener("error", () => connection.close());
}

let indoorState = null;
let indoorReceivedAt = 0;
function clearIndoor() {
  indoorState = null;
  renderIndoor();
}
function applyIndoor(data) {
  indoorState = data;
  indoorReceivedAt = Date.now();
  renderIndoor();
}
function renderIndoor() {
  const updated = Date.parse(indoorState?.lastUpdated || "");
  const valid = indoorState?.sensorAvailable === true &&
    Number.isFinite(indoorState.temperature) && Number.isFinite(indoorState.humidity) &&
    Number.isFinite(updated) && Date.now() - updated < indoorRules.staleAfterMs &&
    Date.now() - indoorReceivedAt < indoorRules.staleAfterMs;
  const temperature = valid ? `${indoorState.temperature.toFixed(1)} °C` : "--";
  const humidity = valid ? `%${Number(indoorState.humidity.toFixed(1))} Nem` : "--";
  const temperatureStatus = valid ? indoorState.temperatureStatus || "--" : "--";
  const humidityStatus = valid ? indoorState.humidityStatus || "--" : "--";
  for (const prefix of ["indoor", "indoorPreview"]) {
    ui(prefix + "Temperature").textContent = temperature;
    ui(prefix + "Humidity").textContent = humidity;
    ui(prefix + "TemperatureStatus").textContent = temperatureStatus;
    ui(prefix + "HumidityStatus").textContent = humidityStatus;
  }
  ui("indoorAvailability").textContent = valid ? `Son ölçüm: ${relativeTime(updated)}` : "Sensör verisi yok veya güncel değil.";
}
function initializeIndoorHelp() {
  for (const metric of ["temperature", "humidity"]) {
    const guide = ui(metric === "temperature" ? "indoorTemperatureGuide" : "indoorHumidityGuide");
    for (const rule of indoorRules[metric]) {
      const row = document.createElement("div");
      const label = document.createElement("dt"), description = document.createElement("dd");
      label.textContent = rule.label;
      description.textContent = rule.description;
      row.append(label, description); guide.append(row);
    }
  }
  ui("indoorAbout").addEventListener("click", () => ui("indoorAboutDialog").showModal());
  ui("indoorAboutClose").addEventListener("click", () => ui("indoorAboutDialog").close());
}

function renderSpotify(data) {
  renderSpotifyCard(data);
  const enabled = spotifySwitch.checked;
  if (previewPanel === "spotify") itemCount.textContent = enabled ? "ON" : "OFF";

  if (!enabled) {
    spotifyTrack.textContent = "Spotify kapalı";
    spotifyArtist.textContent = "Ev ekranında göstermeyi açın";
    spotifyAlbum.textContent = "";
    return;
  }

  if (!data?.title) {
    spotifyTrack.textContent = "Çalan şarkı yok";
    spotifyArtist.textContent = "Spotify’da bir parça başlatın";
    spotifyAlbum.textContent = "";
    return;
  }

  spotifyTrack.textContent = data.title || "Bilinmeyen şarkı";
  spotifyArtist.textContent = data.artist || "Bilinmeyen sanatçı";
  spotifyAlbum.textContent = `${data.playing ? "Çalıyor" : "Duraklatıldı"} · ${data.album || ""}`;
}

async function fetchSpotifyCurrent() {
  if (!spotifySession) {
    const error = new Error("Spotify Bagla ile hesabi bagla"); error.status = 401; throw error;
  }
  const response = await fetch(`${apiBaseUrl}/spotify/current`, {
    headers: {Authorization: `Bearer ${spotifySession}`}, signal: AbortSignal.timeout(30000), cache: "no-store"
  });
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(data.detail || "Spotify istegi basarisiz"); error.status = response.status; throw error;
  }
  if (data.session_token && data.session_token !== spotifySession) {
    spotifySession = data.session_token;
    save("spotifySession", spotifySession);
  }
  return data;
}

function iconForCondition(condition) {
  return {
    clear: "☀",
    partly_cloudy: "◐",
    cloudy: "☁",
    fog: "≋",
    rain: "╲",
    snow: "*",
    storm: "ϟ",
  }[condition] || "?";
}

function setWeatherConditionClass(condition) {
  const conditions = ["clear", "partly_cloudy", "cloudy", "fog", "rain", "snow", "storm", "unknown"];
  weatherPreview.classList.remove(...conditions.map((name) => `condition-${name}`));
  weatherPreview.classList.add(`condition-${condition || "unknown"}`);
}

function createWeatherEffect(condition) {
  const effect = document.createElement("div");
  effect.className = "weather-effect";
  effect.setAttribute("aria-hidden", "true");

  const particleCount = {
    clear: 8,
    partly_cloudy: 3,
    cloudy: 4,
    fog: 5,
    rain: 14,
    snow: 16,
    storm: 12,
  }[condition] || 3;

  for (let index = 0; index < particleCount; index += 1) {
    const particle = document.createElement("span");
    particle.style.setProperty("--i", index);
    particle.style.setProperty("--x", `${8 + ((index * 19) % 84)}%`);
    particle.style.setProperty("--delay", `${(index % 7) * -0.22}s`);
    effect.appendChild(particle);
  }

  return effect;
}

function renderWeather(data) {
  renderWeatherCard(data);
  const enabled = weatherSwitch.checked;
  if (previewPanel === "weather") itemCount.textContent = enabled ? "ON" : "OFF";

  if (!enabled) {
    setWeatherConditionClass("unknown");
    weatherPreview.querySelectorAll(".weather-effect").forEach((effect) => effect.remove());
    weatherIcon.textContent = "OFF";
    weatherTemp.textContent = "—";
    weatherLabel.textContent = "Ev ekranında göstermeyi açın";
    weatherForecast.innerHTML = "";
    return;
  }

  if (!data) {
    setWeatherConditionClass("unknown");
    weatherPreview.querySelectorAll(".weather-effect").forEach((effect) => effect.remove());
    weatherIcon.textContent = "SKY";
    weatherTemp.textContent = "—";
    weatherLabel.textContent = "Hava bilgisi bekleniyor";
    weatherForecast.innerHTML = "";
    return;
  }

  setWeatherConditionClass(data.condition);
  weatherIcon.textContent = iconForCondition(data.condition);
  weatherTemp.textContent = `${data.temperature} °C`;
  weatherLabel.textContent = `${data.stale ? "Eski veri · " : ""}${data.label} · Nem ${data.humidity}% · Rüzgâr ${data.wind} km/sa`;
  weatherForecast.innerHTML = "";
  weatherPreview.querySelectorAll(".weather-effect").forEach((effect) => effect.remove());
  weatherForecast.before(createWeatherEffect(data.condition));

  for (const day of data.forecast || []) {
    const item = document.createElement("div");
    item.className = "forecast-day";
    item.classList.add(`condition-${day.condition || "unknown"}`);
    const dayLabel = document.createElement("span"); dayLabel.textContent = day.day;
    const temperatures = document.createElement("strong"); temperatures.textContent = `${day.max}/${day.min} °C`;
    const rain = document.createElement("small"); rain.textContent = `${iconForCondition(day.condition)} ${day.rain}%`;
    item.append(dayLabel, temperatures, rain);
    weatherForecast.appendChild(item);
  }
}

async function fetchWeather() {
  const lat = encodeURIComponent(weatherLat.value || "41.0082");
  const lon = encodeURIComponent(weatherLon.value || "28.9784");
  const response = await fetch(`${apiBaseUrl}/weather?lat=${lat}&lon=${lon}`, {signal: AbortSignal.timeout(30000)});
  if (!response.ok) {
    throw new Error("Weather request failed");
  }
  return response.json();
}

async function sendWeatherCurrent() {
  if (!weatherSwitch.checked) {
    renderWeather(null);
    sendSocketPayload({ type: "weather", enabled: false });
    return;
  }

  if (!weatherLat.checkValidity() || !weatherLon.checkValidity() || !weatherLat.value.trim() || !weatherLon.value.trim()) {
    feedback("weatherFeedback", "Geçerli enlem (-90…90) ve boylam (-180…180) girin.", true);
    return;
  }
  if (weatherLoading) return;
  weatherLoading = true;
  refreshWeatherButton.disabled = true;
  refreshWeatherButton.textContent = "Yenileniyor…";
  feedback("weatherFeedback", "Hava bilgisi alınıyor…");
  try {
    const data = await fetchWeather();
    if (!weatherSwitch.checked) return;
    renderWeather(data);
    received("weather");
    feedback("weatherFeedback", data.stale ? "Eski hava verisi gösteriliyor; yenileme tekrar denenecek." : "Hava bilgisi alındı; sunucuya gönderiliyor.", Boolean(data.stale));
    sendSocketPayload({ ...data, enabled: true });
  } catch (error) {
    feedback("weatherFeedback", "Hava bilgisi yenilenemedi. Bağlantınızı kontrol edip tekrar deneyin.", true);
  } finally {
    weatherLoading = false;
    refreshWeatherButton.textContent = "Havayı yenile";
    refreshWeatherButton.disabled = !socket || socket.readyState !== WebSocket.OPEN;
  }
}

function setWeatherPolling(enabled) {
  save("weatherEnabled", enabled);
  save("weatherLat", weatherLat.value);
  save("weatherLon", weatherLon.value);
  window.clearInterval(weatherTimer);
  weatherTimer = undefined;

  if (enabled) {
    sendWeatherCurrent();
    weatherTimer = window.setInterval(sendWeatherCurrent, 15 * 60 * 1000);
  } else {
    sendWeatherCurrent();
  }
}

async function sendSpotifyCurrent() {
  if (spotifyLoading) return;
  if (!spotifySwitch.checked) { renderSpotify(null); return; }
  spotifyLoading = true;
  refreshSpotifyButton.disabled = true;
  refreshSpotifyButton.textContent = "Yenileniyor…";
  feedback("spotifyFeedback", "Parça bilgisi alınıyor…");
  try {
    const data = await fetchSpotifyCurrent();
    if (!spotifySwitch.checked) return;
    renderSpotify(data);
    received("spotify");
    feedback("spotifyFeedback", "Parça bilgisi yenilendi.");
  } catch (error) {
    const message = error.status === 401 ? "Spotify hesabınızı bağlayın veya yeniden yetkilendirin." : "Spotify bilgisi yenilenemedi. Tekrar deneyin.";
    feedback("spotifyFeedback", message, true);
    spotifyTrack.textContent = error.status === 401 ? "Spotify bağlantısı gerekli" : "Spotify bilgisi alınamadı";
    spotifyArtist.textContent = message;
    spotifyAlbum.textContent = "";
  } finally {
    spotifyLoading = false;
    refreshSpotifyButton.textContent = "Parçayı yenile";
    refreshSpotifyButton.disabled = !socket || socket.readyState !== WebSocket.OPEN;
  }
}

async function setSpotifyPolling(enabled) {
  save("spotifyEnabled", enabled);
  window.clearInterval(spotifyTimer);
  spotifyTimer = undefined;
  if (!spotifySession) { renderSpotify(null); await sendSpotifyCurrent(); return; }
  try {
    const response = await fetch(`${apiBaseUrl}/spotify/enabled`, {
      method: "POST", headers: {Authorization: `Bearer ${spotifySession}`, "Content-Type": "application/json"},
      body: JSON.stringify({enabled}), signal: AbortSignal.timeout(30000)
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Spotify bağlantısı gerekli");
    if (data.session_token && data.session_token !== spotifySession) {
      spotifySession = data.session_token;
      save("spotifySession", spotifySession);
    }
    renderSpotify(data);
  } catch (error) { spotifyTrack.textContent = error.message; feedback("spotifyFeedback", "Spotify ayarı kaydedilemedi. Hesabınızı yeniden bağlayın veya tekrar deneyin.", true); }
  // Playback polling belongs to FastAPI; this tab can now be closed.
}

tabs.indoor.addEventListener("click", () => setActivePanel("indoor"));
deviceTab.addEventListener("click", () => setActivePanel("device"));
Object.values(tabs).forEach((tab, index, all) => tab.addEventListener("keydown", event => {
  let next;
  if (event.key === "ArrowRight") next = (index + 1) % all.length;
  if (event.key === "ArrowLeft") next = (index + all.length - 1) % all.length;
  if (event.key === "Home") next = 0;
  if (event.key === "End") next = all.length - 1;
  if (next === undefined) return;
  event.preventDefault();
  setActivePanel(Object.keys(tabs)[next]); all[next].focus();
}));
notesTab.addEventListener("click", () => setActivePanel("notes"));
spotifyTab.addEventListener("click", () => setActivePanel("spotify"));
weatherTab.addEventListener("click", () => setActivePanel("weather"));
spotifySwitch.addEventListener("change", () => setSpotifyPolling(spotifySwitch.checked));
refreshSpotifyButton.addEventListener("click", sendSpotifyCurrent);
weatherSwitch.addEventListener("change", () => setWeatherPolling(weatherSwitch.checked));
refreshWeatherButton.addEventListener("click", sendWeatherCurrent);
initNotes();
setConnectionState(false);
setActivePanel("notes");
initializeIndoorHelp();
renderIndoor();
window.setInterval(renderIndoor, 1000);
renderSpotify(null);
renderWeather(null);
window.setInterval(updateSyncLabels, 1000);
connectSocket();

window.addEventListener("storage", event => {
  if (event.key === STORAGE_PREFIX + "spotifySession") {
    const wasConnected = Boolean(spotifySession);
    spotifySession = loadSaved("spotifySession", "");
    if (!wasConnected && spotifySession) { spotifySwitch.checked = true; setSpotifyPolling(true); }
  }
});
window.addEventListener("focus", () => {
  if (!socket || socket.readyState === WebSocket.CLOSED) { window.clearTimeout(reconnectTimer); connectSocket(); }
  refreshNotes();
  if (spotifySwitch.checked) sendSpotifyCurrent();
  if (weatherSwitch.checked) sendWeatherCurrent();
});

function displayValue(value, unit = "") {
  return value !== null && value !== undefined && Number.isFinite(Number(value)) ? `${value}${unit}` : "—";
}
function setArt(image, value) {
  let url = "";
  try { const parsed = new URL(value); if (parsed.protocol === "https:" && parsed.hostname === "i.scdn.co") url = parsed.href; } catch { /* No artwork available. */ }
  const usable = url && image.dataset.failedArt !== url;
  image.hidden = !usable;
  if (usable && image.getAttribute("src") !== url) image.src = url;
  if (!usable) image.removeAttribute("src");
  return Boolean(usable);
}
function formatDuration(value) {
  const seconds = Math.floor(Math.max(0, Number(value) || 0) / 1000);
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}
function renderSpotifyCard(data) {
  const enabled = spotifySwitch.checked;
  const hasTrack = enabled && Boolean(data?.title);
  ui("spotifyPlayback").textContent = !enabled ? "Kapalı" : hasTrack ? data.playing ? "Çalıyor" : "Duraklatıldı" : "Parça yok";
  ui("spotifyControlTrack").textContent = hasTrack ? data.title : "Şu anda çalan bir parça yok.";
  ui("spotifyControlArtist").textContent = hasTrack ? data.artist || "Sanatçı bilgisi yok" : !spotifySession ? "Spotify hesabınızı bağlayarak başlayın." : enabled ? "Spotify’da bir parça başlatın." : "Ev ekranında göstermeyi açarak başlayın.";
  ui("spotifyControlAlbum").textContent = hasTrack ? data.album || "" : "";
  ui("spotifyArtPlaceholder").hidden = setArt(ui("spotifyArt"), hasTrack ? data.album_art_url : "");
  setArt(ui("spotifyPreviewArt"), hasTrack ? data.album_art_url : "");
  const duration = Math.max(0, Number(data?.duration_ms) || 0);
  const progress = Math.min(duration, Math.max(0, Number(data?.progress_ms) || 0));
  ui("spotifyProgressArea").hidden = !hasTrack || !duration;
  ui("spotifyProgress").max = duration || 1;
  ui("spotifyProgress").value = progress;
  ui("spotifyElapsed").textContent = formatDuration(progress);
  ui("spotifyDuration").textContent = formatDuration(duration);
  ui("spotifyDevice").hidden = !hasTrack || !data.device_name;
  ui("spotifyDevice").textContent = hasTrack && data.device_name ? `Çalma cihazı: ${data.device_name}` : "";
  const messages = {authorization_required: "Spotify hesabınızı bağlayın veya yeniden yetkilendirin.", no_active_device: "Spotify’da bir çalma cihazı seçip bir parça başlatın.", scope_or_premium_required: "Spotify izinlerinizi ve hesap erişimini kontrol edin.", rate_limited: "Spotify istek sınırına ulaşıldı. Otomatik yenileme için biraz bekleyin."};
  if (data?.error) feedback("spotifyFeedback", messages[data.error] || "Spotify bilgisi alınamadı. Daha sonra tekrar deneyin.", true);
  else feedback("spotifyFeedback", "");
}
function renderWeatherCard(data) {
  const available = weatherSwitch.checked && Boolean(data);
  ui("weatherLocation").textContent = available && data.location ? data.location : "Konum bekleniyor";
  const condition = available ? data.condition : "unknown";
  const symbols = {clear: "weather-clear", partly_cloudy: "icon-weather", cloudy: "weather-cloudy", rain: "weather-rain", fog: "weather-fog", snow: "weather-snow", storm: "weather-storm"};
  ui("weatherControlIcon").innerHTML = symbols[condition] ? `<svg class="icon" aria-hidden="true"><use href="#${symbols[condition]}"/></svg>` : "—";
  ui("weatherControlTemp").textContent = available ? displayValue(data.temperature, " °C") : "—";
  ui("weatherControlLabel").textContent = available ? `${data.stale ? "Eski veri · " : ""}${data.label || "Durum bilgisi yok"}` : weatherSwitch.checked ? "Hava bilgisi bekleniyor." : "Ev ekranında göstermeyi açarak başlayın.";
  ui("weatherHumidity").textContent = available ? displayValue(data.humidity, "%") : "—";
  ui("weatherWind").textContent = available ? displayValue(data.wind, " km/sa") : "—";
  ui("weatherControlForecast").replaceChildren();
  if (available) for (const day of data.forecast || []) {
    const card = document.createElement("div"); card.className = "forecast-day";
    for (const [tag, value] of [["span", day.day], ["strong", `${displayValue(day.max)} / ${displayValue(day.min)} °C`], ["small", `${iconForCondition(day.condition)} · ${displayValue(day.rain, "%")}`]]) {
      const element = document.createElement(tag); element.textContent = value; card.appendChild(element);
    }
    ui("weatherControlForecast").appendChild(card);
  }
}
ui("spotifyArt").addEventListener("error", () => { ui("spotifyArt").dataset.failedArt = ui("spotifyArt").getAttribute("src"); ui("spotifyArt").hidden = true; ui("spotifyArtPlaceholder").hidden = false; });
ui("spotifyPreviewArt").addEventListener("error", () => { ui("spotifyPreviewArt").dataset.failedArt = ui("spotifyPreviewArt").getAttribute("src"); ui("spotifyPreviewArt").hidden = true; });
