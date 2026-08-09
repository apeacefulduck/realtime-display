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
#include <U8g2_for_Adafruit_GFX.h>

// ---- Fonts ------------------------------------------------------------
#include <Fonts/FreeSansBold12pt7b.h>   // Header / logo ("HomeFlow")
#include <Fonts/FreeSansBold9pt7b.h>    // Empty-state / connecting messages
#include <Fonts/FreeSans9pt7b.h>        // List items / status / footer

// ============================================================================
// NETWORK CONFIG — UNCHANGED
// ============================================================================
const char *WIFI_SSID = "apeacefulduck5";
const char *WIFI_PASSWORD = "Muslera26";
const char *WEBSOCKET_HOST = "realtime-display.onrender.com";
const uint16_t WEBSOCKET_PORT = 443;
const char *WEBSOCKET_PATH = "/ws";

const uint8_t TFT_CS_PIN = 5;
const uint8_t TFT_DC_PIN = 2;
const uint8_t TFT_RST_PIN = 4;

const uint8_t JOY_X_PIN = 34;
const uint8_t JOY_Y_PIN = 35;
const uint8_t JOY_SW_PIN = 25;

const uint16_t WIFI_RETRY_DELAY_MS = 500;
const uint16_t WEBSOCKET_RECONNECT_MS = 3000;
const uint16_t JOYSTICK_REPEAT_MS = 260;
const uint16_t JOYSTICK_DEADZONE = 650;
const uint16_t JOYSTICK_EDGE_MARGIN = 260;
const uint8_t JOYSTICK_CALIBRATION_SAMPLES = 40;
const uint8_t MENU_HEIGHT_DIVISOR = 3;

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
const uint8_t CARD_MARGIN      = 6;
const uint8_t CARD_RADIUS      = 6;
const uint8_t CARD_PADDING     = 12;
const uint8_t HEADER_OFFSET_Y  = 16;
const uint8_t DIVIDER_OFFSET_Y = 42;
const uint8_t FOOTER_HEIGHT    = 26;
const uint8_t DOT_RADIUS       = 3;
const uint8_t DOT_SPACING      = 16;

// Mirrors script.js's `const maxItems = 20;` so the two sides can never
// disagree about how many lines a full snapshot can contain.
const uint8_t MAX_ITEMS = 20;
const uint8_t SHOPPING_ITEMS_PER_PAGE = 4;
String shoppingLines[MAX_ITEMS];   // already-formatted lines, e.g. "1. Ekmek"
uint8_t shoppingLineCount = 0;
uint8_t shoppingPage = 0;
bool spotifyEnabled = false;
bool spotifyPlaying = false;
String spotifyTitle = "";
String spotifyArtist = "";
String spotifyAlbum = "";

// The web app always sends this exact title as the first line; we recognize
// and skip it since the card header already shows the "HomeFlow" brand.
const char *LIST_TITLE = "ALISVERIS LISTESI";

Adafruit_ILI9341 tft(TFT_CS_PIN, TFT_DC_PIN, TFT_RST_PIN);
U8G2_FOR_ADAFRUIT_GFX u8g2Fonts;
WebSocketsClient webSocket;

enum ConnectionState {
  STATE_CONNECTING,
  STATE_CONNECTED,
  STATE_DISCONNECTED,
  STATE_ERROR
};

ConnectionState currentState = STATE_CONNECTING;

enum ScreenId {
  SCREEN_HOME,
  SCREEN_INDOOR,
  SCREEN_WEATHER,
  SCREEN_SLEEP,
  SCREEN_SHOPPING,
  SCREEN_SPOTIFY,
  SCREEN_COUNT
};

ScreenId currentScreen = SCREEN_HOME;
bool menuVisible = false;
uint8_t selectedMenuIndex = SCREEN_HOME;
uint16_t joyXCenter = 2048;
uint16_t joyYCenter = 2048;
uint32_t lastJoystickMoveAt = 0;
uint32_t lastButtonChangeAt = 0;
bool lastButtonReading = HIGH;
bool buttonStableReading = HIGH;
bool buttonPressedLatch = false;

// ============================================================================
// LOW-LEVEL LAYOUT HELPERS
// ============================================================================
int16_t menuH() { return tft.height() / MENU_HEIGHT_DIVISOR; }
int16_t cardX() { return CARD_MARGIN; }
int16_t cardY() { return CARD_MARGIN; }
int16_t cardW() { return tft.width() - cardX() - CARD_MARGIN; }
int16_t cardH() { return tft.height() - CARD_MARGIN * 2 - (menuVisible ? menuH() : 0); }

int16_t contentY() { return cardY() + DIVIDER_OFFSET_Y + 10; }
int16_t contentH() { return cardH() - DIVIDER_OFFSET_Y - 10 - FOOTER_HEIGHT - 6; }
int16_t contentX() { return cardX() + CARD_PADDING; }
int16_t contentW() { return cardW() - CARD_PADDING * 2; }

int16_t footerY() { return cardY() + cardH() - FOOTER_HEIGHT; }
int16_t footerH() { return FOOTER_HEIGHT - 6; }
int16_t pageCenterX() { return tft.width() / 2; }

const char *screenTitle(ScreenId screen) {
  switch (screen) {
    case SCREEN_HOME:     return "Giriş";
    case SCREEN_INDOOR:   return "Ev";
    case SCREEN_WEATHER:  return "Hava";
    case SCREEN_SLEEP:    return "Uyku";
    case SCREEN_SHOPPING: return "Alışveriş";
    case SCREEN_SPOTIFY:  return "Spotify";
    default:              return "";
  }
}

const char *screenMenuLabel(ScreenId screen) {
  switch (screen) {
    case SCREEN_HOME:     return "Giriş";
    case SCREEN_INDOOR:   return "Ev";
    case SCREEN_WEATHER:  return "Hava";
    case SCREEN_SLEEP:    return "Uy";
    case SCREEN_SHOPPING: return "Alışv.";
    case SCREEN_SPOTIFY:  return "Spot.";
    default:              return "";
  }
}

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
    case STATE_CONNECTED:    return "Bağlandı";
    case STATE_DISCONNECTED: return "Bağlantı yok";
    case STATE_ERROR:        return "Hata";
    case STATE_CONNECTING:
    default:                 return "Bağlanıyor";
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

void drawUtf8Text(const char *text, int16_t x, int16_t baselineY, uint16_t color) {
  u8g2Fonts.setForegroundColor(color);
  u8g2Fonts.setBackgroundColor(COLOR_CARD_BG);
  u8g2Fonts.setCursor(x, baselineY);
  u8g2Fonts.print(text);
}

void drawCenteredUtf8Text(const char *text, int16_t centerX, int16_t baselineY, uint16_t color) {
  int16_t w = u8g2Fonts.getUTF8Width(text);
  drawUtf8Text(text, centerX - w / 2, baselineY, color);
}

void drawUtf8String(const String &text, int16_t x, int16_t baselineY, uint16_t color) {
  drawUtf8Text(text.c_str(), x, baselineY, color);
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
  drawCenteredText("HomeFlow", pageCenterX(), cardY() + HEADER_OFFSET_Y + 10, COLOR_ACCENT_BLUE);

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
  u8g2Fonts.setFont(u8g2_font_10x20_te);
  drawCenteredUtf8Text(text.c_str(), pageCenterX(), centerY + 8, color);
}

void drawFooter(const String &text, uint16_t dotColor) {
  clearFooterArea();
  int16_t dotX = cardX() + CARD_PADDING;
  int16_t dotY = footerY() + footerH() / 2;
  tft.fillCircle(dotX, dotY, DOT_RADIUS, dotColor);

  u8g2Fonts.setFont(u8g2_font_6x12_te);
  drawUtf8String(text, dotX + DOT_RADIUS + 8, dotY + 5, COLOR_TEXT_SECOND);
}

void drawLoading(uint8_t activeIndex, int16_t centerY) {
  int16_t totalWidth = DOT_SPACING * 2;
  int16_t startX = pageCenterX() - totalWidth / 2;

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

void drawMenuIcon(ScreenId screen, int16_t cx, int16_t cy, uint16_t color) {
  switch (screen) {
    case SCREEN_HOME:
      tft.drawTriangle(cx - 8, cy, cx, cy - 8, cx + 8, cy, color);
      tft.drawRect(cx - 6, cy, 12, 10, color);
      break;
    case SCREEN_INDOOR:
      tft.drawRect(cx - 5, cy - 8, 10, 16, color);
      tft.fillCircle(cx, cy + 10, 4, color);
      break;
    case SCREEN_WEATHER:
      tft.fillCircle(cx - 5, cy - 2, 5, color);
      tft.fillCircle(cx + 2, cy - 4, 7, color);
      tft.fillCircle(cx + 9, cy - 1, 4, color);
      tft.drawFastHLine(cx - 10, cy + 5, 22, color);
      break;
    case SCREEN_SLEEP:
      tft.fillCircle(cx, cy, 10, color);
      tft.fillCircle(cx + 5, cy - 3, 10, COLOR_CARD_BG);
      break;
    case SCREEN_SHOPPING:
      tft.drawRect(cx - 9, cy - 3, 18, 12, color);
      tft.drawLine(cx - 5, cy - 3, cx - 2, cy - 9, color);
      tft.drawLine(cx + 5, cy - 3, cx + 2, cy - 9, color);
      break;
    case SCREEN_SPOTIFY:
      tft.drawCircle(cx, cy, 12, color);
      tft.drawFastHLine(cx - 6, cy - 4, 12, color);
      tft.drawFastHLine(cx - 7, cy, 14, color);
      tft.drawFastHLine(cx - 5, cy + 4, 10, color);
      break;
    default:
      break;
  }
}

void drawMenu() {
  if (!menuVisible) return;

  int16_t y = tft.height() - menuH();
  int16_t itemW = tft.width() / SCREEN_COUNT;
  tft.fillRect(0, y, tft.width(), menuH(), COLOR_BACKGROUND);
  tft.drawFastHLine(0, y, tft.width(), COLOR_BORDER);
  u8g2Fonts.setFont(u8g2_font_6x12_te);

  for (uint8_t i = 0; i < SCREEN_COUNT; i++) {
    int16_t x = i * itemW;
    bool selected = i == selectedMenuIndex;
    uint16_t bg = selected ? COLOR_ACCENT_BLUE : COLOR_CARD_BG;
    uint16_t fg = selected ? COLOR_TEXT_PRIMARY : COLOR_TEXT_SECOND;
    tft.fillRoundRect(x + 3, y + 6, itemW - 6, menuH() - 12, 6, bg);
    drawMenuIcon((ScreenId)i, x + itemW / 2, y + 24, fg);
    drawCenteredUtf8Text(screenMenuLabel((ScreenId)i), x + itemW / 2, y + 58, fg);
  }
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

void drawSimpleMetric(const String &label, const String &value, int16_t y, uint16_t valueColor) {
  u8g2Fonts.setFont(u8g2_font_7x13_te);
  drawUtf8String(label, contentX(), y, COLOR_TEXT_SECOND);

  u8g2Fonts.setFont(u8g2_font_10x20_te);
  drawUtf8String(value, contentX(), y + 32, valueColor);
}

void renderHomeScreen() {
  clearContentArea();
  drawSimpleMetric("Genel özet", "HomeFlow hazır", contentY() + 18, COLOR_TEXT_PRIMARY);
  drawSimpleMetric("Bağlantı", labelForState(currentState), contentY() + 82, accentForState(currentState));
}

void renderIndoorScreen() {
  clearContentArea();
  drawSimpleMetric("Ev sıcaklık", "-- C", contentY() + 24, COLOR_WARNING);
  drawSimpleMetric("Ev nem", "-- %", contentY() + 96, COLOR_ACCENT_BLUE);
}

void renderWeatherScreen() {
  clearContentArea();
  drawSimpleMetric("Hava durumu", "Dışarı", contentY() + 24, COLOR_TEXT_PRIMARY);
  drawSimpleMetric("Sıcaklık", "-- C", contentY() + 96, COLOR_WARNING);
}

void renderSleepScreen() {
  clearContentArea();
  drawSimpleMetric("Uyku modu", "Hazır", contentY() + 42, COLOR_ACCENT_BLUE);
  drawSimpleMetric("Durum", "Kapalı", contentY() + 112, COLOR_TEXT_SECOND);
}

void renderSpotifyScreen() {
  clearContentArea();

  if (!spotifyEnabled) {
    drawStatus("Spotify kapalı", COLOR_TEXT_SECOND, contentY() + contentH() / 2);
    clearFooterArea();
    return;
  }

  if (!spotifyPlaying || spotifyTitle.length() == 0) {
    drawSimpleMetric("Spotify", "Çalan şarkı yok", contentY() + 42, COLOR_TEXT_SECOND);
    drawSimpleMetric("Durum", "Bekleniyor", contentY() + 112, COLOR_ACCENT_BLUE);
    drawFooter(labelForState(currentState), accentForState(currentState));
    return;
  }

  u8g2Fonts.setFont(u8g2_font_10x20_te);
  drawUtf8String(spotifyTitle, contentX(), contentY() + 34, COLOR_TEXT_PRIMARY);
  u8g2Fonts.setFont(u8g2_font_7x13_te);
  drawUtf8String(spotifyArtist, contentX(), contentY() + 78, COLOR_ACCENT_BLUE);
  drawUtf8String(spotifyAlbum, contentX(), contentY() + 108, COLOR_TEXT_SECOND);
  drawFooter("Spotify - " + labelForState(currentState), accentForState(currentState));
}

// Every call does a FULL clearContentArea() + redraw of the current lines.
void renderShoppingList(uint16_t textColor) {
  clearContentArea();
  tft.setFont(&FreeSans9pt7b);
  tft.setTextSize(1);
  tft.setTextWrap(false);

  if (shoppingLineCount == 0) {
    drawStatus("Liste boş", COLOR_TEXT_SECOND, contentY() + contentH() / 2);
    clearFooterArea();
  } else {
    uint8_t maxPage = (shoppingLineCount == 0) ? 0 : (shoppingLineCount - 1) / SHOPPING_ITEMS_PER_PAGE;
    if (shoppingPage > maxPage) shoppingPage = maxPage;

    u8g2Fonts.setFont(u8g2_font_7x13_te);
    int16_t lineHeight = 28;
    int16_t y = contentY() + 18;
    int16_t bottomLimit = contentY() + contentH() - 4;
    uint8_t start = shoppingPage * SHOPPING_ITEMS_PER_PAGE;
    uint8_t end = min((uint8_t)(start + SHOPPING_ITEMS_PER_PAGE), shoppingLineCount);

    for (uint8_t i = start; i < end; i++) {
      if (y > bottomLimit) break;
      drawUtf8String(shoppingLines[i], contentX(), y, textColor);
      y += lineHeight;
    }
    uint8_t pageCount = max((uint8_t)1, (uint8_t)((shoppingLineCount + SHOPPING_ITEMS_PER_PAGE - 1) / SHOPPING_ITEMS_PER_PAGE));
    drawFooter(labelForState(currentState) + " - " + String(shoppingLineCount) + " ürün"
               + " - " + String(shoppingPage + 1) + "/" + String(pageCount),
               accentForState(currentState));
  }
}

void renderCurrentContent(uint16_t textColor = COLOR_TEXT_PRIMARY) {
  switch (currentScreen) {
    case SCREEN_HOME:     renderHomeScreen(); break;
    case SCREEN_INDOOR:   renderIndoorScreen(); break;
    case SCREEN_WEATHER:  renderWeatherScreen(); break;
    case SCREEN_SLEEP:    renderSleepScreen(); break;
    case SCREEN_SHOPPING: renderShoppingList(textColor); return;
    case SCREEN_SPOTIFY:  renderSpotifyScreen(); return;
    default:              renderHomeScreen(); break;
  }
  drawFooter(labelForState(currentState), accentForState(currentState));
}

void renderScreen(uint16_t textColor = COLOR_TEXT_PRIMARY) {
  drawShell(currentState);
  renderCurrentContent(textColor);
  drawMenu();
}

// Applies a full "display" payload: text == "" means the list was cleared;
// otherwise it's split into lines (title stripped) and stored as the new,
// complete state of the card.
void applyDisplayText(const String &text, uint16_t textColor) {
  String lines[MAX_ITEMS];
  uint8_t count = splitIntoLines(text, lines, MAX_ITEMS);

  shoppingLineCount = count;
  for (uint8_t i = 0; i < count; i++) shoppingLines[i] = lines[i];
  if (shoppingPage > 0 && shoppingPage * SHOPPING_ITEMS_PER_PAGE >= shoppingLineCount) shoppingPage = 0;

  if (currentScreen == SCREEN_SHOPPING || currentScreen == SCREEN_HOME) {
    renderScreen(textColor);
  }
}

void applySpotifyPayload(JsonDocument &document) {
  spotifyEnabled = document["enabled"] | false;
  spotifyPlaying = document["playing"] | false;
  spotifyTitle = (const char *)(document["title"] | "");
  spotifyArtist = (const char *)(document["artist"] | "");
  spotifyAlbum = (const char *)(document["album"] | "");

  if (currentScreen == SCREEN_SPOTIFY || currentScreen == SCREEN_HOME) {
    renderScreen(COLOR_TEXT_PRIMARY);
  }
}

// ============================================================================
// CONNECTION STATE — updates header dot + footer WITHOUT touching the list,
// so a WebSocket reconnect never wipes what's on screen.
// ============================================================================
void updateConnectionState(ConnectionState state) {
  currentState = state;
  renderScreen(COLOR_TEXT_PRIMARY);
}

void animateScreenChange(int8_t direction) {
  int16_t startX = direction > 0 ? tft.width() : 0;
  int16_t step = tft.width() / 8;

  for (uint8_t i = 0; i < 8; i++) {
    int16_t x = direction > 0 ? startX - (i + 1) * step : i * step;
    tft.fillRect(x, 0, step + 2, tft.height(), COLOR_BACKGROUND);
    delay(12);
  }

  renderScreen(COLOR_TEXT_PRIMARY);
}

void setScreen(ScreenId screen, int8_t direction) {
  if (screen == currentScreen) {
    renderScreen(COLOR_TEXT_PRIMARY);
    return;
  }
  currentScreen = screen;
  selectedMenuIndex = screen;
  animateScreenChange(direction);
}

void moveMenuSelection(int8_t delta) {
  int8_t next = (int8_t)selectedMenuIndex + delta;
  if (next < 0) next = SCREEN_COUNT - 1;
  if (next >= SCREEN_COUNT) next = 0;
  selectedMenuIndex = next;
  renderScreen(COLOR_TEXT_PRIMARY);
}

void moveShoppingPage(int8_t delta) {
  if (currentScreen != SCREEN_SHOPPING || shoppingLineCount == 0) return;
  uint8_t pageCount = (shoppingLineCount + SHOPPING_ITEMS_PER_PAGE - 1) / SHOPPING_ITEMS_PER_PAGE;
  int8_t next = (int8_t)shoppingPage + delta;
  if (next < 0) next = pageCount - 1;
  if (next >= pageCount) next = 0;
  if (next == shoppingPage) return;
  shoppingPage = next;
  animateScreenChange(delta);
}

uint16_t readAveragedAnalog(uint8_t pin) {
  uint32_t total = 0;
  for (uint8_t i = 0; i < JOYSTICK_CALIBRATION_SAMPLES; i++) {
    total += analogRead(pin);
    delay(2);
  }
  return total / JOYSTICK_CALIBRATION_SAMPLES;
}

void calibrateJoystick() {
  joyXCenter = readAveragedAnalog(JOY_X_PIN);
  joyYCenter = readAveragedAnalog(JOY_Y_PIN);

  Serial.print("Joystick center X=");
  Serial.print(joyXCenter);
  Serial.print(" Y=");
  Serial.println(joyYCenter);
}

int8_t axisDirection(uint16_t value, uint16_t center) {
  if (center > JOYSTICK_EDGE_MARGIN && value + JOYSTICK_DEADZONE < center) return -1;
  if (center < 4095 - JOYSTICK_EDGE_MARGIN && value > center + JOYSTICK_DEADZONE) return 1;
  return 0;
}

void handleJoystick() {
  bool buttonReading = digitalRead(JOY_SW_PIN);
  uint32_t buttonNow = millis();
  if (buttonReading != lastButtonReading) {
    lastButtonChangeAt = buttonNow;
    lastButtonReading = buttonReading;
  }

  if (buttonNow - lastButtonChangeAt > 35 && buttonReading != buttonStableReading) {
    buttonStableReading = buttonReading;
    if (buttonStableReading == LOW && !buttonPressedLatch) {
      if (menuVisible) {
        menuVisible = false;
        setScreen((ScreenId)selectedMenuIndex, 1);
      } else {
        selectedMenuIndex = currentScreen;
        menuVisible = true;
        renderScreen(COLOR_TEXT_PRIMARY);
      }
      buttonPressedLatch = true;
    }
  }
  if (buttonStableReading == HIGH) buttonPressedLatch = false;

  uint32_t now = millis();
  if (now - lastJoystickMoveAt < JOYSTICK_REPEAT_MS) return;

  uint16_t yValue = analogRead(JOY_Y_PIN);
  uint16_t xValue = analogRead(JOY_X_PIN);
  int8_t yDir = axisDirection(yValue, joyYCenter);
  int8_t xDir = axisDirection(xValue, joyXCenter);

  if (yDir != 0) {
    lastJoystickMoveAt = now;
    if (menuVisible) moveMenuSelection(yDir);
  } else if (xDir != 0) {
    lastJoystickMoveAt = now;
    moveShoppingPage(xDir);
  }
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
  String type = document["type"] | "display";

  if (type == "spotify") {
    applySpotifyPayload(document);
  } else {
    applyDisplayText(text, mapDisplayColor(colorName));
  }
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
  drawStatus("Wi-Fi'ye bağlanılıyor", COLOR_TEXT_PRIMARY, contentY() + 26);
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
  webSocket.beginSSL("realtime-display.onrender.com", 443, "/ws");
  webSocket.onEvent(handleSocket);
  webSocket.setReconnectInterval(WEBSOCKET_RECONNECT_MS);
}

// ============================================================================
// ARDUINO ENTRY POINTS
// ============================================================================
void setup() {
  Serial.begin(115200);
  pinMode(JOY_SW_PIN, INPUT_PULLUP);
  analogReadResolution(12);
  analogSetPinAttenuation(JOY_X_PIN, ADC_11db);
  analogSetPinAttenuation(JOY_Y_PIN, ADC_11db);
  calibrateJoystick();

  tft.begin();
  tft.setRotation(1);
  u8g2Fonts.begin(tft);
  u8g2Fonts.setFontMode(1);
  u8g2Fonts.setFontDirection(0);

  connectWifi();

  renderScreen(COLOR_TEXT_PRIMARY);

  connectWebSocket();
}

void loop() {
  webSocket.loop();
  handleJoystick();
}
