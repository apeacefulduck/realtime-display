const socketUrl = `ws://${window.location.hostname || "localhost"}:8000/ws`;

const itemInput = document.querySelector("#itemInput");
const sendButton = document.querySelector("#sendButton");
const clearButton = document.querySelector("#clearButton");
const connectionStatus = document.querySelector("#connectionStatus");
const shoppingPreview = document.querySelector("#shoppingPreview");
const itemCount = document.querySelector("#itemCount");

let socket;
let reconnectTimer;
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
  if (socket.readyState !== WebSocket.OPEN) {
    return;
  }

  socket.send(
    JSON.stringify({
      type: "display",
      text: getDisplayText(),
      color: "white",
      size: 3,
    }),
  );
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
itemInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    addItemAndSend();
  }
});

renderShoppingList();
setConnectionState(false);
connectSocket();
