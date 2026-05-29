"""
Orchestrator module - handles the main Flask app, routing, and SVG generation.

Abstracts away the core functionality from the music service providers (Spotify, Last.fm).
"""

from __future__ import annotations

import colorsys
import json
import os
import random
import secrets
import time
from io import BytesIO
from typing import Any, Optional, Tuple

import requests
from base64 import b64encode

from colorthief import ColorThief
from flask import Flask, Response, render_template, request, redirect, session, url_for

from .config import (
    app_config,
    ColorPalette,
    compact_svg_config,
    svg_config,
    template_config,
    validate_background_type,
    validate_hex_color,
    validate_int,
)
from .exceptions import (
    AuthenticationError,
    ImageProcessingError,
    MusicWidgetError,
)
from . import spotify
from .storage import ConnectedUser, generate_public_id, get_user, save_user, update_tokens


app = Flask(__name__)
app.secret_key = app_config.secret_key


# ============================================================================
# Image Processing
# ============================================================================


class ImageData:
    """
    Container for image data and extracted color palettes.
    
    Fetches image once and caches the bytes for reuse.
    """

    def __init__(self, url: str) -> None:
        self.url = url
        self._bytes: Optional[bytes] = None
        self._bar_palette: Optional[ColorPalette] = None
        self._song_palette: Optional[ColorPalette] = None

    def _fetch(self) -> bytes:
        """Fetch image bytes from URL."""
        if self._bytes is None:
            try:
                response = requests.get(self.url, timeout=10)
                response.raise_for_status()
                self._bytes = response.content
            except requests.RequestException as e:
                raise ImageProcessingError(str(e)) from e
        return self._bytes

    def get_base64(self) -> str:
        """Get image as base64 encoded string."""
        return b64encode(self._fetch()).decode("ascii")

    def get_palette(self, color_count: int) -> ColorPalette:
        """Extract color palette from image."""
        try:
            image_bytes = self._fetch()
            color_thief = ColorThief(BytesIO(image_bytes))
            return color_thief.get_palette(color_count)
        except Exception as e:
            raise ImageProcessingError(str(e)) from e

    @property
    def bar_palette(self) -> ColorPalette:
        """Get 4-color palette for equalizer bars (cached)."""
        if self._bar_palette is None:
            self._bar_palette = self.get_palette(4)
        return self._bar_palette

    @property
    def song_palette(self) -> ColorPalette:
        """Get 2-color palette for song/artist text (cached)."""
        if self._song_palette is None:
            self._song_palette = self.get_palette(2)
        return self._song_palette


def load_image_with_fallback(url: str) -> Tuple[str, ColorPalette, ColorPalette]:
    """
    Load image and extract color palettes, with fallback handling.
    
    Args:
        url: URL to the album art image
        
    Returns:
        Tuple of (base64_image, bar_palette, song_palette)
    """
    if url:
        try:
            image_data = ImageData(url)
            return (
                image_data.get_base64(),
                image_data.bar_palette,
                image_data.song_palette,
            )
        except ImageProcessingError:
            pass  # Fall through to placeholder

    # Try placeholder URL for random colors
    try:
        image_data = ImageData(svg_config.placeholder_url)
        return (
            svg_config.placeholder_image,
            image_data.bar_palette,
            image_data.song_palette,
        )
    except ImageProcessingError:
        pass  # Fall through to defaults

    # Use defaults
    return (
        svg_config.placeholder_image,
        svg_config.default_bar_palette,
        svg_config.default_song_palette,
    )


def normalize_text_palette(
    palette: ColorPalette,
    min_l: float = 0.35,
    max_l: float = 0.75,
) -> ColorPalette:
    """
    Compress the brightness range of a text colour palette.

    Clamps the HSL lightness of each colour to [min_l, max_l] while
    preserving hue and saturation, so the lightest and darkest points
    stay readable without extreme contrast.

    Args:
        palette: List of RGB tuples
        min_l: Minimum lightness (0-1)
        max_l: Maximum lightness (0-1)

    Returns:
        Adjusted palette with clamped lightness
    """
    result: ColorPalette = []
    for r, g, b in palette:
        h, l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
        l = max(min_l, min(max_l, l))
        rn, gn, bn = colorsys.hls_to_rgb(h, l, s)
        result.append((int(rn * 255), int(gn * 255), int(bn * 255)))
    return result


# ============================================================================
# SVG Generation
# ============================================================================


def generate_bar_css(
    bar_count: int,
    beat_duration_ms: int = 500,
    energy: float = 0.5,
    bar_palette: Optional[list] = None,
) -> str:
    """
    Generate CSS for the SVG equalizer bars animation, synced to BPM.

    Produces a ``@keyframes barcolor`` rule that cycles ``fill`` through
    the palette, plus per-bar ``.bar:nth-child(N)`` rules that stagger
    ``animation-delay`` for both the pulse and colour-wave animations.

    Args:
        bar_count: Number of equalizer bars to generate
        beat_duration_ms: Duration of one beat in milliseconds
        energy: Track energy (0-1), affects animation intensity
        bar_palette: List of RGB tuples for bar colors

    Returns:
        CSS string for barcolor keyframe and per-bar animation timing
    """
    css_rules: list[str] = []
    palette = bar_palette or svg_config.default_bar_palette

    # Build @keyframes barcolor — cycles fill through palette colours
    looped = list(palette) + [palette[0]]
    stops: list[str] = []
    for idx, (r, g, b) in enumerate(looped):
        pct = idx / (len(looped) - 1) * 100
        stops.append(f"  {pct:.0f}% {{ fill: rgb({r},{g},{b}); }}")
    css_rules.append("@keyframes barcolor {\n" + "\n".join(stops) + "\n}")

    # --- per-bar animation timing ---
    energy_factor = 0.5 + (energy * 0.5)
    wave_duration_ms = 45000  # very slow colour drift across the row

    for i in range(1, bar_count + 1):
        # Pulse timing (slight per-bar variation)
        beat_variance = random.uniform(0.9, 1.1)
        pulse_dur = int(beat_duration_ms * beat_variance * (2 - energy_factor))
        pulse_dur = max(200, min(pulse_dur, 1500))
        pulse_delay = int((i / bar_count) * beat_duration_ms * 0.5)

        # Colour-wave delay: spread one full cycle across all bars
        wave_delay = int((i - 1) / bar_count * wave_duration_ms)

        css_rules.append(
            f".bar:nth-child({i}) {{ "
            f"animation-duration: {pulse_dur}ms, {wave_duration_ms}ms; "
            f"animation-delay: -{pulse_delay}ms, -{wave_delay}ms; "
            f"}}"
        )

    return "\n".join(css_rules)


def generate_bar_svg(
    bar_count: int,
    x_start: float,
    y_bottom: float,
    area_width: float,
    bar_height: int,
    gap: int = 1,
    bar_palette: Optional[list] = None,
) -> str:
    """
    Generate SVG ``<rect>`` elements for the equalizer bars.

    Bars are placed as native SVG shapes (not foreignObject HTML) so they
    are re-rendered as vectors at every display resolution, eliminating
    scaling artefacts.  ``shape-rendering: crispEdges`` (set in the
    template CSS) snaps edges to device pixels for uniform appearance.

    Args:
        bar_count: Number of bars
        x_start: Left edge of the bar area in SVG user units
        y_bottom: Bottom edge of the bar area in SVG user units
        area_width: Total width available for bars in SVG user units
        bar_height: Height of each bar in SVG user units
        gap: Gap between bars in SVG user units
        bar_palette: RGB tuples for initial fill colours

    Returns:
        SVG markup string containing ``<rect>`` elements
    """
    palette = bar_palette or svg_config.default_bar_palette
    bar_width = (area_width - (bar_count - 1) * gap) / bar_count
    stride = bar_width + gap
    y = y_bottom - bar_height

    paths: list[str] = []
    
    # Radius for top corners
    r = 2.0
    
    for i in range(bar_count):
        x = x_start + i * stride
        
        # Clamp radius if bar is too narrow
        actual_r = min(r, bar_width / 2)
        
        # Path for top-rounded bar
        # Start bottom-left -> go up -> curve top-left -> line top -> curve top-right -> go down -> close
        d = (
            f"M {x:.2f},{y + bar_height:.2f} "  # Bottom-left (y is top, so y+height is bottom)
            f"L {x:.2f},{y + actual_r:.2f} "    # Left vertical up to start of curve
            f"Q {x:.2f},{y:.2f} {x + actual_r:.2f},{y:.2f} " # Top-left curve
            f"L {x + bar_width - actual_r:.2f},{y:.2f} "      # Top horizontal
            f"Q {x + bar_width:.2f},{y:.2f} {x + bar_width:.2f},{y + actual_r:.2f} " # Top-right curve
            f"L {x + bar_width:.2f},{y + bar_height:.2f} "    # Right vertical down
            f"Z" # Close
        )

        color = palette[i % len(palette)]
        fill = f"rgb({color[0]},{color[1]},{color[2]})"
        
        # Use shape-rendering="geometricPrecision" to help with sub-pixel aliasing (clumping)
        paths.append(
            f'<path class="bar" d="{d}" '
            f'fill="{fill}" shape-rendering="geometricPrecision" />'
        )

    return "\n".join(paths)


def get_template_name() -> str:
    """
    Get the current theme template name from configuration.
    
    Returns:
        Template filename
    """
    try:
        with open(template_config.config_path, "r", encoding="utf-8") as f:
            templates = json.load(f)
            theme = templates.get("current-theme", template_config.default_theme)
            return templates.get("templates", {}).get(theme, template_config.fallback_theme)
    except (OSError, json.JSONDecodeError) as e:
        print(f"Failed to load templates: {e}")
        return template_config.fallback_theme


def escape_xml(text: str) -> str:
    """
    Escape special characters for XML/SVG compatibility.
    
    Args:
        text: Text to escape
        
    Returns:
        Escaped text safe for XML
    """
    if not text:
        return ""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("'", "&apos;")


def calculate_marquee(text: str, font_size: int, container_width: int = 330) -> dict:
    """
    Calculate marquee scroll parameters if text overflows its container.

    Uses a simple continuous loop: text is duplicated and scrolls left.
    The duration is fixed per character to maintain a consistent speed.

    Args:
        text: The raw (unescaped) display text
        font_size: CSS font-size in px
        container_width: Available width in px

    Returns:
        Dict with 'enabled', and when True: 'duration' (s)
    """
    # Estimate width (avg char width ~0.6em)
    char_width = font_size * 0.6
    text_width = len(text) * char_width
    
    # 50px is the spacer width in base.html.j2
    spacer_width = 50
    
    # Enable marquee if text + spacer overflows
    if text_width + (spacer_width / 2) <= container_width:
        return {"enabled": False}
        
    # Constant speed: pixels per second
    speed_px_per_sec = 25
    
    # Distance of one loop is text_width + spacer_width
    duration = round((text_width + spacer_width) / speed_px_per_sec, 1)
    
    return {"enabled": True, "duration": max(5.0, duration)}


def make_svg(
    track_data: dict[str, Any],
    background_color: str,
    border_color: str,
    background_type: str = "color",
    show_status: bool = False,
    is_compact: bool = False,
    width: Optional[int] = None,
    height: Optional[int] = None,
) -> str:
    """
    Generate SVG widget from normalized track data.
    
    Args:
        track_data: Normalized track data dict
        background_color: Hex color for background (without #)
        border_color: Hex color for border (without #)
        background_type: Type of background ("color", "blur_dark", "blur_light")
        show_status: Whether to show "Vibing to:" / "Recently played:" text
        is_compact: Whether to use compact mode layout
        width: Optional custom width
        height: Optional custom height
    
    Returns:
        Rendered SVG template string
    """
    # Select configuration based on mode
    cfg = compact_svg_config if is_compact else svg_config
    
    actual_width = width if width and width > 0 else cfg.width
    actual_height = height if height and height > 0 else cfg.height

    bar_count = cfg.eq_bar_count

    # Get audio features for BPM-synced animation
    audio_features = track_data.get("audio_features") or {}
    tempo = audio_features.get("tempo", cfg.default_tempo)
    energy = audio_features.get("energy", cfg.default_energy)

    # Calculate beat duration from BPM
    beat_duration_ms = int(60000 / tempo) if tempo > 0 else 500

    # Load image and extract colors first (needed for per-bar colors)
    album_art_url = track_data.get("album_art_url", "")
    image, bar_palette, song_palette = load_image_with_fallback(album_art_url)

    # Compress brightness so the gradient stays readable and bars aren't
    # too dark or washed out
    song_palette = normalize_text_palette(song_palette)
    bar_palette = normalize_text_palette(bar_palette, min_l=0.3, max_l=0.7)

    # Generate bar CSS with audio features and per-bar colors
    bar_css = generate_bar_css(bar_count, beat_duration_ms, energy, bar_palette)

    # --- SVG bar positioning ---
    # Compute the bar area rectangle in SVG user-space coordinates.
    # x: content starts after left-padding + border + album art + gap
    bar_x_start = (
        cfg.widget_padding_left
        + cfg.widget_border_width
        + cfg.album_art_size
        + cfg.art_content_gap
    )
    bar_x_end = (
        actual_width
        - cfg.widget_padding_right
        - cfg.widget_border_width
    )
    bar_area_width = bar_x_end - bar_x_start

    # y: the .content column (text + bars) is vertically centred in .main
    # independently of the album art.  Estimate its bottom edge.
    inner_h = (
        actual_height
        - (cfg.widget_padding_top + cfg.widget_border_width)
        - (cfg.widget_padding_bottom + cfg.widget_border_width)
    )
    content_h = cfg.content_column_height
    
    # Align bars to bottom of album art (which is vertically centered)
    # Album art vertical center is same as container center
    # So bottom is center + half size
    center_y = (
        cfg.widget_padding_top 
        + cfg.widget_border_width 
        + inner_h / 2
    )
    
    # If we want bars aligned to bottom of art:
    bars_y_bottom = center_y + (cfg.album_art_size / 2)

    bar_max_height = int(cfg.eq_bar_max_height + energy * 8)
    bar_svg = generate_bar_svg(
        bar_count,
        bar_x_start,
        bars_y_bottom,
        bar_area_width,
        bar_max_height,
        gap=cfg.eq_bar_gap,
        bar_palette=bar_palette,
    )

    # Set status text based on playing state or custom override
    is_playing = track_data.get("is_playing", False)
    status = track_data.get("status_text", "Vibing to:" if is_playing else "Recently played:")

    # Calculate marquee params from raw text (before XML escaping)
    raw_song = track_data.get("track_name", "Unknown Track")
    raw_artist = track_data.get("artist_name", "Unknown Artist")
    
    # Calculate marquee with font size from config
    song_marquee = calculate_marquee(raw_song, cfg.song_font_size, container_width=bar_area_width)
    artist_marquee = calculate_marquee(raw_artist, cfg.artist_font_size, container_width=bar_area_width)

    # Escape text for XML
    artist_name = escape_xml(raw_artist)
    song_name = escape_xml(raw_song)
    
    # Escape URLs for XML to prevent ampersands from breaking SVG parsing
    song_uri = escape_xml(track_data.get("track_url", ""))
    artist_uri = escape_xml(track_data.get("artist_url", ""))

    # Determine background mode
    use_blur_background = background_type in ("blur_dark", "blur_light")
    blur_is_dark = background_type == "blur_dark"

    template_data = {
        # Bar animation (SVG rects + CSS)
        "bar_svg": bar_svg,
        "bar_css": bar_css,
        # Audio features for template
        "beat_duration_ms": beat_duration_ms,
        "energy": energy,
        # Track info
        "artist_name": artist_name,
        "song_name": song_name,
        "song_uri": song_uri,
        "artist_uri": artist_uri,
        "service_name": "Spotify",
        # Image and colors
        "image": image,
        "bar_palette": bar_palette,
        "song_palette": song_palette,
        # Styling
        "background_color": background_color,
        "border_color": border_color,
        "background_type": background_type,
        "use_blur_background": use_blur_background,
        "blur_is_dark": blur_is_dark,
        "blur_amount": cfg.blur_amount,
        "blur_overlay_opacity": (
            cfg.blur_dark_opacity if blur_is_dark else cfg.blur_light_opacity
        ),
        # Status
        "status": status,
        "show_status": show_status,
        # Dimensions & layout (single source of truth from config)
        "width": actual_width,
        "height": actual_height,
        "album_size": cfg.album_art_size,
        "border_radius": cfg.border_radius,
        "widget_padding_top": cfg.widget_padding_top,
        "widget_padding_right": cfg.widget_padding_right,
        "widget_padding_bottom": cfg.widget_padding_bottom,
        "widget_padding_left": cfg.widget_padding_left,
        "widget_border_width": cfg.widget_border_width,
        "art_content_gap": cfg.art_content_gap,
        "eq_spacer_height": cfg.eq_spacer_height,
        "eq_spacer_margin_top": cfg.eq_spacer_margin_top,
        "artist_margin_top": cfg.artist_margin_top,
        # Font sizes
        "song_font_size": cfg.song_font_size,
        "artist_font_size": cfg.artist_font_size,
        # Marquee
        "song_marquee": song_marquee,
        "artist_marquee": artist_marquee,
    }

    return render_template(get_template_name(), **template_data)


# ============================================================================
# Connected User Handling
# ============================================================================


def get_public_base_url() -> str:
    """Return the base URL used in generated markdown snippets."""
    if app_config.base_url:
        return app_config.base_url.rstrip("/")
    return request.url_root.rstrip("/")


def build_markdown(public_id: str, params: str = "background_type=blur_dark&border_color=ffffff") -> str:
    """Build the README markdown for a connected user."""
    base_url = get_public_base_url()
    query = f"?{params}" if params else ""
    return (
        f"[![Spotify Now Playing]({base_url}/api/now-playing/{public_id}.svg{query})]"
        f"({base_url}/redirect/{public_id})"
    )


def make_list_svg(
    track_data: dict[str, Any],
    background_color: str,
    border_color: str,
    background_type: str = "color",
    is_compact: bool = False,
    width: Optional[int] = None,
    height: Optional[int] = None,
) -> str:
    """Generate SVG widget for a list of tracks/artists."""
    cfg = compact_svg_config if is_compact else svg_config
    
    actual_width = width if width and width > 0 else cfg.width
    
    # We will use the first item's art for the blur background
    album_art_url = track_data.get("album_art_url", "")
    image, _, song_palette = load_image_with_fallback(album_art_url)
    song_palette = normalize_text_palette(song_palette)

    # Process items to escape XML
    items = []
    for raw_item in track_data.get("items", []):
        item_img, _, _ = load_image_with_fallback(raw_item.get("album_art_url", ""))
        items.append({
            "track_name": escape_xml(raw_item.get("track_name", "Unknown")),
            "artist_name": escape_xml(raw_item.get("artist_name", "Unknown")),
            "image": item_img,
            "track_url": escape_xml(raw_item.get("track_url", "")),
        })

    use_blur_background = background_type in ("blur_dark", "blur_light")
    blur_is_dark = background_type == "blur_dark"
    
    # Calculate a dynamic height based on the number of items
    item_height = 50 if is_compact else 60
    header_height = 40
    padding = 20
    dynamic_height = header_height + (len(items) * item_height) + padding
    
    actual_height = height if height and height > 0 else dynamic_height

    template_data = {
        "items": items,
        "title": escape_xml(track_data.get("status_text", "Top Items")),
        "image": image,
        "song_palette": song_palette,
        "background_color": background_color,
        "border_color": border_color,
        "background_type": background_type,
        "use_blur_background": use_blur_background,
        "blur_is_dark": blur_is_dark,
        "blur_amount": cfg.blur_amount,
        "blur_overlay_opacity": cfg.blur_dark_opacity if blur_is_dark else cfg.blur_light_opacity,
        "width": actual_width,
        "height": actual_height,
        "item_height": item_height,
        "border_radius": cfg.border_radius,
    }

    return render_template("list.html.j2", **template_data)


def load_connected_track(public_id: str, fetch_type: str = "now_playing", time_range: str = "short_term") -> tuple[ConnectedUser | None, dict[str, Any] | None, MusicWidgetError | None]:
    """Load a connected user's Spotify track/artist based on the fetch_type, refreshing tokens when needed."""
    user = get_user(public_id)
    if user is None:
        return None, None, AuthenticationError("Spotify", "Unknown widget id")

    def _fetch_data(token: str) -> dict[str, Any]:
        if fetch_type == "recently_played":
            data = spotify.get_recently_played_items(token)
            data["status_text"] = "Recently Played (Top 5)"
            return data
        elif fetch_type == "top_track":
            data = spotify.get_top_items(token, item_type="tracks", time_range=time_range)
            data["status_text"] = "Top Tracks"
            return data
        elif fetch_type == "top_artist":
            data = spotify.get_top_items(token, item_type="artists", time_range=time_range)
            data["status_text"] = "Top Artists"
            return data
        else:
            return spotify.get_now_playing(token)

    access_token = user.access_token
    # If no cached access token or expired, refresh it
    if not access_token or user.expires_at <= int(time.time()):
        try:
            refreshed = spotify.refresh_access_token(user.refresh_token, user.client_id, user.client_secret)
            access_token = refreshed["access_token"]
            update_tokens(
                public_id,
                access_token,
                spotify.token_expiry_timestamp(refreshed.get("expires_in", 3600)),
                refreshed.get("refresh_token"),
            )
            user = get_user(public_id) or user
        except AuthenticationError as e:
            return user, None, e

    try:
        return user, _fetch_data(access_token), None
    except AuthenticationError:
        try:
            refreshed = spotify.refresh_access_token(user.refresh_token, user.client_id, user.client_secret)
            access_token = refreshed["access_token"]
            update_tokens(
                public_id,
                access_token,
                spotify.token_expiry_timestamp(refreshed.get("expires_in", 3600)),
                refreshed.get("refresh_token"),
            )
            return user, _fetch_data(access_token), None
        except MusicWidgetError as e:
            return user, None, e
    except MusicWidgetError as e:
        return user, None, e


# ============================================================================
# Error Response Generation
# ============================================================================


@app.errorhandler(MusicWidgetError)
def handle_music_widget_error(error: MusicWidgetError) -> Response:
    """Handle custom application errors and return appropriate responses."""
    # If the request expects an SVG (e.g. now-playing endpoint), return an error SVG
    if request.path.endswith(".svg") or "api/now-playing" in request.path:
        # Always return 200 for SVGs so the error message is visible in <img> tags
        return make_error_svg(error.message, 200)
    
    # Otherwise return a plain text error
    return Response(error.message, status=error.status_code, mimetype="text/plain")


@app.errorhandler(Exception)
def handle_generic_error(error: Exception) -> Response:
    """Handle unexpected errors."""
    message = f"An unexpected error occurred: {str(error)}"
    # Log the error for debugging (in a real app, use app.logger)
    print(message)
    
    if request.path.endswith(".svg") or "api/now-playing" in request.path:
        # Always return 200 for SVGs so the error message is visible in <img> tags
        return make_error_svg("Internal Server Error", 200)
    
    return Response(message, status=500, mimetype="text/plain")


import textwrap

def make_error_svg(message: str, status_code: int = 200, width: int = 0, height: int = 0) -> Response:
    """
    Generate an error SVG response.
    
    Args:
        message: Error message to display
        status_code: HTTP status code (defaults to 200 so it renders in <img>)
        width: Optional custom width
        height: Optional custom height
        
    Returns:
        Flask Response with error SVG
    """
    # Wrap text to ~60 characters to fit within the SVG bounds
    lines = textwrap.wrap(message, width=60)
    
    # Generate <tspan> elements for each line
    tspans = []
    # Center vertically: offset starts from the top line
    line_height = 18
    actual_width = width if width > 0 else svg_config.width
    actual_height = height if height > 0 else svg_config.height
    
    start_y = 50 - ((len(lines) - 1) * (line_height / 2) / actual_height * 100)
    
    for i, line in enumerate(lines):
        dy = f"{i * line_height}px" if i > 0 else "0"
        tspans.append(f'<tspan x="50%" dy="{dy}">{escape_xml(line)}</tspan>')
        
    tspans_str = "\n            ".join(tspans)

    error_svg = f"""<svg width="{actual_width}" height="{actual_height}" xmlns="http://www.w3.org/2000/svg">
        <rect width="100%" height="100%" fill="#1a1a1a" rx="5"/>
        <text x="50%" y="{start_y}%" fill="#ff6b6b" font-family="sans-serif" font-size="14" text-anchor="middle" dominant-baseline="middle">
            {tspans_str}
        </text>
    </svg>"""

    resp = Response(error_svg, mimetype="image/svg+xml", status=status_code)
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


# ============================================================================
# Routes
# ============================================================================


@app.route("/")
def home_page() -> Response:
    """Public landing page where users connect Spotify and copy markdown."""
    return Response(
        render_template(
            "home.html.j2",
            spotify_ready=spotify.is_configured(),
            callback_url=app_config.callback_url(),
        ),
        mimetype="text/html",
    )


@app.route("/login", methods=["GET", "POST"])
def login() -> Response:
    """Start Spotify OAuth."""
    # Allow users to provide their own credentials via form, stripping any accidental whitespace
    user_client_id = request.form.get("client_id", "").strip()
    user_client_secret = request.form.get("client_secret", "").strip()

    if not spotify.is_configured() and not user_client_id:
        return Response(
            "Spotify credentials not configured. Please provide your own Client ID and Secret or set them in the environment.",
            status=500,
            mimetype="text/plain",
        )

    state = secrets.token_urlsafe(24)
    session["oauth_state"] = state
    
    # Store user-provided credentials in session to use in callback
    if user_client_id and user_client_secret:
        session["user_client_id"] = user_client_id
        session["user_client_secret"] = user_client_secret
    else:
        session.pop("user_client_id", None)
        session.pop("user_client_secret", None)

    return redirect(spotify.get_authorize_url(state, client_id=user_client_id))


@app.route("/callback")
def callback() -> Response:
    """Handle Spotify OAuth callback and show the markdown snippet."""
    error = request.args.get("error")
    if error:
        return Response(f"Spotify login failed: {error}", status=400, mimetype="text/plain")

    state = request.args.get("state", "")
    if not state or state != session.pop("oauth_state", ""):
        return Response("Invalid OAuth state. Please try connecting again.", status=400, mimetype="text/plain")

    code = request.args.get("code", "")
    if not code:
        return Response("Missing Spotify OAuth code.", status=400, mimetype="text/plain")

    # Get user-provided credentials from session if they exist
    user_client_id = session.pop("user_client_id", None)
    user_client_secret = session.pop("user_client_secret", None)

    tokens = spotify.exchange_code(code, client_id=user_client_id, client_secret=user_client_secret)
    profile = spotify.get_profile(tokens["access_token"])
    spotify_user_id = profile.get("id")
    if not spotify_user_id:
        return Response("Spotify profile response did not include a user id.", status=502, mimetype="text/plain")

    public_id = generate_public_id(
        tokens["refresh_token"], 
        profile.get("display_name") or spotify_user_id,
        client_id=user_client_id or "",
        client_secret=user_client_secret or ""
    )
    display_name = profile.get("display_name") or spotify_user_id
    save_user(
        public_id=public_id,
        spotify_user_id=spotify_user_id,
        display_name=display_name,
        refresh_token=tokens["refresh_token"],
        access_token=tokens["access_token"],
        expires_at=spotify.token_expiry_timestamp(tokens.get("expires_in", 3600)),
    )

    markdown = build_markdown(public_id)
    widget_url = f"{get_public_base_url()}{url_for('now_playing_svg', public_id=public_id)}"
    redirect_url = f"{get_public_base_url()}{url_for('redirect_to_song', public_id=public_id)}"

    return Response(
        render_template(
            "connected.html.j2",
            display_name=display_name,
            markdown=markdown,
            widget_url=widget_url,
            redirect_url=redirect_url,
            public_id=public_id,
        ),
        mimetype="text/html",
    )


@app.route("/api/orchestrator")
def legacy_orchestrator() -> Response:
    """Compatibility endpoint; requires ?user=<public_id>."""
    public_id = request.args.get("user", "")
    if not public_id:
        return make_error_svg("Connect Spotify first, then use your generated widget URL.", 200)
    return now_playing_svg(public_id)


@app.route("/api/now-playing/<public_id>.svg")
def now_playing_svg(public_id: str) -> Response:
    """Serve a connected user's now playing SVG widget."""
    return _generate_widget_response(public_id, "now_playing", request.args)

@app.route("/api/recently-played/<public_id>.svg")
def recently_played_svg(public_id: str) -> Response:
    """Serve a connected user's recently played SVG widget."""
    return _generate_widget_response(public_id, "recently_played", request.args)

@app.route("/api/top-track/<public_id>.svg")
def top_track_svg(public_id: str) -> Response:
    """Serve a connected user's top track SVG widget."""
    return _generate_widget_response(public_id, "top_track", request.args)

@app.route("/api/top-artist/<public_id>.svg")
def top_artist_svg(public_id: str) -> Response:
    """Serve a connected user's top artist SVG widget."""
    return _generate_widget_response(public_id, "top_artist", request.args)


def _generate_widget_response(public_id: str, fetch_type: str, args: Any) -> Response:
    """Helper to parse args and generate the SVG response."""
    # Validate and sanitize parameters
    raw_background = args.get("background_color", "")
    raw_border = args.get("border_color", "")
    raw_bg_type = args.get("background_type", "")
    raw_width = args.get("width", "")
    raw_height = args.get("height", "")

    background_color = validate_hex_color(raw_background, svg_config.default_background)
    border_color = validate_hex_color(raw_border, svg_config.default_border)
    background_type = validate_background_type(raw_bg_type, svg_config.default_background_type)
    width = validate_int(raw_width, 0, min_val=100, max_val=2000)
    height = validate_int(raw_height, 0, min_val=50, max_val=2000)

    # Optional parameters
    show_status = args.get("show_status", "").lower() in ("true", "1", "yes")
    is_compact = args.get("compact", "").lower() in ("true", "1", "yes")
    time_range = args.get("time_range", "short_term")

    _user, track_data, error = load_connected_track(public_id, fetch_type, time_range)
    if error:
        return make_error_svg(error.message, 200, width=width, height=height)
    if track_data is None:
        return make_error_svg("No Spotify track available.", 200, width=width, height=height)

    if track_data.get("type") == "list":
        svg = make_list_svg(
            track_data, 
            background_color, 
            border_color, 
            background_type, 
            is_compact,
            width=width,
            height=height
        )
    else:
        # Override status text for specific widgets if show_status is requested
        if fetch_type == "recently_played":
            track_data["is_playing"] = False
        elif fetch_type == "top_track":
            track_data["is_playing"] = False
            track_data["status_text"] = "Top Track:"
        elif fetch_type == "top_artist":
            track_data["is_playing"] = False
            track_data["status_text"] = "Top Artist:"
        
        svg = make_svg(
            track_data, 
            background_color, 
            border_color, 
            background_type, 
            show_status, 
            is_compact,
            width=width,
            height=height
        )

    resp = Response(svg, mimetype="image/svg+xml")
    resp.headers["Cache-Control"] = "s-maxage=1"

    return resp


@app.route("/preview")
def preview_page() -> Response:
    """Serve the preview page for local development."""
    candidates = [
        os.path.join(os.getcwd(), "preview.html"),
        os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "preview.html",
        ),
    ]
    for path in candidates:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                return Response(f.read(), mimetype="text/html")
    return Response("preview.html not found", status=404, mimetype="text/plain")


@app.route("/redirect/<public_id>")
def redirect_to_song(public_id: str) -> Response:
    """Redirect to the currently playing song."""
    return _handle_redirect(public_id, "now_playing")

@app.route("/redirect-recently-played/<public_id>")
def redirect_to_recent(public_id: str) -> Response:
    """Redirect to the recently played song."""
    return _handle_redirect(public_id, "recently_played")

@app.route("/redirect-top-track/<public_id>")
def redirect_to_top_track(public_id: str) -> Response:
    """Redirect to the top track."""
    return _handle_redirect(public_id, "top_track", request.args.get("time_range", "short_term"))

@app.route("/redirect-top-artist/<public_id>")
def redirect_to_top_artist(public_id: str) -> Response:
    """Redirect to the top artist."""
    return _handle_redirect(public_id, "top_artist", request.args.get("time_range", "short_term"))

def _handle_redirect(public_id: str, fetch_type: str, time_range: str = "short_term") -> Response:
    fallback_url = "https://open.spotify.com/"

    try:
        _user, track_data, error = load_connected_track(public_id, fetch_type, time_range)
        track_url = track_data.get("track_url") if track_data and not error else ""
        
        if track_url:
            return redirect(track_url)
    except Exception:
        # In case of any error (service not configured, API error), use fallback
        pass

    return redirect(fallback_url)


@app.route("/health")
def health_check() -> Response:
    """Health check endpoint for monitoring."""
    return Response("OK", status=200, mimetype="text/plain")


# ============================================================================
# Main Entry Point
# ============================================================================


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", debug=True, port=port)
