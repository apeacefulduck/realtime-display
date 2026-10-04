"""Single-home Spotify player. OAuth and playback are owned by the server.

Run one ASGI worker; mount SPOTIFY_SESSION_FILE on persistent storage if sessions
must survive a host replacement. The file contains an authenticated ciphertext.
"""
import asyncio
import contextlib
import json
import logging
import math
import os
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException

log = logging.getLogger("uvicorn.error.homeflow.spotify")
PLAYER = "https://api.spotify.com/v1/me/player"
POLL_SECONDS = 5
ACTIONS = {"play": ("PUT", "play"), "resume": ("PUT", "play"),
           "pause": ("PUT", "pause"), "next": ("POST", "next"),
           "previous": ("POST", "previous")}


def milliseconds(value):
    if isinstance(value, (int, float)) and math.isfinite(value):
        return max(0, min(int(value), 86400000))
    return 0


def empty_state(error="", connected=False):
    return dict(type="spotify", enabled=connected, connected=connected,
                playing=False, is_playing=False, title="", track_name="",
                artist="", artist_name="", album="", album_name="",
                track_id="", album_art_url="", progress_ms=0, duration_ms=0,
                has_active_device=False, device_name="", error=error,
                state_updated_at=int(time.time() * 1000))


def playback_state(payload):
    if not isinstance(payload, dict):
        raise ValueError("Invalid playback response")
    state = empty_state(connected=True)
    item = payload.get("item") or {}
    device = payload.get("device") or {}
    if not isinstance(item, dict) or not isinstance(device, dict):
        raise ValueError("Invalid playback item/device")
    state.update(has_active_device=bool(device.get("is_active")),
                 device_name=str(device.get("name") or "")[:120])
    if not item:
        return state
    album = item.get("album") or item.get("show") or {}
    images = album.get("images") or item.get("images") or []
    candidates = [im for im in images if isinstance(im, dict)
                  and urlparse(str(im.get("url", ""))).scheme == "https"
                  and urlparse(str(im.get("url", ""))).hostname == "i.scdn.co"]
    # Prefer the smallest native image; Spotify normally supplies 64px artwork.
    art = min(candidates, key=lambda im: milliseconds(im.get("width")) or 9999, default={})
    title = str(item.get("name") or "")[:240]
    artist = ", ".join(str(a.get("name") or "") for a in item.get("artists", [])
                       if isinstance(a, dict))[:240] or str(album.get("publisher") or "")[:240]
    album_name = str(album.get("name") or "")[:240]
    duration = milliseconds(item.get("duration_ms"))
    state.update(title=title, track_name=title, artist=artist, artist_name=artist,
                 album=album_name, album_name=album_name,
                 track_id=str(item.get("id") or item.get("uri") or "")[:160],
                 album_art_url=str(art.get("url") or ""), duration_ms=duration,
                 progress_ms=min(milliseconds(payload.get("progress_ms")), duration),
                 playing=bool(payload.get("is_playing")), is_playing=bool(payload.get("is_playing")))
    return state


class SpotifyPlayer:
    def __init__(self, request, publish, has_clients, validate):
        self.request, self.publish = request, publish
        self.has_clients, self.validate = has_clients, validate
        self.lock = asyncio.Lock()
        self.session = ""
        self.enabled = True
        self.state = empty_state("authorization_required")
        self.retry_at = 0.0
        self.last_fetch = 0.0
        self.last_command = 0.0
        self.task = None
        self.path = Path(os.getenv("SPOTIFY_SESSION_FILE", ".spotify-session"))

    def remember(self, session):
        self.validate(session, "session")
        changed = session != self.session
        self.session = session
        if changed:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.path.with_suffix(".tmp")
                temporary.write_text(session, encoding="utf-8")
                temporary.chmod(0o600)
                temporary.replace(self.path)
            except OSError:
                log.warning("Spotify session persistence unavailable; session remains in memory")

    async def start(self):
        try:
            self.remember(self.path.read_text(encoding="utf-8").strip())
        except (OSError, HTTPException):
            pass
        await self.publish(self.state)
        self.task = asyncio.create_task(self.poll())

    async def stop(self):
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task

    async def poll(self):
        while True:
            if self.session and self.enabled and self.has_clients():
                try:
                    await self.refresh()
                except HTTPException:
                    pass
            await asyncio.sleep(POLL_SECONDS)

    def check_response(self, response):
        status = response.status_code
        if status < 400:
            return
        errors = {401: "authorization_required", 403: "scope_or_premium_required",
                  404: "no_active_device", 429: "rate_limited"}
        if status == 429:
            try:
                delay = float(response.headers.get("Retry-After", "30"))
                if not math.isfinite(delay):
                    delay = 30
            except ValueError:
                delay = 30
            self.retry_at = time.monotonic() + max(1, delay)
        raise HTTPException(status if status in errors else 502, errors.get(status, "service_unavailable"))

    async def api(self, method, suffix=""):
        if time.monotonic() < self.retry_at:
            raise HTTPException(429, "rate_limited")
        if not self.session:
            raise HTTPException(401, "authorization_required")
        try:
            response, session = await self.request(PLAYER + ("/" + suffix if suffix else ""), self.session, method)
            self.remember(session)
            self.check_response(response)
            return response
        except httpx.HTTPError as exc:
            raise HTTPException(502, "service_unavailable") from exc
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(502, "invalid_response") from exc

    async def fetch_locked(self):
        try:
            response = await self.api("GET")
            state = empty_state(connected=True) if response.status_code == 204 else playback_state(response.json())
            self.last_fetch = time.monotonic()
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            await self.fail(502, "invalid_response")
            raise HTTPException(502, "invalid_response") from exc
        except HTTPException as exc:
            await self.fail(exc.status_code, str(exc.detail))
            raise
        if state["track_id"] != self.state["track_id"]:
            log.info("Spotify track changed")
        if state["album_art_url"] != self.state["album_art_url"]:
            log.info("Spotify album art changed")
        if state["is_playing"] != self.state["is_playing"] or state["has_active_device"] != self.state["has_active_device"]:
            log.info("Spotify playback state: playing=%s active_device=%s", state["is_playing"], state["has_active_device"])
        self.state = state
        await self.publish(state)
        return state

    async def fail(self, status, error):
        if status == 401:
            error = "authorization_required"
            self.session = ""
        if self.state.get("error") != error:
            log.warning("Spotify API error: %s", error)
        self.state = empty_state(error, connected=bool(self.session))
        if status not in (401, 429):
            self.retry_at = max(self.retry_at, time.monotonic() + 10)
        await self.publish(self.state)

    async def refresh(self, session=None):
        async with self.lock:
            if session:
                self.remember(session)
            if not self.enabled:
                return self.state
            if self.last_fetch and time.monotonic() - self.last_fetch < POLL_SECONDS:
                return self.state
            return await self.fetch_locked()

    async def set_enabled(self, session, enabled):
        async with self.lock:
            self.remember(session)
            self.enabled = enabled
            self.last_fetch = 0
            if not enabled:
                self.state = empty_state()
                await self.publish(self.state)
                return self.state
            return await self.fetch_locked()

    async def control(self, action):
        if action not in {*ACTIONS, "toggle"}:
            raise HTTPException(400, "invalid_action")
        async with self.lock:
            if not self.enabled:
                raise HTTPException(409, "spotify_disabled")
            now = time.monotonic()
            if now - self.last_command < 0.75:
                raise HTTPException(429, "command_cooldown")
            self.last_command = now
            # A toggle always uses actual playback, never an optimistic browser value.
            if action == "toggle" or now - self.last_fetch > POLL_SECONDS:
                await self.fetch_locked()
            if not self.state["has_active_device"]:
                raise HTTPException(404, "no_active_device")
            chosen = ("pause" if self.state["is_playing"] else "play") if action == "toggle" else action
            method, suffix = ACTIONS[chosen]
            previous_track = self.state["track_id"]
            try:
                await self.api(method, suffix)
            except HTTPException as exc:
                await self.fail(exc.status_code, str(exc.detail))
                raise
            log.info("Spotify control: %s", chosen)
            await asyncio.sleep(0.35)
            # Report accepted control separately if its follow-up read fails.
            try:
                await self.fetch_locked()
                if chosen in {"next", "previous"} and self.state["track_id"] == previous_track:
                    # Spotify may acknowledge before metadata has propagated.
                    await asyncio.sleep(0.65)
                    await self.fetch_locked()
                refreshed = True
            except HTTPException:
                refreshed = False
            return dict(type="spotify_control_result", ok=True, action=action, state_refreshed=refreshed)
