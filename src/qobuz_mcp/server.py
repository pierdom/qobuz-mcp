import json
import os
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.fastmcp import FastMCP

from .client import QobuzClient, QobuzError

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


mcp = FastMCP("qobuz-mcp", lifespan=lifespan)


def _c() -> QobuzClient:
    assert _client is not None, "Client not initialized"
    return _client


# --- format helpers ---

def _dur(seconds: int) -> str:
    m, s = divmod(seconds, 60)
    return f"{m}:{s:02d}"


def _fmt_track(t: dict) -> dict:
    artist = (
        t.get("performer", {}).get("name")
        or t.get("album", {}).get("artist", {}).get("name", "Unknown")
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
    return out


def _fmt_album(a: dict) -> dict:
    return {
        "album_id": a["id"],
        "title": a.get("title", ""),
        "artist": a.get("artist", {}).get("name", ""),
        "year": (a.get("release_date_original") or "")[:4],
        "tracks_count": a.get("tracks_count", 0),
        "duration": _dur(a.get("duration", 0)),
        "genre": a.get("genre", {}).get("name", ""),
        "label": a.get("label", {}).get("name", ""),
    }


def _fmt_artist(a: dict) -> dict:
    return {
        "artist_id": a["id"],
        "name": a.get("name", ""),
        "albums_count": a.get("albums_count", 0),
    }


def _fmt_playlist(p: dict) -> dict:
    return {
        "playlist_id": p["id"],
        "title": p.get("name", ""),
        "owner": p.get("owner", {}).get("name", ""),
        "tracks_count": p.get("tracks_count", 0),
        "duration": _dur(p.get("duration", 0)),
        "is_public": p.get("is_public", False),
        "description": p.get("description") or "",
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
    return _json({
        "url": data.get("url"),
        "format_id": data.get("format_id"),
        "mime_type": data.get("mime_type"),
        "sampling_rate": data.get("sampling_rate"),
        "bit_depth": data.get("bit_depth"),
        "file_size": data.get("file_size"),
    })


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
    return _json({
        "playlist_id": data["id"],
        "name": data.get("name"),
        "status": "created",
    })


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
    return _json({
        "playlist_id": playlist_id,
        "added": len(track_ids),
        "status": "ok",
    })


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
    return _json({
        "playlist_id": playlist_id,
        "removed": len(playlist_track_ids),
        "status": "ok",
    })
