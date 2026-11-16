import base64
import hashlib
import re
import time
from typing import Any

import httpx

API_BASE = "https://www.qobuz.com/api.json/0.2/"
PLAY_BASE = "https://play.qobuz.com"

QUALITY_IDS = {
    "MP3_320": 5,
    "FLAC_CD": 6,
    "FLAC_24_96": 7,
    "FLAC_24_192": 27,
}

_BUNDLE_RE = re.compile(r'<script src="(/resources/[^"]+/bundle\.js)"')
_ALL_APP_IDS_RE = re.compile(r'appId:"(\d+)"')
_APP_ID_SECRET_RE = re.compile(r'appId:"(\d{9})",appSecret:"(\w{32})"')
_SEED_TZ_RE = re.compile(r'[a-z]\.initialSeed\("([\w=]+)",window\.utimezone\.([a-z]+)\)')
_TZ_INFO_RE = re.compile(r'name:"\w+/([a-z]+)",info:"([\w=]+)",extras:"([\w=]+)"')


def _derive_secrets(bundle: str) -> list[str]:
    # Try direct extraction first (app_secret sometimes present in clear)
    direct = _APP_ID_SECRET_RE.search(bundle)
    if direct:
        return [direct.group(2)]

    seed_tz_pairs = _SEED_TZ_RE.findall(bundle)
    tz_info = {name: (info, extras) for name, info, extras in _TZ_INFO_RE.findall(bundle)}

    secrets = []
    for seed, timezone in seed_tz_pairs:
        if timezone not in tz_info:
            continue
        info, extras = tz_info[timezone]
        try:
            combined = seed + info + extras
            decoded = base64.standard_b64decode(combined[:-44]).decode("utf-8")
            if decoded:
                secrets.append(decoded)
        except Exception:
            pass

    return secrets


class QobuzError(Exception):
    pass


class QobuzClient:
    def __init__(
        self,
        app_id: str | None = None,
        app_secret: str | None = None,
    ):
        self._app_id = app_id
        self._app_secret = app_secret
        self._secret_candidates: list[str] = [app_secret] if app_secret else []
        self.user_auth_token: str | None = None
        self._http = httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        await self._http.aclose()

    async def login(self, email: str, password: str) -> None:
        bundle_app_ids = await self._fetch_bundle_credentials()

        pwd_md5 = hashlib.md5(password.encode()).hexdigest()

        # If app_id was supplied explicitly, try it first
        candidates = (
            [self._app_id] + [i for i in bundle_app_ids if i != self._app_id]
            if self._app_id
            else bundle_app_ids
        )

        last_err: QobuzError | None = None
        for app_id in candidates:
            self._app_id = app_id
            try:
                data = await self._get("user/login", username=email, password=pwd_md5)
            except QobuzError as e:
                try:
                    data = await self._get("user/login", username=email, password=password)
                except QobuzError as e2:
                    last_err = e2
                    continue
            self.user_auth_token = data["user_auth_token"]
            return

        raise last_err or QobuzError("No working app_id found in Qobuz bundle")

    # --- bundle credential resolution ---

    async def _fetch_bundle_credentials(self) -> list[str]:
        r = await self._http.get(f"{PLAY_BASE}/login")
        r.raise_for_status()

        bundle_match = _BUNDLE_RE.search(r.text)
        if not bundle_match:
            raise QobuzError("Cannot locate Qobuz bundle script in login page")

        r = await self._http.get(PLAY_BASE + bundle_match.group(1))
        r.raise_for_status()
        bundle = r.text

        # Extract all candidate app_ids (deduplicated, order preserved)
        all_ids = list(dict.fromkeys(_ALL_APP_IDS_RE.findall(bundle)))
        if not all_ids:
            raise QobuzError("Cannot extract app_id from Qobuz bundle")

        # Extract secret candidates
        candidates = _derive_secrets(bundle)
        self._secret_candidates = candidates + [
            s for s in self._secret_candidates if s not in candidates
        ]

        return all_ids

    # --- internal helpers ---

    def _base_params(self) -> dict[str, Any]:
        assert self._app_id, "app_id not resolved"
        params: dict[str, Any] = {"app_id": self._app_id}
        if self.user_auth_token:
            params["user_auth_token"] = self.user_auth_token
        return params

    async def _get(self, endpoint: str, **params: Any) -> dict:
        p = self._base_params()
        p.update(params)
        r = await self._http.get(API_BASE + endpoint, params=p)
        self._raise_for_status(r)
        return r.json()

    async def _post(self, endpoint: str, **data: Any) -> dict:
        d = self._base_params()
        d.update(data)
        r = await self._http.post(API_BASE + endpoint, data=d)
        self._raise_for_status(r)
        return r.json()

    @staticmethod
    def _raise_for_status(r: httpx.Response) -> None:
        if r.is_error:
            try:
                msg = r.json().get("message", r.text)
            except Exception:
                msg = r.text
            raise QobuzError(f"Qobuz API error {r.status_code}: {msg}")

    def _sign(self, track_id: str, format_id: int, secret: str) -> tuple[str, int]:
        ts = int(time.time())
        r_ts = ts - (ts % 600)
        clean = "".join(c for c in secret if c.isalnum())
        raw = f"trackgetFileUrlformat_id{format_id}intentstreamtrack_id{track_id}{r_ts}{clean}"
        return hashlib.md5(raw.encode()).hexdigest(), r_ts

    # --- catalog ---

    async def create_connect_token(self) -> tuple[str, str]:
        """Mint a Qobuz Connect socket token: ``(endpoint, jwt)``. One token per
        socket — the cloud evicts a second socket using the same token."""
        r = await self._http.post(
            API_BASE + "qws/createToken",
            data={"jwt": "jwt_qws"},
            headers={"X-App-Id": self._app_id or "", "X-User-Auth-Token": self.user_auth_token or ""},
        )
        self._raise_for_status(r)
        tok = (r.json() or {}).get("jwt_qws") or {}
        if not tok.get("jwt") or not tok.get("endpoint"):
            raise QobuzError("Connect token missing from qws/createToken response")
        return tok["endpoint"], tok["jwt"]

    async def search(self, query: str, type: str = "tracks", limit: int = 20) -> dict:
        return await self._get("catalog/search", query=query, type=type, limit=limit)

    async def get_album(self, album_id: str) -> dict:
        return await self._get("album/get", album_id=album_id)

    async def get_track(self, track_id: str) -> dict:
        return await self._get("track/get", track_id=track_id)

    async def get_artist(self, artist_id: str, albums_limit: int = 20) -> dict:
        return await self._get(
            "artist/get",
            artist_id=artist_id,
            extra="albums",
            albums_limit=albums_limit,
            albums_sort="release_date",
        )

    async def get_streaming_url(self, track_id: str, quality: str = "FLAC_CD") -> dict:
        format_id = QUALITY_IDS.get(quality, QUALITY_IDS["FLAC_CD"])

        # Try each secret candidate; cache the first one that works
        candidates = self._secret_candidates or [self._app_secret or ""]
        last_error: QobuzError | None = None

        for secret in candidates:
            sig, ts = self._sign(track_id, format_id, secret)
            try:
                result = await self._get(
                    "track/getFileUrl",
                    track_id=track_id,
                    format_id=format_id,
                    intent="stream",
                    request_ts=ts,
                    request_sig=sig,
                )
                # Cache winning secret at front of list
                self._app_secret = secret
                self._secret_candidates = [secret] + [
                    s for s in self._secret_candidates if s != secret
                ]
                return result
            except QobuzError as e:
                last_error = e

        raise last_error or QobuzError("No valid app_secret found for streaming")

    # --- user library ---

    async def get_user_favorites(self, type: str = "tracks", limit: int = 50) -> dict:
        return await self._get("favorite/getUserFavorites", type=type, limit=limit)

    async def _favorites(
        self,
        endpoint: str,
        track_ids: list[str] | None,
        album_ids: list[str] | None,
        artist_ids: list[str] | None,
    ) -> dict:
        params = {
            k: ",".join(str(i) for i in v)
            for k, v in (
                ("track_ids", track_ids),
                ("album_ids", album_ids),
                ("artist_ids", artist_ids),
            )
            if v
        }
        if not params:
            raise QobuzError("No track, album, or artist IDs given")
        return await self._post(endpoint, **params)

    async def add_favorites(
        self,
        track_ids: list[str] | None = None,
        album_ids: list[str] | None = None,
        artist_ids: list[str] | None = None,
    ) -> dict:
        return await self._favorites("favorite/create", track_ids, album_ids, artist_ids)

    async def remove_favorites(
        self,
        track_ids: list[str] | None = None,
        album_ids: list[str] | None = None,
        artist_ids: list[str] | None = None,
    ) -> dict:
        return await self._favorites("favorite/delete", track_ids, album_ids, artist_ids)

    async def get_user_playlists(self, limit: int = 50) -> dict:
        return await self._get("playlist/getUserPlaylists", limit=limit)

    async def get_playlist(self, playlist_id: str) -> dict:
        return await self._get(
            "playlist/get",
            playlist_id=playlist_id,
            extra="tracks",
            limit=500,
        )

    # --- playlist write ---

    async def create_playlist(
        self,
        name: str,
        description: str = "",
        is_public: bool = False,
        track_ids: list[str] | None = None,
    ) -> dict:
        result = await self._post(
            "playlist/create",
            name=name,
            description=description,
            is_public="true" if is_public else "false",
        )
        if track_ids:
            await self.add_tracks_to_playlist(str(result["id"]), track_ids)
        return result

    async def update_playlist(
        self,
        playlist_id: str,
        name: str | None = None,
        description: str | None = None,
        is_public: bool | None = None,
    ) -> dict:
        data: dict[str, Any] = {"playlist_id": playlist_id}
        if name is not None:
            data["name"] = name
        if description is not None:
            data["description"] = description
        if is_public is not None:
            data["is_public"] = "true" if is_public else "false"
        return await self._post("playlist/update", **data)

    async def add_tracks_to_playlist(self, playlist_id: str, track_ids: list[str]) -> dict:
        return await self._post(
            "playlist/addTracks",
            playlist_id=playlist_id,
            track_ids=",".join(str(t) for t in track_ids),
        )

    async def remove_tracks_from_playlist(
        self, playlist_id: str, playlist_track_ids: list[str]
    ) -> dict:
        return await self._post(
            "playlist/deleteTracks",
            playlist_id=playlist_id,
            playlist_track_ids=",".join(str(t) for t in playlist_track_ids),
        )

    async def delete_playlist(self, playlist_id: str) -> dict:
        return await self._post("playlist/delete", playlist_id=playlist_id)
