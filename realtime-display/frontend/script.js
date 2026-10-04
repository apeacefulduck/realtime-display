const socketUrl = "wss://realtime-display.onrender.com/ws?role=browser";
const itemInput = document.querySelector("#itemInput");
const sendButton = document.querySelector("#sendButton");
const clearButton = document.querySelector("#clearButton");
const connectionStatus = document.querySelector("#connectionStatus");
const shoppingPreview = document.querySelector("#shoppingPreview");
const spotifyPreview = document.querySelector("#spotifyPreview");
const spotifyTrack = document.querySelector("#spotifyTrack");
const spotifyArtist = document.querySelector("#spotifyArtist");
const spotifyAlbum = document.querySelector("#spotifyAlbum");
const itemCount = document.querySelector("#itemCount");
const previewTitle = document.querySelector("#previewTitle");
const shoppingTab = document.querySelector("#shoppingTab");
const spotifyTab = document.querySelector("#spotifyTab");
const shoppingControls = document.querySelector("#shoppingControls");
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
let activePanel = "shopping";
const STORAGE_PREFIX = "homeflow:";
function loadSaved(name, fallback) {
  try { const raw = localStorage.getItem(STORAGE_PREFIX + name); return raw === null ? fallback : JSON.parse(raw); }
  catch { return fallback; }
}
function save(name, value) {
  try { localStorage.setItem(STORAGE_PREFIX + name, JSON.stringify(value)); } catch { /* Storage may be disabled. */ }
}
const savedItems = loadSaved("shopping", null);
let hasShoppingSnapshot = Array.isArray(savedItems);
const shoppingItems = hasShoppingSnapshot ? savedItems.filter(item => typeof item === "string").slice(-8) : [];
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
const maxItems = 8;
const apiBaseUrl = "https://realtime-display.onrender.com";

function setConnectionState(isConnected) {
  connectionStatus.classList.toggle("connected", isConnected);
  connectionStatus.classList.toggle("disconnected", !isConnected);
  connectionStatus.innerHTML = `<span aria-hidden="true">&bull;</span> ${
    isConnected ? (deviceCount > 0 ? "ESP32 bagli" : "Sunucu bagli; ESP32 bekleniyor") : "Baglanti kuruluyor; liste saklaniyor"
  }`;
  sendButton.disabled = false;
  clearButton.disabled = false;
  spotifySwitch.disabled = false;
  refreshSpotifyButton.disabled = !isConnected;
  weatherSwitch.disabled = false;
  refreshWeatherButton.disabled = !isConnected;
}

function setActivePanel(panel) {
  activePanel = panel;
  const isSpotify = panel === "spotify";
  const isWeather = panel === "weather";
  const isShopping = panel === "shopping";
  shoppingTab.classList.toggle("active", isShopping);
  spotifyTab.classList.toggle("active", isSpotify);
  weatherTab.classList.toggle("active", isWeather);
  shoppingControls.hidden = !isShopping;
  spotifyControls.hidden = !isSpotify;
  weatherControls.hidden = !isWeather;
  shoppingPreview.hidden = !isShopping;
  spotifyPreview.hidden = !isSpotify;
  weatherPreview.hidden = !isWeather;
  previewTitle.textContent = isWeather ? "WEATHER" : isSpotify ? "SPOTIFY" : "SHOPPING LIST";
  itemCount.textContent = isWeather
    ? (weatherSwitch.checked ? "ON" : "OFF")
    : isSpotify
      ? (spotifySwitch.checked ? "ON" : "OFF")
      : shoppingItems.length.toString();
}

function renderShoppingList() {
  shoppingPreview.innerHTML = "";
  if (activePanel === "shopping") itemCount.textContent = shoppingItems.length.toString();

  if (shoppingItems.length === 0) {
    const emptyItem = document.createElement("li");
    emptyItem.className = "empty";
    emptyItem.textContent = "Liste bos";
    shoppingPreview.appendChild(emptyItem);
    return;
  }

  for (const item of shoppingItems) {
    const listItem = document.createElement("li");
    listItem.textContent = item;
    shoppingPreview.appendChild(listItem);
  }
}

function sendSocketPayload(payload) {
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    pendingPayloads.set(payload.type, payload);
    return false;
  }

  socket.send(JSON.stringify(payload));
  return true;
}

function getDisplayText() {
  if (shoppingItems.length === 0) {
    return "";
  }

  return `ALISVERIS LISTESI\n${shoppingItems
    .map((item, index) => `${index + 1}. ${item}`)
    .join("\n")}`;
}

function connectSocket() {
  const connection = new WebSocket(socketUrl);
  socket = connection;
  connection.addEventListener("open", () => {
    if (socket !== connection) return;
    window.clearTimeout(reconnectTimer);
    lastPongAt = Date.now();
    setConnectionState(true);
    for (const payload of pendingPayloads.values()) sendSocketPayload(payload);
    pendingPayloads.clear();
    if (hasShoppingSnapshot) sendShoppingList();
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
    } else if (data.type === "display") {
      const lines = String(data.text || "").split("\n");
      if (lines[0] === "ALISVERIS LISTESI") lines.shift();
      shoppingItems.splice(0, shoppingItems.length, ...lines.filter(Boolean).map(line => line.replace(/^\d+\.\s*/, "")).slice(-maxItems));
      hasShoppingSnapshot = true;
      save("shopping", shoppingItems);
      renderShoppingList();
    } else if (data.type === "weather") {
      weatherSwitch.checked = Boolean(data.enabled);
      save("weatherEnabled", weatherSwitch.checked);
      renderWeather(data.enabled ? data : null);
    } else if (data.type === "spotify") {
      // Display the shared device state without sharing the browser's OAuth credential.
      spotifySwitch.checked = Boolean(data.enabled);
      renderSpotify(data);
    }
  });
  connection.addEventListener("close", () => {
    if (socket !== connection) return;
    window.clearInterval(heartbeatTimer);
    deviceCount = 0;
    setConnectionState(false);
    reconnectTimer = window.setTimeout(connectSocket, 1500);
  });
  connection.addEventListener("error", () => connection.close());
}

function sendShoppingList() {
  hasShoppingSnapshot = true;
  save("shopping", shoppingItems);
  sendSocketPayload({
    type: "display",
    text: getDisplayText(),
    color: "white",
    size: 3,
  });
}

function renderSpotify(data) {
  const enabled = spotifySwitch.checked;
  if (activePanel === "spotify") itemCount.textContent = enabled ? "ON" : "OFF";

  if (!enabled) {
    spotifyTrack.textContent = "Spotify kapali";
    spotifyArtist.textContent = "Switch'i acinca ESP32'ye gonderilir";
    spotifyAlbum.textContent = "";
    return;
  }

  if (!data?.playing) {
    spotifyTrack.textContent = "Calan sarki yok";
    spotifyArtist.textContent = "Spotify'da bir parca baslat";
    spotifyAlbum.textContent = "";
    return;
  }

  spotifyTrack.textContent = data.title || "Bilinmeyen sarki";
  spotifyArtist.textContent = data.artist || "Bilinmeyen sanatci";
  spotifyAlbum.textContent = data.album || "";
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
  const enabled = weatherSwitch.checked;
  if (activePanel === "weather") itemCount.textContent = enabled ? "ON" : "OFF";

  if (!enabled) {
    setWeatherConditionClass("unknown");
    weatherPreview.querySelectorAll(".weather-effect").forEach((effect) => effect.remove());
    weatherIcon.textContent = "OFF";
    weatherTemp.textContent = "-- C";
    weatherLabel.textContent = "Switch'i acinca ESP32'ye gonderilir";
    weatherForecast.innerHTML = "";
    return;
  }

  if (!data) {
    setWeatherConditionClass("unknown");
    weatherPreview.querySelectorAll(".weather-effect").forEach((effect) => effect.remove());
    weatherIcon.textContent = "SKY";
    weatherTemp.textContent = "-- C";
    weatherLabel.textContent = "Hava bilgisi alinamadi";
    weatherForecast.innerHTML = "";
    return;
  }

  setWeatherConditionClass(data.condition);
  weatherIcon.textContent = iconForCondition(data.condition);
  weatherTemp.textContent = `${data.temperature} C`;
  weatherLabel.textContent = `${data.label} - Nem ${data.humidity}% - Ruzgar ${data.wind} km/s`;
  weatherForecast.innerHTML = "";
  weatherPreview.querySelectorAll(".weather-effect").forEach((effect) => effect.remove());
  weatherForecast.before(createWeatherEffect(data.condition));

  for (const day of data.forecast || []) {
    const item = document.createElement("div");
    item.className = "forecast-day";
    item.classList.add(`condition-${day.condition || "unknown"}`);
    item.innerHTML = `<span>${day.day}</span><strong>${day.max}/${day.min} C</strong><small>${iconForCondition(day.condition)} ${day.rain}%</small>`;
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

  try {
    const data = await fetchWeather();
    renderWeather(data);
    sendSocketPayload({ ...data, enabled: true });
  } catch (error) {
    renderWeather(null);
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
  if (!spotifySwitch.checked) {
    renderSpotify(null);
    sendSocketPayload({ type: "spotify", enabled: false });
    return;
  }

  try {
    const data = await fetchSpotifyCurrent();
    renderSpotify(data);
    sendSocketPayload({
      type: "spotify",
      enabled: true,
      playing: data.playing,
      title: data.title || "",
      artist: data.artist || "",
      album: data.album || "",
    });
  } catch (error) {
    spotifyTrack.textContent = error.status === 401 ? "Spotify baglantisi gerekli" : "Spotify bilgisi alinamadi";
    spotifyArtist.textContent = error.status === 401 ? "Spotify Bagla ile yeniden baglan" : `Sunucu hatasi${error.status ? ` (${error.status})` : ""}; yeniden denenecek`;
    spotifyAlbum.textContent = "";
  }
}

function setSpotifyPolling(enabled) {
  save("spotifyEnabled", enabled);
  window.clearInterval(spotifyTimer);
  spotifyTimer = undefined;

  if (enabled) {
    sendSpotifyCurrent();
    spotifyTimer = window.setInterval(sendSpotifyCurrent, 10000);
  } else {
    sendSpotifyCurrent();
  }
}

function addItemAndSend() {
  const item = itemInput.value.trim();

  if (!item) {
    return;
  }

  if (shoppingItems.length >= maxItems) {
    shoppingItems.shift();
  }

  shoppingItems.push(item);
  itemInput.value = "";
  renderShoppingList();
  sendShoppingList();
}

function clearShoppingList() {
  shoppingItems.length = 0;
  itemInput.value = "";
  renderShoppingList();
  sendShoppingList();
  itemInput.focus();
}

sendButton.addEventListener("click", addItemAndSend);
clearButton.addEventListener("click", clearShoppingList);
shoppingTab.addEventListener("click", () => setActivePanel("shopping"));
spotifyTab.addEventListener("click", () => setActivePanel("spotify"));
weatherTab.addEventListener("click", () => setActivePanel("weather"));
spotifySwitch.addEventListener("change", () => setSpotifyPolling(spotifySwitch.checked));
refreshSpotifyButton.addEventListener("click", sendSpotifyCurrent);
weatherSwitch.addEventListener("change", () => setWeatherPolling(weatherSwitch.checked));
refreshWeatherButton.addEventListener("click", sendWeatherCurrent);
itemInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    addItemAndSend();
  }
});

renderShoppingList();
setConnectionState(false);
setActivePanel("shopping");
connectSocket();

window.addEventListener("storage", event => {
  if (event.key === STORAGE_PREFIX + "spotifySession") {
    const wasConnected = Boolean(spotifySession);
    spotifySession = loadSaved("spotifySession", "");
    if (!wasConnected && spotifySession) { spotifySwitch.checked = true; setSpotifyPolling(true); }
  }
});
window.addEventListener("focus", () => {
  if (!socket || socket.readyState === WebSocket.CLOSED) connectSocket();
  if (spotifySwitch.checked) sendSpotifyCurrent();
  if (weatherSwitch.checked) sendWeatherCurrent();
});
