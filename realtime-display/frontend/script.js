const socketUrl = "wss://realtime-display.onrender.com/ws";
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
const refreshSpotifyButton = document.querySelector("#refreshSpotifyButton");
const weatherTab = document.querySelector("#weatherTab");
const weatherControls = document.querySelector("#weatherControls");
const weatherPreview = document.querySelector("#weatherPreview");
const weatherIcon = document.querySelector("#weatherIcon");
const weatherTemp = document.querySelector("#weatherTemp");
const weatherLabel = document.querySelector("#weatherLabel");
const weatherForecast = document.querySelector("#weatherForecast");
const refreshWeatherButton = document.querySelector("#refreshWeatherButton");
const weatherLat = document.querySelector("#weatherLat");
const weatherLon = document.querySelector("#weatherLon");

let socket;
let reconnectTimer;
let spotifyTimer;
let weatherTimer;
let activePanel = "shopping";
const shoppingItems = [];
const maxItems = 8;
const apiBaseUrl = "https://realtime-display.onrender.com";
const forecastPreviewDays = 4;

function setConnectionState(isConnected) {
  connectionStatus.classList.toggle("connected", isConnected);
  connectionStatus.classList.toggle("disconnected", !isConnected);
  connectionStatus.innerHTML = `<span aria-hidden="true">&bull;</span> ${
    isConnected ? "Connected" : "Disconnected"
  }`;
  sendButton.disabled = !isConnected;
  clearButton.disabled = !isConnected;
  refreshSpotifyButton.disabled = !isConnected;
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
    ? "AUTO"
    : isSpotify
      ? "AUTO"
      : shoppingItems.length.toString();
}

function renderShoppingList() {
  shoppingPreview.innerHTML = "";
  itemCount.textContent = shoppingItems.length.toString();

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
    return;
  }

  socket.send(JSON.stringify(payload));
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
  socket = new WebSocket(socketUrl);

  socket.addEventListener("open", () => {
    window.clearTimeout(reconnectTimer);
    setConnectionState(true);
    setSpotifyPolling(true);
    setWeatherPolling(true);
  });

  socket.addEventListener("close", () => {
    setConnectionState(false);
    reconnectTimer = window.setTimeout(connectSocket, 1500);
  });

  socket.addEventListener("error", () => {
    socket.close();
  });
}

function sendShoppingList() {
  sendSocketPayload({
    type: "display",
    text: getDisplayText(),
    color: "white",
    size: 3,
  });
}

function renderSpotify(data) {
  itemCount.textContent = "AUTO";

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
  const response = await fetch(`${apiBaseUrl}/spotify/current`);
  if (!response.ok) {
    throw new Error("Spotify current request failed");
  }
  return response.json();
}

function iconForCondition(condition) {
  return {
    clear: "SUN",
    partly_cloudy: "PART",
    cloudy: "CLD",
    fog: "FOG",
    rain: "RAIN",
    snow: "*",
    storm: "STORM",
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
    particle.style.setProperty("--y", `${12 + ((index * 23) % 60)}%`);
    particle.style.setProperty("--delay", `${(index % 7) * -0.22}s`);
    effect.appendChild(particle);
  }

  return effect;
}

function renderWeather(data) {
  itemCount.textContent = "AUTO";

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

  for (const [index, day] of (data.forecast || []).slice(0, forecastPreviewDays).entries()) {
    const item = document.createElement("div");
    item.className = "forecast-day";
    if (index === 0) item.classList.add("today");
    item.classList.add(`condition-${day.condition || "unknown"}`);
    item.innerHTML = `<span>${index === 0 ? "Bugun" : day.day}</span><strong>${day.max}/${day.min} C</strong><small>${iconForCondition(day.condition)} ${day.rain}%</small>`;
    weatherForecast.appendChild(item);
  }
}

async function fetchWeather() {
  const lat = encodeURIComponent(weatherLat.value || "41.0082");
  const lon = encodeURIComponent(weatherLon.value || "28.9784");
  const response = await fetch(`${apiBaseUrl}/weather?lat=${lat}&lon=${lon}`);
  if (!response.ok) {
    throw new Error("Weather request failed");
  }
  return response.json();
}

async function sendWeatherCurrent() {
  try {
    const data = await fetchWeather();
    renderWeather(data);
    sendSocketPayload({ ...data, enabled: true });
  } catch (error) {
    renderWeather(null);
  }
}

function setWeatherPolling(enabled) {
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
    spotifyTrack.textContent = "Spotify baglantisi gerekli";
    spotifyArtist.textContent = "Spotify Bagla ile hesabi yetkilendir";
    spotifyAlbum.textContent = "";
  }
}

function setSpotifyPolling(enabled) {
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
refreshSpotifyButton.addEventListener("click", sendSpotifyCurrent);
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
