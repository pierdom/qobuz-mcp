# qobuz-mcp

A [Model Context Protocol](https://modelcontextprotocol.io) server that connects Claude to [Qobuz](https://www.qobuz.com), the hi-res music streaming service. Use it to explore new music, get personalised recommendations, and curate playlists — all through conversation.

## What you can do

- **Discover music** — ask Claude to find artists, albums, or tracks based on mood, genre, era, or similarity to music you already like
- **Explore your library** — Claude can read your favorites and playlists to understand your taste and make grounded recommendations
- **Curate playlists** — ask Claude to build a playlist from scratch, seed it with tracks it finds, then refine it by adding or removing tracks
- **Research artists and albums** — get full discographies, tracklists, and metadata

## Requirements

- A [Qobuz](https://www.qobuz.com) subscription
- [Claude Desktop](https://claude.ai/download)
- [uv](https://docs.astral.sh/uv/) (`brew install uv`)

## Installation

```bash
git clone https://github.com/pierdom/qobuz-mcp
cd qobuz-mcp
uv sync
```

## Configuration

Add the server to your Claude Desktop config at `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "qobuz-mcp": {
      "command": "uv",
      "args": ["--directory", "/path/to/qobuz-mcp", "run", "qobuz-mcp"],
      "env": {
        "QOBUZ_EMAIL": "your@email.com",
        "QOBUZ_PASSWORD": "your_password"
      }
    }
  }
}
```

Replace `/path/to/qobuz-mcp` with the actual path where you cloned the repo.

The server resolves Qobuz API credentials automatically from the web player bundle on startup — no developer account or API keys needed.

Restart Claude Desktop after editing the config.

## Tools

| Tool | Description |
|------|-------------|
| `search` | Search tracks, albums, artists, or playlists |
| `get_track` | Full details for a track |
| `get_album` | Album metadata and complete tracklist |
| `get_artist` | Artist details and discography |
| `get_streaming_url` | Time-limited streaming URL for a track |
| `get_user_favorites` | Your favorited tracks, albums, or artists |
| `get_user_playlists` | List your playlists |
| `get_playlist` | A playlist with its full tracklist |
| `create_playlist` | Create a new playlist, optionally with tracks |
| `add_tracks_to_playlist` | Add tracks to an existing playlist |
| `remove_tracks_from_playlist` | Remove tracks by their playlist position ID |
| `update_playlist` | Rename or update a playlist's description |

## Usage examples

**Discover music based on your taste**
> "Look at my Qobuz favorites and suggest 5 albums I'd probably enjoy. Tell me why each one fits."

**Build a playlist from scratch**
> "Create a playlist called 'Late Night Jazz' — piano trios, sparse, melancholic. Find 12 tracks and add them."

**Curate from a specific artist**
> "I want to get into ECM Records. Start with the 10 most essential albums and build me a sampler playlist."

**Refine an existing playlist**
> "Look at my 'Running' playlist and replace anything under 130 BPM with more energetic alternatives."

## Notes

- `remove_tracks_from_playlist` uses `playlist_track_id` (the per-position ID), not the regular `track_id`. Claude handles this automatically when it has called `get_playlist` first.
- Streaming URLs expire after ~10 minutes.
- Higher quality formats (FLAC 24-bit) require a matching Qobuz subscription tier.
