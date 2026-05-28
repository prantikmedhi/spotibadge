"""Stateless token store for connected Spotify users.

Encodes user credentials into a URL-safe signed token (public_id) 
to eliminate the need for a database.
Uses an in-memory cache to temporarily store access tokens and reduce API calls.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from itsdangerous import URLSafeSerializer, BadSignature

from .config import app_config

@dataclass(frozen=True)
class ConnectedUser:
    public_id: str
    spotify_user_id: str
    display_name: str
    refresh_token: str
    access_token: str
    expires_at: int
    client_id: str = ""
    client_secret: str = ""

# In-memory cache for access tokens: refresh_token -> (access_token, expires_at)
_access_token_cache: dict[str, tuple[str, int]] = {}

def _get_serializer() -> URLSafeSerializer:
    """Get the serializer using the application secret key."""
    return URLSafeSerializer(app_config.secret_key, salt="spotibadge-app")

def generate_public_id(refresh_token: str, display_name: str, client_id: str = "", client_secret: str = "") -> str:
    """
    Generate a stateless public_id containing the refresh token, display name,
    and optional user-provided Spotify credentials.
    """
    s = _get_serializer()
    # Use short keys to keep the URL as short as possible
    payload = {
        "r": refresh_token,
        "d": display_name
    }
    if client_id:
        payload["ci"] = client_id
    if client_secret:
        payload["cs"] = client_secret
        
    return s.dumps(payload)

def get_user(public_id: str) -> Optional[ConnectedUser]:
    """
    Decode the public_id back into a ConnectedUser object.
    Since we don't store access tokens in the URL, we check the in-memory cache.
    If not cached or expired, we return it with expires_at=0 to force a refresh.
    """
    s = _get_serializer()
    try:
        data = s.loads(public_id)
    except BadSignature:
        return None

    refresh_token = data.get("r", "")
    display_name = data.get("d", "")
    client_id = data.get("ci", "")
    client_secret = data.get("cs", "")

    if not refresh_token:
        return None

    # Check in-memory cache for access token
    access_token = ""
    expires_at = 0
    cached = _access_token_cache.get(refresh_token)
    if cached:
        cached_token, cached_expiry = cached
        if cached_expiry > int(time.time()):
            access_token = cached_token
            expires_at = cached_expiry

    return ConnectedUser(
        public_id=public_id,
        spotify_user_id="", # Not needed for stateless operations
        display_name=display_name,
        refresh_token=refresh_token,
        access_token=access_token,
        expires_at=expires_at,
        client_id=client_id,
        client_secret=client_secret,
    )

def get_user_by_spotify_id(spotify_user_id: str) -> Optional[ConnectedUser]:
    """
    Unsupported in stateless mode. 
    Always returns None to force generating a new public_id on login.
    """
    return None

def save_user(
    *,
    public_id: str,
    spotify_user_id: str,
    display_name: str,
    refresh_token: str,
    access_token: str,
    expires_at: int,
) -> None:
    """
    In stateless mode, there is no database. We only update the in-memory cache.
    """
    _access_token_cache[refresh_token] = (access_token, expires_at)

def update_tokens(public_id: str, access_token: str, expires_at: int, refresh_token: str | None = None) -> None:
    """
    Update the access token in the in-memory cache.
    """
    user = get_user(public_id)
    if user:
        r_token = refresh_token or user.refresh_token
        _access_token_cache[r_token] = (access_token, expires_at)
