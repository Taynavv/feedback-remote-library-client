# SPDX-License-Identifier: AGPL-3.0-or-later
"""FeedForge community-catalog library provider (``feedforge.v1``).

FeedForge (``feedforge.org``) is a closed, community FeedPak catalog: a Next.js + NextAuth
app that *indexes externally-hosted* packages (it hosts nothing itself). This provider
registers a FeedForge account as a FeedBack library provider by

  1. driving the NextAuth **credentials** login (username + password) and holding the
     resulting session cookie (auto-relogin when it lapses),
  2. scraping the server-rendered ``/library`` catalog into a cached song list, and
  3. resolving each song's external download URL on demand
     (``POST /api/songs/{id}/download`` -> ``{ok, url}``) and streaming it into the local
     cache — typically a Google Drive link, so the Drive download path in
     :mod:`remote_library_client.google_drive` is reused.

Only the **username/password** login is implemented. Discord-authenticated accounts are not
supported by this plugin-only build: a captured Discord session would need an interactive
browser step that FeedBack core does not currently expose to plugins.

Auth, listing, and download were reverse-engineered against the live site (the FeedBack
creators greenlit the integration). The **scrape markup is the fragile part** — a vibe-coded
app can change it unannounced — so the card selectors are constants near the top; adjust them
if the live HTML drifts (this is the same class of fragility as the Google folder scraper).
No dependency is added: like the Google Drive type this is pure stdlib. No song content ever
ships here; tests use synthetic fixtures only.
"""
from __future__ import annotations

import html
import json
import re
import threading
import time
from collections import OrderedDict
from pathlib import Path
from urllib import error, parse, request

from fastapi.responses import Response

from remote_library_client.google_drive import (
    download_drive_file,
    drive_file_id_from_url,
)
from remote_library_client.provider import (
    MAX_BINARY_RESPONSE_BYTES,
    MAX_JSON_RESPONSE_BYTES,
    AuthRequiredError,
    BaseLibraryProvider,
    LibraryImporter,
    _public_error_message,
    _read_limited,
    _remote_error,
    _safe_int,
    playback_settings_key,
    provider_id_for_source,
    sanitize_filename,
)

FEEDFORGE_DEFAULT_BASE_URL = "https://feedforge.org"
FEEDFORGE_HOST = "feedforge.org"
PACKAGE_SUFFIXES = (".feedpak", ".sloppak", ".psarc", ".zip")
# A real browser User-Agent: feedforge.org sits behind Cloudflare, which is friendlier to a
# browser-ish UA than to none.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
# NextAuth's session cookie (JWT strategy — forced by the credentials provider). Its presence
# in the jar is our "are we logged in?" signal; it self-renews as the server re-issues it.
_SESSION_COOKIE = "__Secure-next-auth.session-token"
# The site is ~5-6 catalog pages today; cap pagination so a markup change can't loop forever.
_MAX_LIBRARY_PAGES = 60

# ---- scrape selectors (verified against the live site; adjust if the markup drifts) --------
# Each browsable song is an <article class="song-card"> carrying an id (a cuid) in a
# href="/songs/{id}", a `song-title` anchor, an artist <p>, a `song-card-meta` row of spans
# (tuning, year, duration), and an art <img>.
_CARD_RE = re.compile(r'<article\b[^>]*\bclass="[^"]*\bsong-card\b[^"]*"[^>]*>(.*?)</article>', re.S | re.I)
_SONG_ID_RE = re.compile(r'href="/songs/([A-Za-z0-9_-]+)"')
_TITLE_RE = re.compile(r'\bclass="[^"]*\bsong-title\b[^"]*"[^>]*>(.*?)</a>', re.S | re.I)
_ARTIST_RE = re.compile(r"<p\b[^>]*>(.*?)</p>", re.S | re.I)
_META_BLOCK_RE = re.compile(r'\bclass="[^"]*\bsong-card-meta\b[^"]*"[^>]*>(.*?)</div>', re.S | re.I)
_SPAN_RE = re.compile(r"<span\b[^>]*>(.*?)</span>", re.S | re.I)
_ART_RE = re.compile(r'<img\b[^>]*\bsrc="([^"]+)"', re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_DURATION_RE = re.compile(r"^\s*(\d{1,2}):([0-5]?\d)\s*$")


def _text(raw: str) -> str:
    """Strip tags + unescape HTML entities + collapse whitespace."""
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub("", raw or ""))).strip()


def _first(pattern: re.Pattern, block: str) -> str:
    match = pattern.search(block)
    return _text(match.group(1)) if match else ""


def _duration_seconds(text: str) -> int | None:
    match = _DURATION_RE.match(str(text or ""))
    if match:
        return int(match.group(1)) * 60 + int(match.group(2))
    value = _safe_int(text, -1)
    return value if value >= 0 else None


def is_feedforge_url(url: str) -> bool:
    """True when the input points at a FeedForge host (``feedforge.org`` or a subdomain)."""
    host = (parse.urlparse(str(url or "").strip()).hostname or "").lower()
    return host == FEEDFORGE_HOST or host.endswith("." + FEEDFORGE_HOST)


def normalize_feedforge_base_url(url: str) -> str:
    """Reduce user input to a bare ``scheme://host[:port]`` origin, defaulting to
    ``https://feedforge.org`` when nothing usable is supplied. A path/query is dropped — the
    provider owns the API/scrape paths."""
    raw = str(url or "").strip()
    if not raw:
        return FEEDFORGE_DEFAULT_BASE_URL
    if "://" not in raw:
        raw = "https://" + raw.lstrip("/")
    parsed = parse.urlparse(raw)
    host = parsed.hostname
    # Reject scheme mismatches and anything that isn't a plausible host (e.g. free text a user
    # typed by mistake) rather than emitting a broken "https://not a url" — fall back to the
    # default. Allows domains, IPv4, and IPv6 (colons).
    if parsed.scheme not in {"http", "https"} or not host or not re.match(r"^[A-Za-z0-9._:\-]+$", host):
        return FEEDFORGE_DEFAULT_BASE_URL
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{host}{port}"


def parse_library_html(text: str) -> list[dict]:
    """Parse the server-rendered ``/library`` HTML into a list of card dicts.

    Best-effort and tolerant: a card missing a song id is skipped; any missing field degrades
    to an empty/None value rather than raising. Kept a module function (not a method) so tests
    can exercise the parser directly on synthetic HTML.
    """
    cards: list[dict] = []
    seen: set[str] = set()
    for block in _CARD_RE.findall(text or ""):
        id_match = _SONG_ID_RE.search(block)
        if not id_match:
            continue
        song_id = id_match.group(1)
        if song_id in seen:
            continue
        seen.add(song_id)
        meta_block = _META_BLOCK_RE.search(block)
        spans = [_text(span) for span in _SPAN_RE.findall(meta_block.group(1))] if meta_block else []
        art_match = _ART_RE.search(block)
        cards.append({
            "song_id": song_id,
            "title": _first(_TITLE_RE, block) or song_id,
            "artist": _first(_ARTIST_RE, block) or "Unknown artist",
            "tuning": spans[0] if len(spans) > 0 else "",
            "year": _safe_int(spans[1], 0) if len(spans) > 1 else 0,
            "duration": _duration_seconds(spans[2]) if len(spans) > 2 else None,
            "art": html.unescape(art_match.group(1)) if art_match else "",
        })
    return cards


class FeedForgeProvider(BaseLibraryProvider):
    """Library provider backed by a FeedForge account (username/password login)."""

    type = "feedforge.v1"

    def __init__(
        self,
        source: dict,
        cache_dir: Path,
        local_library_root: Path | None = None,
        library_importer: LibraryImporter | None = None,
        nam_config_dir: Path | None = None,
    ) -> None:
        base_url = normalize_feedforge_base_url(source.get("baseUrl") or "")
        host = parse.urlparse(base_url).hostname or FEEDFORGE_HOST
        self.username = str(source.get("username") or "").strip()
        self.password = str(source.get("password") or "")
        self._account_key = sanitize_filename(f"{host}_{self.username}" if self.username else host, "feedforge")
        provider_id = str(
            source.get("providerId")
            or provider_id_for_source(f"feedforge_{self._account_key}", base_url, prefix="feedforge")
        )
        super().__init__(
            {**source, "providerId": provider_id},
            cache_dir,
            origin_host=host,
            allow_unsafe_redirects=bool(source.get("allowUnsafeRedirects")),
            local_library_root=local_library_root,
            library_importer=library_importer,
            nam_config_dir=nam_config_dir,
        )
        self.base_url = base_url
        default_label = f"FeedForge ({self.username})" if self.username else "FeedForge"
        self.label = str(source.get("label") or source.get("sourceName") or default_label)
        # Serialize logins so N concurrent requests hitting an expired session re-auth once.
        self._login_lock = threading.Lock()
        # Background-sync state (FeedBack core caps sync-song at ~250ms — far too short for an
        # internet download — so downloads run off-thread; see sync_song). song_id -> entry.
        self._sync_lock = threading.Lock()
        self._downloads: dict[str, dict] = {}

    # -- HTTP + auth -----------------------------------------------------

    def _headers(self, accept: str = "text/html,application/xhtml+xml") -> dict:
        return {"User-Agent": _USER_AGENT, "Accept": accept}

    def _download_headers(self) -> dict:
        # For the external CDN fetch (typically Google Drive): a browser UA only. The
        # per-provider cookie jar won't send feedforge.org cookies to a different host.
        return {"User-Agent": _USER_AGENT}

    def _has_session(self) -> bool:
        return any(cookie.name == _SESSION_COOKIE for cookie in self._cookies)

    def _get_json(self, path: str, timeout: float = 20) -> dict:
        req = request.Request(f"{self.base_url}{path}", headers=self._headers("application/json"))
        try:
            with self._urlopen(req, timeout=timeout) as response:
                raw = _read_limited(response, MAX_JSON_RESPONSE_BYTES).decode("utf-8", errors="replace")
        except error.HTTPError as exc:
            raise _remote_error(exc) from exc
        try:
            return json.loads(raw or "{}")
        except json.JSONDecodeError:
            return {}

    def _login(self) -> None:
        """Drive the NextAuth credentials login, populating the session cookie in the jar.

        ``GET /api/auth/csrf`` (sets + returns the CSRF token) then a form-POST to
        ``/api/auth/callback/credentials``; success is confirmed by ``/api/auth/session``
        returning a ``user`` (bad credentials still answer 200 but establish no session).
        """
        if not self.username or not self.password:
            raise AuthRequiredError("FeedForge requires a username and password")
        self._cookies.clear()  # drop any stale/half session before a fresh handshake
        csrf = str(self._get_json("/api/auth/csrf").get("csrfToken") or "")
        if not csrf:
            raise RuntimeError("FeedForge did not return a CSRF token")
        body = parse.urlencode({
            "csrfToken": csrf,
            "username": self.username,
            "password": self.password,
            "callbackUrl": self.base_url,
            "json": "true",
        }).encode("utf-8")
        req = request.Request(
            f"{self.base_url}/api/auth/callback/credentials",
            data=body,
            headers={
                **self._headers("application/json"),
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Requested-With": "fetch",
            },
        )
        try:
            with self._urlopen(req, timeout=20) as response:
                _read_limited(response, MAX_JSON_RESPONSE_BYTES)  # drain; the jar captures Set-Cookie
        except error.HTTPError as exc:
            raise _remote_error(exc) from exc
        session = self._get_json("/api/auth/session")
        if not (isinstance(session, dict) and session.get("user")):
            raise AuthRequiredError("FeedForge rejected the username or password")

    def _ensure_session(self) -> None:
        if self._has_session():
            return
        with self._login_lock:
            if not self._has_session():
                self._login()

    def _get_html(self, url: str, timeout: float = 30) -> tuple[str, str]:
        req = request.Request(url, headers=self._headers())
        try:
            with self._urlopen(req, timeout=timeout) as response:
                text = _read_limited(response, MAX_BINARY_RESPONSE_BYTES).decode("utf-8", errors="replace")
                return text, response.geturl()
        except error.HTTPError as exc:
            raise _remote_error(exc) from exc

    @staticmethod
    def _looks_logged_out(final_url: str, text: str) -> bool:
        # An expired session redirects (top-level) to /login; the opener follows it, so the
        # final URL is the tell. Fall back to a content check for a login form with no cards.
        if "/login" in (final_url or ""):
            return True
        return 'name="password"' in (text or "") and "song-card" not in (text or "")

    def _authed_html(self, path: str) -> str:
        """Fetch an authenticated page, re-logging-in once if the session has lapsed."""
        self._ensure_session()
        url = f"{self.base_url}{path}"
        text, final_url = self._get_html(url)
        if self._looks_logged_out(final_url, text):
            with self._login_lock:
                self._login()
            text, final_url = self._get_html(url)
            if self._looks_logged_out(final_url, text):
                raise AuthRequiredError("FeedForge session could not be established")
        return text

    # -- catalog ---------------------------------------------------------

    def _fetch_all_cards(self) -> list[dict]:
        cards: list[dict] = []
        seen: set[str] = set()
        for page in range(1, _MAX_LIBRARY_PAGES + 1):
            try:
                page_cards = parse_library_html(self._authed_html(f"/library?page={page}"))
            except AuthRequiredError:
                raise  # a lapsed/invalid session must surface, not look like an empty catalog
            except Exception:
                # A page past the end may 404/err (we don't know the out-of-range behavior);
                # once we already have songs, treat that as the end rather than failing.
                if cards:
                    break
                raise
            fresh = [card for card in page_cards if card["song_id"] not in seen]
            for card in fresh:
                seen.add(card["song_id"])
            cards.extend(fresh)
            # Stop at an empty page, or one that added nothing new (the site clamps an
            # out-of-range ?page back to the last page, which would otherwise loop).
            if not page_cards or not fresh:
                break
        return cards

    def _entries(self) -> list[dict]:
        key = self._metadata_cache_key("catalog", {"base": self.base_url, "user": self.username})
        cached = self._cache_get(key)
        if cached is not None:
            return list(cached.get("cards") or [])
        cards = self._fetch_all_cards()
        self._cache_put(key, {"cards": cards})
        return cards

    def _card_by_id(self, song_id: str) -> dict | None:
        return next((card for card in self._entries() if card["song_id"] == song_id), None)

    # -- normalization + querying ---------------------------------------

    def _remote_filename(self, card: dict) -> str:
        """Deterministic local filename for a card: ``Artist - Title.feedpak``.

        We import under this name (not the CDN's Content-Disposition) so the browse-time
        ``settingsKey`` — derived from the same name — matches core's key for the imported
        file (the client<->core playback-settings-key contract)."""
        artist = card.get("artist") or "Unknown artist"
        title = card.get("title") or card.get("song_id") or "song"
        return sanitize_filename(f"{artist} - {title}.feedpak", "remote-song.feedpak")

    def _downloaded_names(self) -> frozenset[str]:
        if not self.local_library_root or not self.local_library_root.exists():
            return frozenset()
        folder = self.local_library_root / self._source_folder_name()
        try:
            return frozenset(item.name for item in folder.iterdir() if item.is_file())
        except OSError:
            return frozenset()

    def _normalize_card(self, card: dict, downloaded: frozenset[str] = frozenset()) -> dict:
        song_id = card["song_id"]
        remote_name = self._remote_filename(card)
        local = f"{self._source_folder_name()}/{remote_name}" if remote_name in downloaded else ""
        year = card.get("year") or None
        return {
            "filename": local or song_id,
            "song_id": song_id,
            "remote_id": song_id,
            "remoteSongId": song_id,
            "remoteFilename": remote_name,
            "libraryProviderId": self.id,
            "provider": self.id,
            "sourceId": self.source.get("sourceId") or f"feedforge_{self._account_key}",
            "sourceName": self.label,
            "title": card.get("title") or song_id,
            "artist": card.get("artist") or "Unknown artist",
            "album": "",
            "year": year,
            "duration": card.get("duration"),
            "format": "sloppak",
            "packageForm": "sloppak-zip",
            "syncSupport": "syncable",
            "status": "remote-only",
            "capabilities": ["package-download"],
            "settingsKey": playback_settings_key(remote_name),
            "arrangements": [],
            "has_lyrics": False,
            "hasLyrics": False,
            "stem_count": 0,
            "stemCount": 0,
            "stem_ids": [],
            "stemIds": [],
            "tuning": card.get("tuning") or "",
            "tuning_name": card.get("tuning") or "",
            "sizeBytes": 0,
            "localFilename": local,
            "local_filename": local,
            "playFilename": local,
        }

    def _all_songs(self) -> list[dict]:
        downloaded = self._downloaded_names()
        songs = [self._normalize_card(card, downloaded) for card in self._entries()]
        songs.sort(key=lambda song: (song["artist"].lower(), song["title"].lower()))
        return songs

    def _apply_text_query(self, songs: list[dict], q: str = "", **_kwargs) -> list[dict]:
        needle = str(q or "").strip().lower()
        if not needle:
            return songs
        return [
            song for song in songs
            if needle in song["title"].lower()
            or needle in song["artist"].lower()
            or needle in (song["tuning"] or "").lower()
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
            songs_for_artist = by_artist[name]
            artists.append({
                "name": name,
                "album_count": 1,
                "song_count": len(songs_for_artist),
                "albums": [{"name": "", "song_count": len(songs_for_artist), "songs": songs_for_artist}],
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
        cards = self._entries()
        return {
            "ok": True,
            "sourceId": f"feedforge_{self._account_key}",
            "sourceName": self.label,
            "songCount": len(cards),
            "capabilities": ["library.read", "song.sync"],
            "server": {"protocol": self.type},
        }

    # -- artwork ---------------------------------------------------------

    def get_art(self, song_id: str):
        # Best-effort: FeedForge cards carry an art URL. Fetch + cache it through the session;
        # any failure degrades to the base "no art" default rather than erroring the card.
        cached = self._read_cached_art(song_id)
        if cached:
            content, media_type = cached
            return Response(content=content, media_type=media_type, headers={"Cache-Control": "public, max-age=86400"})
        card = self._card_by_id(song_id)
        art_url = (card or {}).get("art") or ""
        if not art_url:
            return None
        try:
            self._ensure_session()
            art_full_url = parse.urljoin(self.base_url + "/", art_url)
            content, media_type, _headers = self._open_bytes(art_full_url, self._headers())
        except Exception:
            return None
        if not content:
            return None
        self._write_cached_art(song_id, content, media_type)
        return Response(content=content, media_type=media_type, headers={"Cache-Control": "public, max-age=86400"})

    # -- download + sync -------------------------------------------------

    def _post_download(self, song_id: str) -> dict:
        """``POST /api/songs/{id}/download`` -> ``{ok, url}`` (the external link), re-logging-in
        once on a 401 / login redirect. The app route authorizes on the session cookie +
        ``X-Requested-With`` header only (no CSRF token)."""
        self._ensure_session()
        endpoint = f"{self.base_url}/api/songs/{parse.quote(song_id)}/download"
        for attempt in range(2):
            req = request.Request(
                endpoint,
                data=b"",  # empty body -> POST; the real client sends no body
                headers={**self._headers("application/json"), "X-Requested-With": "fetch"},
            )
            try:
                with self._urlopen(req, timeout=30) as response:
                    final_url = response.geturl()
                    raw = _read_limited(response, MAX_JSON_RESPONSE_BYTES).decode("utf-8", errors="replace")
            except error.HTTPError as exc:
                if exc.code == 401 and attempt == 0:
                    with self._login_lock:
                        self._login()
                    continue
                raise _remote_error(exc) from exc
            if "/login" in final_url and attempt == 0:
                with self._login_lock:
                    self._login()
                continue
            try:
                return json.loads(raw or "{}")
            except json.JSONDecodeError:
                return {}
        return {}

    def _resolve_download_url(self, song_id: str) -> str:
        payload = self._post_download(song_id)
        url = str(payload.get("url") or "")
        if not payload.get("ok") or not url:
            raise RuntimeError("FeedForge did not return a download link for this song")
        return url

    def _do_sync(self, song_id: str) -> dict:
        card = self._card_by_id(song_id)
        remote_name = self._remote_filename(card) if card else (sanitize_filename(song_id, "remote-song") + ".feedpak")
        url = self._resolve_download_url(song_id)
        file_id = drive_file_id_from_url(url)
        if file_id:
            # The common case: FeedForge points at Google Drive — reuse the Drive download
            # path (redirect + large-file confirm-token flow) from google_drive.py.
            target, content_hash, bytes_read, _headers = download_drive_file(
                self, file_id, remote_name, self._download_headers()
            )
        else:
            target, content_hash, bytes_read, _headers = self._download_url_to_cache(
                url, remote_name, self._download_headers()
            )
        self.clear_metadata_cache()
        result = {
            "ok": True,
            "song_id": song_id,
            "remoteSongId": song_id,
            "cached": True,
            "cacheState": "ready",
            "bytes": bytes_read,
        }
        # Import under the deterministic name (see _remote_filename) to keep settingsKey stable.
        result.update(self._import_into_library(target, content_hash, remote_name))
        return result

    def _download_label(self, song_id: str, card: dict | None = None) -> str:
        card = card or self._card_by_id(song_id)
        if not card:
            return song_id
        artist = card.get("artist") or ""
        title = card.get("title") or song_id
        return f"{artist} – {title}" if artist and artist != "Unknown artist" else title

    def sync_song(self, song_id: str) -> dict:
        # Non-blocking by necessity (core's ~250ms sync budget can't cover an internet
        # download): play now if already local, else start a background download and report
        # "downloading". The screen polls active_downloads(); the next click plays it.
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
            "message": "Downloading from FeedForge…",
        }

    def _local_ready(self, song_id: str) -> dict | None:
        with self._sync_lock:
            entry = self._downloads.get(song_id)
        if entry and entry.get("status") == "ready":
            result = entry.get("result") or {}
            if result.get("filename"):
                return result
        card = self._card_by_id(song_id)
        if not card or not self.local_library_root or not self.local_library_root.exists():
            return None
        candidate = self.local_library_root / self._source_folder_name() / self._remote_filename(card)
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
                "status": "ready", "title": self._download_label(song_id, card),
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
