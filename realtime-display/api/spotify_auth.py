"""Single-household OAuth owner; refresh and disk updates share one async lock."""
import asyncio
import hashlib
import logging
import math
import secrets
import time

import httpx
from fastapi import HTTPException

from spotify_session import seal, unseal
from spotify_token_store import session_store

log = logging.getLogger('uvicorn.error.homeflow.spotify')
TOKEN_URL = 'https://accounts.spotify.com/api/token'


def refresh_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


class SpotifyAuthManager:
    def __init__(self, credentials, store=None):
        self.credentials = credentials
        self.store = store or session_store()
        self.lock = asyncio.Lock()
        self.tokens = None
        self.session = ''
        self.enabled = True
        self.invalid = False
        self.persisted = False
        self.restore_pending = False
        self.retry_at = 0.0
        self.load_failed = False

    async def _commit(self, tokens):
        # Keep a rotated token in memory even if disk fails; never roll it back.
        self.tokens = tokens
        self.session = seal(tokens)
        try:
            await asyncio.to_thread(self.store.save, self.session)
            self.persisted = True
        except OSError:
            self.persisted = False
            log.error('[Spotify] Session persistence unavailable; restart recovery is not guaranteed.')

    async def register(self, session):
        async with self.lock:
            await self._register(session)

    async def _register(self, session):
        tokens = unseal(session, 'session')
        if not isinstance(tokens.get('refresh_token'), str) or not tokens['refresh_token']:
            raise HTTPException(502, 'invalid_authorization_response')
        if not isinstance(tokens.get('access_token'), str) or not tokens['access_token']:
            raise HTTPException(502, 'invalid_authorization_response')
        expiry = tokens.get('expires_at')
        if not isinstance(expiry, (int, float)) or not math.isfinite(expiry):
            raise HTTPException(502, 'invalid_authorization_response')
        tokens = {**tokens, 'session_id': tokens.get('session_id') or secrets.token_urlsafe(32),
                  'legacy_refresh_hash': tokens.get('legacy_refresh_hash') or refresh_hash(tokens['refresh_token']),
                  'enabled': True, 'invalid': False}
        self.enabled, self.invalid, self.restore_pending = True, False, False
        self.retry_at = 0
        await self._commit(tokens)
        if not self.persisted:
            raise HTTPException(503, 'session_persistence_unavailable')

    async def authorize(self, session):
        async with self.lock:
            return await self._authorize(session)

    async def _authorize(self, session):
        incoming = unseal(session, 'session')
        if self.load_failed and not self.tokens:
            raise HTTPException(503, 'session_store_unavailable')
        if self.invalid:
            raise HTTPException(401, 'authorization_required')
        if not self.tokens:
            # Upgrade an existing encrypted browser session once. New clients hold
            # only an opaque household ID and cannot supply Spotify credentials.
            if incoming.get('refresh_token'):
                await self._register(session)
                self.restore_pending = True
            else:
                raise HTTPException(401, 'authorization_required')
        if incoming.get('session_id'):
            matches = secrets.compare_digest(str(incoming['session_id']), self.tokens['session_id'])
        else:
            token = incoming.get('refresh_token')
            fingerprint = refresh_hash(token) if isinstance(token, str) else ''
            matches = fingerprint in {self.tokens['legacy_refresh_hash'], refresh_hash(self.tokens['refresh_token'])}
        if not matches:
            raise HTTPException(403, 'different_spotify_session')
        # A stale browser is authenticated, but its older tokens never overwrite
        # the current credentials or the rotated refresh token on disk.
        return self.session

    def client_session(self):
        if not self.tokens or self.invalid:
            return ''
        return seal({'purpose': 'session', 'session_id': self.tokens['session_id']})

    async def set_enabled(self, enabled):
        async with self.lock:
            self.enabled = enabled
            if self.tokens:
                await self._commit({**self.tokens, 'enabled': enabled})
                if not self.persisted:
                    raise HTTPException(503, 'session_persistence_unavailable')

    async def invalidate(self):
        self.invalid = True
        self.restore_pending = False
        if self.tokens:
            # Persist a tombstone: an old browser must not revive revoked auth.
            await self._commit({**self.tokens, 'invalid': True})
        self.session = ''
        log.warning('[Spotify] Stored Spotify authorization is no longer valid.')

    async def load(self):
        try:
            session = await asyncio.to_thread(self.store.load)
            self.load_failed = False
            if session is None:
                log.info('[Spotify] No stored session. Authorization required.')
                return False
            tokens = unseal(session, 'session')
            if (not isinstance(tokens.get('refresh_token'), str) or not tokens['refresh_token']
                    or not isinstance(tokens.get('access_token'), str) or not tokens['access_token']
                    or not isinstance(tokens.get('expires_at'), (int, float))
                    or not math.isfinite(tokens['expires_at'])):
                raise HTTPException(401, 'authorization_required')
            tokens.setdefault('session_id', secrets.token_urlsafe(32))
            tokens.setdefault('legacy_refresh_hash', refresh_hash(tokens['refresh_token']))
            if not isinstance(tokens['session_id'], str) or not isinstance(tokens['legacy_refresh_hash'], str):
                raise HTTPException(401, 'authorization_required')
            self.enabled = tokens.get('enabled', True) is not False
            self.invalid = bool(tokens.get('invalid'))
            self.tokens, self.session = tokens, session
            self.persisted = True
            if self.invalid:
                self.session = ''
                log.warning('[Spotify] Stored Spotify authorization is no longer valid.')
                return False
            self.restore_pending = True
            log.info('[Spotify] Stored session found')
            return True
        except HTTPException as exc:
            if exc.status_code == 503:
                log.error('[Spotify] Session restore unavailable; Spotify configuration is missing.')
                return False
            self.invalid = True
            log.warning('[Spotify] Stored Spotify authorization is no longer valid.')
        except (ValueError, TypeError, KeyError, AttributeError, UnicodeError):
            self.invalid = True
            log.warning('[Spotify] Stored Spotify authorization is no longer valid.')
        except OSError:
            self.load_failed = True
            self.persisted = False
            log.error('[Spotify] Stored session could not be read; authorization was not cleared.')
        return False

    async def restore(self):
        async with self.lock:
            if not self.session or self.invalid:
                return False
            log.info('[Spotify] Restoring session...')
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    await self._refresh(client)
                self.restore_pending = False
                log.info('[Spotify] Session restored successfully')
                return True
            except httpx.HTTPError:
                log.warning('[Spotify] Session restore deferred; token service unreachable.')
            except (ValueError, KeyError, TypeError):
                log.warning('[Spotify] Session restore deferred; invalid token service response.')
            except HTTPException as exc:
                if exc.status_code != 401:
                    log.warning('[Spotify] Session restore deferred; token service unavailable.')
            return False

    async def _refresh(self, client):
        if time.monotonic() < self.retry_at:
            raise HTTPException(429, 'rate_limited')
        response = await client.post(TOKEN_URL,
            data={'grant_type': 'refresh_token', 'refresh_token': self.tokens['refresh_token']},
            auth=self.credentials())
        if response.status_code == 429:
            try:
                delay = float(response.headers.get('Retry-After', 30))
                delay = delay if math.isfinite(delay) else 30
            except ValueError:
                delay = 30
            self.retry_at = time.monotonic() + max(1, delay)
            raise HTTPException(429, 'rate_limited')
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError('Invalid token response')
        if response.status_code == 400 and isinstance(payload, dict) and payload.get('error') == 'invalid_grant':
            await self.invalidate()
            raise HTTPException(401, 'authorization_required')
        if response.status_code >= 400:
            # invalid_client, 5xx and network failures are not revoked user auth.
            raise HTTPException(502, 'token_service_unavailable')
        access = payload.get('access_token')
        expires = payload.get('expires_in', 3600)
        if not isinstance(access, str) or not access or not isinstance(expires, (int, float)) or not math.isfinite(expires) or expires <= 0:
            raise ValueError('Invalid token response')
        replacement = payload.get('refresh_token')
        refresh = replacement if isinstance(replacement, str) and replacement else self.tokens['refresh_token']
        # Save immediately, before the playback request: Spotify may rotate the
        # refresh token even if the following API request fails or times out.
        await self._commit({**self.tokens, 'access_token': access, 'refresh_token': refresh,
                      'expires_at': time.time() + expires})
        self.restore_pending = False
        log.info('[Spotify] Access token refreshed')

    async def request(self, url, session, method='GET'):
        async with self.lock:
            await self._authorize(session)
            if not self.persisted:
                await self._commit(self.tokens)
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    if self.restore_pending or self.tokens.get('expires_at', 0) <= time.time() + 30:
                        await self._refresh(client)
                    response = await client.request(method, url,
                        headers={'Authorization': 'Bearer ' + self.tokens['access_token']})
                    if response.status_code == 401:
                        await self._refresh(client)
                        response = await client.request(method, url,
                            headers={'Authorization': 'Bearer ' + self.tokens['access_token']})
                        # A rejected access token is not proof of invalid_grant.
                        if response.status_code == 401:
                            raise HTTPException(502, 'token_service_unavailable')
                return response, self.session
            except httpx.HTTPError as exc:
                raise HTTPException(502, 'service_unavailable') from exc
            except (ValueError, KeyError, TypeError) as exc:
                raise HTTPException(502, 'invalid_response') from exc
