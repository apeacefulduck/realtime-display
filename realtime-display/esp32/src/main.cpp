// ============================================================================
// HomeFlow Display — ESP32 + ILI9341 — Shopping List Edition (v3)
// ----------------------------------------------------------------------------
// This version is matched to the REAL protocol seen in script.js / main.py:
//
//   Every add/clear from the web UI sends the FULL list as one message:
//     { "type": "display", "text": "ALISVERIS LISTESI\n1. Ekmek\n2. Sut",
//       "color": "white", "size": 3 }
//   Clearing the list sends the same shape with text = "" (empty string).
//
// FIX #1 — overlap / garbled text on rapid updates:
//   The old wrapText() only split on spaces, so the '\n' characters in the
//   text above were swallowed into "words" and cut/drawn in the wrong
//   places. Text is now split on '\n' FIRST into real lines, each of which
//   is stored as one shopping-list line and re-wrapped individually only if
//   it's too wide for the screen. Every update fully clears the content
//   area and redraws the complete current list from scratch (idempotent
//   full re-render) — so successive updates can no longer overlap.
//
// FIX #2 — "Listeyi Sifirla" not clearing the TFT:
//   This was actually a BACKEND bug: main.py only broadcast a message when
//   display_message.text was truthy, so the empty text sent on clear was
//   silently dropped and never reached the ESP32. Fixed in main.py (see the
//   accompanying file) — broadcasting is now unconditional. On the ESP32
//   side, text == "" is treated as "the list is empty" and clears the card.
//
// NOTE ON TURKISH CHARACTERS: standard Adafruit_GFX FreeSans fonts only
// cover Latin-1 (ü, ö, ç render fine) but NOT ş, ğ, ı, İ. Your current UI
// strings (script.js, main.py) don't use those characters, so this is not
// currently an issue — but if a user later types an item name with ş/ğ/ı,
// those glyphs may render as blanks/boxes. Flagging for awareness only.
//
// Networking logic (WiFi, WebSocket) is otherwise unchanged from your setup.
// ============================================================================

#include <ArduinoJson.h>
#include <WiFi.h>
#include <WebSocketsClient.h>
#include <Adafruit_GFX.h>
#include <Adafruit_ILI9341.h>

// ---- Fonts ------------------------------------------------------------
#include <Fonts/FreeSansBold12pt7b.h>   // Header / logo ("HomeFlow")
#include <Fonts/FreeSansBold9pt7b.h>    // Empty-state / connecting messages
#include <Fonts/FreeSans9pt7b.h>        // List items / status / footer

// ============================================================================
// NETWORK CONFIG — UNCHANGED
// ============================================================================
const char *WIFI_SSID = "apeacefulduck5";
const char *WIFI_PASSWORD = "Muslera26";
const char *WEBSOCKET_HOST = "192.168.1.103";
const uint16_t WEBSOCKET_PORT = 8000;
const char *WEBSOCKET_PATH = "/ws";

const uint8_t TFT_CS_PIN = 5;
const uint8_t TFT_DC_PIN = 2;
const uint8_t TFT_RST_PIN = 4;

const uint16_t WIFI_RETRY_DELAY_MS = 500;
const uint16_t WEBSOCKET_RECONNECT_MS = 3000;

// ============================================================================
// DESIGN SYSTEM — COLORS (RGB565)
// ============================================================================
const uint16_t COLOR_BACKGROUND   = 0x08A5; // #0F172A
const uint16_t COLOR_CARD_BG      = 0x1947; // #1E293B
const uint16_t COLOR_BORDER       = 0x320A; // #334155
const uint16_t COLOR_TEXT_PRIMARY = 0xFFFF; // #FFFFFF
const uint16_t COLOR_TEXT_SECOND  = 0x9517; // #94A3B8
const uint16_t COLOR_ACCENT_BLUE  = 0x3C1E; // #3B82F6
const uint16_t COLOR_SUCCESS      = 0x262B; // #22C55E
const uint16_t COLOR_WARNING      = 0xFB82; // #F97316
const uint16_t COLOR_ERROR        = 0xEA28; // #EF4444

// ============================================================================
// LAYOUT CONSTANTS
// ============================================================================
const uint8_t CARD_MARGIN      = 18;
const uint8_t CARD_RADIUS      = 10;
const uint8_t CARD_PADDING     = 16;
const uint8_t HEADER_OFFSET_Y  = 16;
const uint8_t DIVIDER_OFFSET_Y = 42;
const uint8_t FOOTER_HEIGHT    = 26;
const uint8_t DOT_RADIUS       = 3;
const uint8_t DOT_SPACING      = 16;

// Mirrors script.js's `const maxItems = 8;` so the two sides can never
// disagree about how many lines a full snapshot can contain.
const uint8_t MAX_ITEMS = 8;
String shoppingLines[MAX_ITEMS];   // already-formatted lines, e.g. "1. Ekmek"
uint8_t shoppingLineCount = 0;

// The web app always sends this exact title as the first line; we recognize
// and skip it since the card header already shows the "HomeFlow" brand.
const char *LIST_TITLE = "ALISVERIS LISTESI";

Adafruit_ILI9341 tft(TFT_CS_PIN, TFT_DC_PIN, TFT_RST_PIN);
WebSocketsClient webSocket;

enum ConnectionState {
  STATE_CONNECTING,
  STATE_CONNECTED,
  STATE_DISCONNECTED,
  STATE_ERROR
};

ConnectionState currentState = STATE_CONNECTING;

// ============================================================================
// LOW-LEVEL LAYOUT HELPERS
// ============================================================================
int16_t cardX() { return CARD_MARGIN; }
int16_t cardY() { return CARD_MARGIN; }
int16_t cardW() { return tft.width() - CARD_MARGIN * 2; }
int16_t cardH() { return tft.height() - CARD_MARGIN * 2; }

int16_t contentY() { return cardY() + DIVIDER_OFFSET_Y + 10; }
int16_t contentH() { return cardH() - DIVIDER_OFFSET_Y - 10 - FOOTER_HEIGHT - 6; }
int16_t contentX() { return cardX() + CARD_PADDING; }
int16_t contentW() { return cardW() - CARD_PADDING * 2; }

int16_t footerY() { return cardY() + cardH() - FOOTER_HEIGHT; }
int16_t footerH() { return FOOTER_HEIGHT - 6; }

uint16_t accentForState(ConnectionState state) {
  switch (state) {
    case STATE_CONNECTED:    return COLOR_SUCCESS;
    case STATE_DISCONNECTED: return COLOR_ERROR;
    case STATE_ERROR:        return COLOR_WARNING;
    case STATE_CONNECTING:
    default:                 return COLOR_ACCENT_BLUE;
  }
}

String labelForState(ConnectionState state) {
  switch (state) {
    case STATE_CONNECTED:    return "Baglandi";
    case STATE_DISCONNECTED: return "Baglanti yok";
    case STATE_ERROR:        return "Hata";
    case STATE_CONNECTING:
    default:                 return "Baglaniyor";
  }
}

// Maps the optional "color" field from the JSON payload to a display color.
// script.js always sends "white" today, but this keeps the ESP32 ready if
// that ever changes (e.g. a future "urgent" item in red).
uint16_t mapDisplayColor(const String &colorName) {
  String c = colorName;
  c.toLowerCase();
  if (c == "red")    return COLOR_ERROR;
  if (c == "green")  return COLOR_SUCCESS;
  if (c == "orange") return COLOR_WARNING;
  if (c == "blue")   return COLOR_ACCENT_BLUE;
  if (c == "gray" || c == "grey") return COLOR_TEXT_SECOND;
  return COLOR_TEXT_PRIMARY; // "white" and anything unrecognized
}

// ============================================================================
// TEXT CENTERING / MEASUREMENT HELPERS
// ============================================================================
void getCenteredCursor(const String &text, int16_t centerX, int16_t centerY,
                        int16_t &cursorX, int16_t &cursorY) {
  int16_t x1, y1;
  uint16_t w, h;
  tft.getTextBounds(text, 0, 0, &x1, &y1, &w, &h);
  cursorX = centerX - (int16_t)(w / 2) - x1;
  cursorY = centerY - (int16_t)(h / 2) - y1;
}

void drawCenteredText(const String &text, int16_t centerX, int16_t centerY, uint16_t color) {
  int16_t cx, cy;
  getCenteredCursor(text, centerX, centerY, cx, cy);
  tft.setTextColor(color);
  tft.setCursor(cx, cy);
  tft.print(text);
}

int16_t measureLineHeight() {
  int16_t x1, y1;
  uint16_t w, h;
  tft.getTextBounds("Ag", 0, 0, &x1, &y1, &w, &h);
  return h + 8;
}

// ============================================================================
// WORD WRAPPING — space-based, for wrapping a SINGLE line (no '\n' inside).
// Splitting on '\n' happens one level up, in splitIntoLines(). This
// separation is exactly what the old code was missing.
// ============================================================================
int wrapText(const String &message, String lines[], int maxLines, int16_t maxWidth) {
  int lineCount = 0;
  String currentLine = "";
  int wordStart = 0;
  int16_t x1, y1;
  uint16_t w, h;

  while (wordStart < (int)message.length() && lineCount < maxLines) {
    int wordEnd = message.indexOf(' ', wordStart);
    if (wordEnd == -1) wordEnd = message.length();

    String word = message.substring(wordStart, wordEnd);
    String candidate = currentLine.length() == 0 ? word : currentLine + " " + word;

    tft.getTextBounds(candidate, 0, 0, &x1, &y1, &w, &h);

    if (w <= maxWidth) {
      currentLine = candidate;
    } else {
      if (currentLine.length() > 0) lines[lineCount++] = currentLine;

      while (word.length() > 0 && lineCount < maxLines) {
        int splitAt = word.length();
        String piece = word;
        tft.getTextBounds(piece, 0, 0, &x1, &y1, &w, &h);
        while (w > maxWidth && splitAt > 1) {
          splitAt--;
          piece = word.substring(0, splitAt);
          tft.getTextBounds(piece, 0, 0, &x1, &y1, &w, &h);
        }
        if (splitAt == (int)word.length()) {
          currentLine = word;
          word = "";
        } else {
          lines[lineCount++] = piece;
          word = word.substring(splitAt);
        }
      }
    }
    wordStart = wordEnd + 1;
  }

  if (currentLine.length() > 0 && lineCount < maxLines) lines[lineCount++] = currentLine;
  return lineCount;
}

// ============================================================================
// CARD / HEADER / FOOTER
// ============================================================================
void drawCard() {
  tft.fillRoundRect(cardX(), cardY(), cardW(), cardH(), CARD_RADIUS, COLOR_CARD_BG);
  tft.drawRoundRect(cardX(), cardY(), cardW(), cardH(), CARD_RADIUS, COLOR_BORDER);
}

void drawHeader(uint16_t accentColor) {
  tft.setFont(&FreeSansBold12pt7b);
  tft.setTextSize(1);
  tft.setTextWrap(false);
  drawCenteredText("HomeFlow", tft.width() / 2, cardY() + HEADER_OFFSET_Y + 10, COLOR_ACCENT_BLUE);

  // Status dot reflects live connection state; redrawing it alone (same
  // position/size, only the fill color changes) never needs a background clear.
  tft.fillCircle(cardX() + cardW() - 18, cardY() + 18, 4, accentColor);

  tft.drawFastHLine(cardX() + CARD_PADDING, cardY() + DIVIDER_OFFSET_Y,
                     cardW() - CARD_PADDING * 2, COLOR_BORDER);
}

void clearContentArea() {
  tft.fillRect(contentX(), contentY(), contentW(), contentH(), COLOR_CARD_BG);
}

void clearFooterArea() {
  tft.fillRect(cardX() + 1, footerY(), cardW() - 2, footerH(), COLOR_CARD_BG);
}

void drawStatus(const String &text, uint16_t color, int16_t centerY) {
  tft.setFont(&FreeSansBold9pt7b);
  tft.setTextSize(1);
  tft.setTextWrap(false);
  drawCenteredText(text, tft.width() / 2, centerY, color);
}

void drawFooter(const String &text, uint16_t dotColor) {
  clearFooterArea();
  int16_t dotX = cardX() + CARD_PADDING;
  int16_t dotY = footerY() + footerH() / 2;
  tft.fillCircle(dotX, dotY, DOT_RADIUS, dotColor);

  tft.setFont(&FreeSans9pt7b);
  tft.setTextSize(1);
  tft.setTextWrap(false);
  tft.setTextColor(COLOR_TEXT_SECOND);
  int16_t x1, y1; uint16_t w, h;
  tft.getTextBounds(text, 0, 0, &x1, &y1, &w, &h);
  int16_t cursorY = dotY - (h / 2) - y1;
  tft.setCursor(dotX + DOT_RADIUS + 8, cursorY);
  tft.print(text);
}

void drawLoading(uint8_t activeIndex, int16_t centerY) {
  int16_t totalWidth = DOT_SPACING * 2;
  int16_t startX = tft.width() / 2 - totalWidth / 2;

  tft.fillRect(startX - DOT_RADIUS - 2, centerY - DOT_RADIUS - 2,
               totalWidth + DOT_RADIUS * 2 + 4, DOT_RADIUS * 2 + 4, COLOR_CARD_BG);

  for (uint8_t i = 0; i < 3; i++) {
    int16_t dx = startX + i * DOT_SPACING;
    uint16_t color = (i == activeIndex) ? COLOR_ACCENT_BLUE : COLOR_BORDER;
    tft.fillCircle(dx, centerY, DOT_RADIUS, color);
  }
}

void drawShell(ConnectionState state) {
  currentState = state;
  tft.fillScreen(COLOR_BACKGROUND);
  drawCard();
  drawHeader(accentForState(state));
}

// ============================================================================
// SHOPPING LIST — split, store, render
// ----------------------------------------------------------------------------
// splitIntoLines() is the actual bug fix: it breaks the incoming text on
// '\n' FIRST (real lines), skips the recognized title line, and keeps the
// rest verbatim (they already arrive pre-numbered as "1. Ekmek" etc. from
// script.js, so we don't re-number them ourselves).
// ============================================================================
uint8_t splitIntoLines(const String &text, String outLines[], uint8_t maxLines) {
  uint8_t count = 0;
  int start = 0;
  bool firstLine = true;

  while (start <= (int)text.length() && count < maxLines) {
    int nl = text.indexOf('\n', start);
    if (nl == -1) nl = text.length();
    String line = text.substring(start, nl);
    line.trim();
    start = nl + 1;

    if (firstLine) {
      firstLine = false;
      String upper = line;
      upper.toUpperCase();
      if (upper == LIST_TITLE) {
        continue; // skip the redundant title line, card header already shows branding
      }
    }

    if (line.length() > 0) {
      outLines[count++] = line;
    }

    if (nl >= (int)text.length()) break;
  }

  return count;
}

// Every call does a FULL clearContentArea() + redraw of the current lines —
// idempotent re-render of one authoritative array, so rapid successive
// updates can never overlap (each one fully erases the previous first).
void renderShoppingList(uint16_t textColor) {
  clearContentArea();
  tft.setFont(&FreeSans9pt7b);
  tft.setTextSize(1);
  tft.setTextWrap(false);

  if (shoppingLineCount == 0) {
    drawStatus("Liste bos", COLOR_TEXT_SECOND, contentY() + contentH() / 2);
  } else {
    int16_t lineHeight = measureLineHeight();
    int16_t y = contentY() + lineHeight / 2 + 2;
    int16_t bottomLimit = contentY() + contentH() - lineHeight / 2;

    for (uint8_t i = 0; i < shoppingLineCount; i++) {
      String wrapped[3];
      int wrappedCount = wrapText(shoppingLines[i], wrapped, 3, contentW());

      for (int line = 0; line < wrappedCount; line++) {
        if (y > bottomLimit) break; // never draw into the footer strip
        int16_t rx1, ry1; uint16_t rw, rh;
        tft.getTextBounds(wrapped[line], 0, 0, &rx1, &ry1, &rw, &rh);
        int16_t cursorX = contentX() - rx1;
        int16_t cursorY = y - (rh / 2) - ry1;
        tft.setTextColor(textColor);
        tft.setCursor(cursorX, cursorY);
        tft.print(wrapped[line]);
        y += lineHeight;
      }
    }
  }

  drawFooter(labelForState(currentState) + " - " + String(shoppingLineCount) + " urun",
             accentForState(currentState));
}

// Applies a full "display" payload: text == "" means the list was cleared;
// otherwise it's split into lines (title stripped) and stored as the new,
// complete state of the card.
void applyDisplayText(const String &text, uint16_t textColor) {
  String lines[MAX_ITEMS];
  uint8_t count = splitIntoLines(text, lines, MAX_ITEMS);

  shoppingLineCount = count;
  for (uint8_t i = 0; i < count; i++) shoppingLines[i] = lines[i];

  renderShoppingList(textColor);
}

// ============================================================================
// CONNECTION STATE — updates header dot + footer WITHOUT touching the list,
// so a WebSocket reconnect never wipes what's on screen.
// ============================================================================
void updateConnectionState(ConnectionState state) {
  currentState = state;
  drawHeader(accentForState(state)); // same position/size dot, just repaints color
  renderShoppingList(COLOR_TEXT_PRIMARY);
}

// ============================================================================
// JSON / PAYLOAD HANDLING — matches main.py's DisplayMessage exactly:
//   { "type": "display", "text": "...", "color": "white", "size": 3 }
// ============================================================================
void handleIncomingPayload(const String &payload) {
  JsonDocument document;
  DeserializationError error = deserializeJson(document, payload);

  if (error) {
    // Not valid JSON — fall back to treating the raw payload as display text.
    applyDisplayText(payload, COLOR_TEXT_PRIMARY);
    return;
  }

  String text = document["text"] | "";
  String colorName = document["color"] | "white";
  applyDisplayText(text, mapDisplayColor(colorName));
}

// ============================================================================
// WEBSOCKET EVENT HANDLER
// ============================================================================
void handleSocket(WStype_t type, uint8_t *payload, size_t length) {
  switch (type) {
    case WStype_CONNECTED:
      updateConnectionState(STATE_CONNECTED);
      break;
    case WStype_DISCONNECTED:
      updateConnectionState(STATE_DISCONNECTED);
      break;
    case WStype_TEXT: {
      String rawPayload = "";
      rawPayload.reserve(length);
      for (size_t i = 0; i < length; i++) {
        rawPayload += static_cast<char>(payload[i]);
      }
      handleIncomingPayload(rawPayload);
      break;
    }
    default:
      break;
  }
}

// ============================================================================
// WIFI CONNECTION — UNCHANGED LOGIC, ANIMATED UI WHILE WAITING
// ============================================================================
void connectWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  drawShell(STATE_CONNECTING);
  drawStatus("Wi-Fi'ye baglaniliyor", COLOR_TEXT_PRIMARY, contentY() + 26);
  drawLoading(0, contentY() + 60);

  uint8_t dotIndex = 0;
  while (WiFi.status() != WL_CONNECTED) {
    delay(WIFI_RETRY_DELAY_MS);
    drawLoading(dotIndex, contentY() + 60);
    dotIndex = (dotIndex + 1) % 3;
  }
}

// ============================================================================
// WEBSOCKET SETUP — UNCHANGED LOGIC
// ============================================================================
void connectWebSocket() {
  webSocket.begin(WEBSOCKET_HOST, WEBSOCKET_PORT, WEBSOCKET_PATH);
  webSocket.onEvent(handleSocket);
  webSocket.setReconnectInterval(WEBSOCKET_RECONNECT_MS);
}

// ============================================================================
// ARDUINO ENTRY POINTS
// ============================================================================
void setup() {
  Serial.begin(115200);
  tft.begin();
  tft.setRotation(1);

  connectWifi();

  drawShell(STATE_DISCONNECTED);
  renderShoppingList(COLOR_TEXT_PRIMARY);

  connectWebSocket();
}

void loop() {
  webSocket.loop();
}
