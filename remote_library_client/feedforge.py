# SPDX-License-Identifier: AGPL-3.0-or-later
"""FeedForge community-catalog library provider (``feedforge.v1``).

FeedForge (``feedforge.org``) is a closed, community FeedPak catalog: a Next.js app that
*indexes externally-hosted* packages (it hosts nothing itself). This provider registers a
FeedForge account as a FeedBack library provider through FeedForge's **documented v1 plugin
API** (see ``FeedForge-Plugin-API-Guide.md``; live-verified 2026-07-16):

1. auth is a user-created **access key** (``ffp_…``, from Profile -> Connected apps) sent as
   ``Authorization: Bearer`` — stored in the per-source ``token`` field (a secret, stripped
   from every API response). The old NextAuth username/password login is gone; the API guide
   prohibits collecting credentials, and keys also work for Discord-login accounts;
2. the catalog is held as a **local mirror**: one paced, cursor-paginated walk of
   ``GET /api/v1/songs`` (the guide's recommended sync model), then incremental
   ``updatedAfter`` deltas with ``ETag``/304 revalidation. Browsing, search, sorting, the A-Z
   letter rail, artist browsing, and totals are all served locally from the mirror — including
   the descending and year sorts the server itself does not offer;
3. downloads stay resolve-then-fetch: ``POST /api/v1/songs/{id}/download`` -> ``{ok, url}``
   (an external link — Google Drive, Dropbox, or a Proton Drive share in the wild), streamed
   into the local cache by the matching host path.

Rate limits are documented (catalog: 60/min, 2000/day; downloads: 20/min, 500/day), so the
walk is paced ~1 page/1.2s and refreshes are incremental; a 429 honors ``Retry-After`` once.
Deletions never appear in ``updatedAfter`` deltas, so a periodic full re-walk reconciles
ghosts (and a 404 on download drops the record immediately).

Stdlib-only, like the Google Drive type — the Proton-hosted download path reuses
:mod:`remote_library_client.proton_drive`, whose native deps (``bcrypt`` + ``pysequoia``)
stay lazy and are only needed if such a song is actually downloaded. No song content ever
ships here; tests use synthetic fixtures only.
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib import error, parse, request

from fastapi.responses import Response

from remote_library_client import proton_drive
from remote_library_client.google_drive import (
    download_drive_file,
    drive_file_id_from_url,
)
from remote_library_client.provider import (
    MAX_JSON_RESPONSE_BYTES,
    AuthRequiredError,
    BaseLibraryProvider,
    LibraryImporter,
    _public_error_message,
    _read_error_detail,
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


def _plugin_version() -> str:
    """The plugin's own version (from plugin.json, two levels up), for the User-Agent."""
    try:
        manifest = json.loads(
            (Path(__file__).resolve().parent.parent / "plugin.json").read_text(encoding="utf-8")
        )
        return str(manifest.get("version") or "0")
    except (OSError, ValueError):
        return "0"


# An HONEST User-Agent, at the FeedForge dev's request (2026-07-18): Cloudflare now exempts
# registered API clients, so the browser masquerade the scrape era needed is gone —
# deliberately. If the firewall ever challenges this UA again we fail loudly with
# CLOUDFLARE_BLOCKED_MESSAGE (the dev asked to be told) rather than faking a browser.
_USER_AGENT = (
    f"feedback-remote-library-client/{_plugin_version()} "
    "(+https://github.com/Taynavv/feedback-remote-library-client)"
)
_API_SONGS_PATH = "/api/v1/songs"
_API_ME_PATH = "/api/v1/me"
_API_DELETIONS_PATH = "/api/v1/deletions"
# The server caps `limit` at 50 (verified); a module-level constant so tests can shrink it.
_API_PAGE_LIMIT = 50
# ~50 pages/min keeps the walk safely under the documented 60/min catalog limit.
_WALK_PACE_SECONDS = 1.2
# Backstop against a runaway cursor loop (~20k songs at 50/page) — not an expected limit.
_MAX_WALK_PAGES = 400
_MIRROR_SCHEMA = "feedforge-catalog-mirror.v1"

KEY_REQUIRED_MESSAGE = (
    "A FeedForge access key is required. Sign in at feedforge.org, open Profile, then "
    "Connected apps, create a key, and paste it here."
)
KEY_MIGRATE_MESSAGE = (
    "FeedForge now uses access keys instead of a username and password. Sign in at "
    "feedforge.org, open Profile, then Connected apps, create a key, and paste it here."
)
KEY_REJECTED_MESSAGE = (
    "FeedForge rejected the access key (it may be expired or revoked). Create a new key "
    "under Profile, then Connected apps, and paste it here."
)
RATE_LIMITED_MESSAGE = "FeedForge is rate-limiting this key; try again in a minute."
CATALOG_SYNCING_MESSAGE = (
    "Syncing the FeedForge catalog — songs appear as they arrive; refresh to update the count."
)
CLOUDFLARE_BLOCKED_MESSAGE = (
    "FeedForge's firewall blocked this API client (a Cloudflare challenge). That should not "
    "happen with the plugin's registered User-Agent — please report it to the FeedForge dev."
)
# The /me endpoint reports the key's expiry; the card starts warning this many days ahead.
KEY_EXPIRY_WARNING_DAYS = 30


class SongGoneError(RuntimeError):
    """The song no longer exists (or is unpublished) on FeedForge — HTTP 404 on its endpoints."""


def _utc_iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_ts(value) -> float:
    """ISO-8601 (Zulu) -> epoch seconds; 0.0 when missing/unparseable. Parsed once at reduce
    time so every sort is a plain float compare."""
    raw = str(value or "").strip()
    if not raw:
        return 0.0
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def is_feedforge_url(url: str) -> bool:
    """True when the input points at a FeedForge host (``feedforge.org`` or a subdomain)."""
    host = (parse.urlparse(str(url or "").strip()).hostname or "").lower()
    return host == FEEDFORGE_HOST or host.endswith("." + FEEDFORGE_HOST)


def normalize_feedforge_base_url(url: str) -> str:
    """Reduce user input to a bare ``scheme://host[:port]`` origin, defaulting to
    ``https://feedforge.org`` when nothing usable is supplied. A path/query is dropped — the
    provider owns the API paths."""
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
    ``?dl=1``, so force that. Google Drive and Proton Drive are handled separately; everything
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
    without a mirror record (e.g. mid-walk) so it still imports under a meaningful name."""
    name = parse.unquote(Path(parse.urlparse(str(url or "")).path).name)
    if name and name.lower().endswith(PACKAGE_SUFFIXES):
        return sanitize_filename(name, "remote-song.feedpak")
    return sanitize_filename(song_id, "remote-song") + ".feedpak"


def _reduce_record(item: dict) -> dict:
    """One API song record -> the compact mirror record we persist and query.

    ``fileSizeBytes`` arrives as a JSON *string* when present (BigInt-serialized; verified
    live) and ``year``/``durationSec`` as numbers — ``_safe_int`` tolerates both. Timestamps
    are parsed once so sorts are float compares."""
    return {
        "id": str(item.get("id") or ""),
        "title": str(item.get("title") or "").strip(),
        "artist": str(item.get("artist") or "").strip(),
        "album": str(item.get("album") or "").strip(),
        "year": _safe_int(item.get("year"), 0) or None,
        "durationSec": _safe_int(item.get("durationSec"), 0) or None,
        "tuning": str(item.get("tuning") or "").strip(),
        "coverUrl": str(item.get("coverUrl") or "").strip(),
        "sizeBytes": _safe_int(item.get("fileSizeBytes"), 0),
        "createdTs": _parse_ts(item.get("createdAt")),
        "updatedTs": _parse_ts(item.get("updatedAt")),
    }


def _matches_query(record: dict, needle: str) -> bool:
    return (
        needle in record["title"].lower()
        or needle in record["artist"].lower()
        or needle in record["album"].lower()
    )


def _artist_sort_key(record: dict):
    return (record["artist"].lower(), record["album"].lower(), record["title"].lower())


def _sorted_records(records: list[dict], sort: str, direction: str) -> list[dict]:
    """Order mirror records by core's sort vocabulary — all locally, so every option the
    Songs menu offers (including the descending pair and Year, which FeedForge's own API
    cannot sort by) is faithful. Core encodes direction in the sort string (``artist-desc``);
    an explicit ``direction=desc`` kwarg is honored too."""
    base = str(sort or "artist").lower()
    desc = base.endswith("-desc") or str(direction or "").lower() == "desc"
    base = base.removesuffix("-desc")
    if base in ("recent", "newest", "created"):
        # "Recently added" is inherently newest-first. Core's direction kwarg is always
        # "asc" (direction rides in the sort string), so it must not flip this.
        return sorted(records, key=lambda r: r["createdTs"], reverse=True)
    if base == "updated":
        return sorted(records, key=lambda r: r["updatedTs"], reverse=True)
    if base == "year":
        # Unknown years sink to the end regardless of direction rather than polluting either
        # extreme of the timeline.
        dated = sorted(
            (r for r in records if r["year"]),
            key=lambda r: (r["year"], _artist_sort_key(r)),
            reverse=desc,
        )
        return dated + [r for r in records if not r["year"]]
    if base == "title":
        return sorted(records, key=lambda r: (r["title"].lower(), r["artist"].lower()), reverse=desc)
    return sorted(records, key=_artist_sort_key, reverse=desc)


class FeedForgeProvider(BaseLibraryProvider):
    """Library provider backed by a FeedForge access key and a local catalog mirror."""

    type = "feedforge.v1"
    # Mirror freshness: an `updatedAfter` delta (usually one request, often a 304) runs when
    # the mirror is older than this. With the provider reused across status polls that is
    # ~one API request per interval when the catalog is quiet.
    metadata_cache_ttl_seconds = 900
    # The initial walk paces itself under FeedForge's documented 60/min catalog limit; tests
    # zero this.
    walk_pace_seconds = _WALK_PACE_SECONDS
    # After a failed walk, wait this long before another attempt (no hot retry loops).
    walk_retry_seconds = 60.0
    # Routine ghost cleanup now rides the /api/v1/deletions feed (every delta refresh); this
    # monthly full re-walk is the belt-and-braces backstop for missed tombstones and the
    # pre-feature deletion gap (the feed only covers removals recorded after it deployed).
    full_resync_seconds = 30 * 86400
    # Honor a 429's Retry-After (retry once, per the API guide) only up to this long; a longer
    # server-requested wait surfaces as an error instead of stalling a request thread.
    max_retry_after_seconds = 30.0
    # The walk thread can afford longer waits than interactive paths: a bigger Retry-After
    # budget, and a few attempts per page (growing backoff) before the walk records a failure.
    walk_max_retry_after_seconds = 90.0
    walk_page_attempts = 3
    # How long an interactive call waits for the first page of a cold walk before serving
    # whatever is there (class attributes so tests can zero them).
    browse_wait_seconds = 10.0
    describe_wait_seconds = 15.0

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
        self.token = str(source.get("token") or "").strip()
        # Legacy (pre-API) sources stored a username/password; the username is kept for the
        # label, and its presence without a key selects the "migrate to access keys" message.
        self.username = str(source.get("username") or "").strip()
        self._legacy_credentials = bool(source.get("password")) or bool(self.username and not self.token)
        # The API has no whoami endpoint, so an account has no derivable identity (raised with
        # the FeedForge dev). A per-source random seed (minted at add time) keeps providerId —
        # and the local import folder — stable across key rotations and distinct across
        # accounts; legacy sources keep their username-derived identity.
        ident = str(source.get("accountSeed") or "").strip() or self.username
        self._account_key = sanitize_filename(f"{host}_{ident}" if ident else host, "feedforge")
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
        # Catalog mirror state. `_records` maps song id -> reduced record; `_mirror_complete`
        # means a full walk has finished (this process or a persisted one was loaded).
        self._mirror_lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._records: dict[str, dict] = {}
        self._mirror_load_mtime: float | None = None  # mtime of the persisted mirror we last read
        self._mirror_complete = False
        # Monotonic time of the last successful walk/delta. -inf = never synced: 0.0 would NOT
        # read as stale on a low-uptime host (time.monotonic() is seconds-since-boot on Linux,
        # so shortly after boot `monotonic() - 0.0` can sit under the TTL).
        self._synced_at = float("-inf")
        self._watermark = ""  # updatedAfter cursor (the last walk/delta start time, ISO)
        self._delta_etag = ""  # ETag of the last unchanged-watermark delta request
        self._deletions_watermark = ""  # deletedAfter cursor for the tombstone feed
        self._deletions_etag = ""
        self._full_walk_wall = 0.0  # wall-clock time of the last *completed* full walk
        # Account status from /api/v1/me: identity for labels, key expiry for the card
        # warning. Seeded from the stored username (legacy sources) until the first fetch.
        self._account_username = self.username
        self._key_expires_ts = 0.0
        self._me_fetched_at = float("-inf")
        self._walk_thread: threading.Thread | None = None
        self._first_page_event = threading.Event()
        self._walk_error = ""
        self._walk_auth_error = False
        self._walk_failed_at = 0.0
        # Resume state: a walk that dies mid-catalog (e.g. a rate-limit burst) continues from
        # this cursor on the next attempt instead of burning the request budget from page 1.
        self._walk_cursor = ""
        self._walk_seen: set[str] = set()
        self._walk_started_iso = ""
        # Background-sync state (FeedBack core caps sync-song at ~250ms — far too short for an
        # internet download — so downloads run off-thread; see sync_song). song_id -> entry.
        self._sync_lock = threading.Lock()
        self._downloads: dict[str, dict] = {}

    # -- API client --------------------------------------------------------

    def _require_key(self) -> None:
        if not self.token:
            raise AuthRequiredError(KEY_MIGRATE_MESSAGE if self._legacy_credentials else KEY_REQUIRED_MESSAGE)
        if not self.token.isascii():
            raise AuthRequiredError(KEY_REJECTED_MESSAGE)

    def _api_headers(self) -> dict:
        # The Bearer key goes ONLY to the configured FeedForge origin — every caller of these
        # headers builds its URL from self.base_url. Art and external package downloads use
        # _download_headers() (UA only) so the key can never leak cross-host on a redirect.
        return {
            "User-Agent": _USER_AGENT,
            "Accept": "application/json",
            "Authorization": f"Bearer {self.token}",
        }

    def _download_headers(self) -> dict:
        return {"User-Agent": _USER_AGENT}

    @staticmethod
    def _retry_after_seconds(exc: error.HTTPError) -> float | None:
        headers = getattr(exc, "headers", None)
        try:
            return max(0.0, float(headers.get("Retry-After"))) if headers else None
        except (TypeError, ValueError):
            return None

    def _api_error(self, exc: error.HTTPError) -> Exception:
        headers = getattr(exc, "headers", None)
        if exc.code == 403 and headers and headers.get("Cf-Mitigated"):
            # The request never reached FeedForge — Cloudflare challenged the client. With
            # the registered honest UA this is a firewall regression to report, not something
            # to mask by faking a browser again.
            return RuntimeError(CLOUDFLARE_BLOCKED_MESSAGE)
        if exc.code == 401:
            return AuthRequiredError(KEY_REJECTED_MESSAGE)
        if exc.code == 404:
            return SongGoneError("this song is no longer available on FeedForge")
        if exc.code == 429:
            # 429 bodies now say which limit was hit (user vs IP) — pass that through.
            detail = ""
            try:
                detail = str(json.loads(_read_error_detail(exc)).get("error") or "")
            except (ValueError, TypeError):
                pass
            return RuntimeError(f"{RATE_LIMITED_MESSAGE} ({detail})" if detail else RATE_LIMITED_MESSAGE)
        return _remote_error(exc)

    def _open_api(self, req: request.Request, timeout: float, *, sent_etag: str = "",
                  retry_after_cap: float | None = None) -> tuple[dict | None, str]:
        """Open one API request: JSON on 200, ``(None, etag)`` on 304, one Retry-After-honoring
        retry on 429 (the guide's rule: wait, retry once, never loop). ``retry_after_cap``
        bounds how long a 429 wait may block — interactive paths keep the small default; the
        walk thread passes a larger budget."""
        cap = self.max_retry_after_seconds if retry_after_cap is None else retry_after_cap
        for attempt in range(2):
            try:
                with self._urlopen(req, timeout=timeout) as response:
                    raw = _read_limited(response, MAX_JSON_RESPONSE_BYTES).decode("utf-8", errors="replace")
                    response_etag = str(response.headers.get("ETag") or "")
            except error.HTTPError as exc:
                if exc.code == 304:
                    return None, sent_etag
                retry_after = self._retry_after_seconds(exc)
                if exc.code == 429 and attempt == 0 and retry_after is not None and retry_after <= cap:
                    time.sleep(retry_after)
                    continue
                raise self._api_error(exc) from exc
            try:
                return json.loads(raw or "{}"), response_etag
            except json.JSONDecodeError:
                return {}, response_etag
        raise RuntimeError(RATE_LIMITED_MESSAGE)

    def _api_get(self, path: str, params: dict | None = None, *, etag: str = "",
                 timeout: float = 30.0, retry_after_cap: float | None = None) -> tuple[dict | None, str]:
        self._require_key()
        url = f"{self.base_url}{path}"
        if params:
            url += "?" + parse.urlencode(params)
        headers = self._api_headers()
        if etag:
            headers["If-None-Match"] = etag
        return self._open_api(request.Request(url, headers=headers), timeout,
                              sent_etag=etag, retry_after_cap=retry_after_cap)

    # -- catalog mirror ------------------------------------------------------

    def _mirror_path(self) -> Path:
        return self.cache_dir / "catalog.json"

    def _load_mirror_from_disk_locked(self) -> None:
        """(Re)load the persisted mirror whenever the on-disk file is new to this instance
        (mtime-gated — one ``stat`` per catalog use when nothing changed). Not once-per-
        instance: an earlier process, or a sibling instance created before this one was
        registered, may have completed a walk this instance never ran. Only an incomplete,
        non-walking instance adopts the file — a complete one is already at least as fresh
        (it deltas), and our own persists record their mtime so we never re-read our own
        writes. A loaded mirror counts as complete but stale, so the first use runs an
        ``updatedAfter`` delta — which also revalidates the key after a restart."""
        if self._mirror_complete or self._walk_alive():
            return
        path = self._mirror_path()
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return
        if self._mirror_load_mtime is not None and mtime == self._mirror_load_mtime:
            return
        self._mirror_load_mtime = mtime
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict) or raw.get("schema") != _MIRROR_SCHEMA:
            return
        records = {}
        for item in raw.get("records") or []:
            if isinstance(item, dict) and item.get("id"):
                records[str(item["id"])] = item
        if not records:
            return
        self._records = records
        self._watermark = str(raw.get("watermark") or "")
        self._delta_etag = str(raw.get("etag") or "")
        # Pre-deletions-feed mirrors have no deletion watermark: fall back to the update
        # watermark (both mark "state is authoritative up to here").
        self._deletions_watermark = str(raw.get("deletionsWatermark") or raw.get("watermark") or "")
        self._deletions_etag = ""
        self._full_walk_wall = float(raw.get("fullWalkAt") or 0.0)
        self._mirror_complete = True
        # Deliberately stale (-inf, NOT 0.0 — see __init__) so the first use delta-refreshes,
        # which also revalidates the key after a restart.
        self._synced_at = float("-inf")

    def _persist_mirror_locked(self) -> None:
        payload = {
            "schema": _MIRROR_SCHEMA,
            "watermark": self._watermark,
            "etag": self._delta_etag,
            "deletionsWatermark": self._deletions_watermark,
            "fullWalkAt": self._full_walk_wall,
            "records": list(self._records.values()),
        }
        try:
            self._write_atomic(self._mirror_path(), json.dumps(payload).encode("utf-8"))
            # Remember our own write so the disk loader never re-reads it back over
            # (possibly fresher) in-memory state.
            self._mirror_load_mtime = self._mirror_path().stat().st_mtime
        except OSError:
            pass  # a failed persist only costs a re-walk on the next restart

    def _walk_alive(self) -> bool:
        return bool(self._walk_thread and self._walk_thread.is_alive())

    def _start_catalog_walk(self) -> None:
        thread = threading.Thread(target=self._walk_catalog, daemon=True, name="feedforge-catalog-walk")
        self._walk_thread = thread
        thread.start()

    def _walk_page(self, params: dict) -> dict:
        """One catalog page for the walk, tolerating transient failures: up to
        ``walk_page_attempts`` tries with a growing backoff and a larger Retry-After budget
        than interactive paths (the walk thread can afford to sleep). A rejected key is fatal
        immediately — retrying it would never help."""
        last_exc: Exception | None = None
        for attempt in range(max(1, self.walk_page_attempts)):
            try:
                payload, _etag = self._api_get(
                    _API_SONGS_PATH, params, retry_after_cap=self.walk_max_retry_after_seconds
                )
                return payload or {}
            except AuthRequiredError:
                raise
            except Exception as exc:  # noqa: BLE001 — retried, then surfaced by the walk
                last_exc = exc
                if attempt + 1 < self.walk_page_attempts and self.walk_pace_seconds:
                    time.sleep(self.walk_pace_seconds * (attempt + 2))
        raise last_exc if last_exc else RuntimeError("FeedForge catalog page failed")

    def _walk_catalog(self) -> None:
        """Full paced cursor walk of the catalog on ``sort=newest`` (createdAt never changes,
        so existing records cannot shuffle mid-walk; anything added or edited *during* the walk
        has ``updatedAt`` past our watermark and is caught by the next delta).

        A walk that dies mid-catalog leaves its cursor + seen-set behind and the next attempt
        **resumes** there instead of re-spending the request budget from page 1. Ghost cleanup
        (mirror records the walk did not see — the only way deletions surface, since
        ``updatedAfter`` never reports them) runs only after an *uninterrupted* pass: a resumed
        pass can miss records that were prepended between attempts, and dropping those would
        just churn (the next delta would re-add them)."""
        with self._mirror_lock:
            cursor = self._walk_cursor
            seen = set(self._walk_seen)
            resumed = bool(cursor or seen)
            if not resumed:
                self._walk_started_iso = _utc_iso_now()
            started_iso = self._walk_started_iso
        pages = 0
        try:
            while True:
                params: dict = {"limit": _API_PAGE_LIMIT, "sort": "newest"}
                if cursor:
                    params["cursor"] = cursor
                payload = self._walk_page(params)
                records = [_reduce_record(item) for item in payload.get("data") or [] if item.get("id")]
                pagination = payload.get("pagination") or {}
                cursor = str(pagination.get("nextCursor") or "")
                with self._mirror_lock:
                    for record in records:
                        self._records[record["id"]] = record
                        seen.add(record["id"])
                    # Checkpoint after every page so a failure anywhere resumes from here.
                    self._walk_cursor = cursor
                    self._walk_seen = set(seen)
                self._first_page_event.set()  # partial browsing can start after page 1
                pages += 1
                if not pagination.get("hasMore") or not cursor or pages >= _MAX_WALK_PAGES:
                    break
                if self.walk_pace_seconds:
                    time.sleep(self.walk_pace_seconds)
        except Exception as exc:  # noqa: BLE001 — recorded and surfaced by _ensure_catalog
            with self._mirror_lock:
                self._walk_error = _public_error_message(exc)
                self._walk_auth_error = isinstance(exc, AuthRequiredError)
                self._walk_failed_at = time.monotonic()
            self._first_page_event.set()
            return
        with self._mirror_lock:
            if not resumed:
                for stale_id in set(self._records) - seen:
                    self._records.pop(stale_id, None)
            self._watermark = started_iso
            self._delta_etag = ""
            # A completed walk IS the deletion reconciliation up to its start time; the
            # tombstone feed takes over from here.
            self._deletions_watermark = started_iso
            self._deletions_etag = ""
            self._mirror_complete = True
            self._synced_at = time.monotonic()
            self._full_walk_wall = time.time()
            self._walk_error = ""
            self._walk_auth_error = False
            self._walk_failed_at = 0.0
            self._walk_cursor = ""
            self._walk_seen = set()
            self._walk_started_iso = ""
            self._persist_mirror_locked()

    def _refresh_delta(self) -> None:
        """Merge changes since the watermark (``sort=updated&updatedAfter=…``), revalidating
        with the stored ETag when the watermark is unchanged. Watermark/ETag rules: a 304
        leaves both; an empty 200 keeps the watermark but stores the ETag (the next identical
        poll can 304); a 200 with records advances the watermark and clears the ETag (the next
        request differs, so the old validator is useless)."""
        with self._mirror_lock:
            watermark = self._watermark
            etag = self._delta_etag
        if not watermark:
            return
        started_iso = _utc_iso_now()
        cursor = ""
        merged = 0
        first_etag = ""
        while True:
            params: dict = {"limit": _API_PAGE_LIMIT, "sort": "updated", "updatedAfter": watermark}
            if cursor:
                params["cursor"] = cursor
            payload, response_etag = self._api_get(
                _API_SONGS_PATH, params, etag=etag if not cursor else ""
            )
            if payload is None:  # 304 — nothing changed since the last look
                with self._mirror_lock:
                    self._synced_at = time.monotonic()
                return
            if not cursor:
                first_etag = response_etag
            data = payload.get("data") or []
            with self._mirror_lock:
                for item in data:
                    if item.get("id"):
                        self._records[str(item["id"])] = _reduce_record(item)
                        merged += 1
            pagination = payload.get("pagination") or {}
            cursor = str(pagination.get("nextCursor") or "")
            if not pagination.get("hasMore") or not cursor:
                break
            if self.walk_pace_seconds:
                time.sleep(self.walk_pace_seconds)
        with self._mirror_lock:
            self._synced_at = time.monotonic()
            if merged:
                self._watermark = started_iso
                self._delta_etag = ""
                self._persist_mirror_locked()
            else:
                self._delta_etag = first_etag

    def _refresh_deletions(self) -> None:
        """Drop mirror records tombstoned since the deletion watermark
        (``GET /api/v1/deletions?deletedAfter=…``, cursor-paginated + ETag like the catalog).
        The feed only covers removals recorded after it deployed, so the monthly full re-walk
        stays as the backstop. Tombstone items are parsed defensively (the live feed was empty
        when verified): a dict's ``id``/``songId``, or a bare id string."""
        with self._mirror_lock:
            watermark = self._deletions_watermark
            etag = self._deletions_etag
        if not watermark:
            return  # no completed walk yet — nothing authoritative to reconcile against
        started_iso = _utc_iso_now()
        cursor = ""
        dropped = 0
        first_etag = ""
        while True:
            params: dict = {"limit": _API_PAGE_LIMIT, "deletedAfter": watermark}
            if cursor:
                params["cursor"] = cursor
            try:
                payload, response_etag = self._api_get(
                    _API_DELETIONS_PATH, params, etag=etag if not cursor else ""
                )
            except SongGoneError:
                return  # a 404 here means the feed itself is gone — the re-walk still covers us
            if payload is None:  # 304 — no new tombstones since the last look
                return
            if not cursor:
                first_etag = response_etag
            with self._mirror_lock:
                for item in payload.get("data") or []:
                    if isinstance(item, dict):
                        song_id = str(item.get("id") or item.get("songId") or "")
                    else:
                        song_id = str(item or "")
                    if song_id and self._records.pop(song_id, None) is not None:
                        dropped += 1
            pagination = payload.get("pagination") or {}
            cursor = str(pagination.get("nextCursor") or "")
            if not pagination.get("hasMore") or not cursor:
                break
            if self.walk_pace_seconds:
                time.sleep(self.walk_pace_seconds)
        with self._mirror_lock:
            if dropped:
                self._deletions_watermark = started_iso
                self._deletions_etag = ""
                self._persist_mirror_locked()
            else:
                self._deletions_etag = first_etag

    def _refresh_if_stale(self) -> None:
        if self._walk_alive():
            return  # a walk is already syncing; don't stack a delta on top
        with self._refresh_lock:
            with self._mirror_lock:
                stale = time.monotonic() - self._synced_at > self.metadata_cache_ttl_seconds
            if stale:
                self._refresh_delta()
                self._refresh_deletions()
        with self._mirror_lock:
            wants_rewalk = (
                self._full_walk_wall
                and time.time() - self._full_walk_wall > self.full_resync_seconds
                and not self._walk_alive()
            )
            if wants_rewalk:
                self._start_catalog_walk()

    def _ensure_catalog(self, wait_seconds: float | None = None) -> None:
        """Make the mirror serveable: adopt the persisted copy if it is newer than what this
        instance holds, start (or resume) the walk if one is needed, wait briefly for the
        first page on a cold start, and delta-refresh a stale complete mirror. Raises (auth
        errors included) only when there is nothing to serve."""
        wait_seconds = self.browse_wait_seconds if wait_seconds is None else wait_seconds
        with self._mirror_lock:
            self._load_mirror_from_disk_locked()
            if not self._mirror_complete and not self._walk_alive():
                in_cooloff = self._walk_failed_at and (
                    time.monotonic() - self._walk_failed_at
                ) < self.walk_retry_seconds
                if not in_cooloff:
                    self._walk_failed_at = 0.0
                    self._walk_error = ""
                    self._first_page_event.clear()
                    self._start_catalog_walk()
        if not self._mirror_complete:
            self._first_page_event.wait(wait_seconds)
            with self._mirror_lock:
                if not self._records and self._walk_error:
                    if self._walk_auth_error:
                        raise AuthRequiredError(self._walk_error)
                    raise RuntimeError(self._walk_error)
            return
        self._refresh_if_stale()

    def _record(self, song_id: str) -> dict | None:
        with self._mirror_lock:
            return self._records.get(song_id)

    def _drop_record(self, song_id: str) -> None:
        with self._mirror_lock:
            if self._records.pop(song_id, None) is not None:
                self._persist_mirror_locked()

    def _snapshot_records(self, q: str = "", **kwargs) -> list[dict]:
        with self._mirror_lock:
            records = list(self._records.values())
        needle = str(q or "").strip().lower()
        if needle:
            records = [record for record in records if _matches_query(record, needle)]
        return records

    # -- normalization + querying ---------------------------------------

    def _remote_filename(self, record: dict) -> str:
        """Deterministic local filename for a record: ``Artist - Title.feedpak``.

        We import under this name (not the CDN's Content-Disposition) so the browse-time
        ``settingsKey`` — derived from the same name — matches core's key for the imported
        file (the client<->core playback-settings-key contract)."""
        artist = record.get("artist") or "Unknown artist"
        title = record.get("title") or record.get("id") or "song"
        return sanitize_filename(f"{artist} - {title}.feedpak", "remote-song.feedpak")

    def _downloaded_names(self) -> frozenset[str]:
        if not self.local_library_root or not self.local_library_root.exists():
            return frozenset()
        folder = self.local_library_root / self._source_folder_name()
        try:
            return frozenset(item.name for item in folder.iterdir() if item.is_file())
        except OSError:
            return frozenset()

    def _normalize_card(self, record: dict, downloaded: frozenset[str] = frozenset()) -> dict:
        song_id = record["id"]
        remote_name = self._remote_filename(record)
        local = f"{self._source_folder_name()}/{remote_name}" if remote_name in downloaded else ""
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
            "title": record.get("title") or song_id,
            "artist": record.get("artist") or "Unknown artist",
            "album": record.get("album") or "",
            "year": record.get("year"),
            "duration": record.get("durationSec"),
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
            "tuning": record.get("tuning") or "",
            "tuning_name": record.get("tuning") or "",
            "sizeBytes": record.get("sizeBytes") or 0,
            "localFilename": local,
            "local_filename": local,
            "playFilename": local,
        }

    def query_page(self, page: int = 0, size: int = 24, sort: str = "artist",
                   direction: str = "asc", **kwargs):
        if kwargs.get("favorites_only"):
            return [], 0
        self._ensure_catalog()
        size = max(1, min(100, _safe_int(size, 24)))
        page = max(0, _safe_int(page, 0))
        records = _sorted_records(self._snapshot_records(**kwargs), sort, direction)
        offset = page * size
        window = records[offset:offset + size]
        downloaded = self._downloaded_names()
        songs = [self._normalize_card(record, downloaded) for record in window]
        total = len(records)
        if not self._mirror_complete:
            # The initial walk is still filling the mirror: report an *at-least* total so the
            # grid keeps paginating; it settles to the exact count when the walk completes.
            total = max(total, offset + len(window)) + _API_PAGE_LIMIT
        return songs, total

    def query_artists(self, letter: str = "", page: int = 0, size: int = 50, **kwargs):
        if kwargs.get("favorites_only"):
            return [], 0
        self._ensure_catalog()
        records = self._snapshot_records(**kwargs)
        by_artist: dict[str, list[dict]] = {}
        for record in records:
            by_artist.setdefault(record["artist"] or "Unknown artist", []).append(record)
        artist_names = sorted(by_artist, key=str.lower)
        if letter:
            artist_names = [name for name in artist_names if name[:1].upper() == letter.upper()]
        total = len(artist_names)
        size = max(1, _safe_int(size, 50))
        page = max(0, _safe_int(page, 0))
        downloaded = self._downloaded_names()
        artists = []
        for name in artist_names[page * size:page * size + size]:
            group = sorted(by_artist[name], key=_artist_sort_key)
            by_album: dict[str, list[dict]] = {}
            for record in group:
                by_album.setdefault(record["album"] or "", []).append(record)
            albums = [
                {
                    "name": album,
                    "song_count": len(items),
                    "songs": [self._normalize_card(record, downloaded) for record in items],
                }
                for album, items in by_album.items()
            ]
            artists.append({
                "name": name,
                "album_count": len(albums),
                "song_count": len(group),
                "albums": albums,
            })
        return artists, total

    def query_stats(self, **kwargs) -> dict:
        if kwargs.get("favorites_only"):
            return {"total_songs": 0, "total_artists": 0, "letters": {}}
        self._ensure_catalog()
        records = self._snapshot_records(**kwargs)
        letters: dict[str, int] = {}
        artists = {record["artist"] or "Unknown artist" for record in records}
        for artist in artists:
            letter = artist[:1].upper() if artist else "#"
            if not letter.isalpha():
                letter = "#"
            letters[letter] = letters.get(letter, 0) + 1
        return {"total_songs": len(records), "total_artists": len(artists), "letters": letters}

    @property
    def catalog_syncing(self) -> bool:
        """True while the initial (or a reconciling) walk has not yet completed — the card
        shows a "syncing" message and counts are at-least values."""
        return not self._mirror_complete

    @property
    def account_username(self) -> str:
        return self._account_username

    @property
    def key_expiry_message(self) -> str:
        """A card warning once the key is within ``KEY_EXPIRY_WARNING_DAYS`` of expiring.
        An already-dead key is not a countdown — it surfaces through the 401 path instead."""
        if not self._key_expires_ts:
            return ""
        days = (self._key_expires_ts - time.time()) / 86400
        if days <= 0 or days > KEY_EXPIRY_WARNING_DAYS:
            return ""
        when = "today" if days < 1 else f"in {int(days)} day{'s' if int(days) != 1 else ''}"
        return (
            f"FeedForge access key expires {when} — create a new key under Profile → "
            "Connected apps and paste it here."
        )

    def _fetch_account_status(self) -> None:
        """``GET /api/v1/me`` — validates the key and captures the account identity (for
        default labels) plus the key's expiry (for the card warning). Shape (verified live):
        ``data.user.{username, displayName}`` and ``data.token.{scopes, expiresAt, lastUsedAt}``."""
        payload, _etag = self._api_get(_API_ME_PATH, timeout=15)
        data = (payload or {}).get("data") or {}
        user = data.get("user") or {}
        token = data.get("token") or {}
        with self._mirror_lock:
            fetched = str(user.get("username") or user.get("displayName") or "").strip()
            if fetched:
                self._account_username = fetched
            self._key_expires_ts = _parse_ts(token.get("expiresAt"))
            self._me_fetched_at = time.monotonic()

    def describe_source(self) -> dict:
        with self._mirror_lock:
            self._load_mirror_from_disk_locked()
            have_records = bool(self._records)
            me_stale = time.monotonic() - self._me_fetched_at > self.metadata_cache_ttl_seconds
        if not have_records:
            # First contact for this source: /me validates the key fast (a bad/expired key
            # fails the add immediately, not minutes into a walk) and yields the identity.
            self._fetch_account_status()
        elif me_stale:
            try:
                self._fetch_account_status()
            except AuthRequiredError:
                raise  # a revoked/expired key must flip the card to Key required
            except Exception:  # noqa: BLE001 — identity/expiry refresh is best-effort on polls
                pass
        self._ensure_catalog(wait_seconds=self.describe_wait_seconds)
        with self._mirror_lock:
            count = len(self._records)
            username = self._account_username
        return {
            "ok": True,
            "sourceId": f"feedforge_{self._account_key}",
            "sourceName": f"FeedForge ({username})" if username else self.label,
            "accountUsername": username,
            "songCount": count,
            "syncing": self.catalog_syncing,
            "keyExpiryWarning": self.key_expiry_message,
            "capabilities": ["library.read", "song.sync"],
            "server": {"protocol": self.type},
        }

    # -- artwork ---------------------------------------------------------

    def get_art(self, song_id: str):
        # Covers are served publicly by feedforge.org (verified live) — fetched WITHOUT the
        # Bearer key, so the secret cannot ride a cross-host art redirect. Any failure
        # degrades to the base "no art" default rather than erroring the card.
        cached = self._read_cached_art(song_id)
        if cached:
            content, media_type = cached
            return Response(content=content, media_type=media_type, headers={"Cache-Control": "public, max-age=86400"})
        record = self._record(song_id)
        art_url = (record or {}).get("coverUrl") or ""
        if not art_url:
            return None
        try:
            content, media_type, _headers = self._open_bytes(
                parse.urljoin(self.base_url + "/", art_url), self._download_headers()
            )
        except Exception:
            return None
        if not content:
            return None
        self._write_cached_art(song_id, content, media_type)
        return Response(content=content, media_type=media_type, headers={"Cache-Control": "public, max-age=86400"})

    # -- download + sync -------------------------------------------------

    def _post_download(self, song_id: str) -> dict:
        """``POST /api/v1/songs/{id}/download`` -> ``{ok, url}`` (the external link). The
        tracked endpoint keeps the user's FeedForge download history + the public counter
        accurate — the guide asks for it over the detail response's raw ``downloadUrl``."""
        self._require_key()
        endpoint = f"{self.base_url}{_API_SONGS_PATH}/{parse.quote(song_id)}/download"
        req = request.Request(endpoint, data=b"", headers=self._api_headers())
        try:
            payload, _etag = self._open_api(req, timeout=30)
        except SongGoneError:
            # The guide's 404 rule: the song is gone/unpublished — drop the local record now
            # rather than waiting for the next full re-walk to reconcile it.
            self._drop_record(song_id)
            raise
        return payload or {}

    def _resolve_download_url(self, song_id: str) -> str:
        payload = self._post_download(song_id)
        url = str(payload.get("url") or "")
        if not payload.get("ok") or not url:
            raise RuntimeError("FeedForge did not return a download link for this song")
        return url

    def _do_sync(self, song_id: str) -> dict:
        url = self._resolve_download_url(song_id)
        # Name the local file deterministically: from the mirror record if we have it, else
        # from the resolved URL (…/Artist-Title.feedpak). Keeps settingsKey stable.
        record = self._record(song_id)
        remote_name = self._remote_filename(record) if record else _filename_from_url(url, song_id)
        file_id = drive_file_id_from_url(url)
        if file_id:
            # FeedForge points at Google Drive — reuse the Drive download path (redirect +
            # large-file confirm-token flow) from google_drive.py.
            target, content_hash, bytes_read, _headers = download_drive_file(
                self, file_id, remote_name, self._download_headers()
            )
        elif proton_drive.is_proton_share_url(url):
            # A Proton Drive share (seen in the wild alongside Drive/Dropbox). Reuses the
            # Proton provider's SRP + OpenPGP machinery; its native deps are lazy, so a
            # missing install degrades to a clear message instead of a stack trace.
            try:
                target, content_hash, bytes_read = proton_drive.download_share_package(
                    self, url, remote_name
                )
            except ImportError as exc:
                raise RuntimeError(
                    "this song is hosted on Proton Drive; install the plugin requirements "
                    "(bcrypt + pysequoia) to download it"
                ) from exc
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

    def _download_label(self, song_id: str, record: dict | None = None) -> str:
        record = record or self._record(song_id)
        if not record:
            return song_id
        artist = record.get("artist") or ""
        title = record.get("title") or song_id
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
        record = self._record(song_id)
        if not record or not self.local_library_root or not self.local_library_root.exists():
            return None
        candidate = self.local_library_root / self._source_folder_name() / self._remote_filename(record)
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
