from __future__ import annotations

import io
import base64
from PIL import Image 

import json
import os
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse

app = FastAPI(title="ESP32 Live Display")

# --- CORS Ayarı ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://realtime-display-lime.vercel.app"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# --------------------------------

SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_NOW_PLAYING_URL = "https://api.spotify.com/v1/me/player/currently-playing"
SPOTIFY_SCOPES = "user-read-currently-playing user-read-playback-state"
OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
WEATHER_FORECAST_DAYS = 4

spotify_state: str | None = None
spotify_tokens: dict[str, Any] = {}


def weather_label(code: int) -> tuple[str, str]:
    if code == 0:
        return "clear", "Açık"
    if code in {1, 2}:
        return "partly_cloudy", "Parçalı bulutlu"
    if code == 3:
        return "cloudy", "Bulutlu"
    if code in {45, 48}:
        return "fog", "Sisli"
    if code in {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82}:
        return "rain", "Yağmurlu"
    if code in {71, 73, 75, 77, 85, 86}:
        return "snow", "Karlı"
    if code in {95, 96, 99}:
        return "storm", "Fırtına"
    return "unknown", "Bilinmiyor"


def short_day(date_text: str) -> str:
    # YYYY-MM-DD -> MM/DD keeps the ESP payload compact and language-neutral.
    return f"{date_text[5:7]}/{date_text[8:10]}"


def spotify_config() -> tuple[str, str, str]:
    client_id = os.getenv("SPOTIFY_CLIENT_ID", "")
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET", "")
    redirect_uri = os.getenv("SPOTIFY_REDIRECT_URI", "")

    if not client_id or not client_secret or not redirect_uri:
        raise HTTPException(
            status_code=503,
            detail=(
                "Spotify is not configured. Set SPOTIFY_CLIENT_ID, "
                "SPOTIFY_CLIENT_SECRET, and SPOTIFY_REDIRECT_URI."
            ),
        )

    return client_id, client_secret, redirect_uri


def spotify_token_auth() -> tuple[str, str]:
    client_id, client_secret, _ = spotify_config()
    return client_id, client_secret


async def refresh_spotify_token() -> None:
    refresh_token = spotify_tokens.get("refresh_token")
    if not refresh_token:
        raise HTTPException(status_code=401, detail="Spotify account is not connected.")

    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(
            SPOTIFY_TOKEN_URL,
            data={"grant_type": "refresh_token", "refresh_token": refresh_token},
            auth=spotify_token_auth(),
        )

    if response.status_code >= 400:
        spotify_tokens.clear()
        raise HTTPException(status_code=401, detail="Spotify token refresh failed.")

    payload = response.json()
    spotify_tokens["access_token"] = payload["access_token"]
    if payload.get("refresh_token"):
        spotify_tokens["refresh_token"] = payload["refresh_token"]

async def fetch_and_convert_album_art(url: str, target_size=(80, 80)) -> str:
    if not url:
        return ""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            res = await client.get(url)
            if res.status_code != 200:
                return ""

        # Resmi aç ve 80x80 boyutuna getir
        img = Image.open(io.BytesIO(res.content)).convert("RGB")
        img = img.resize(target_size, Image.Resampling.LANCZOS)

        # RGB565 formatına dönüştür
        raw_bytes = bytearray()
        for y in range(img.height):
            for x in range(img.width):
                r, g, b = img.getpixel((x, y))
                rgb565 = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
                raw_bytes.append((rgb565 >> 8) & 0xFF)  # yüksek byte
                raw_bytes.append(rgb565 & 0xFF)         # düşük byte

        return base64.b64encode(raw_bytes).decode('utf-8')
    except Exception:
        return ""

async def spotify_get(url: str) -> httpx.Response:
    access_token = spotify_tokens.get("access_token")
    if not access_token:
        raise HTTPException(status_code=401, detail="Spotify account is not connected.")

    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(url, headers={"Authorization": f"Bearer {access_token}"})

    if response.status_code == 401:
        await refresh_spotify_token()
        access_token = spotify_tokens["access_token"]
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(url, headers={"Authorization": f"Bearer {access_token}"})

    return response


@dataclass(frozen=True)
class DisplayMessage:
    type: str   
    text: str
    color: str = "white"
    size: int = 3

    @classmethod
    def from_raw(cls, raw_message: str) -> "DisplayMessage":
        try:
            payload: Any = json.loads(raw_message)
        except json.JSONDecodeError:
            return cls(type="display", text=raw_message.strip())

        if isinstance(payload, dict):
            return cls(
                type=str(payload.get("type", "display")),
                text=str(payload.get("text", "")).strip(),
                color=str(payload.get("color", "white")),
                size=int(payload.get("size", 3)),
            )

        return cls(type="display", text=str(payload).strip())

    def to_json(self) -> str:
        return json.dumps(
            {
                "type": self.type,
                "text": self.text,
                "color": self.color,
                "size": self.size,
            },
            ensure_ascii=False,
        )


def normalize_websocket_message(raw_message: str) -> str:
    try:
        payload: Any = json.loads(raw_message)
    except json.JSONDecodeError:
        return DisplayMessage(type="display", text=raw_message.strip()).to_json()

    if isinstance(payload, dict):
        return json.dumps(payload, ensure_ascii=False)

    return DisplayMessage(type="display", text=str(payload).strip()).to_json()


class ConnectionManager:
    def __init__(self) -> None:
        self.active_connections: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.active_connections.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self.active_connections.discard(websocket)

    async def broadcast(self, message: str, sender: WebSocket) -> None:
        disconnected_clients: list[WebSocket] = []

        for connection in self.active_connections:
            if connection is sender:
                continue

            try:
                await connection.send_text(message)
            except RuntimeError:
                disconnected_clients.append(connection)

        for connection in disconnected_clients:
            self.disconnect(connection)


manager = ConnectionManager()

@app.get("/spotify/current")
async def spotify_current() -> dict[str, Any]:
    response = await spotify_get(SPOTIFY_NOW_PLAYING_URL)

    if response.status_code == 204:
        return {"connected": True, "playing": False}
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail="Spotify request failed.")

    payload = response.json()
    item = payload.get("item") or {}
    artists = ", ".join(artist.get("name", "") for artist in item.get("artists", [])).strip(", ")

    # Resim URL'sini çek (en küçük resmi tercih edebiliriz, örn: 300x300 veya 64x64)
    images = (item.get("album") or {}).get("images", [])
    image_url = images[-1]["url"] if images else "" # En küçük boyut

    # Resmi indirip RGB565 base64'e çevir
    image_raw_b64 = await fetch_and_convert_album_art(image_url, target_size=(80, 80))

    return {
        "connected": True,
        "playing": bool(payload.get("is_playing")),
        "title": item.get("name", ""),
        "artist": artists,
        "album": (item.get("album") or {}).get("name", ""),
        "progress_ms": payload.get("progress_ms", 0),
        "duration_ms": item.get("duration_ms", 0),
        "image_raw": image_raw_b64 # <-- ESP32'ye gönderilecek görsel verisi
    }

@app.get("/spotify/login")
async def spotify_login() -> RedirectResponse:
    global spotify_state

    client_id, _, redirect_uri = spotify_config()
    spotify_state = secrets.token_urlsafe(24)
    query = urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": SPOTIFY_SCOPES,
            "state": spotify_state,
        }
    )
    return RedirectResponse(f"{SPOTIFY_AUTH_URL}?{query}")


@app.get("/spotify/callback")
async def spotify_callback(request: Request) -> HTMLResponse:
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    _, _, redirect_uri = spotify_config()

    if not code or state != spotify_state:
        raise HTTPException(status_code=400, detail="Invalid Spotify callback.")

    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(
            SPOTIFY_TOKEN_URL,
            data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri},
            auth=spotify_token_auth(),
        )

    if response.status_code >= 400:
        raise HTTPException(status_code=400, detail="Spotify authorization failed.")

    payload = response.json()
    spotify_tokens["access_token"] = payload["access_token"]
    spotify_tokens["refresh_token"] = payload["refresh_token"]

    return HTMLResponse(
        "<!doctype html><title>Spotify connected</title>"
        "<p>Spotify baglandi. Bu pencereyi kapatip HomeFlow'a donebilirsin.</p>"
    )


@app.get("/weather")
async def weather(lat: float = 41.0082, lon: float = 28.9784) -> dict[str, Any]:
    params = {
        "latitude": lat,
        "longitude": lon,
        "timezone": "auto",
        "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "forecast_days": WEATHER_FORECAST_DAYS,
    }

    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(OPEN_METEO_FORECAST_URL, params=params)

    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail="Weather request failed.")

    payload = response.json()
    current = payload.get("current") or {}
    daily = payload.get("daily") or {}
    condition, label = weather_label(int(current.get("weather_code", -1)))

    forecast: list[dict[str, Any]] = []
    dates = daily.get("time", [])
    codes = daily.get("weather_code", [])
    max_temps = daily.get("temperature_2m_max", [])
    min_temps = daily.get("temperature_2m_min", [])
    rain_probs = daily.get("precipitation_probability_max", [])

    for index, date_text in enumerate(dates[:WEATHER_FORECAST_DAYS]):
        day_code = int(codes[index]) if index < len(codes) else -1
        day_condition, day_label = weather_label(day_code)
        forecast.append(
            {
                "day": short_day(str(date_text)),
                "condition": day_condition,
                "label": day_label,
                "code": day_code,
                "max": round(float(max_temps[index])) if index < len(max_temps) else 0,
                "min": round(float(min_temps[index])) if index < len(min_temps) else 0,
                "rain": int(rain_probs[index]) if index < len(rain_probs) and rain_probs[index] is not None else 0,
            }
        )

    return {
        "type": "weather",
        "location": "Istanbul",
        "temperature": round(float(current.get("temperature_2m", 0))),
        "humidity": int(current.get("relative_humidity_2m", 0)),
        "wind": round(float(current.get("wind_speed_10m", 0))),
        "condition": condition,
        "label": label,
        "forecast": forecast,
    }


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await manager.connect(websocket)

    try:
        while True:
            raw_message = await websocket.receive_text()

            # NOTE: previously this only broadcast when text was non-empty.
            # Clearing the shopping list sends text="" (falsy in Python),
            # so that event was silently dropped and never reached the
            # ESP32 (or any other connected client). Broadcasting
            # unconditionally lets an empty text act as a real "clear"
            # signal downstream.
            await manager.broadcast(normalize_websocket_message(raw_message), sender=websocket)
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)
        await websocket.close(code=1011)
