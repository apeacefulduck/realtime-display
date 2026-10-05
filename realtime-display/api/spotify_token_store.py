"""Atomic storage for authenticated ciphertext, outside the source tree by default."""
import logging
import os
import tempfile
from pathlib import Path

import httpx

log = logging.getLogger('uvicorn.error.homeflow.spotify')


class SpotifyTokenStore:
    def __init__(self):
        configured = os.getenv('SPOTIFY_SESSION_FILE')
        if configured:
            self.path = Path(configured).expanduser().resolve()
        else:
            base = os.getenv('LOCALAPPDATA') if os.name == 'nt' else os.getenv('XDG_DATA_HOME')
            self.path = (Path(base) if base else Path.home() / '.local' / 'share') / 'HomeFlow' / '.spotify-session'
        self.legacy_paths = [Path.cwd() / '.spotify-session', Path(__file__).parent / '.spotify-session']
        if os.getenv('RENDER') and not configured:
            log.warning('[Spotify] Render storage is ephemeral. Set SPOTIFY_SESSION_FILE on persistent storage.')

    def load(self):
        try:
            return self.path.read_text(encoding='utf-8').strip()
        except FileNotFoundError:
            for legacy in self.legacy_paths:
                if legacy != self.path and legacy.is_file():
                    return legacy.read_text(encoding='utf-8').strip()
            return None

    def save(self, ciphertext):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=self.path.name + '.', suffix='.tmp', dir=self.path.parent)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                stream.write(ciphertext)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)


class SupabaseTokenStore:
    """Server-only PostgREST store. The database holds Fernet ciphertext only."""
    def __init__(self):
        self.url = os.getenv('HOMEFLOW_SUPABASE_URL', '').rstrip('/')
        self.key = os.getenv('HOMEFLOW_SUPABASE_SECRET_KEY', '')
        self.slot = 'spotify'

    def _client(self):
        if not self.url.startswith('https://') or not self.key:
            raise OSError('Persistent session store is not configured')
        headers = {'apikey': self.key, 'Cache-Control': 'no-store'}
        # Modern sb_secret keys use apikey alone. Legacy service_role JWTs also
        # need Authorization; never send a modern key as a Bearer JWT.
        if not self.key.startswith('sb_secret_'):
            headers['Authorization'] = 'Bearer ' + self.key
        return httpx.Client(timeout=15, headers=headers)

    def load(self):
        try:
            with self._client() as client:
                response = client.get(self.url + '/rest/v1/homeflow_sessions',
                    params={'slot': 'eq.' + self.slot, 'select': 'ciphertext', 'limit': '1'})
                response.raise_for_status()
                rows = response.json()
                if not isinstance(rows, list):
                    raise ValueError('Invalid store response')
                if not rows:
                    return None
                ciphertext = rows[0].get('ciphertext')
                if not isinstance(ciphertext, str) or not ciphertext:
                    raise ValueError('Invalid stored session')
                return ciphertext
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
            # Do not include response bodies, headers or secret-bearing requests.
            raise OSError('Persistent session store read failed') from None

    def save(self, ciphertext):
        try:
            with self._client() as client:
                response = client.post(self.url + '/rest/v1/homeflow_sessions',
                    params={'on_conflict': 'slot'},
                    headers={'Prefer': 'resolution=merge-duplicates,return=minimal'},
                    json={'slot': self.slot, 'ciphertext': ciphertext})
                response.raise_for_status()
        except httpx.HTTPError:
            raise OSError('Persistent session store write failed') from None


def session_store():
    backend = os.getenv('HOMEFLOW_SESSION_STORE', 'file')
    if backend == 'supabase':
        return SupabaseTokenStore()
    if backend != 'file':
        raise ValueError('Unknown session store backend')
    return SpotifyTokenStore()
