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
const spotifySwitch = document.querySelector("#spotifySwitch");
const refreshSpotifyButton = document.querySelector("#refreshSpotifyButton");

let socket;
let reconnectTimer;
let spotifyTimer;
let activePanel = "shopping";
const shoppingItems = [];
const maxItems = 8;

function setConnectionState(isConnected) {
  connectionStatus.classList.toggle("connected", isConnected);
  connectionStatus.classList.toggle("disconnected", !isConnected);
  connectionStatus.innerHTML = `<span aria-hidden="true">&bull;</span> ${
    isConnected ? "Connected" : "Disconnected"
  }`;
  sendButton.disabled = !isConnected;
  clearButton.disabled = !isConnected;
  spotifySwitch.disabled = !isConnected;
  refreshSpotifyButton.disabled = !isConnected;
}

function setActivePanel(panel) {
  activePanel = panel;
  const isSpotify = panel === "spotify";
  shoppingTab.classList.toggle("active", !isSpotify);
  spotifyTab.classList.toggle("active", isSpotify);
  shoppingControls.hidden = isSpotify;
  spotifyControls.hidden = !isSpotify;
  shoppingPreview.hidden = isSpotify;
  spotifyPreview.hidden = !isSpotify;
  previewTitle.textContent = isSpotify ? "SPOTIFY" : "SHOPPING LIST";
  itemCount.textContent = isSpotify ? (spotifySwitch.checked ? "ON" : "OFF") : shoppingItems.length.toString();
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
  const enabled = spotifySwitch.checked;
  itemCount.textContent = enabled ? "ON" : "OFF";

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
  const response = await fetch("https://realtime-display.onrender.com/spotify/current");
  if (!response.ok) {
    throw new Error("Spotify current request failed");
  }
  return response.json();
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
spotifySwitch.addEventListener("change", () => setSpotifyPolling(spotifySwitch.checked));
refreshSpotifyButton.addEventListener("click", sendSpotifyCurrent);
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
