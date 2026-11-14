import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.auth.auth import AccessToken, AuthProvider

from .client import QobuzClient, QobuzError
from .config import config

_client: QobuzClient | None = None


@asynccontextmanager
async def lifespan(server):
    global _client
    _client = QobuzClient(
        app_id=os.environ.get("QOBUZ_APP_ID"),
        app_secret=os.environ.get("QOBUZ_APP_SECRET"),
    )
    await _client.login(
        email=os.environ["QOBUZ_EMAIL"],
        password=os.environ["QOBUZ_PASSWORD"],
    )
    try:
        yield
    finally:
        await _client.close()


class _StaticBearerAuth(AuthProvider):
    """Accepts exactly one pre-shared bearer token; rejects everything else."""

    def __init__(self, token: str) -> None:
        super().__init__()
        self._token = token

    async def verify_token(self, token: str) -> AccessToken | None:
        if token == self._token:
            return AccessToken(token=token, client_id="homelab", scopes=[])
        return None


def _make_auth() -> AuthProvider | None:
    """Auth provider for HTTP transport.

    Precedence: OIDC (remote OAuth via an upstream IdP such as PocketID, required by
    Claude's remote connectors) > static bearer token (machine-to-machine). stdio needs
    no auth.
    """
    if config.transport != "http":
        return None

    if config.oidc_enabled:
        from fastmcp.server.auth.oidc_proxy import OIDCProxy

        return OIDCProxy(
            config_url=config.oidc_config_url,
            client_id=config.oidc_client_id,
            client_secret=config.oidc_client_secret,
            base_url=config.oidc_base_url,
            redirect_path=config.oidc_redirect_path,
            required_scopes=config.oidc_required_scopes,
            allowed_client_redirect_uris=config.oidc_allowed_redirect_uris,
            verify_id_token=config.oidc_verify_id_token,
            forward_resource=config.oidc_forward_resource,
        )

    if not config.http_bearer_token:
        raise RuntimeError(
            "HTTP transport needs auth: set OIDC_* (config_url/client_id/client_secret/base_url) "
            "or QOBUZ_MCP_HTTP_BEARER_TOKEN"
        )
    return _StaticBearerAuth(config.http_bearer_token)


mcp = FastMCP("qobuz-mcp", lifespan=lifespan, auth=_make_auth())


def _c() -> QobuzClient:
    assert _client is not None, "Client not initialized"
    return _client


# --- format helpers ---


def _dur(seconds: int) -> str:
    m, s = divmod(seconds, 60)
    return f"{m}:{s:02d}"


def _ts(unix: int | None) -> str | None:
    if not unix:
        return None
    return datetime.fromtimestamp(unix, tz=UTC).strftime("%Y-%m-%d")


def _fmt_track(t: dict) -> dict:
    artist = t.get("performer", {}).get("name") or t.get("album", {}).get("artist", {}).get(
        "name", "Unknown"
    )
    out: dict[str, Any] = {
        "track_id": t["id"],
        "title": t.get("title", ""),
        "artist": artist,
        "album": t.get("album", {}).get("title", ""),
        "album_id": t.get("album", {}).get("id"),
        "duration": _dur(t.get("duration", 0)),
    }
    if t.get("version"):
        out["version"] = t["version"]
    if t.get("parental_warning"):
        out["explicit"] = True
    if t.get("playlist_track_id"):
        out["playlist_track_id"] = t["playlist_track_id"]
    if t.get("favorited_at"):
        out["favorited_at"] = _ts(t["favorited_at"])
    return out


def _fmt_album(a: dict) -> dict:
    out = {
        "album_id": a["id"],
        "title": a.get("title", ""),
        "artist": a.get("artist", {}).get("name", ""),
        "year": (a.get("release_date_original") or "")[:4],
        "tracks_count": a.get("tracks_count", 0),
        "duration": _dur(a.get("duration", 0)),
        "genre": a.get("genre", {}).get("name", ""),
        "label": a.get("label", {}).get("name", ""),
    }
    if a.get("favorited_at"):
        out["favorited_at"] = _ts(a["favorited_at"])
    return out


def _fmt_artist(a: dict) -> dict:
    out = {
        "artist_id": a["id"],
        "name": a.get("name", ""),
        "albums_count": a.get("albums_count", 0),
    }
    if a.get("favorited_at"):
        out["favorited_at"] = _ts(a["favorited_at"])
    return out


def _fmt_playlist(p: dict) -> dict:
    return {
        "playlist_id": p["id"],
        "title": p.get("name", ""),
        "owner": p.get("owner", {}).get("name", ""),
        "tracks_count": p.get("tracks_count", 0),
        "duration": _dur(p.get("duration", 0)),
        "is_public": p.get("is_public", False),
        "description": (p.get("description") or "")[:240],
    }


def _json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


# --- tools ---


@mcp.tool()
async def search(
    query: str,
    type: str = "tracks",
    limit: int = 20,
) -> str:
    """Search the Qobuz catalog.

    Args:
        query: Search terms (artist name, album title, track title, etc.)
        type: What to search for — one of: tracks, albums, artists, playlists
        limit: Max results to return (1–100, default 20)
    """
    data = await _c().search(query, type=type, limit=limit)

    if type == "tracks":
        items = data.get("tracks", {}).get("items", [])
        if items:
            words = [w.lower() for w in query.split() if len(w) > 2]

            def _score(t: dict) -> int:
                title = t.get("title", "").lower()
                artist = (
                    (t.get("performer") or {}).get("name", "")
                    or (t.get("album") or {}).get("artist", {}).get("name", "")
                ).lower()
                return sum(1 for w in words if w in title or w in artist)

            items = sorted(items, key=_score, reverse=True)
        return _json([_fmt_track(t) for t in items])

    if type == "albums":
        items = data.get("albums", {}).get("items", [])
        return _json([_fmt_album(a) for a in items])

    if type == "artists":
        items = data.get("artists", {}).get("items", [])
        return _json([_fmt_artist(a) for a in items])

    if type == "playlists":
        items = data.get("playlists", {}).get("items", [])
        return _json([_fmt_playlist(p) for p in items])

    return _json(data)


@mcp.tool()
async def get_track(track_id: str) -> str:
    """Get full details for a single track by its Qobuz track ID."""
    data = await _c().get_track(track_id)
    return _json(_fmt_track(data))


@mcp.tool()
async def get_album(album_id: str) -> str:
    """Get album details including the full tracklist.

    Returns album metadata and all tracks with their IDs, useful for adding
    specific tracks to a playlist.
    """
    data = await _c().get_album(album_id)
    album = _fmt_album(data)
    tracks = [_fmt_track(t) for t in data.get("tracks", {}).get("items", [])]
    return _json({"album": album, "tracks": tracks})


@mcp.tool()
async def get_artist(artist_id: str) -> str:
    """Get artist details and their discography (most recent albums first)."""
    data = await _c().get_artist(artist_id)
    artist = _fmt_artist(data)
    albums = [_fmt_album(a) for a in data.get("albums", {}).get("items", [])]
    artist["albums_count"] = len(albums)
    return _json({"artist": artist, "albums": albums})


@mcp.tool()
async def get_streaming_url(track_id: str, quality: str = "FLAC_CD") -> str:
    """Get a time-limited streaming URL for a track.

    Args:
        track_id: Qobuz track ID
        quality: One of MP3_320, FLAC_CD (16-bit/44.1kHz), FLAC_24_96, FLAC_24_192
                 Higher qualities require a matching Qobuz subscription tier.
    """
    data = await _c().get_streaming_url(track_id, quality=quality)
    return _json(
        {
            "url": data.get("url"),
            "format_id": data.get("format_id"),
            "mime_type": data.get("mime_type"),
            "sampling_rate": data.get("sampling_rate"),
            "bit_depth": data.get("bit_depth"),
            "file_size": data.get("file_size"),
        }
    )


@mcp.tool()
async def get_user_favorites(type: str = "tracks", limit: int = 50) -> str:
    """Get the current user's favorited tracks, albums, or artists.

    Args:
        type: One of: tracks, albums, artists
        limit: Max results (default 50)
    """
    data = await _c().get_user_favorites(type=type, limit=limit)

    if type == "tracks":
        items = data.get("tracks", {}).get("items", [])
        return _json([_fmt_track(t) for t in items])

    if type == "albums":
        items = data.get("albums", {}).get("items", [])
        return _json([_fmt_album(a) for a in items])

    if type == "artists":
        items = data.get("artists", {}).get("items", [])
        return _json([_fmt_artist(a) for a in items])

    return _json(data)


@mcp.tool()
async def get_user_playlists() -> str:
    """List all playlists owned by the current user."""
    data = await _c().get_user_playlists()
    items = data.get("playlists", {}).get("items", [])
    return _json([_fmt_playlist(p) for p in items])


@mcp.tool()
async def get_playlist(playlist_id: str) -> str:
    """Get a playlist with its full track listing.

    Each track includes a playlist_track_id which is needed to remove it
    from the playlist (different from the regular track_id).
    """
    data = await _c().get_playlist(playlist_id)
    playlist = _fmt_playlist(data)
    tracks = [_fmt_track(t) for t in data.get("tracks", {}).get("items", [])]
    return _json({"playlist": playlist, "tracks": tracks})


@mcp.tool()
async def create_playlist(
    name: str,
    description: str = "",
    track_ids: list[str] | None = None,
    is_public: bool = False,
) -> str:
    """Create a new playlist, optionally seeding it with tracks.

    Args:
        name: Playlist name
        description: Optional description
        track_ids: Optional list of Qobuz track IDs to add immediately
        is_public: Whether the playlist is publicly visible (default False)
    """
    data = await _c().create_playlist(
        name=name,
        description=description,
        is_public=is_public,
        track_ids=track_ids,
    )
    return _json(
        {
            "playlist_id": data["id"],
            "name": data.get("name"),
            "status": "created",
        }
    )


@mcp.tool()
async def update_playlist(
    playlist_id: str,
    name: str | None = None,
    description: str | None = None,
    is_public: bool | None = None,
) -> str:
    """Update a playlist's name, description, or visibility.

    Only the fields you provide will be changed.
    """
    await _c().update_playlist(
        playlist_id=playlist_id,
        name=name,
        description=description,
        is_public=is_public,
    )
    return _json({"playlist_id": playlist_id, "status": "updated"})


@mcp.tool()
async def add_tracks_to_playlist(playlist_id: str, track_ids: list[str]) -> str:
    """Add one or more tracks to an existing playlist.

    Args:
        playlist_id: Target playlist ID
        track_ids: List of Qobuz track IDs to add
    """
    await _c().add_tracks_to_playlist(playlist_id, track_ids)
    return _json(
        {
            "playlist_id": playlist_id,
            "added": len(track_ids),
            "status": "ok",
        }
    )


@mcp.tool()
async def delete_playlist(playlist_id: str) -> str:
    """Permanently delete a playlist.

    Args:
        playlist_id: The Qobuz playlist ID to delete
    """
    await _c().delete_playlist(playlist_id)
    return _json({"playlist_id": playlist_id, "status": "deleted"})


@mcp.tool()
async def remove_tracks_from_playlist(
    playlist_id: str,
    playlist_track_ids: list[str],
) -> str:
    """Remove tracks from a playlist by their playlist_track_id.

    IMPORTANT: Use playlist_track_id (from get_playlist), NOT the regular
    track_id. Each entry in a playlist has a unique playlist_track_id.

    Args:
        playlist_id: The playlist to edit
        playlist_track_ids: List of playlist_track_id values to remove
    """
    await _c().remove_tracks_from_playlist(playlist_id, playlist_track_ids)
    return _json(
        {
            "playlist_id": playlist_id,
            "removed": len(playlist_track_ids),
            "status": "ok",
        }
    )


# --- Qobuz Connect: control playback on the user's devices ---
#
# Each call opens a short-lived controller session (connect/controller.py):
# join the account's Connect session, act, confirm, leave. Works from anywhere
# the MCP server runs — commands travel through the Qobuz cloud to the device.


def _connect():
    from .connect.controller import ConnectController

    return ConnectController(_c().create_connect_token)


def _clock(seconds: float) -> str:
    return _dur(int(seconds))


async def _tracks_by_id(track_ids: list[int]) -> dict[int, dict]:
    async def one(tid: int) -> tuple[int, dict | None]:
        try:
            return tid, _fmt_track(await _c().get_track(str(tid)))
        except QobuzError:
            return tid, None

    pairs = await asyncio.gather(*(one(t) for t in dict.fromkeys(track_ids)))
    return {tid: t for tid, t in pairs if t}


def _fmt_device(d, active: bool) -> dict:  # d: connect.controller.Device
    out: dict[str, Any] = {"name": d.name, "type": d.type, "active": active}
    # "Apple iPhone"; a model that repeats the brand ("qobit"/"qobit") once.
    parts = [d.brand] if d.brand else []
    if d.model and d.model.lower() not in (d.brand or "").lower():
        parts.append(d.model)
    if parts:
        out["device"] = " ".join(parts)
    if d.max_quality:
        out["max_quality"] = d.max_quality
    return out


async def _status(c) -> dict:
    active = c.devices.get(c.active_id) if c.active_id is not None else None
    out: dict[str, Any] = {
        "devices": [_fmt_device(d, d.renderer_id == c.active_id) for d in c.devices.values()],
        "active_device": active.name if active else None,
    }
    p = c.player()
    if active is None or p is None:
        out["playback"] = None
        return out
    order = c.play_order()
    ids = [i for i, _ in order]
    cur = ids.index(p.item_id) if p.item_id in ids else -1
    upcoming = order[cur + 1 : cur + 6] if cur >= 0 else []
    want = ([order[cur][1]] if cur >= 0 else []) + [t for _, t in upcoming]
    info = await _tracks_by_id(want)
    out["playback"] = {
        "state": p.state,
        "position": f"{_clock(p.position_s)} / {_clock(p.duration_s)}",
        "now_playing": info.get(order[cur][1]) if cur >= 0 else None,
        "up_next": [info[t] for _, t in upcoming if t in info],
        "queue_length": len(c.queue),
        "queue_position": cur + 1 if cur >= 0 else None,
        "shuffle": c.shuffle,
    }
    return out


@mcp.tool()
async def connect_status() -> str:
    """Show the user's Qobuz Connect devices and what is playing.

    Lists every Connect device currently online on the account (e.g. a qobit
    desktop player, the Qobuz phone app, a hi-fi streamer) with its type,
    brand/model and max_quality (mp3 < cd < hi-res L1 < L2 < L3 — prefer the
    highest for hi-res listening) and, for the active one, the playback state,
    position, current track and the next few tracks. Only devices connected
    right now appear (an app must be open, qobit running with Connect on).
    Call this first to learn device names before controlling playback.
    """
    async with _connect() as c:
        return _json(await _status(c))


@mcp.tool()
async def connect_play(
    track_ids: list[str] | None = None,
    album_id: str | None = None,
    playlist_id: str | None = None,
    device: str | None = None,
    start_index: int = 0,
) -> str:
    """Play music on a Qobuz Connect device, replacing its current queue.

    Give exactly one source: track_ids (played in that order), album_id or
    playlist_id. Playback starts at start_index (0-based) and the rest becomes
    the queue. If device is given (case-insensitive name or part of it, e.g.
    "qobit"), playback moves there first; otherwise the currently active device
    plays. Returns what the device actually started playing.
    """
    sources = [x for x in (track_ids, album_id, playlist_id) if x]
    if len(sources) != 1:
        return _json({"error": "Give exactly one of track_ids, album_id, playlist_id"})
    if track_ids:
        ids = [int(t) for t in track_ids]
    elif album_id:
        data = await _c().get_album(album_id)
        ids = [int(t["id"]) for t in data.get("tracks", {}).get("items", [])]
    else:
        data = await _c().get_playlist(playlist_id)
        ids = [int(t["id"]) for t in data.get("tracks", {}).get("items", [])]
    if not ids:
        return _json({"error": "No tracks found for that source"})
    if not 0 <= start_index < len(ids):
        return _json({"error": f"start_index must be between 0 and {len(ids) - 1}"})

    from .connect.controller import PLAYING, ConnectError

    async with _connect() as c:
        try:
            if device:
                target = c.find_device(device)
                if c.active_id != target.renderer_id:
                    t_switch = time.time()
                    if not await c.set_active(target.renderer_id):
                        return _json({"error": f"{target.name} did not become the active device"})
                    # Let the activation handover land first: the server sends the
                    # new device the session's current (often paused) state, and if
                    # that arrives after our queue load it can pause what we start.
                    await c.wait_for_player(lambda p: True, since=t_switch, timeout=3.0)
            elif c.active_id is None:
                return _json(
                    {"error": "No active Connect device — pass device=<name>", **await _status(c)}
                )
            t0 = time.time()
            await c.load(ids, start_index)
        except ConnectError as e:
            return _json({"error": str(e)})
        started = await c.wait_for_player(lambda p: p.state == "playing", since=t0)
        if started is None:
            # Loaded but not playing (a renderer that doesn't autostart, or a late
            # handover paused it): ask for playback explicitly.
            t1 = time.time()
            await c.set_player_state(playing=PLAYING)
            started = await c.wait_for_player(lambda p: p.state == "playing", since=t1)
        result = await _status(c)
        result["started"] = started is not None
        if started is None:
            result["note"] = "Queue loaded, but the device has not reported playing yet"
        return _json(result)


@mcp.tool()
async def connect_control(action: str, position_seconds: int | None = None) -> str:
    """Control playback on the active Qobuz Connect device.

    Args:
        action: one of play, pause, next, previous, seek
        position_seconds: target position for seek (seconds from track start)
    """
    from .connect.controller import PAUSED, PLAYING

    action = action.strip().lower()
    async with _connect() as c:
        if c.active_id is None:
            return _json({"error": "No active Connect device", **await _status(c)})
        p = c.player()
        order = [i for i, _ in c.play_order()]
        cur = order.index(p.item_id) if p and p.item_id in order else -1
        t0 = time.time()
        if action == "play":
            await c.set_player_state(playing=PLAYING)
            ok = lambda s: s.state == "playing"
        elif action == "pause":
            await c.set_player_state(playing=PAUSED)
            ok = lambda s: s.state == "paused"
        elif action in ("next", "previous"):
            j = cur + (1 if action == "next" else -1)
            if cur < 0 or not 0 <= j < len(order):
                return _json({"error": f"No {action} track in the queue", **await _status(c)})
            await c.set_player_state(item_id=order[j])
            ok = lambda s, want=order[j]: s.item_id == want
        elif action == "seek":
            if position_seconds is None:
                return _json({"error": "seek needs position_seconds"})
            await c.set_player_state(position_s=position_seconds)
            ok = lambda s: abs(s.position_s - position_seconds) < 5
        else:
            return _json({"error": "action must be play, pause, next, previous or seek"})
        confirmed = await c.wait_for_player(ok, since=t0)
        result = await _status(c)
        result["confirmed"] = confirmed is not None
        return _json(result)


@mcp.tool()
async def connect_queue(track_ids: list[str], position: str = "next") -> str:
    """Add tracks to the queue of the active Qobuz Connect device without
    interrupting playback.

    Args:
        track_ids: Qobuz track IDs, in the order they should play
        position: "next" (right after the current track) or "end" (after the
                  rest of the queue)
    """
    from .connect.controller import ConnectError

    ids = [int(t) for t in track_ids]
    async with _connect() as c:
        if c.active_id is None:
            return _json({"error": "No active Connect device", **await _status(c)})
        p = c.player()
        try:
            if position == "next":
                if p is None or p.item_id < 0:
                    return _json({"error": "Nothing is playing — use connect_play instead"})
                await c.insert_next(ids, after_item_id=p.item_id)
            elif position == "end":
                await c.add_last(ids)
            else:
                return _json({"error": 'position must be "next" or "end"'})
        except ConnectError as e:
            return _json({"error": str(e)})
        # The echo carries only the inserted items; ask for the full snapshot so
        # the returned "up_next" reflects the change.
        c.queue_seen = False
        c.ask_queue_state()
        await c.wait(lambda: c.queue_seen, timeout=3.0)
        return _json({"added": len(ids), **await _status(c)})


@mcp.tool()
async def connect_transfer(device: str, play: bool = True) -> str:
    """Move playback (and its queue) to another Qobuz Connect device.

    Args:
        device: device name or part of it, e.g. "qobit" or "iPhone"
        play: start playing on the new device (default true); false leaves it paused
    """
    from .connect.controller import PLAYING, ConnectError

    async with _connect() as c:
        try:
            target = c.find_device(device)
        except ConnectError as e:
            return _json({"error": str(e)})
        if not await c.set_active(target.renderer_id):
            return _json({"error": f"{target.name} did not become the active device"})
        t0 = time.time()
        if play:
            await c.set_player_state(playing=PLAYING)
            await c.wait_for_player(lambda s: s.state == "playing", since=t0)
        else:
            await c.wait_for_player(lambda s: True, since=t0, timeout=3.0)
        return _json(await _status(c))


def _http_security_kwargs() -> dict:
    """Host/Origin guard config for HTTP transport.

    FastMCP (v3) only trusts localhost by default and returns 421 for a proxied Host
    header. Behind a TLS reverse proxy the real controls are TLS + OAuth, so the guard
    defaults OFF. Set MCP_HOST_ORIGIN_PROTECTION=true to harden: the public host/origin
    (from OIDC_BASE_URL) and Claude's connector origins are then allowed automatically,
    alongside any MCP_ALLOWED_HOSTS / MCP_ALLOWED_ORIGINS.
    """
    from urllib.parse import urlsplit

    if not config.host_origin_protection:
        return {"host_origin_protection": False}

    allowed_hosts = list(config.allowed_hosts or [])
    allowed_origins = list(config.allowed_origins or [])
    if config.oidc_base_url:
        parsed = urlsplit(config.oidc_base_url)
        if parsed.hostname and parsed.hostname not in allowed_hosts:
            allowed_hosts.append(parsed.hostname)
        public_origin = f"{parsed.scheme}://{parsed.netloc}"
        if public_origin not in allowed_origins:
            allowed_origins.append(public_origin)
    for claude_origin in ("https://claude.ai", "https://claude.com"):
        if claude_origin not in allowed_origins:
            allowed_origins.append(claude_origin)
    return {
        "host_origin_protection": True,
        "allowed_hosts": allowed_hosts,
        "allowed_origins": allowed_origins,
    }


def main() -> None:
    if config.transport == "http":
        mcp.run(
            transport="streamable-http",
            host=config.http_host,
            port=config.http_port,
            **_http_security_kwargs(),
        )
    else:
        mcp.run(transport="stdio")
