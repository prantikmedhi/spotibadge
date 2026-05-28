"""
Spotify API integration module.

Uses the Spotify API to fetch currently playing or recently played tracks.
Requires environment variables:
    - SPOTIFY_CLIENT_ID: Your Spotify application client ID
    - SPOTIFY_SECRET_ID: Your Spotify application client secret
"""

from __future__ import annotations

import random
import time
from base64 import b64encode
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlencode

import requests

from .config import app_config, spotify_config
from .exceptions import APIError, AuthenticationError, NoTracksError


@dataclass
class TrackInfo:
    """Normalized track information."""

    is_playing: bool
    track_name: str
    artist_name: str
    album_name: str
    album_art_url: str
    track_url: str
    artist_url: str
    track_id: str = ""


@dataclass
class AudioFeatures:
    """Audio features for a track from Spotify's audio analysis."""

    tempo: float  # BPM (beats per minute)
    energy: float  # 0.0 to 1.0 - intensity and activity
    danceability: float  # 0.0 to 1.0 - how suitable for dancing
    valence: float  # 0.0 to 1.0 - musical positivity/happiness
    loudness: float  # dB - overall loudness

    @property
    def beat_duration_ms(self) -> int:
        """Calculate duration of one beat in milliseconds."""
        if self.tempo <= 0:
            return 500  # Default to 120 BPM
        return int(60000 / self.tempo)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for template use."""
        return {
            "tempo": self.tempo,
            "energy": self.energy,
            "danceability": self.danceability,
            "valence": self.valence,
            "loudness": self.loudness,
            "beat_duration_ms": self.beat_duration_ms,
        }


def _get_auth_header(client_id: str = "", client_secret: str = "") -> str:
    """Get base64 encoded Spotify application authorization header."""
    c_id = client_id or spotify_config.client_id
    c_secret = client_secret or spotify_config.client_secret
    credentials = f"{c_id}:{c_secret}"
    return b64encode(credentials.encode()).decode("ascii")


def is_configured() -> bool:
    """
    Check if Spotify environment variables are properly configured.
    
    Returns:
        True if all required variables are set, False otherwise
    """
    return spotify_config.is_configured()


def get_authorize_url(state: str, client_id: str = "") -> str:
    """Build the URL that starts Spotify OAuth for an end user."""
    params = {
        "client_id": client_id or spotify_config.client_id,
        "response_type": "code",
        "redirect_uri": app_config.callback_url(),
        "scope": "user-read-currently-playing user-read-recently-played user-top-read",
        "state": state,
        "show_dialog": "true",
    }
    return f"{spotify_config.authorize_url}?{urlencode(params)}"


def exchange_code(code: str, client_id: str = "", client_secret: str = "") -> dict[str, Any]:
    """Exchange an OAuth callback code for Spotify tokens."""
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": app_config.callback_url(),
    }
    headers = {"Authorization": f"Basic {_get_auth_header(client_id, client_secret)}"}

    try:
        response = requests.post(
            spotify_config.token_url,
            data=data,
            headers=headers,
            timeout=10,
        )
        if not response.ok:
            raise AuthenticationError("Spotify", f"Token exchange failed: {response.status_code} {response.text}")
        result = response.json()
    except requests.RequestException as e:
        raise AuthenticationError("Spotify", str(e)) from e

    if "access_token" not in result or "refresh_token" not in result:
        raise AuthenticationError("Spotify", "Token response was missing required fields")
    return result


def refresh_access_token(refresh_token: str, client_id: str = "", client_secret: str = "") -> dict[str, Any]:
    """Refresh a user's Spotify access token."""
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    headers = {"Authorization": f"Basic {_get_auth_header(client_id, client_secret)}"}

    try:
        response = requests.post(
            spotify_config.token_url,
            data=data,
            headers=headers,
            timeout=10,
        )
        if not response.ok:
            raise AuthenticationError("Spotify", f"Token refresh failed: {response.status_code} {response.text}")
        result = response.json()
    except requests.RequestException as e:
        raise AuthenticationError("Spotify", str(e)) from e

    if "access_token" not in result:
        raise AuthenticationError("Spotify", "No access token in refresh response")
    return result


def get_profile(access_token: str) -> dict[str, Any]:
    """Fetch the connected user's Spotify profile."""
    return _api_get(spotify_config.profile_url, access_token=access_token)


def token_expiry_timestamp(expires_in: int) -> int:
    """Convert Spotify's expires_in seconds into an epoch timestamp."""
    return int(time.time()) + max(0, expires_in - 60)


def _api_get(url: str, access_token: str) -> dict[str, Any]:
    """
    Make an authenticated GET request to the Spotify API.
    
    Args:
        url: The API endpoint URL
        access_token: Spotify user access token
        
    Returns:
        JSON response as dictionary
        
    Raises:
        APIError: If the request fails
        AuthenticationError: If authentication fails
    """
    headers = {"Authorization": f"Bearer {access_token}"}

    try:
        response = requests.get(url, headers=headers, timeout=10)

        if response.status_code == 204:
            raise NoTracksError("Spotify")

        if not response.ok:
            raise APIError("Spotify", response.status_code, response.text)

        return response.json()

    except requests.RequestException as e:
        raise APIError("Spotify", 0, str(e)) from e


def get_recent_tracks(access_token: str, limit: int = 10) -> dict[str, Any]:
    """
    Fetch recent tracks from Spotify.
    
    Args:
        access_token: Spotify user access token
        limit: Number of recent tracks to fetch (default 10, max 50)
    
    Returns:
        The API response containing recent tracks
        
    Raises:
        APIError: If the request fails
    """
    limit = min(max(1, limit), 50)  # Clamp to valid range
    url = f"{spotify_config.recently_played_url}?limit={limit}"
    return _api_get(url, access_token=access_token)


def get_audio_features(track_id: str, access_token: str) -> Optional[AudioFeatures]:
    """
    Fetch audio features for a track from Spotify.
    
    Args:
        track_id: Spotify track ID
        
    Returns:
        AudioFeatures object with tempo, energy, etc., or None if unavailable
    """
    if not track_id:
        return None

    try:
        url = f"https://api.spotify.com/v1/audio-features/{track_id}"
        data = _api_get(url, access_token=access_token)

        if not data:
            return None

        return AudioFeatures(
            tempo=data.get("tempo", 120.0),
            energy=data.get("energy", 0.5),
            danceability=data.get("danceability", 0.5),
            valence=data.get("valence", 0.5),
            loudness=data.get("loudness", -10.0),
        )

    except (APIError, NoTracksError):
        # Audio features not available for this track
        return None


def _extract_track_info(item: dict[str, Any], is_playing: bool) -> TrackInfo:
    """
    Extract normalized track information from Spotify API response item.
    
    Args:
        item: Track item from Spotify API
        is_playing: Whether the track is currently playing
        
    Returns:
        Normalized TrackInfo object
    """
    # Extract album art URL (prefer medium size - index 1)
    album_art_url = ""
    images = item.get("album", {}).get("images", [])
    if images:
        # Prefer medium size (index 1), fall back to first available
        album_art_url = images[1]["url"] if len(images) > 1 else images[0]["url"]

    # Extract artist info (use first artist)
    artists = item.get("artists", [{}])
    first_artist = artists[0] if artists else {}

    # Extract track ID from URI or ID field
    track_id = item.get("id", "")
    if not track_id:
        uri = item.get("uri", "")
        if uri.startswith("spotify:track:"):
            track_id = uri.split(":")[-1]

    return TrackInfo(
        is_playing=is_playing,
        track_name=item.get("name", "Unknown Track"),
        artist_name=first_artist.get("name", "Unknown Artist"),
        album_name=item.get("album", {}).get("name", "Unknown Album"),
        album_art_url=album_art_url,
        track_url=item.get("external_urls", {}).get("spotify", ""),
        artist_url=first_artist.get("external_urls", {}).get("spotify", ""),
        track_id=track_id,
    )


def get_top_items(access_token: str, item_type: str = "tracks", time_range: str = "short_term", limit: int = 5) -> dict[str, Any]:
    """
    Get the user's top tracks or artists from Spotify.
    """
    url = f"{spotify_config.top_tracks_url if item_type == 'tracks' else spotify_config.top_artists_url}?limit={limit}&time_range={time_range}"
    data = _api_get(url, access_token=access_token)
    items = data.get("items", [])

    if not items:
        raise NoTracksError("Spotify")

    result_items = []
    for item in items:
        if item_type == "artists":
            images = item.get("images", [])
            image_url = images[1]["url"] if len(images) > 1 else (images[0]["url"] if images else "")
            result_items.append({
                "track_name": item.get("name", "Unknown Artist"),
                "artist_name": "Top Artist",
                "album_art_url": image_url,
                "track_url": item.get("external_urls", {}).get("spotify", ""),
            })
        else:
            track_info = _extract_track_info(item, False)
            result_items.append({
                "track_name": track_info.track_name,
                "artist_name": track_info.artist_name,
                "album_art_url": track_info.album_art_url,
                "track_url": track_info.track_url,
            })

    return {
        "type": "list",
        "items": result_items,
        # We can use the first item's album art as the background for the whole widget
        "album_art_url": result_items[0]["album_art_url"] if result_items else "",
        "track_url": result_items[0]["track_url"] if result_items else "",
    }


def get_recently_played_items(access_token: str, limit: int = 5) -> dict[str, Any]:
    """
    Get the most recently played tracks from Spotify.
    """
    data = _api_get(f"{spotify_config.recently_played_url}?limit={limit}", access_token=access_token)
    items = data.get("items", [])

    if not items:
        raise NoTracksError("Spotify")

    result_items = []
    # Deduplicate tracks (recently played can have the same track back-to-back)
    seen_ids = set()
    for item_wrapper in items:
        item = item_wrapper["track"]
        track_info = _extract_track_info(item, False)
        if track_info.track_id in seen_ids:
            continue
        seen_ids.add(track_info.track_id)
        
        result_items.append({
            "track_name": track_info.track_name,
            "artist_name": track_info.artist_name,
            "album_art_url": track_info.album_art_url,
            "track_url": track_info.track_url,
        })
        if len(result_items) == limit:
            break

    # If deduplication caused us to have no items (e.g. all empty IDs?), fallback
    if not result_items:
        raise NoTracksError("Spotify")

    return {
        "type": "list",
        "items": result_items,
        "album_art_url": result_items[0]["album_art_url"],
        "track_url": result_items[0]["track_url"],
    }


def get_now_playing(access_token: str) -> dict[str, Any]:
    """
    Get the currently playing or most recently played track from Spotify.
    
    Returns:
        A normalized track object with the following structure:
            - is_playing: bool - Whether the track is currently playing
            - track_name: str - Name of the track
            - artist_name: str - Name of the artist
            - album_name: str - Name of the album
            - album_art_url: str - URL to the album art
            - track_url: str - URL to the track on Spotify
            - artist_url: str - URL to the artist on Spotify
            - audio_features: dict or None - Audio features (tempo, energy, etc.)
    
    Raises:
        NoTracksError: If no tracks are available
        APIError: If the API request fails
    """
    is_playing = False
    item: Optional[dict[str, Any]] = None

    # Try to get currently playing track
    try:
        data = _api_get(spotify_config.now_playing_url, access_token=access_token)
        if data and "item" in data:
            is_playing = data.get("is_playing", False)
            item = data["item"]
    except NoTracksError:
        pass  # Fall through to get recent tracks

    # If not currently playing, get from recently played
    if item is None:
        data = _api_get(f"{spotify_config.recently_played_url}?limit=10", access_token=access_token)
        items = data.get("items", [])

        if not items:
            raise NoTracksError("Spotify")

        # Pick a random recent track for variety
        random_index = random.randint(0, len(items) - 1)
        item = items[random_index]["track"]
        is_playing = False

    track_info = _extract_track_info(item, is_playing)

    # Fetch audio features for BPM-synced animation
    audio_features = get_audio_features(track_info.track_id, access_token=access_token)

    # Return as dictionary for compatibility with existing code
    return {
        "is_playing": track_info.is_playing,
        "track_name": track_info.track_name,
        "artist_name": track_info.artist_name,
        "album_name": track_info.album_name,
        "album_art_url": track_info.album_art_url,
        "track_url": track_info.track_url,
        "artist_url": track_info.artist_url,
        "audio_features": audio_features.to_dict() if audio_features else None,
    }
