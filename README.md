<div align="center">
  
# 🎧 SpotiBadge

**Music, live on your profile.**

SpotiBadge generates a dynamic, real-time Spotify "Now Playing" widget for your GitHub README. 
No database, no trackers, just your music.

> *Placeholder: `![SpotiBadge Preview](docs/images/hero-preview.png)`*

</div>

## ✨ Features

- **Real-Time Now Playing:** Instantly displays the song you are currently vibing to on Spotify.
- **Recently Played Fallback:** If you pause your music, the widget automatically falls back to showing your most recently played track.
- **BPM-Synced Equalizer:** The animated EQ bars pulse and change color based on the actual Tempo (BPM) and Energy of the track.
- **Dynamic Blur Themes:** The widget extracts a color palette from the album art to generate a beautiful, dynamic blurred background.
- **Click to Play:** The entire widget is a link that redirects visitors straight to the track on Spotify.
- **Stateless & Secure:** No database is required! Your Spotify credentials are encrypted and signed directly into the widget URL.
- **Self-Service Mode:** Friends can use your hosted instance without you having to manually whitelist them in the Spotify Developer Dashboard. They just provide their own Client ID and Secret!

## 🚀 Quick Start

Getting your own SpotiBadge is easy and requires no coding.

👉 **[Read the Full Setup Guide (SETUP.md)](SETUP.md)**

The setup guide covers:
1. How to generate your free Spotify Developer credentials.
2. How to bypass Spotify's "403 Forbidden" errors.
3. How to deploy your own instance of SpotiBadge for free on Vercel.

## 🎨 Customization

You can customize your badge directly via URL parameters.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `background_type` | string | `color` | Options: `blur_dark`, `blur_light`, `color` |
| `border_color` | hex | `ffffff` | Hex color code without the `#` |
| `background_color`| hex | `181414` | Hex color code without the `#` (Only works if type is `color`) |
| `show_status` | boolean | `false` | Set to `true` to show "Vibing to:" or "Recently played:" |
| `compact` | boolean | `false` | Set to `true` for a smaller, space-saving widget layout |

**Example Markdown:**
```markdown
[![Spotify Now Playing](https://your-app.vercel.app/api/now-playing/your_id.svg?background_type=blur_dark&show_status=true)](https://your-app.vercel.app/redirect/your_id)
```

## 📡 API Endpoints

SpotiBadge is entirely stateless. Once you generate your `public_id` via the login flow, you can interact with these core endpoints:

### 1. Now Playing
- **SVG Image:** `GET /api/now-playing/{public_id}.svg`
- **Redirect:** `GET /redirect/{public_id}`

### 2. Recently Played
- **SVG Image:** `GET /api/recently-played/{public_id}.svg`
- **Redirect:** `GET /redirect-recently-played/{public_id}`

### 3. Top Tracks
- **SVG Image:** `GET /api/top-track/{public_id}.svg?time_range=short_term`
- **Redirect:** `GET /redirect-top-track/{public_id}?time_range=short_term`
- *Options for `time_range`: `short_term` (4 weeks), `medium_term` (6 months), `long_term` (1 year)*

### 4. Top Artists
- **SVG Image:** `GET /api/top-artist/{public_id}.svg?time_range=short_term`
- **Redirect:** `GET /redirect-top-artist/{public_id}?time_range=short_term`

*(Note: Adding custom CSS or changing widget parameters via query parameters like `?compact=true` works on all SVG endpoints.)*

## 💻 Tech Stack

- **Python 3** & **Flask** (Routing and OAuth Orchestration)
- **Requests** (Spotify API interaction)
- **ColorThief** (Extracting color palettes from Album Art)
- **Jinja2** (SVG and HTML Templating)
- **ItsDangerous** (Stateless URL encryption)

## 🤝 Contributing

Contributions are welcome! If you want to add new widgets (like Top Tracks or Top Artists), feel free to open a Pull Request.

1. Fork the Project
2. Create your Feature Branch (`git checkout -b feature/AmazingFeature`)
3. Commit your Changes (`git commit -m 'Add some AmazingFeature'`)
4. Push to the Branch (`git push origin feature/AmazingFeature`)
5. Open a Pull Request