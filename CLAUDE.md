# CLAUDE.md

## Project

A stdio MCP server exposing Qobuz music streaming functionality to Claude Desktop. Built with Python + FastMCP. The primary use case is music discovery and playlist curation through conversation.

## Structure

```
src/qobuz_mcp/
  __main__.py   entry point — loads .env, runs stdio transport
  server.py     FastMCP instance, lifespan, all tool definitions
  client.py     async httpx wrapper around the Qobuz API
  connect/      Qobuz Connect controller (playback control on the user's devices)
    proto/        generated protobuf modules + .proto (vendored from qobit)
    wire.py       websocket framing (vendored from qobit — keep in sync)
    controller.py ConnectController: short-lived controller session per call
```

## Key implementation details

**Credential resolution** (`client.py:_fetch_bundle_credentials`)
Qobuz doesn't offer a public developer API. Credentials are resolved at startup by fetching `play.qobuz.com/login`, finding the bundle JS URL, then extracting all `appId` values from it. Each candidate is tried against `user/login` until one succeeds. The correct `app_id` as of the current bundle is `425621600`, but this can change — the brute-force approach handles that automatically.

**App secret** (`client._derive_secrets`)
Used only for signing `track/getFileUrl` requests. Derived from `seed`, `info`, and `extras` base64 values in the bundle via `base64_decode((seed + info + extras)[:-44])`. Multiple candidates are tried; the first that produces a valid streaming URL is cached.

**Streaming URL signing** (`client._sign`)
Qobuz uses an MD5 signature: `md5("trackgetFileUrl" + "format_id" + str(format_id) + "intentstream" + "track_id" + str(track_id) + str(rounded_ts) + clean_secret)` where `rounded_ts = ts - (ts % 600)` and `clean_secret` strips non-alphanumeric characters.

**playlist_track_id vs track_id**
Removing tracks from a playlist requires `playlist_track_id` (the per-entry position ID), not the regular `track_id`. `get_playlist` returns both. Always call `get_playlist` before `remove_tracks_from_playlist`.

**Qobuz Connect control** (`connect/`, `connect_*` tools)
The protocol is reverse-engineered; the renderer side lives in qobit
(github.com/pierdom/qobit, `docs/qobuz-connect.md`), and `proto/` + `wire.py`
are vendored from there. Facts that shaped the design, all verified live:
- The server rejects a join without a device, and *any* joined device is
  listed as a renderer in the apps' picker — so each tool call opens a
  short-lived session (`async with _connect() as c`) and leaves, instead of a
  persistent socket.
- Picking music = `CTRL_SRVR_QUEUE_LOAD_TRACKS`; the active renderer starts
  `tracks[queue_position]` by itself. No separate "play" is needed, but
  `connect_play` sends one as a fallback if the device doesn't report playing.
- After `SET_ACTIVE_RENDERER`, wait for the new device's first report before
  loading: the server's activation handover (often PAUSED) can otherwise land
  after the load and pause it.
- Confirmations only count renderer reports received *after* the command
  (`wait_for_player(..., since=t0)`): queue item ids restart at 0, so a stale
  "item 0, playing" would look like success.
- `qws/createToken` works with the email/password session's app_id.
- Join/leave per call is stable: 12 sequential and 3 concurrent joins (same
  controller uuid) all saw the device list, same session id, no Disconnect,
  and the renderer stayed registered. The device list (ADD_RENDERER) arrives
  before SESSION_STATE. An empty list still settles 1.5 s and rejoins once on a
  fresh token before being reported (`rejoined: true` in the status).
- Failures are real MCP errors: `ConnectError` subclasses fastmcp's `ToolError`;
  tools `raise ToolError(...)` rather than returning `{"error": ...}`. Messages
  name what is online and the session/endpoint checked (`_online(c)`), and
  `connect_status` returns a `connect` block {session, endpoint, rejoined} so a
  failure report can be matched to a session.

## Development

```bash
uv sync
uv run qobuz-mcp   # runs the server (requires .env with credentials)
```

`.env` format:
```
QOBUZ_EMAIL=your@email.com
QOBUZ_PASSWORD=your_password
```

`QOBUZ_APP_ID` and `QOBUZ_APP_SECRET` can be added as overrides but are not required.

## Adding tools

1. Add a method to `QobuzClient` in `client.py`
2. Add a `@mcp.tool()` function in `server.py` that calls it and returns a JSON string
3. Keep return values focused — extract only the fields Claude needs, avoid returning raw Qobuz API responses
