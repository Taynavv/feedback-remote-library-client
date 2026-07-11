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
# The catalog is a few thousand songs (~60-100 pages of 25) and grows over time; this is a
# backstop against a runaway loop, set well above the real size — not an expected limit.
_MAX_LIBRARY_PAGES = 400
# FeedForge serves a fixed 25 songs per /library page (verified live). A module constant so
# tests can monkeypatch a smaller page size to exercise multi-page mapping cheaply.
_PAGE_SIZE = 25
# Map FeedBack's sort vocabulary to FeedForge's server-side ?sort= values (verified: the default
# order and ?sort=artist both paginate stably + non-overlapping). An unmapped sort falls through
# to FeedForge's default stable order rather than risking an unstable one that breaks paging.
_SORT_MAP = {
    "artist": "artist",
    "title": "title",
    "newest": "newest",
    "updated": "updated",
    "downloads": "downloads",
    "date": "newest",
    "recent": "newest",
}

# ---- scrape selectors (verified live against feedforge.org 2026-07-10; adjust if the markup
# drifts). The catalog is a <table>: each song is a <tr> with a `song-title` anchor to
# /songs/{id} and typed `linked-cell` facet anchors whose hrefs carry the value
# (?artist= / ?album= / ?tuning=). Art is a `cover-thumb` <img> (its /feedpak-covers/{coverId}
# id differs from the song id). Keying on the href facet — not column position — keeps the parse
# robust to the table's columns being reordered.
_ROW_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.S | re.I)
_TITLE_RE = re.compile(r'class="song-title"[^>]*?href="/songs/([A-Za-z0-9_-]+)"[^>]*>(.*?)</a>', re.S | re.I)
# Facet cells are <a class="linked-cell" href="/library?…&facet=Value">Value</a>. The facet
# param can sit anywhere in the query (the order varies — e.g. `?page=1&artist=…`), so match it
# positionally-independent (`[^"]*\bfacet=`) rather than assuming it follows `?`.
_ARTIST_RE = re.compile(r'href="/library\?[^"]*\bartist=[^"]*"[^>]*>(.*?)</a>', re.S | re.I)
_ALBUM_RE = re.compile(r'href="/library\?[^"]*\balbum=[^"]*"[^>]*>(.*?)</a>', re.S | re.I)
_TUNING_RE = re.compile(r'href="/library\?[^"]*\btuning=[^"]*"[^>]*>(.*?)</a>', re.S | re.I)
_YEAR_RE = re.compile(r'href="/library\?[^"]*\byear=[^"]*"[^>]*>(.*?)</a>', re.S | re.I)
# Duration is a plain <td>M:SS</td> (not a link); take the last M:SS in the row (it sits near the
# end, after Year), so an earlier numeric cell can't be mistaken for it.
_DURATION_RE = re.compile(r"<td[^>]*>\s*(\d{1,2}:\d{2})\s*</td>", re.I)
_ART_RE = re.compile(r'class="cover-thumb"[^>]*>\s*<img\b[^>]*\bsrc="([^"]+)"', re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")


def _text(raw: str) -> str:
    """Strip tags + unescape HTML entities + collapse whitespace."""
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub("", raw or ""))).strip()


def _first(pattern: re.Pattern, block: str) -> str:
    match = pattern.search(block)
    return _text(match.group(1)) if match else ""


def _duration_seconds(text: str) -> int | None:
    match = re.match(r"^\s*(\d{1,2}):([0-5]?\d)\s*$", str(text or ""))
    return int(match.group(1)) * 60 + int(match.group(2)) if match else None


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


def _direct_download_url(url: str) -> str:
    """Coerce a resolved share link to its direct-download form. FeedForge indexes external
    hosts; Dropbox share links serve an HTML *preview* at ``?dl=0`` and stream the file only at
    ``?dl=1``, so force that. Google Drive is handled separately (confirm-token flow); everything
    else is returned unchanged."""
    parsed = parse.urlparse(str(url or ""))
    host = (parsed.hostname or "").lower()
    if host == "dropbox.com" or host.endswith(".dropbox.com"):
        query = [(key, value) for key, value in parse.parse_qsl(parsed.query, keep_blank_values=True) if key != "dl"]
        query.append(("dl", "1"))
        return parse.urlunparse(parsed._replace(query=parse.urlencode(query)))
    return url


def _filename_from_url(url: str, song_id: str) -> str:
    """Best-effort package filename from a resolved download URL (it usually ends in
    ``…/Artist-Title.feedpak?…``), falling back to the song id. Used when a song was synced
    without being browsed (no cached card) so it still imports under a meaningful, stable name."""
    name = parse.unquote(Path(parse.urlparse(str(url or "")).path).name)
    if name and name.lower().endswith(PACKAGE_SUFFIXES):
        return sanitize_filename(name, "remote-song.feedpak")
    return sanitize_filename(song_id, "remote-song") + ".feedpak"


def parse_library_html(text: str) -> list[dict]:
    """Parse the server-rendered ``/library`` table into a list of card dicts.

    Best-effort and tolerant: a row without a ``song-title`` anchor (e.g. the header row) is
    skipped; any missing facet degrades to an empty value rather than raising. Kept a module
    function (not a method) so tests can exercise the parser directly on synthetic HTML.
    """
    cards: list[dict] = []
    seen: set[str] = set()
    for row in _ROW_RE.findall(text or ""):
        title_match = _TITLE_RE.search(row)
        if not title_match:
            continue
        song_id = title_match.group(1)
        if song_id in seen:
            continue
        seen.add(song_id)
        art_match = _ART_RE.search(row)
        durations = _DURATION_RE.findall(row)
        cards.append({
            "song_id": song_id,
            "title": _text(title_match.group(2)) or song_id,
            "artist": _first(_ARTIST_RE, row) or "Unknown artist",
            "album": _first(_ALBUM_RE, row),
            "tuning": _first(_TUNING_RE, row),
            "year": _safe_int(_first(_YEAR_RE, row), 0),
            "duration": _duration_seconds(durations[-1]) if durations else None,
            "art": html.unescape(art_match.group(1)) if art_match else "",
        })
    return cards


class FeedForgeProvider(BaseLibraryProvider):
    """Library provider backed by a FeedForge account (username/password login)."""

    type = "feedforge.v1"
    # The full-catalog scrape spans many pages (~25s for a few thousand songs). Cache it far
    # longer than the base 5 min so, with the provider reused across status polls, there is
    # only ~one scrape per this interval — repeated rapid scrapes trip Cloudflare rate-limiting.
    metadata_cache_ttl_seconds = 900
    # A page can transiently come back empty under that rate-limiting; retry an empty page a few
    # times with backoff before treating it as the end of the catalog, so a blip doesn't
    # silently truncate the scrape. (Tests set the backoff to 0.)
    empty_page_retries = 3
    empty_page_backoff_seconds = 0.6

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
        # Cards seen while browsing, keyed by song id. Lazy browsing never holds the whole
        # catalog, so sync/art look a song up here (populated by query_page) instead of scanning.
        self._card_cache: dict[str, dict] = {}
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

    # -- catalog (lazy: only the viewed page is fetched; the full catalog is never scraped) ----

    def _query_params(self, q: str = "", sort: str = "") -> dict:
        """Server-side query params for a /library request: full-text ``?q=`` + a mapped ``?sort=``."""
        params: dict[str, str] = {}
        q = str(q or "").strip()
        if q:
            params["q"] = q[:200]
        sort_value = _SORT_MAP.get(str(sort or "").lower())
        if sort_value:
            params["sort"] = sort_value
        return params

    def _fetch_page_cards(self, ff_page: int, params: dict | None = None, attempts: int | None = None) -> list[dict]:
        """One FeedForge library page's cards for a given query. During *browsing* an empty/errored
        result is retried a few times with backoff so a transient empty page (a Cloudflare
        rate-limit blip) isn't mistaken for the end; the catalog-size probe passes ``attempts=1``
        since there empties are expected (past the end) and retrying them is pure latency. Returns
        ``[]`` only when the page stays empty. A lapsed session still raises."""
        attempts = self.empty_page_retries if attempts is None else max(1, attempts)
        path = "/library?" + parse.urlencode({"page": ff_page, **(params or {})})
        for attempt in range(attempts):
            try:
                page_cards = parse_library_html(self._authed_html(path))
            except AuthRequiredError:
                raise  # a lapsed/invalid session must surface, not look like an empty catalog
            except Exception:
                page_cards = []
            if page_cards:
                return page_cards
            if attempt + 1 < attempts and self.empty_page_backoff_seconds:
                time.sleep(self.empty_page_backoff_seconds * (attempt + 1))
        return []

    def _fetch_ff_page(self, ff_page: int, params: dict, attempts: int | None = None) -> list[dict]:
        """Fetch one FeedForge page and remember its cards by id, so sync/art can resolve a browsed
        song without re-fetching. A non-empty result is cached; an empty one is not (it may be a
        transient blip or an end-of-catalog probe — caching it could poison a later browse)."""
        cache_key = self._metadata_cache_key(f"page:{ff_page}", params)
        cached = self._cache_get(cache_key)
        if cached is not None:
            cards = list(cached.get("cards") or [])
        else:
            cards = self._fetch_page_cards(ff_page, params, attempts=attempts)
            if cards:
                self._cache_put(cache_key, {"cards": cards})
        for card in cards:
            self._card_cache[card["song_id"]] = card
        return cards

    def _catalog_total(self) -> int:
        key = self._metadata_cache_key("catalog_total", {})
        cached = self._cache_get(key)
        if cached is not None:
            return int(cached.get("total") or 0)
        total = self._compute_catalog_total()
        self._cache_put(key, {"total": total})
        return total

    def _compute_catalog_total(self) -> int:
        """Approximate the catalog size without a full scrape. Pages are ``_PAGE_SIZE``, contiguous
        and stable, so the size is ``(last_non_empty_page - 1) * _PAGE_SIZE + len(last_page)``. Find
        the last non-empty page by an exponential probe + binary search — a handful of single-page
        fetches (cached), not ~80. Under heavy rate-limiting a probed page can come back empty and
        undercount; that only skews the *displayed* total, never what query_page can fetch."""
        def count(ff_page: int) -> int:
            # attempts=1: empties here are expected (probing past the end), not blips to retry.
            return len(self._fetch_ff_page(ff_page, {}, attempts=1))

        if count(1) == 0:
            return 0
        low, high = 1, 2
        while high <= _MAX_LIBRARY_PAGES and count(high) > 0:
            low, high = high, high * 2
        high = min(high, _MAX_LIBRARY_PAGES + 1)
        while low + 1 < high:  # largest page with content in [low, high)
            mid = (low + high) // 2
            if count(mid) > 0:
                low = mid
            else:
                high = mid
        return (low - 1) * _PAGE_SIZE + count(low)

    def _card_by_id(self, song_id: str) -> dict | None:
        # From the browse cache only — lazy browsing never holds the whole catalog.
        return self._card_cache.get(song_id)

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
            "album": card.get("album") or "",
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

    def query_page(self, page: int = 0, size: int = 24, sort: str = "artist",
                   direction: str = "asc", **kwargs):
        # Lazy: fetch only the FeedForge page(s) covering the requested window, with server-side
        # search (?q=) + sort. FeedForge is _PAGE_SIZE/page and stable, so map core's (page, size)
        # onto that grid and slice.
        if kwargs.get("favorites_only"):
            return [], 0
        size = max(1, min(100, _safe_int(size, 24)))
        page = max(0, _safe_int(page, 0))
        params = self._query_params(q=kwargs.get("q", ""), sort=sort)
        offset = page * size
        first_ff = offset // _PAGE_SIZE + 1
        last_ff = (offset + size - 1) // _PAGE_SIZE + 1
        window: list[dict] = []
        ended = False
        for ff_page in range(first_ff, last_ff + 1):
            cards = self._fetch_ff_page(ff_page, params)
            window.extend(cards)
            if len(cards) < _PAGE_SIZE:
                ended = True  # a short page is the last page of results — nothing beyond it
                break
        local_offset = offset - (first_ff - 1) * _PAGE_SIZE
        page_cards = window[local_offset:local_offset + size]
        downloaded = self._downloaded_names()
        songs = [self._normalize_card(card, downloaded) for card in page_cards]
        # There is no server total, and computing one requires probing (see _catalog_total, used
        # only for the source-card count on add/status). Keep browsing truly lazy — fetch only the
        # window — and report an *at-least* total: it grows while full pages keep coming and settles
        # to the exact count at the last (short) page. "more" == the window ran to a full final page
        # or has rows past this slice, so more results follow.
        more = not ended or len(window) > local_offset + size
        total = offset + len(page_cards) + (_PAGE_SIZE if more else 0)
        return songs, total

    def query_artists(self, letter: str = "", page: int = 0, size: int = 50, **kwargs):
        # Browse-by-artist needs the whole catalog (FeedForge exposes no artist list or total),
        # which the lazy design deliberately never scrapes. Degrade to empty: the song list
        # (query_page) + server-side search is the supported browse path for FeedForge.
        return [], 0

    def query_stats(self, **kwargs) -> dict:
        # Only the total is cheap (binary-searched); the A–Z letter rail + artist count need the
        # whole catalog, so they degrade to empty. A filtered/search view has no known total.
        if kwargs.get("favorites_only") or str(kwargs.get("q") or "").strip():
            return {"total_songs": 0, "total_artists": 0, "letters": {}}
        return {"total_songs": self._catalog_total(), "total_artists": 0, "letters": {}}

    def describe_source(self) -> dict:
        return {
            "ok": True,
            "sourceId": f"feedforge_{self._account_key}",
            "sourceName": self.label,
            "songCount": self._catalog_total(),
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
        url = self._resolve_download_url(song_id)
        # Name the local file deterministically: from the browsed card if we have it, else from
        # the resolved URL (…/Artist-Title.feedpak). Keeps settingsKey stable (see _remote_filename).
        card = self._card_by_id(song_id)
        remote_name = self._remote_filename(card) if card else _filename_from_url(url, song_id)
        file_id = drive_file_id_from_url(url)
        if file_id:
            # The common case: FeedForge points at Google Drive — reuse the Drive download
            # path (redirect + large-file confirm-token flow) from google_drive.py.
            target, content_hash, bytes_read, _headers = download_drive_file(
                self, file_id, remote_name, self._download_headers()
            )
        else:
            # Non-Drive host (e.g. Dropbox) — coerce to a direct-download URL, then stream.
            target, content_hash, bytes_read, _headers = self._download_url_to_cache(
                _direct_download_url(url), remote_name, self._download_headers()
            )
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
