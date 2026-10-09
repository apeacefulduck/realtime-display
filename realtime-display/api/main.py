from __future__ import annotations

import json
import asyncio
import time
import os
import secrets
import math
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from spotify_session import seal, unseal, make_session
from spotify_player import SpotifyPlayer
from spotify_auth import SpotifyAuthManager
import logging
from contextlib import asynccontextmanager
import notes_api
import indoor_sensor
from weather_retry import WeatherRetry

@asynccontextmanager
async def lifespan(app):
    await player.start()
    indoor_task = asyncio.create_task(expire_indoor_loop())
    weather_task = asyncio.create_task(refresh_weather_loop())
    try:
        yield
    finally:
        weather_task.cancel()
        await asyncio.gather(weather_task, return_exceptions=True)
        indoor_task.cancel()
        await asyncio.gather(indoor_task, return_exceptions=True)
        await player.stop()

app = FastAPI(title="ESP32 Live Display", lifespan=lifespan)
app.include_router(notes_api.router)

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
SPOTIFY_SCOPES = "user-read-currently-playing user-read-playback-state user-modify-playback-state"
OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

FRONTEND_ORIGIN = "https://realtime-display-lime.vercel.app"
WEATHER_CACHE_TTL_SECONDS = 15 * 60

weather_cache: dict[str, Any] = {
    "data": None,
    "updated_at": 0.0,
}

weather_lock = asyncio.Lock()
weather_refresh = asyncio.Event()
weather_enabled = True
weather_location = (41.0082, 28.9784)
weather_revision = 0
weather_retry = WeatherRetry()


def weather_snapshot(data, *, failed=False):
    # Copy metadata; never move the successful measurement time on a failure.
    age = max(0, int(time.time() - float(data.get("updatedAt", 0))))
    return {**data, "ageSeconds": age,
            "stale": failed or age >= WEATHER_CACHE_TTL_SECONDS}


async def refresh_weather_loop():
    delay = 0
    while True:
        if delay:
            try:
                await asyncio.wait_for(weather_refresh.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
        weather_refresh.clear()
        revision = weather_revision
        enabled, location = weather_enabled, weather_location
        if not enabled:
            delay = 60
            continue
        # No browser is needed. Request only while a client is interested.
        if not manager.active_connections:
            delay = 60
            continue
        try:
            data = await weather(*location)
            message = {**data, "enabled": True}
            delay = data.get("retryAfter", 60) if data["stale"] else WEATHER_CACHE_TTL_SECONDS
        except HTTPException as exc:
            delay = int((exc.headers or {}).get("Retry-After", 60))
            message = {"type": "weather_error", "error": weather_retry.error, "retryAfter": delay}
        # A browser may have disabled weather or changed location during IO.
        if revision != weather_revision:
            weather_refresh.set()
            continue
        await manager.broadcast(json.dumps(message, ensure_ascii=False), sender=None)


def configure_weather(payload):
    global weather_enabled, weather_location, weather_revision
    enabled = bool(payload.get("enabled", True))
    solar = payload.get("solar") or {}
    lat = float(payload.get("latitude", solar.get("latitude", weather_location[0])))
    lon = float(payload.get("longitude", solar.get("longitude", weather_location[1])))
    if not math.isfinite(lat) or not math.isfinite(lon) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ValueError("invalid_coordinates")
    weather_enabled, weather_location = enabled, (lat, lon)
    weather_revision += 1
    weather_refresh.set()

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


auth_manager = SpotifyAuthManager(spotify_token_auth)


async def spotify_get(url: str, session: str, method: str = "GET") -> tuple[httpx.Response, str]:
    return await auth_manager.request(url, session, method)


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
        self.roles: dict[WebSocket, str] = {}
        self.latest: dict[str, str] = {"indoor": json.dumps(indoor_sensor.unavailable())}
        self.indoor_received_at = 0.0
        self.lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self.lock:
            self.active_connections.add(websocket)
            self.roles[websocket] = websocket.query_params.get("role", "unknown")
            self.expire_indoor_state()
            for message in self.latest.values():
                cached = json.loads(message)
                if cached.get("type") == "weather" and cached.get("enabled"):
                    message = json.dumps(weather_snapshot(cached), ensure_ascii=False)
                await websocket.send_text(message)
            await self.send_status()
        if self.roles.get(websocket) == "device":
            weather_refresh.set()


    def disconnect(self, websocket: WebSocket) -> None:
        self.active_connections.discard(websocket)
        self.roles.pop(websocket, None)

    def expire_indoor_state(self) -> bool:
        state = json.loads(self.latest["indoor"])
        if state["sensorAvailable"] and time.monotonic() - self.indoor_received_at >= indoor_sensor.STALE_SECONDS:
            self.latest["indoor"] = json.dumps(indoor_sensor.unavailable(state["lastUpdated"]))
            return True
        return False

    async def send_status(self) -> None:
        status = json.dumps({"type": "status", "devices": sum(role == "device" for role in self.roles.values())})
        for connection in tuple(self.active_connections):
            if self.roles.get(connection) != "browser":
                continue
            try:
                await connection.send_text(status)
            except Exception:
                self.disconnect(connection)

    async def broadcast(self, message: str, sender: WebSocket) -> None:
        async with self.lock:
            payload = json.loads(message)
            kind = payload.get("type") if isinstance(payload, dict) else None
            if kind in {"weather", "spotify", "indoor"}:
                self.latest[kind] = message
            if kind == "indoor" and payload.get("sensorAvailable"):
                self.indoor_received_at = time.monotonic()
            await self._send_connections(message, sender)

    async def _send_connections(self, message, sender):
        for connection in tuple(self.active_connections):
            if connection is sender:
                continue
            try:
                await connection.send_text(message)
            except Exception:
                self.disconnect(connection)


manager = ConnectionManager()

async def expire_indoor_loop():
    while True:
        await asyncio.sleep(1)
        async with manager.lock:
            if manager.expire_indoor_state():
                await manager._send_connections(manager.latest["indoor"], sender=None)



async def publish_spotify(state):
    await manager.broadcast(json.dumps(state, ensure_ascii=False), sender=None)


player = SpotifyPlayer(spotify_get, publish_spotify,
    lambda: bool(manager.active_connections), unseal, auth_manager)


def device_can_control(websocket):
    expected = os.getenv("HOMEFLOW_DEVICE_TOKEN", "")
    supplied = websocket.headers.get("x-homeflow-device-token", "")
    return bool(expected and secrets.compare_digest(expected, supplied))


async def publish_notes_change(payload):
    await manager.broadcast(json.dumps(payload, ensure_ascii=False), sender=None)


notes_api.publish_change = publish_notes_change
notes_api.device_authorized = device_can_control


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "version": "2026-10-05-notes", "worker": os.getpid(),
        "spotify_control_configured": bool(os.getenv("HOMEFLOW_DEVICE_TOKEN")),
        "clients": len(manager.active_connections),
        "devices": sum(role == "device" for role in manager.roles.values()),
        "streams": list(manager.latest),
        "spotify_session_stored": auth_manager.persisted,
        "spotify_authorization_required": not bool(auth_manager.session) and not auth_manager.load_failed,
        "spotify_session_store_unavailable": auth_manager.load_failed or bool(auth_manager.tokens and not auth_manager.persisted)}


@app.get("/spotify/login")
async def spotify_login() -> RedirectResponse:
    client_id, _, redirect_uri = spotify_config()
    state = seal({"purpose": "oauth-state", "nonce": secrets.token_urlsafe(24)})
    query = urlencode({"client_id": client_id, "response_type": "code",
        "redirect_uri": redirect_uri, "scope": SPOTIFY_SCOPES, "state": state})
    response = RedirectResponse(f"{SPOTIFY_AUTH_URL}?{query}")
    response.set_cookie("homeflow_spotify_state", state, max_age=600,
        httponly=True, secure=True, samesite="lax", path="/spotify")
    return response


@app.get("/spotify/callback")
async def spotify_callback(request: Request) -> RedirectResponse:
    if request.query_params.get("error"):
        return RedirectResponse(FRONTEND_ORIGIN + "/#spotify_error=authorization_denied")
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    cookie_state = request.cookies.get("homeflow_spotify_state", "")
    if not code or not state or not secrets.compare_digest(state, cookie_state):
        raise HTTPException(400, "Invalid Spotify callback.")
    unseal(state, "oauth-state", ttl=600)
    _, _, redirect_uri = spotify_config()
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(SPOTIFY_TOKEN_URL,
                data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri},
                auth=spotify_token_auth())
    except httpx.HTTPError as exc:
        raise HTTPException(502, "Spotify authorization service unavailable.") from exc
    if response.status_code >= 400:
        raise HTTPException(400, "Spotify authorization failed.")
    try:
        session = make_session(response.json())
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(502, "Invalid Spotify authorization response.") from exc
    async with player.lock:
        await auth_manager.register(session)
        await player.remember(auth_manager.session)
        player.enabled = True
        player.last_fetch = 0
        player.retry_at = 0
    logging.getLogger("uvicorn.error.homeflow.spotify").info("Spotify authorization completed")
    # A URL fragment is never sent to Vercel or included in HTTP request logs.
    result = RedirectResponse(FRONTEND_ORIGIN + "/#spotify_session=" + auth_manager.client_session(),
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
    result.delete_cookie("homeflow_spotify_state", path="/spotify", secure=True, samesite="lax")
    return result


async def request_session(request: Request) -> str:
    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "authorization_required")
    session = authorization[7:]
    return await auth_manager.authorize(session)


async def request_object(request: Request) -> dict:
    try:
        body = await request.json()
    except (ValueError, UnicodeError) as exc:
        raise HTTPException(400, "invalid_json") from exc
    if not isinstance(body, dict):
        raise HTTPException(400, "invalid_json_object")
    return body


@app.get("/spotify/current")
async def spotify_current(request: Request) -> dict[str, Any]:
    state = await player.refresh(await request_session(request))
    return {**state, "session_token": auth_manager.client_session()}


@app.post("/spotify/enabled")
async def spotify_enabled(request: Request) -> dict[str, Any]:
    session = await request_session(request)
    body = await request_object(request)
    if not isinstance(body, dict) or not isinstance(body.get("enabled"), bool):
        raise HTTPException(400, "enabled_must_be_boolean")
    state = await player.set_enabled(session, body["enabled"])
    return {**state, "session_token": auth_manager.client_session()}


@app.post("/spotify/control")
async def spotify_control(request: Request) -> dict[str, Any]:
    session = await request_session(request)
    body = await request_object(request)
    action = body.get("action") if isinstance(body, dict) else None
    if not isinstance(action, str):
        raise HTTPException(400, "invalid_action")
    return await player.control(action)


@app.get("/weather")
async def weather(lat: float = 41.0082, lon: float = 28.9784) -> dict[str, Any]:
    if not math.isfinite(lat) or not math.isfinite(lon) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise HTTPException(400, "invalid_coordinates")
    # One provider request at a time, rather than one TLS client per requester.
    async with weather_lock:
        try:
            return await fetch_weather(lat, lon)
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
            delay = weather_retry.fail()
            logging.warning("Weather provider invalid payload; retry in %ss", delay)
            return weather_failure(lat, lon)


def weather_failure(lat, lon):
    delay = max(1, weather_retry.remaining())
    if weather_cache.get("location") == (lat, lon) and weather_cache["data"] is not None:
        return {**weather_snapshot(weather_cache["data"], failed=True),
                "retryAfter": delay, "providerError": weather_retry.error}
    raise HTTPException(502, {"error": weather_retry.error, "retryAfter": delay},
                        headers={"Retry-After": str(delay)})


async def fetch_weather(lat: float, lon: float) -> dict[str, Any]:
    import time

    now = time.time()

    same_location = weather_cache.get("location") == (lat, lon)

    # Cache'de güncel veri varsa dış API'ye tekrar gitme.
    if (
        same_location and weather_cache["data"] is not None
        and now - weather_cache["updated_at"] < WEATHER_CACHE_TTL_SECONDS
    ):
        return weather_snapshot(weather_cache["data"])

    # Reconnects, coordinate changes and queued failures cannot bypass this gate.
    if weather_retry.remaining():
        return weather_failure(lat, lon)

    params = {
        "latitude": lat,
        "longitude": lon,
        "timezone": "auto",
        "timeformat": "unixtime",
        "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset",
        "forecast_days": 7,
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                OPEN_METEO_FORECAST_URL,
                params=params,
            )

        response.raise_for_status()

    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        delay = weather_retry.fail(status, exc.response.headers.get("Retry-After"))
        # Classify quota feedback without logging URLs, credentials or response bodies.
        quota = "unspecified"
        if status == 429:
            try:
                reason = str(exc.response.json().get("reason", "")).lower()
                quota = next((label for label in ("daily", "hourly", "minutely")
                              if label in reason), "unspecified")
            except (ValueError, AttributeError):
                pass
        logging.warning("Weather provider HTTP=%s quota=%s; retry in %ss", status, quota, delay)
        return weather_failure(lat, lon)

    except httpx.HTTPError as exc:
        delay = weather_retry.fail()
        logging.warning("Weather provider transport=%s; retry in %ss", type(exc).__name__, delay)
        return weather_failure(lat, lon)

    payload = response.json()
    current = payload.get("current") or {}
    daily = payload.get("daily") or {}
    if not all(isinstance(current.get(field), (int, float)) and math.isfinite(current[field])
               for field in ("temperature_2m", "relative_humidity_2m", "weather_code", "wind_speed_10m")):
        raise ValueError("missing_current_metrics")

    condition, label = weather_label(
        int(current.get("weather_code", -1))
    )

    forecast: list[dict[str, Any]] = []

    dates = daily.get("time", [])
    codes = daily.get("weather_code", [])
    max_temps = daily.get("temperature_2m_max", [])
    min_temps = daily.get("temperature_2m_min", [])
    rain_probs = daily.get("precipitation_probability_max", [])

    for index, date_text in enumerate(dates[:7]):
        day_code = (
            int(codes[index])
            if index < len(codes)
            else -1
        )

        day_condition, day_label = weather_label(day_code)

        forecast.append(
            {
                "day": short_day(datetime.fromtimestamp(int(date_text) + int(payload.get("utc_offset_seconds", 0)), timezone.utc).strftime("%Y-%m-%d")),
                "condition": day_condition,
                "label": day_label,
                "code": day_code,
                "max": (
                    round(float(max_temps[index]))
                    if index < len(max_temps)
                    else 0
                ),
                "min": (
                    round(float(min_temps[index]))
                    if index < len(min_temps)
                    else 0
                ),
                "rain": (
                    int(rain_probs[index])
                    if (
                        index < len(rain_probs)
                        and rain_probs[index] is not None
                    )
                    else 0
                ),
            }
        )

    sunrises, sunsets = daily.get("sunrise", []), daily.get("sunset", [])
    solar_days = [
        {"start": int(start), "sunrise": int(rise), "sunset": int(setting)}
        for start, rise, setting in zip(dates[:7], sunrises, sunsets)
        if rise is not None and setting is not None and int(start) <= int(rise) < int(setting) < int(start) + 86400
    ]
    result = {
        "updatedAt": int(now),
        "observedAt": current.get("time"),
        "stale": False,
        "ageSeconds": 0,
        "solar": {"latitude": lat, "longitude": lon, "days": solar_days},
        "type": "weather",
        "location": "Istanbul",
        "temperature": round(
            float(current.get("temperature_2m", 0))
        ),
        "humidity": int(
            current.get("relative_humidity_2m", 0)
        ),
        "wind": round(
            float(current.get("wind_speed_10m", 0))
        ),
        "condition": condition,
        "label": label,
        "forecast": forecast,
    }

    # Başarılı sonucu cache'e koy.
    weather_cache["data"] = result
    weather_cache["updated_at"] = now
    weather_cache["location"] = (lat, lon)
    weather_retry.reset()

    return result



@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    try:
        await manager.connect(websocket)
        while True:
            raw_message = await websocket.receive_text()

            message = normalize_websocket_message(raw_message)
            payload = json.loads(message)
            if isinstance(payload, dict) and payload.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
            elif isinstance(payload, dict) and payload.get("type") == "weather_request":
                if weather_enabled:
                    # Wake the single refresh task; provider IO never delays pong.
                    weather_refresh.set()
                else:
                    await websocket.send_json({"type": "weather", "enabled": False})
            elif isinstance(payload, dict) and payload.get("type") == "weather":
                if manager.roles.get(websocket) != "browser":
                    continue
                try:
                    configure_weather(payload)
                except (ValueError, TypeError, AttributeError):
                    await websocket.send_json({"type": "weather_error", "error": "invalid_coordinates"})
                    continue
                if not weather_enabled:
                    await manager.broadcast(json.dumps({"type": "weather", "enabled": False}), sender=None)
            elif isinstance(payload, dict) and payload.get("type") == "indoor":
                if manager.roles.get(websocket) != "device" or not device_can_control(websocket):
                    await websocket.send_json({"type": "indoor_error", "error": "device_not_paired"})
                    continue
                state = indoor_sensor.normalize(payload)
                await manager.broadcast(json.dumps(state, ensure_ascii=False), sender=websocket)
            elif isinstance(payload, dict) and payload.get("type") in {"notes_list", "notes_open", "notes_set"}:
                await notes_api.handle_socket(websocket, payload)
            elif isinstance(payload, dict) and payload.get("type") in {"notes", "note_items", "notes_changed", "notes_result", "notes_error", "display"}:
                # Old browser snapshots must never overwrite durable checklist data.
                await websocket.send_json({"type": "notes_changed", "legacy_display_ignored": True})
            elif isinstance(payload, dict) and payload.get("type") == "spotify_control":
                action = payload.get("action")
                try:
                    if not device_can_control(websocket):
                        raise HTTPException(403, "device_not_paired")
                    if not isinstance(action, str):
                        raise HTTPException(400, "invalid_action")
                    result = await player.control(action)
                except HTTPException as exc:
                    result = {"type": "spotify_control_result", "ok": False,
                              "action": action if isinstance(action, str) else "",
                              "error": str(exc.detail), "status": exc.status_code}
                await websocket.send_json(result)
            elif isinstance(payload, dict) and payload.get("type") == "spotify":
                # Legacy browsers must not overwrite the authoritative server state.
                await websocket.send_json(player.state)
            else:
                await manager.broadcast(message, sender=websocket)
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)
        await websocket.close(code=1011)

    finally:
        notes_api.views.pop(websocket, None)
        manager.disconnect(websocket)
        async with manager.lock:
            await manager.send_status()
