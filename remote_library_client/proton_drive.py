# SPDX-License-Identifier: AGPL-3.0-or-later
"""Proton Drive public-share library provider (``proton-public.v1``).

Registers an anonymous Proton Drive **public share** (``https://drive.proton.me/urls/<token>#<pw>``)
of package files as a FeedBack library provider. Proton public shares are end-to-end encrypted,
so this does real work the other provider types do not:

1. an anonymous **SRP-6a** handshake against the share token grants a session (no account);
2. the URL password (the ``#`` fragment) is bcrypt key-stretched and used to unwind Proton's
   OpenPGP key hierarchy (share key -> root node key -> per-file node keys);
3. filenames are decrypted from the folder listing (metadata is filename-parsed — an
   ``Artist-Title.feedpak`` convention on the shares seen, distinct from Google Drive's
   ``Artist - Album - Title``);
4. on play, the file's encrypted content blocks are downloaded, decrypted, and reassembled
   into the local library.

The SRP handshake lives in :mod:`remote_library_client.proton_srp` (a dependency-light
reimplementation); OpenPGP decryption uses ``pysequoia`` (imported lazily so the module loads —
and its tests run — without the native dependency present). The URL password is a secret: it is
stored per source as ``urlPassword``, never placed in ``baseUrl``, never logged, and stripped
from every API response (see ``routes._public_source``).

Downloads run in the background for the same reason as Google Drive: FeedBack core caps the
sync-song capability at ~250 ms, far too short for an internet download + decrypt.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import threading
import time
from collections import OrderedDict
from pathlib import Path
from urllib import error, parse, request

from remote_library_client import proton_srp
from remote_library_client.provider import (
    MAX_BINARY_RESPONSE_BYTES,
    MAX_JSON_RESPONSE_BYTES,
    MAX_PACKAGE_RESPONSE_BYTES,
    AuthRequiredError,
    BaseLibraryProvider,
    LibraryImporter,
    _public_error_message,
    _read_limited,
    _safe_int,
    playback_settings_key,
    provider_id_for_source,
    sanitize_filename,
)

PROTON_API_BASE = "https://drive.proton.me/api"
PROTON_HOSTS = {"drive.proton.me"}
# Required on data endpoints; Proton bumps this over time and eventually rejects stale values,
# so keep it current if listing/auth starts failing with an app-version error.
PROTON_APP_VERSION = "web-drive@5.2.0"
PACKAGE_SUFFIXES = (".feedpak", ".sloppak", ".psarc", ".zip")
_SUFFIX_RE = re.compile(r"\.(?:feedpak|sloppak|psarc|zip)$", re.I)
_USER_AGENT = "Mozilla/5.0 (compatible; FeedBackRemoteLibraryClient/1.0)"


def _pysequoia():
    """Import pysequoia lazily so this module (and its tests) load without the native dep."""
    import pysequoia

    return pysequoia


# -- URL + filename parsing ----------------------------------------------------


def parse_proton_share_url(url: str) -> tuple[str, str] | None:
    """Return ``(token, url_password)`` from a Proton public-share link, or ``None``.

    The token comes from ``/urls/<token>`` in the path; the URL password is the ``#`` fragment
    (Proton's shareable links for generated-password shares embed it there).
    """
    raw = str(url or "").strip()
    parsed = parse.urlparse(raw)
    if (parsed.hostname or "").lower() not in PROTON_HOSTS:
        return None
    match = re.search(r"/urls/([A-Za-z0-9_-]+)", parsed.path)
    if not match:
        return None
    return match.group(1), (parsed.fragment or "").strip()


def is_proton_share_url(url: str) -> bool:
    """True when the input looks like a Proton Drive public-share link (password aside)."""
    return parse_proton_share_url(url) is not None


def parse_proton_filename(filename: str) -> tuple[str, str, str]:
    """Recover ``(artist, album, title)`` from a Proton package name. Tolerant; never raises.

    Handles both the spaced ``Artist - Album - Title`` convention and the underscored
    ``Artist_Name-Title`` convention seen on Proton shares (``_`` for spaces, a single ``-``
    between artist and title).
    """
    stem = _SUFFIX_RE.sub("", str(filename or "")).strip()
    if " - " in stem:
        parts = [part.strip() for part in stem.split(" - ") if part.strip()]
        if len(parts) >= 3:
            return parts[0], " - ".join(parts[1:-1]), parts[-1]
        if len(parts) == 2:
            return parts[0], "", parts[1]
        if len(parts) == 1:
            return "Unknown artist", "", parts[0]
        return "Unknown artist", "", stem
    if "-" in stem:
        artist, _sep, title = stem.partition("-")
        artist = artist.replace("_", " ").strip()
        title = title.replace("_", " ").strip()
        return artist or "Unknown artist", "", title or stem.replace("_", " ").strip()
    return "Unknown artist", "", stem.replace("_", " ").strip() or str(filename or "")


def format_from_filename(filename: str) -> str:
    return "psarc" if Path(filename or "").suffix.lower() == ".psarc" else "sloppak"


def package_form_from_filename(filename: str) -> str:
    return "psarc-file" if Path(filename or "").suffix.lower() == ".psarc" else "sloppak-zip"


# -- OpenPGP helpers (pysequoia) -----------------------------------------------


def _armored_bytes(value) -> bytes:
    """Coerce a Proton crypto field to raw bytes for pysequoia: pass armored text through,
    base64-decode packet fields (e.g. ``ContentKeyPacket``)."""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    text = str(value or "")
    if "-----BEGIN" in text:
        return text.encode()
    return base64.b64decode(text)


def _as_text(value: bytes) -> str:
    return value.decode("utf-8") if isinstance(value, (bytes, bytearray)) and _is_utf8(value) else (
        value.decode("latin-1") if isinstance(value, (bytes, bytearray)) else str(value)
    )


def _is_utf8(value: bytes) -> bool:
    try:
        value.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def _decrypt_password(message, passphrase: str) -> bytes:
    """Decrypt a symmetric (password-encrypted) PGP message — Proton's ``SharePassphrase``."""
    out = _pysequoia().decrypt(_armored_bytes(message), passwords=[passphrase]).bytes
    if out is None:
        raise RuntimeError("password decryption produced no data")
    return out


def _key_decryptor(armored_key, passphrase):
    """Build a pysequoia decryptor from a passphrase-locked Proton private key."""
    cert = _pysequoia().Cert.from_bytes(_armored_bytes(armored_key))
    secrets = cert.secrets
    if secrets is None:
        raise RuntimeError("Proton key carries no secret material")
    if isinstance(passphrase, (bytes, bytearray)):
        password = _as_text(passphrase)
    else:
        password = str(passphrase) if passphrase else ""
    if not password:
        return secrets.decryptor()
    try:
        return secrets.decryptor(password)
    except RuntimeError as exc:
        # pysequoia rejects a password on an already-unlocked key ("secret key is not
        # encrypted"); use it directly. (Proton keys are locked, so this is a safety net.)
        if "not encrypted" in str(exc):
            return secrets.decryptor()
        raise


def _decrypt_with_key(message, decryptor) -> bytes:
    """Decrypt a key-encrypted PGP message (node passphrase / name / content block)."""
    out = _pysequoia().decrypt(_armored_bytes(message), decryptor=decryptor).bytes
    if out is None:
        raise RuntimeError("key decryption produced no data")
    return out


def _proton_http_error(exc: error.HTTPError) -> Exception:
    """Map a Proton API HTTP error to a friendly, secret-free exception."""
    body = ""
    try:
        body = exc.read(65536).decode("utf-8", "replace")
    except Exception:
        pass
    code = None
    try:
        code = json.loads(body).get("Code")
    except Exception:
        pass
    if exc.code in (401, 429) or exc.code == 405:
        # Anti-abuse throttling shows up as 429 (and, once seen, 405 on the auth endpoint).
        return RuntimeError("Proton is temporarily rate-limiting this share; try again in a few minutes")
    if code == 2026:
        return AuthRequiredError("the Proton share password is incorrect")
    return RuntimeError(f"Proton API error (HTTP {exc.code})")


# -- Proton public-share transport ---------------------------------------------


class _ProtonShareClient:
    """Anonymous transport for one Proton public share: SRP auth (cached), authed JSON calls,
    folder/file metadata fetches, and raw encrypted-block downloads.

    Kept crypto-free (it only moves bytes) so the provider's tests can substitute a fake and the
    OpenPGP decryption can be exercised independently.
    """

    def __init__(self, token: str, url_password: str, urlopen, timeout: float = 20) -> None:
        self._token = token
        self._url_password = url_password
        self._urlopen = urlopen  # the provider's redirect-guarded opener (callable(req, timeout))
        self._timeout = timeout
        self._session: dict | None = None
        self._lock = threading.RLock()

    def _request(self, path: str, data: bytes | None = None, *, authed: bool = True) -> dict:
        headers = {
            "User-Agent": _USER_AGENT,
            "Content-Type": "application/json",
            "x-pm-appversion": PROTON_APP_VERSION,
        }
        if authed:
            session = self._ensure_session()
            headers["x-pm-uid"] = session["uid"]
            headers["Authorization"] = f"Bearer {session['access_token']}"
        req = request.Request(
            f"{PROTON_API_BASE}{path}", data=data, headers=headers, method="POST" if data else "GET"
        )
        try:
            with self._urlopen(req, timeout=self._timeout) as response:
                return json.loads(_read_limited(response, MAX_JSON_RESPONSE_BYTES).decode("utf-8") or "{}")
        except error.HTTPError as exc:
            raise _proton_http_error(exc) from exc

    def _authenticate(self) -> dict:
        info = self._request(f"/drive/urls/{self._token}/info", authed=False)
        modulus = proton_srp.extract_modulus(info["Modulus"])
        user = proton_srp.SRPUser(self._url_password, modulus)
        client_proof = user.process_challenge(
            base64.b64decode(info["UrlPasswordSalt"]), base64.b64decode(info["ServerEphemeral"])
        )
        if client_proof is None:
            raise RuntimeError("Proton SRP safety check failed")
        body = json.dumps(
            {
                "ClientEphemeral": base64.b64encode(user.get_challenge()).decode(),
                "ClientProof": base64.b64encode(client_proof).decode(),
                "SRPSession": info["SRPSession"],
            }
        ).encode()
        result = self._request(f"/drive/urls/{self._token}/auth", data=body, authed=False)
        # Verify the server's proof when present (detects an active MITM); the transport is already
        # TLS-authenticated to drive.proton.me, so a missing field is not treated as fatal.
        server_proof = result.get("ServerProof")
        if server_proof and not user.verify_session(base64.b64decode(server_proof)):
            raise RuntimeError("Proton server proof verification failed")
        expires_in = _safe_int(result.get("ExpiresIn"), 600)
        return {
            "uid": result["UID"],
            "access_token": result["AccessToken"],
            "expires_at": time.monotonic() + max(60, expires_in - 30),
        }

    def _ensure_session(self) -> dict:
        with self._lock:
            if self._session and time.monotonic() < self._session["expires_at"]:
                return self._session
            self._session = self._authenticate()
            return self._session

    def bootstrap(self) -> dict:
        """The share + root-folder crypto material from ``GET /drive/urls/{token}`` -> ``Token``:
        ``ShareKey`` / ``SharePassphrase`` / ``SharePasswordSalt`` plus the root folder's
        ``NodeKey`` / ``NodePassphrase`` / ``LinkID`` — everything needed to unwind the hierarchy
        and list the root folder. (The auth response's ``Share`` carries only the share half.)"""
        self._ensure_session()
        payload = self._request(f"/drive/urls/{self._token}")
        material = payload.get("Token") or payload.get("Share") or payload
        if not material.get("LinkID"):
            raise RuntimeError("Proton share bootstrap is missing the root link id")
        return material

    def fetch_children(self, link_id: str, page_size: int = 150) -> list[dict]:
        children: list[dict] = []
        for page in range(0, 500):  # generous cap; a public library folder is not unbounded
            payload = self._request(
                f"/drive/urls/{self._token}/folders/{parse.quote(link_id)}/children"
                f"?Page={page}&PageSize={page_size}"
            )
            links = payload.get("Links") or []
            children.extend(links)
            if len(links) < page_size:
                break
        return children

    def fetch_file_revision(self, link_id: str, page_size: int = 200) -> dict:
        # Blocks are paginated by FromBlockIndex; walk every page so large files aren't truncated.
        revision: dict = {}
        blocks: list[dict] = []
        from_index = 1
        while True:
            payload = self._request(
                f"/drive/urls/{self._token}/files/{link_id}?FromBlockIndex={from_index}&PageSize={page_size}"
            )
            revision = payload.get("Revision") or payload
            page = revision.get("Blocks") or []
            blocks.extend(page)
            if len(page) < page_size:
                break
            from_index += len(page)
        merged = dict(revision)
        merged["Blocks"] = blocks
        return merged

    def download_block(self, bare_url: str) -> bytes:
        req = request.Request(bare_url, headers={"User-Agent": _USER_AGENT})
        try:
            with self._urlopen(req, timeout=120) as response:
                return _read_limited(response, MAX_BINARY_RESPONSE_BYTES)
        except error.HTTPError as exc:
            raise _proton_http_error(exc) from exc


def _decrypt_child_record(child: dict, parent_decryptor) -> dict | None:
    """Decrypt one folder child to a catalog record, or ``None`` if it is not a package file."""
    try:
        child_passphrase = _decrypt_with_key(child["NodePassphrase"], parent_decryptor)
        child_decryptor = _key_decryptor(child["NodeKey"], child_passphrase)
        # A child's Name is encrypted to its own node key; older shares encrypt it to the
        # parent, so fall back to the parent decryptor.
        try:
            name_bytes = _decrypt_with_key(child["Name"], child_decryptor)
        except Exception:
            name_bytes = _decrypt_with_key(child["Name"], parent_decryptor)
        name = _as_text(name_bytes).strip()
    except Exception:
        return None
    if not name.lower().endswith(PACKAGE_SUFFIXES):
        return None
    # The per-file content key rides on the file link (FileProperties.ContentKeyPacket), not
    # on the revision fetched at download time — capture it now while decrypting the listing.
    file_props = child.get("FileProperties") or {}
    return {
        "linkId": str(child.get("LinkID") or ""),
        "name": name,
        "size": _safe_int(child.get("Size"), 0),
        "nodeKey": child["NodeKey"],
        "nodePassphrase": _as_text(child_passphrase),
        "contentKeyPacket": file_props.get("ContentKeyPacket") or child.get("ContentKeyPacket") or "",
    }


# -- Provider ------------------------------------------------------------------


class ProtonPublicShareProvider(BaseLibraryProvider):
    """Library provider backed by an anonymous Proton Drive public share of package files."""

    type = "proton-public.v1"

    def __init__(
        self,
        source: dict,
        cache_dir: Path,
        local_library_root: Path | None = None,
        library_importer: LibraryImporter | None = None,
        nam_config_dir: Path | None = None,
    ) -> None:
        token, url_password = _share_credentials(source)
        self._token = token
        self._url_password = url_password
        base_url = f"https://drive.proton.me/urls/{token}"
        provider_id = str(
            source.get("providerId") or provider_id_for_source(f"proton_{token}", base_url, prefix="proton")
        )
        super().__init__(
            {**source, "providerId": provider_id},
            cache_dir,
            origin_host="drive.proton.me",
            allow_unsafe_redirects=bool(source.get("allowUnsafeRedirects")),
            local_library_root=local_library_root,
            library_importer=library_importer,
            nam_config_dir=nam_config_dir,
        )
        self.base_url = base_url
        self.label = str(source.get("label") or source.get("sourceName") or f"Proton share {token[:8]}")
        self._client = _ProtonShareClient(token, url_password, self._urlopen)
        # Decrypted-catalog cache (records carry secret node material, so it lives in memory here,
        # not in the deep-copying metadata TTL cache).
        self._catalog_lock = threading.Lock()
        self._catalog: dict | None = None
        # Background-sync state (see sync_song; same rationale as google_drive.py).
        self._sync_lock = threading.Lock()
        self._downloads: dict[str, dict] = {}

    # -- catalog (auth + list + decrypt names) ---------------------------

    def _build_catalog(self) -> list[dict]:
        material = self._client.bootstrap()
        url_passphrase = proton_srp.compute_key_password(
            self._url_password, base64.b64decode(material["SharePasswordSalt"])
        )
        share_passphrase = _decrypt_password(material["SharePassphrase"], url_passphrase)
        share_decryptor = _key_decryptor(material["ShareKey"], share_passphrase)
        # The root folder's own node passphrase is encrypted to the share key; its node key then
        # decrypts each child's passphrase.
        root_passphrase = _decrypt_with_key(material["NodePassphrase"], share_decryptor)
        root_decryptor = _key_decryptor(material["NodeKey"], root_passphrase)
        records: list[dict] = []
        for child in self._client.fetch_children(material["LinkID"]):
            record = self._decrypt_child(child, root_decryptor)
            if record:
                records.append(record)
        return records

    def _decrypt_child(self, child: dict, root_decryptor) -> dict | None:
        return _decrypt_child_record(child, root_decryptor)

    def _catalog_snapshot(self) -> dict:
        with self._catalog_lock:
            now = time.monotonic()
            if self._catalog and now - self._catalog["at"] <= self.metadata_cache_ttl_seconds:
                return self._catalog
            records = self._build_catalog()
            self._catalog = {"at": now, "records": records, "by_id": {r["linkId"]: r for r in records}}
            return self._catalog

    def _catalog_record(self, song_id: str) -> dict | None:
        return self._catalog_snapshot()["by_id"].get(song_id)

    def _invalidate_catalog(self) -> None:
        with self._catalog_lock:
            self._catalog = None

    # -- normalization + querying (mirrors google_drive.py) --------------

    def _downloaded_names(self) -> frozenset[str]:
        if not self.local_library_root or not self.local_library_root.exists():
            return frozenset()
        folder = self.local_library_root / self._source_folder_name()
        try:
            return frozenset(item.name for item in folder.iterdir() if item.is_file())
        except OSError:
            return frozenset()

    def _normalize_record(self, record: dict, downloaded: frozenset[str] = frozenset()) -> dict:
        link_id = record["linkId"]
        filename = record["name"]
        artist, album, title = parse_proton_filename(filename)
        safe_name = sanitize_filename(Path(filename).name, "remote-song.feedpak")
        local = f"{self._source_folder_name()}/{safe_name}" if safe_name in downloaded else ""
        return {
            "filename": local or link_id,
            "song_id": link_id,
            "remote_id": link_id,
            "remoteSongId": link_id,
            "remoteFilename": filename,
            "libraryProviderId": self.id,
            "provider": self.id,
            "sourceId": self.source.get("sourceId") or f"proton_{self._token}",
            "sourceName": self.label,
            "title": title,
            "artist": artist or "Unknown artist",
            "album": album,
            "year": None,
            "duration": None,
            "format": format_from_filename(filename),
            "packageForm": package_form_from_filename(filename),
            "syncSupport": "syncable",
            "status": "remote-only",
            "capabilities": ["package-download"],
            "settingsKey": playback_settings_key(filename),
            "arrangements": [],
            "has_lyrics": False,
            "hasLyrics": False,
            "stem_count": 0,
            "stemCount": 0,
            "stem_ids": [],
            "stemIds": [],
            "tuning": "",
            "tuning_name": "",
            "sizeBytes": record.get("size", 0),
            "localFilename": local,
            "local_filename": local,
            "playFilename": local,
        }

    def _all_songs(self) -> list[dict]:
        downloaded = self._downloaded_names()
        songs = [self._normalize_record(record, downloaded) for record in self._catalog_snapshot()["records"]]
        songs.sort(key=lambda song: (song["artist"].lower(), song["album"].lower(), song["title"].lower()))
        return songs

    def _apply_text_query(self, songs: list[dict], q: str = "", **_kwargs) -> list[dict]:
        needle = str(q or "").strip().lower()
        if not needle:
            return songs
        return [
            song
            for song in songs
            if needle in song["title"].lower()
            or needle in song["artist"].lower()
            or needle in song["album"].lower()
        ]

    def query_page(self, page: int = 0, size: int = 24, sort: str = "artist", direction: str = "asc", **kwargs):
        if kwargs.get("favorites_only"):
            return [], 0
        songs = self._apply_text_query(self._all_songs(), **kwargs)
        if str(direction or "asc").lower() == "desc":
            songs = list(reversed(songs))
        total = len(songs)
        size = max(1, min(100, _safe_int(size, 24)))
        page = max(0, _safe_int(page, 0))
        offset = page * size
        return songs[offset:offset + size], total

    def query_artists(self, letter: str = "", page: int = 0, size: int = 50, **kwargs):
        if kwargs.get("favorites_only"):
            return [], 0
        songs = self._apply_text_query(self._all_songs(), **kwargs)
        by_artist: "OrderedDict[str, list[dict]]" = OrderedDict()
        for song in songs:
            by_artist.setdefault(song["artist"], []).append(song)
        artist_names = sorted(by_artist, key=str.lower)
        if letter:
            artist_names = [name for name in artist_names if name[:1].upper() == letter.upper()]
        total = len(artist_names)
        size = max(1, _safe_int(size, 50))
        page = max(0, _safe_int(page, 0))
        artists = []
        for name in artist_names[page * size:page * size + size]:
            by_album: "OrderedDict[str, list[dict]]" = OrderedDict()
            for song in by_artist[name]:
                by_album.setdefault(song["album"], []).append(song)
            albums = [{"name": album, "song_count": len(items), "songs": items} for album, items in by_album.items()]
            artists.append({
                "name": name,
                "album_count": len(albums),
                "song_count": len(by_artist[name]),
                "albums": albums,
            })
        return artists, total

    def query_stats(self, **kwargs) -> dict:
        if kwargs.get("favorites_only"):
            return {"total_songs": 0, "total_artists": 0, "letters": {}}
        songs = self._apply_text_query(self._all_songs(), **kwargs)
        letters: dict[str, int] = {}
        artists = {song["artist"] for song in songs}
        for artist in artists:
            letter = artist[:1].upper() if artist else "#"
            if not letter.isalpha():
                letter = "#"
            letters[letter] = letters.get(letter, 0) + 1
        return {"total_songs": len(songs), "total_artists": len(artists), "letters": letters}

    def describe_source(self) -> dict:
        records = self._catalog_snapshot()["records"]
        return {
            "ok": True,
            "sourceId": f"proton_{self._token}",
            "sourceName": self.label,
            "songCount": len(records),
            "capabilities": ["library.read", "song.sync"],
            "server": {"protocol": self.type},
        }

    # -- download + sync (background; mirrors google_drive.py) -----------

    def _download_label(self, song_id: str, record: dict | None = None) -> str:
        record = record or self._catalog_record(song_id)
        if not record:
            return song_id
        artist, _album, title = parse_proton_filename(record["name"])
        if artist and artist != "Unknown artist":
            return f"{artist} – {title}"
        return title or record["name"]

    def sync_song(self, song_id: str) -> dict:
        # Non-blocking: FeedBack core caps sync-song at ~250 ms; a Proton download + decrypt
        # cannot meet that. Return immediately — play now if already local, else download in the
        # background and report progress via active_downloads(); the next click plays.
        ready = self._local_ready(song_id)
        if ready:
            return ready
        with self._sync_lock:
            entry = self._downloads.get(song_id)
            already_running = bool(entry and entry.get("status") == "downloading")
            if not already_running:
                self._downloads[song_id] = {
                    "status": "downloading",
                    "title": self._download_label(song_id),
                    "at": time.monotonic(),
                }
        if not already_running:
            self._start_background_sync(song_id)
        return self._downloading_result(song_id)

    def _start_background_sync(self, song_id: str) -> None:
        threading.Thread(target=self._background_sync, args=(song_id,), daemon=True).start()

    def _background_sync(self, song_id: str) -> None:
        title = self._download_label(song_id)
        try:
            result = self._do_sync(song_id)
            entry = {"status": "ready", "title": title, "at": time.monotonic(), "result": result}
        except Exception as exc:  # noqa: BLE001 — record and allow a retry on the next click
            entry = {"status": "error", "title": title, "at": time.monotonic(),
                     "message": _public_error_message(exc)}
        with self._sync_lock:
            self._downloads[song_id] = entry

    def _downloading_result(self, song_id: str) -> dict:
        return {
            "ok": True,
            "song_id": song_id,
            "remoteSongId": song_id,
            "cached": False,
            "cacheState": "downloading",
            "message": "Downloading from Proton Drive…",
        }

    def _local_ready(self, song_id: str) -> dict | None:
        with self._sync_lock:
            entry = self._downloads.get(song_id)
        if entry and entry.get("status") == "ready":
            result = entry.get("result") or {}
            if result.get("filename"):
                return result
        record = self._catalog_record(song_id)
        if not record or not self.local_library_root or not self.local_library_root.exists():
            return None
        candidate = self.local_library_root / self._source_folder_name() / sanitize_filename(
            Path(record["name"]).name, "remote-song.feedpak"
        )
        if not candidate.is_file():
            return None
        relative = candidate.relative_to(self.local_library_root).as_posix()
        result = {
            "ok": True,
            "song_id": song_id,
            "remoteSongId": song_id,
            "cached": True,
            "cacheState": "ready",
            "filename": relative,
            "localFilename": relative,
            "local_filename": relative,
            "playFilename": relative,
            "libraryRelativePath": relative,
            "libraryImportState": "indexed",
            "playbackSource": "library-folder",
        }
        with self._sync_lock:
            self._downloads[song_id] = {
                "status": "ready", "title": self._download_label(song_id, record),
                "at": time.monotonic(), "result": result,
            }
        return result

    def active_downloads(self, max_age_seconds: float = 300.0) -> list[dict]:
        now = time.monotonic()
        items = []
        with self._sync_lock:
            for stale in [
                key for key, value in self._downloads.items()
                if value.get("status") != "downloading" and now - value.get("at", now) > max_age_seconds
            ]:
                self._downloads.pop(stale, None)
            for song_id, entry in self._downloads.items():
                item = {
                    "providerId": self.id,
                    "songId": song_id,
                    "title": entry.get("title") or song_id,
                    "status": entry.get("status") or "downloading",
                }
                if entry.get("status") == "ready":
                    item["localFilename"] = (entry.get("result") or {}).get("filename") or ""
                elif entry.get("status") == "error":
                    item["message"] = entry.get("message") or ""
                items.append(item)
        return items

    def _do_sync(self, song_id: str) -> dict:
        record = self._catalog_record(song_id)
        if not record:
            raise RuntimeError("song is no longer present in the Proton share")
        fallback_filename = sanitize_filename(record["name"], "remote-song.feedpak")
        if not fallback_filename.lower().endswith(PACKAGE_SUFFIXES):
            fallback_filename += ".feedpak"
        file_decryptor = _key_decryptor(record["nodeKey"], record["nodePassphrase"])
        revision = self._client.fetch_file_revision(song_id)
        # The content key comes from the file link (captured into the record when listing); the
        # revision only carries the blocks.
        content_key_packet = _armored_bytes(
            record.get("contentKeyPacket") or revision.get("ContentKeyPacket") or ""
        )
        if not content_key_packet:
            raise RuntimeError("Proton file is missing its content key")
        blocks = sorted((revision.get("Blocks") or []), key=lambda block: _safe_int(block.get("Index"), 0))
        if not blocks:
            raise RuntimeError("Proton file revision has no content blocks")
        target, content_hash, bytes_written = self._decrypt_blocks_to_cache(
            content_key_packet, blocks, file_decryptor, fallback_filename
        )
        self.clear_metadata_cache()
        result = {
            "ok": True,
            "song_id": song_id,
            "remoteSongId": song_id,
            "cached": True,
            "cacheState": "ready",
            "bytes": bytes_written,
        }
        result.update(self._import_into_library(target, content_hash, fallback_filename))
        return result

    def _decrypt_blocks_to_cache(
        self, content_key_packet: bytes, blocks: list[dict], file_decryptor, fallback_filename: str
    ) -> tuple[Path, str, int]:
        return _decrypt_blocks_to_file(self._client, self.cache_dir, content_key_packet, blocks,
                                       file_decryptor, fallback_filename)


def _decrypt_blocks_to_file(
    client: _ProtonShareClient, cache_dir: Path, content_key_packet: bytes, blocks: list[dict],
    file_decryptor, fallback_filename: str,
) -> tuple[Path, str, int]:
    """Download each encrypted block, decrypt it (``ContentKeyPacket`` + block = one OpenPGP
    message under the file node key), and stream the plaintext into the cache atomically."""
    pysequoia = _pysequoia()
    target = cache_dir / sanitize_filename(fallback_filename, "remote-song.feedpak")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    digest = hashlib.sha256()
    total = 0
    try:
        with tmp_path.open("wb") as handle:
            for block in blocks:
                # `URL` is the full block-download URL; `BareURL` is the host+prefix only (a
                # bare fetch of it 400s), so prefer `URL` and keep BareURL as a fallback.
                block_url = block.get("URL") or block.get("BareURL")
                if not block_url:
                    raise RuntimeError("Proton content block is missing its download URL")
                encrypted = client.download_block(block_url)
                plaintext = pysequoia.decrypt(content_key_packet + encrypted, decryptor=file_decryptor).bytes
                if plaintext is None:
                    raise RuntimeError("Proton content block decryption produced no data")
                total += len(plaintext)
                if total > MAX_PACKAGE_RESPONSE_BYTES:
                    raise RuntimeError("Proton package exceeded size limit")
                digest.update(plaintext)
                handle.write(plaintext)
        tmp_path.replace(target)
        return target, digest.hexdigest(), total
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def download_share_package(
    provider: BaseLibraryProvider, share_url: str, fallback_filename: str
) -> tuple[Path, str, int]:
    """Download the package behind a Proton public-share link into ``provider``'s cache.

    Built for other provider types that resolve a song to a Proton link (e.g. a FeedForge song
    whose uploader hosts the file on Proton Drive) — the Drive-flavored sibling is
    ``google_drive.download_drive_file``. Handles both link shapes seen in the wild:

    - a **single-file share** (``LinkType == 2``): the root link *is* the file and the bootstrap
      material carries its ``ContentKeyPacket`` directly (verified live);
    - a **folder share**: the first package-suffixed child is downloaded (the whole-folder case
      is what :class:`ProtonPublicShareProvider` is for).

    The file lands under ``fallback_filename`` (the caller's deterministic name — for FeedForge
    that keeps the ``settingsKey`` contract), never the share's own decrypted name. Requires the
    Proton extras (``bcrypt`` + ``pysequoia``); both are imported lazily, so callers should turn
    an ``ImportError`` into a clear "install requirements" message.
    """
    parsed = parse_proton_share_url(share_url)
    if not parsed:
        raise RuntimeError("not a recognizable Proton public-share URL")
    token, url_password = parsed
    if not url_password:
        raise RuntimeError("the Proton share link is missing its password (the part after '#')")
    client = _ProtonShareClient(token, url_password, provider._urlopen)
    material = client.bootstrap()
    url_passphrase = proton_srp.compute_key_password(
        url_password, base64.b64decode(material["SharePasswordSalt"])
    )
    share_passphrase = _decrypt_password(material["SharePassphrase"], url_passphrase)
    share_decryptor = _key_decryptor(material["ShareKey"], share_passphrase)
    root_passphrase = _decrypt_with_key(material["NodePassphrase"], share_decryptor)
    # LinkType 2 == file (a single-file share; verified live 2026-07-16). A folder share
    # (the provider's usual diet) lists children instead.
    if material.get("LinkType") == 2 or material.get("ContentKeyPacket"):
        link_id = str(material.get("LinkID") or "")
        file_decryptor = _key_decryptor(material["NodeKey"], root_passphrase)
        revision = client.fetch_file_revision(link_id)
        content_key = material.get("ContentKeyPacket") or revision.get("ContentKeyPacket") or ""
    else:
        root_decryptor = _key_decryptor(material["NodeKey"], root_passphrase)
        record = None
        for child in client.fetch_children(str(material.get("LinkID") or "")):
            candidate = _decrypt_child_record(child, root_decryptor)
            if candidate:
                record = candidate
                break
        if not record:
            raise RuntimeError("the Proton share contains no package file")
        file_decryptor = _key_decryptor(record["nodeKey"], record["nodePassphrase"])
        revision = client.fetch_file_revision(record["linkId"])
        content_key = record.get("contentKeyPacket") or revision.get("ContentKeyPacket") or ""
    if not content_key:
        raise RuntimeError("Proton file is missing its content key")
    blocks = sorted((revision.get("Blocks") or []), key=lambda block: _safe_int(block.get("Index"), 0))
    if not blocks:
        raise RuntimeError("Proton file revision has no content blocks")
    return _decrypt_blocks_to_file(
        client, provider.cache_dir, _armored_bytes(content_key), blocks, file_decryptor, fallback_filename
    )


def _share_credentials(source: dict) -> tuple[str, str]:
    """Resolve ``(token, url_password)`` from a stored source or freshly-pasted link."""
    parsed = parse_proton_share_url(source.get("baseUrl") or "")
    token = str(source.get("shareToken") or (parsed[0] if parsed else "")).strip()
    password = str(source.get("urlPassword") or (parsed[1] if parsed else "")).strip()
    if not token:
        raise ValueError("not a recognizable Proton public-share URL")
    if not password:
        raise ValueError("the Proton share password (the part after '#') is required")
    return token, password
