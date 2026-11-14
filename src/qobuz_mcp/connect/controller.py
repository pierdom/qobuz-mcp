"""Short-lived Qobuz Connect *controller* sessions for MCP tools.

Each tool call opens one session — mint a token, join, collect the session's
state, send a command, wait for the server's echo, leave — instead of holding a
socket open. Two reasons, both verified live:

- The server rejects a join without a device, and **any** joined device is
  listed as a renderer in the apps' device picker. A short session means the
  MCP server shows up there for a second, not forever.
- No long-lived state to babysit in the (remote, containerised) MCP process.

Protocol background (renderer side, verified against the iOS app):
github.com/pierdom/qobit/blob/main/docs/qobuz-connect.md
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastmcp.exceptions import ToolError
from websockets.asyncio.client import connect

from . import wire
from .proto import qconnect_pb2 as qp

Mint = Callable[[], Awaitable[tuple[str, str]]]

# Stable per host, so repeated calls reuse one renderer slot server-side.
_SELF_UUID = uuid.uuid5(uuid.NAMESPACE_DNS, f"qobuz-mcp.{socket.gethostname()}").bytes
SELF_NAME = "qobuz-mcp (AI remote)"

PLAYING = qp.PLAYING_STATE_PLAYING
PAUSED = qp.PLAYING_STATE_PAUSED
STOPPED = qp.PLAYING_STATE_STOPPED

_STATE_NAMES = {
    qp.PLAYING_STATE_UNKNOWN: "unknown",
    STOPPED: "stopped",
    PLAYING: "playing",
    PAUSED: "paused",
}
# Device capability as advertised at join. The protocol only names levels;
# higher = higher resolution (exact kHz per level isn't documented).
_QUALITY_NAMES = {
    qp.AUDIO_QUALITY_MP3: "mp3",
    qp.AUDIO_QUALITY_CD: "cd",
    qp.AUDIO_QUALITY_HIRES_LEVEL1: "hi-res L1",
    qp.AUDIO_QUALITY_HIRES_LEVEL2: "hi-res L2",
    qp.AUDIO_QUALITY_HIRES_LEVEL3: "hi-res L3",
}
_TYPE_NAMES = {v: k.removeprefix("DEVICE_TYPE_").lower() for k, v in qp.DeviceType.items()}


class ConnectError(ToolError):
    """A ToolError, so MCP clients get a real error result (isError) with this
    message instead of a success that only says "error" inside the text."""


@dataclass
class Device:
    renderer_id: int
    name: str
    type: str
    brand: str
    model: str
    max_quality: str | None


@dataclass
class PlayerState:
    state: str
    item_id: int
    position_s: float
    duration_s: float


class ConnectController:
    """``async with ConnectController(mint) as c:`` — joined and synced."""

    def __init__(self, mint: Mint, timeout: float = 6.0) -> None:
        self._mint = mint
        self._timeout = timeout
        self._ws = None
        self._reader: asyncio.Task | None = None
        self._changed = asyncio.Condition()
        self._msg_id = 0
        self._batch_id = 0
        # session state
        self.self_id: int | None = None
        self.devices: dict[int, Device] = {}
        self.active_id: int | None = None
        self.session_seen = False
        # Diagnostics, surfaced by connect_status so a failure report can be
        # matched to a session: short session id, endpoint host, retried join.
        self.session_id: str | None = None
        self.endpoint: str | None = None
        self.rejoined = False
        self.queue_version = qp.QueueVersion()
        self.queue: list[tuple[int, int]] = []  # (queue_item_id, track_id), queue order
        self.autoplay: list[tuple[int, int]] = []
        self.shuffle = False
        self._shuffled: list[int] = []
        self.queue_seen = False
        self.renderer_states: dict[int, tuple[qp.RendererState, float]] = {}
        self.echoes: dict[bytes, int] = {}  # action_uuid -> message type
        self.errors: list[str] = []

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def __aenter__(self) -> ConnectController:
        await self._join()
        if not self.devices:
            # The device list normally arrives *before* SESSION_STATE (verified
            # over many joins), but an empty list is what an agent acts on as
            # "nothing online" — so give it a moment, then rejoin once on a
            # fresh token/socket before believing it.
            await self.wait(lambda: bool(self.devices), timeout=1.5)
            if not self.devices:
                self.rejoined = True
                await self.__aexit__(None, None, None)
                self._reset_state()
                await self._join()
                await self.wait(lambda: bool(self.devices), timeout=1.5)
        self.ask_queue_state()
        await self.wait(lambda: self.queue_seen, timeout=3.0)
        if self.active_id is not None and self.active_id not in self.renderer_states:
            self._send(
                qp.QConnectMessage(
                    message_type=qp.MESSAGE_TYPE_CTRL_SRVR_ASK_FOR_RENDERER_STATE,
                    ctrl_srvr_ask_for_renderer_state=qp.CtrlSrvrAskForRendererState(
                        renderer_id=self.active_id
                    ),
                )
            )
            await self.wait(lambda: self.active_id in self.renderer_states, timeout=2.0)
        return self

    def _reset_state(self) -> None:
        self.self_id = None
        self.devices = {}
        self.active_id = None
        self.session_seen = False
        self.session_id = None
        self.renderer_states = {}

    async def _join(self) -> None:
        endpoint, jwt = await self._mint()
        self.endpoint = endpoint.split("//")[-1].split("/")[0]
        self._ws = await connect(endpoint, max_size=None, open_timeout=self._timeout)
        self._msg_id = 2
        await self._ws.send(wire.handshake(1, jwt))
        self._reader = asyncio.create_task(self._read())
        self._send(
            qp.QConnectMessage(
                message_type=qp.MESSAGE_TYPE_CTRL_SRVR_JOIN_SESSION,
                ctrl_srvr_join_session=qp.CtrlSrvrJoinSession(
                    device_info=qp.DeviceInfo(
                        device_uuid=_SELF_UUID,
                        friendly_name=SELF_NAME,
                        type=qp.DEVICE_TYPE_UNKNOWN,
                    )
                ),
            )
        )
        if not await self.wait(lambda: self.session_seen):
            errors = f" (server said: {'; '.join(self.errors)})" if self.errors else ""
            await self.__aexit__(None, None, None)
            raise ConnectError(
                f"Qobuz Connect: joined {self.endpoint} but received no session state{errors}"
            )

    async def __aexit__(self, *exc: object) -> None:
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()

    # ── reading ───────────────────────────────────────────────────────────────

    async def _read(self) -> None:
        async for raw in self._ws:
            if isinstance(raw, str):
                continue
            messages, _ = wire.parse(raw)
            for m in messages:
                self._apply(m)
            async with self._changed:
                self._changed.notify_all()

    def _apply(self, m: qp.QConnectMessage) -> None:
        t = m.message_type
        if t == qp.MESSAGE_TYPE_SRVR_CTRL_ADD_RENDERER:
            a = m.srvr_ctrl_add_renderer
            info = a.device_info
            if info.device_uuid == _SELF_UUID:
                self.self_id = a.renderer_id
            else:
                self.devices[a.renderer_id] = Device(
                    a.renderer_id,
                    info.friendly_name,
                    _TYPE_NAMES.get(info.type, "unknown"),
                    info.brand,
                    info.model,
                    _QUALITY_NAMES.get(info.capabilities.max_audio_quality),
                )
        elif t == qp.MESSAGE_TYPE_SRVR_CTRL_REMOVE_RENDERER:
            self.devices.pop(m.srvr_ctrl_remove_renderer.renderer_id, None)
        elif t == qp.MESSAGE_TYPE_SRVR_CTRL_SESSION_STATE:
            s = m.srvr_ctrl_session_state
            self.active_id = s.active_renderer_id if s.active_renderer_id >= 0 else None
            self._note_version(s.queue_version)
            self.session_id = s.session_uuid.hex()[:8] or None
            self.session_seen = True
        elif t == qp.MESSAGE_TYPE_SRVR_CTRL_ACTIVE_RENDERER_CHANGED:
            rid = m.srvr_ctrl_active_renderer_changed.active_renderer_id
            self.active_id = rid if rid >= 0 else None
        elif t == qp.MESSAGE_TYPE_SRVR_CTRL_RENDERER_STATE_UPDATED:
            u = m.srvr_ctrl_renderer_state_updated
            self.renderer_states[u.renderer_id] = (u.player_state, time.time())
        elif t == qp.MESSAGE_TYPE_SRVR_CTRL_QUEUE_STATE:
            q = m.srvr_ctrl_queue_state
            self._note_version(q.queue_version)
            self.queue = [(r.queue_item_id, r.track_id) for r in q.tracks]
            self.autoplay = [(r.queue_item_id, r.track_id) for r in q.autoplay_tracks]
            self.shuffle = q.shuffle_mode
            idx = list(q.shuffled_track_indexes)
            ok = sorted(idx) == list(range(len(self.queue)))
            self._shuffled = idx if (self.shuffle and ok) else []
            self.queue_seen = True
        elif t == qp.MESSAGE_TYPE_SRVR_CTRL_QUEUE_TRACKS_LOADED:
            q = m.srvr_ctrl_queue_tracks_loaded
            self._note_version(q.queue_version)
            self.queue = [(r.queue_item_id, r.track_id) for r in q.tracks]
            self._shuffled = []
            self.echoes[bytes(q.action_uuid)] = t
        elif t in (
            qp.MESSAGE_TYPE_SRVR_CTRL_QUEUE_TRACKS_INSERTED,
            qp.MESSAGE_TYPE_SRVR_CTRL_QUEUE_TRACKS_ADDED,
            qp.MESSAGE_TYPE_SRVR_CTRL_QUEUE_ERROR_MESSAGE,
        ):
            sub = {
                qp.MESSAGE_TYPE_SRVR_CTRL_QUEUE_TRACKS_INSERTED: m.srvr_ctrl_queue_tracks_inserted,
                qp.MESSAGE_TYPE_SRVR_CTRL_QUEUE_TRACKS_ADDED: m.srvr_ctrl_queue_tracks_added,
                qp.MESSAGE_TYPE_SRVR_CTRL_QUEUE_ERROR_MESSAGE: m.srvr_ctrl_queue_error_message,
            }[t]
            self._note_version(sub.queue_version)
            self.echoes[bytes(sub.action_uuid)] = t
            if t == qp.MESSAGE_TYPE_SRVR_CTRL_QUEUE_ERROR_MESSAGE:
                self.errors.append(f"{sub.error.code}: {sub.error.message}")
        elif t == qp.MESSAGE_TYPE_ERROR:
            self.errors.append(f"{m.error.code}: {m.error.message}")

    def _note_version(self, v: qp.QueueVersion) -> None:
        if (v.major, v.minor) >= (self.queue_version.major, self.queue_version.minor):
            self.queue_version = qp.QueueVersion(major=v.major, minor=v.minor)

    async def wait(self, predicate: Callable[[], bool], timeout: float | None = None) -> bool:
        try:
            async with self._changed:
                await asyncio.wait_for(self._changed.wait_for(predicate), timeout or self._timeout)
            return True
        except TimeoutError:
            return False

    # ── queries ───────────────────────────────────────────────────────────────

    def play_order(self) -> list[tuple[int, int]]:
        base = [self.queue[i] for i in self._shuffled] if self._shuffled else list(self.queue)
        return base + self.autoplay

    def player(self, renderer_id: int | None = None) -> PlayerState | None:
        rid = self.active_id if renderer_id is None else renderer_id
        if rid is None or rid not in self.renderer_states:
            return None
        st, _ = self.renderer_states[rid]
        pos = st.current_position.value / 1000
        if st.playing_state == PLAYING and st.current_position.timestamp:
            # Controllers extrapolate from the renderer's last report.
            pos += max(0.0, time.time() - st.current_position.timestamp / 1000)
        dur = st.duration / 1000
        return PlayerState(
            _STATE_NAMES.get(st.playing_state, "unknown"),
            st.current_queue_item_id,
            min(pos, dur) if dur else pos,
            dur,
        )

    def find_device(self, query: str) -> Device:
        q = query.strip().lower()
        exact = [d for d in self.devices.values() if d.name.lower() == q]
        partial = [d for d in self.devices.values() if q in d.name.lower()]
        matches = exact or partial
        if not matches:
            names = ", ".join(d.name for d in self.devices.values()) or "none online"
            raise ConnectError(
                f"No Connect device matching {query!r}. Available: {names} "
                f"(session {self.session_id} via {self.endpoint})"
            )
        if len(matches) > 1:
            names = ", ".join(d.name for d in matches)
            raise ConnectError(f"{query!r} matches several devices: {names}")
        return matches[0]

    # ── commands ──────────────────────────────────────────────────────────────

    def _send(self, *messages: qp.QConnectMessage) -> None:
        self._msg_id += 1
        self._batch_id += 1
        data = wire.payload(self._msg_id, self._batch_id, list(messages))
        asyncio.get_running_loop().create_task(self._ws.send(data))

    def ask_queue_state(self) -> None:
        self._send(
            qp.QConnectMessage(
                message_type=qp.MESSAGE_TYPE_CTRL_SRVR_ASK_FOR_QUEUE_STATE,
                ctrl_srvr_ask_for_queue_state=qp.CtrlSrvrAskForQueueState(
                    queue_version_ref=self.queue_version, action_uuid=uuid.uuid4().bytes
                ),
            )
        )

    async def set_active(self, renderer_id: int) -> bool:
        if self.active_id == renderer_id:
            return True
        self._send(
            qp.QConnectMessage(
                message_type=qp.MESSAGE_TYPE_CTRL_SRVR_SET_ACTIVE_RENDERER,
                ctrl_srvr_set_active_renderer=qp.CtrlSrvrSetActiveRenderer(renderer_id=renderer_id),
            )
        )
        return await self.wait(lambda: self.active_id == renderer_id)

    async def set_player_state(
        self,
        playing: int | None = None,
        position_s: float | None = None,
        item_id: int | None = None,
    ) -> None:
        kw: dict = {}
        if playing is not None:
            kw["playing_state"] = playing
        if position_s is not None:
            kw["current_position"] = int(max(0.0, position_s) * 1000)
        if item_id is not None:
            kw["current_queue_item"] = qp.QueueItemRef(queue_version=self.queue_version, id=item_id)
        self._send(
            qp.QConnectMessage(
                message_type=qp.MESSAGE_TYPE_CTRL_SRVR_SET_PLAYER_STATE,
                ctrl_srvr_set_player_state=qp.CtrlSrvrSetPlayerState(**kw),
            )
        )

    async def _queue_command(self, build: Callable[[bytes], qp.QConnectMessage]) -> None:
        action = uuid.uuid4().bytes
        self._send(build(action))
        if not await self.wait(lambda: action in self.echoes):
            raise ConnectError("Qobuz Connect: the server did not confirm the queue change")
        if self.echoes[action] == qp.MESSAGE_TYPE_SRVR_CTRL_QUEUE_ERROR_MESSAGE:
            raise ConnectError(f"Qobuz Connect refused the queue change: {self.errors[-1]}")

    async def load(self, track_ids: list[int], start_index: int = 0) -> None:
        """Replace the queue. The active renderer starts ``track_ids[start_index]``
        by itself — that is how the official apps play a song."""
        await self._queue_command(
            lambda action: qp.QConnectMessage(
                message_type=qp.MESSAGE_TYPE_CTRL_SRVR_QUEUE_LOAD_TRACKS,
                ctrl_srvr_queue_load_tracks=qp.CtrlSrvrQueueLoadTracks(
                    queue_version_ref=self.queue_version,
                    action_uuid=action,
                    track_ids=track_ids,
                    queue_position=start_index,
                    context_uuid=uuid.uuid4().bytes,
                ),
            )
        )

    async def insert_next(self, track_ids: list[int], after_item_id: int) -> None:
        await self._queue_command(
            lambda action: qp.QConnectMessage(
                message_type=qp.MESSAGE_TYPE_CTRL_SRVR_QUEUE_INSERT_TRACKS,
                ctrl_srvr_queue_insert_tracks=qp.CtrlSrvrQueueInsertTracks(
                    queue_version_ref=self.queue_version,
                    action_uuid=action,
                    track_ids=track_ids,
                    insert_after=after_item_id,
                    context_uuid=uuid.uuid4().bytes,
                ),
            )
        )

    async def add_last(self, track_ids: list[int]) -> None:
        await self._queue_command(
            lambda action: qp.QConnectMessage(
                message_type=qp.MESSAGE_TYPE_CTRL_SRVR_QUEUE_ADD_TRACKS,
                ctrl_srvr_queue_add_tracks=qp.CtrlSrvrQueueAddTracks(
                    queue_version_ref=self.queue_version,
                    action_uuid=action,
                    track_ids=track_ids,
                    context_uuid=uuid.uuid4().bytes,
                ),
            )
        )

    async def wait_for_player(
        self, predicate: Callable[[PlayerState], bool], since: float, timeout: float = 5.0
    ) -> PlayerState | None:
        """Wait until the active renderer reports, *after* ``since`` (time.time()),
        a state matching ``predicate``. Reports from before the command don't
        count — queue item ids restart at 0, so a stale "item 0, playing" would
        otherwise look like a confirmation. Returns the matching state or None."""

        def ok() -> bool:
            rid = self.active_id
            if rid is None or rid not in self.renderer_states:
                return False
            if self.renderer_states[rid][1] < since:
                return False
            p = self.player()
            return p is not None and predicate(p)

        if await self.wait(ok, timeout=timeout):
            return self.player()
        return None
