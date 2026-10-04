"""Authenticated, encrypted OAuth sessions that survive service restarts."""
import base64
import hashlib
import json
import os
import time

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException

SESSION_TTL = 30 * 24 * 60 * 60

def cipher():
    secret = os.environ.get('SPOTIFY_CLIENT_SECRET', '')
    if not secret:
        raise HTTPException(503, 'Spotify is not configured.')
    key = hashlib.sha256(('homeflow-spotify-session-v1:' + secret).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))

def seal(payload):
    return cipher().encrypt(json.dumps(payload).encode()).decode()

def unseal(token, purpose, ttl=SESSION_TTL):
    try:
        payload = json.loads(cipher().decrypt(token.encode(), ttl=ttl))
        if payload.get('purpose') != purpose:
            raise ValueError('Wrong token purpose')
        return payload
    except (InvalidToken, ValueError, TypeError, AttributeError):
        raise HTTPException(401, 'Spotify session expired. Connect Spotify again.') from None

def make_session(tokens):
    return seal({'purpose': 'session', 'refresh_token': tokens['refresh_token'],
                 'access_token': tokens['access_token'],
                 'expires_at': time.time() + tokens.get('expires_in', 3600)})
