# SPDX-License-Identifier: AGPL-3.0-or-later
"""Google Drive public-folder library provider.

Registers a public ("anyone with the link", no login) Google Drive *folder* of package
files as a FeedBack library provider. Enumeration and download run on the shared stdlib
HTTP stack in :mod:`remote_library_client.provider` (redirect-SSRF guard + size caps),
so no third-party dependency and no API key are needed — the user just pastes the folder
URL.

Metadata comes from the filenames. Community folders follow a consistent
``Artist - Album - Title.feedpak`` convention, so artist/album/title are recovered by
parsing the name; there is no server API, artwork, tuning, or NAM-tone data to read.
"""
from __future__ import annotations

import html
import re
from collections import OrderedDict
from pathlib import Path
from urllib import error, parse, request

from remote_library_client.provider import (
    MAX_JSON_RESPONSE_BYTES,
    BaseLibraryProvider,
    LibraryImporter,
    _header_filename,
    _read_limited,
    _remote_error,
    _safe_int,
    provider_id_for_source,
    sanitize_filename,
)

PACKAGE_SUFFIXES = (".feedpak", ".sloppak", ".psarc", ".zip")
GOOGLE_DRIVE_HOSTS = {"drive.google.com", "drive.usercontent.google.com", "docs.google.com"}
# A browser-ish User-Agent; Google's public endpoints are friendlier to one than to none.
_USER_AGENT = "Mozilla/5.0 (compatible; FeedBackRemoteLibraryClient/1.0)"
# Google's HTML entry markup: each file is `id="entry-<fileId>" ... flip-entry-title">Name</div>`.
_ENTRY_RE = re.compile(r'id="entry-([A-Za-z0-9_-]+)".*?<div class="flip-entry-title">([^<]*)</div>', re.S)
_SUFFIX_RE = re.compile(r"\.(?:feedpak|sloppak|psarc|zip)$", re.I)


def parse_drive_folder_id(url: str) -> str | None:
    """Extract a Drive folder id from a share URL (``/folders/<id>`` or ``?id=<id>``)."""
    raw = str(url or "").strip()
    match = re.search(r"/folders/([A-Za-z0-9_-]{10,})", raw)
    if match:
        return match.group(1)
    match = re.search(r"[?&]id=([A-Za-z0-9_-]{10,})", raw)
    if match:
        return match.group(1)
    return None


def is_google_drive_folder_url(url: str) -> bool:
    """True when the input looks like a public Google Drive *folder* link."""
    host = (parse.urlparse(str(url or "").strip()).hostname or "").lower()
    return host in GOOGLE_DRIVE_HOSTS and parse_drive_folder_id(url) is not None


def format_from_filename(filename: str) -> str:
    suffix = Path(filename or "").suffix.lower()
    if suffix == ".psarc":
        return "psarc"
    # .feedpak / .sloppak / .zip — and anything else we chose to accept — are sloppak-family.
    return "sloppak"


def parse_feedpak_filename(filename: str) -> tuple[str, str, str]:
    """Recover ``(artist, album, title)`` from an ``Artist - Album - Title`` package name.

    Best-effort and tolerant: with 3+ ``" - "`` fields the first is the artist, the last is
    the title, and the middle (which may itself contain hyphens) is the album; with two
    fields there is no album; with one, only a title. Never raises.
    """
    stem = _SUFFIX_RE.sub("", str(filename or "")).strip()
    parts = [part.strip() for part in stem.split(" - ")]
    parts = [part for part in parts if part]
    if len(parts) >= 3:
        return parts[0], " - ".join(parts[1:-1]), parts[-1]
    if len(parts) == 2:
        return parts[0], "", parts[1]
    if len(parts) == 1:
        return "Unknown artist", "", parts[0]
    return "Unknown artist", "", stem or str(filename or "")


class GoogleDrivePublicFolderProvider(BaseLibraryProvider):
    """Library provider backed by a public Google Drive folder of package files."""

    type = "google-drive-public.v1"

    def __init__(
        self,
        source: dict,
        cache_dir: Path,
        local_library_root: Path | None = None,
        library_importer: LibraryImporter | None = None,
        nam_config_dir: Path | None = None,
    ) -> None:
        folder_id = parse_drive_folder_id(source.get("baseUrl") or source.get("folderId") or "")
        if not folder_id:
            raise ValueError("not a recognizable Google Drive folder URL")
        self.folder_id = folder_id
        base_url = f"https://drive.google.com/drive/folders/{folder_id}"
        provider_id = str(
            source.get("providerId") or provider_id_for_source(f"gdrive_{folder_id}", base_url, prefix="gdrive")
        )
        super().__init__(
            {**source, "providerId": provider_id},
            cache_dir,
            origin_host="drive.google.com",
            allow_unsafe_redirects=bool(source.get("allowUnsafeRedirects")),
            local_library_root=local_library_root,
            library_importer=library_importer,
            nam_config_dir=nam_config_dir,
        )
        self.base_url = base_url
        self.label = str(source.get("label") or source.get("sourceName") or f"Google Drive folder {folder_id[:8]}")

    # -- HTTP helpers ----------------------------------------------------

    def _headers(self) -> dict:
        return {"User-Agent": _USER_AGENT}

    # -- enumeration -----------------------------------------------------

    def _fetch_folder_html(self) -> str:
        url = f"https://drive.google.com/embeddedfolderview?id={parse.quote(self.folder_id)}"
        content, _content_type, _headers = self._open_bytes(url, self._headers())
        return content.decode("utf-8", errors="replace")

    def _parse_folder_html(self, text: str) -> list[dict]:
        entries: list[dict] = []
        seen: set[str] = set()
        for file_id, raw_name in _ENTRY_RE.findall(text):
            name = html.unescape(raw_name).strip()
            if not name or file_id in seen:
                continue
            if not name.lower().endswith(PACKAGE_SUFFIXES):
                continue
            seen.add(file_id)
            entries.append({"file_id": file_id, "filename": name})
        return entries

    def _entries(self) -> list[dict]:
        key = self._metadata_cache_key("folder", {"id": self.folder_id})
        cached = self._cache_get(key)
        if cached is not None:
            return list(cached.get("entries") or [])
        entries = self._parse_folder_html(self._fetch_folder_html())
        self._cache_put(key, {"entries": entries})
        return entries

    def _entry_by_id(self, file_id: str) -> dict | None:
        return next((entry for entry in self._entries() if entry["file_id"] == file_id), None)

    # -- normalization + querying ---------------------------------------

    def _normalize_entry(self, entry: dict) -> dict:
        file_id = entry["file_id"]
        filename = entry["filename"]
        artist, album, title = parse_feedpak_filename(filename)
        return {
            "filename": file_id,
            "song_id": file_id,
            "remote_id": file_id,
            "remoteSongId": file_id,
            "remoteFilename": filename,
            "libraryProviderId": self.id,
            "provider": self.id,
            "sourceId": self.source.get("sourceId") or f"gdrive_{self.folder_id}",
            "sourceName": self.label,
            "title": title,
            "artist": artist or "Unknown artist",
            "album": album,
            "format": format_from_filename(filename),
            "packageForm": "feedpak-file",
            "stem_count": 0,
            "stem_ids": [],
            "localFilename": "",
            "local_filename": "",
            "playFilename": "",
            "arrangements": [],
            "has_lyrics": False,
            "tuning": "",
            "tuning_name": "",
            "sizeBytes": 0,
        }

    def _all_songs(self) -> list[dict]:
        songs = [self._normalize_entry(entry) for entry in self._entries()]
        songs.sort(key=lambda song: (song["artist"].lower(), song["album"].lower(), song["title"].lower()))
        return songs

    def _apply_text_query(self, songs: list[dict], q: str = "", **_kwargs) -> list[dict]:
        # Only the free-text query applies here; a public folder carries no tuning / stem /
        # arrangement facets, so those filters are ignored rather than matching nothing.
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
        artists = set()
        for song in songs:
            artists.add(song["artist"])
        for artist in artists:
            letter = artist[:1].upper() if artist else "#"
            if not letter.isalpha():
                letter = "#"
            letters[letter] = letters.get(letter, 0) + 1
        return {"total_songs": len(songs), "total_artists": len(artists), "letters": letters}

    def describe_source(self) -> dict:
        """Validate the folder is reachable and return source metadata for add/refresh."""
        entries = self._entries()
        return {
            "ok": True,
            "sourceId": f"gdrive_{self.folder_id}",
            "sourceName": self.label,
            "songCount": len(entries),
            "capabilities": ["library.read", "song.sync"],
            "server": {"protocol": self.type},
        }

    # -- download + sync -------------------------------------------------

    def _confirmed_download_url(self, html_text: str, file_id: str) -> str:
        """Resolve the post-interstitial download URL for a large file.

        Google serves a "can't scan for viruses" HTML page for big files; the real bytes
        come from a confirm form. Prefer the form's own action + hidden inputs; fall back
        to the documented ``usercontent`` URL with ``confirm=t``.
        """
        lowered = html_text.lower()
        if "download quota" in lowered or "too many users have" in lowered:
            raise RuntimeError(
                "Google has temporarily rate-limited this file (too many recent downloads); try again later"
            )
        action = re.search(r'id="download-form"[^>]+action="([^"]+)"', html_text) or re.search(
            r'<form[^>]+action="([^"]+)"', html_text
        )
        if action:
            fields = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', html_text))
            if fields:
                base = html.unescape(action.group(1))
                query = parse.urlencode({key: html.unescape(value) for key, value in fields.items()})
                return f"{base}?{query}" if query else base
        return f"https://drive.usercontent.google.com/download?id={parse.quote(file_id)}&export=download&confirm=t"

    def _download_drive_file(self, file_id: str, fallback_filename: str) -> tuple[Path, str, int, dict]:
        url = f"https://drive.google.com/uc?export=download&id={parse.quote(file_id)}"
        req = request.Request(url, headers=self._headers())
        try:
            with self._urlopen(req, timeout=120) as response:
                content_type = (response.headers.get("content-type") or "").lower()
                if "text/html" not in content_type:
                    # Common case: Google redirected straight to the file bytes.
                    return self._stream_response_to_cache(response, fallback_filename)
                html_text = _read_limited(response, MAX_JSON_RESPONSE_BYTES).decode("utf-8", errors="replace")
        except error.HTTPError as exc:
            raise _remote_error(exc) from exc
        # Large-file interstitial: resolve the confirmed URL and stream that.
        confirmed = self._confirmed_download_url(html_text, file_id)
        return self._download_url_to_cache(confirmed, fallback_filename, self._headers())

    def sync_song(self, song_id: str) -> dict:
        entry = self._entry_by_id(song_id)
        remote_name = (entry or {}).get("filename") or song_id
        fallback_filename = sanitize_filename(remote_name, "remote-song.feedpak")
        if not fallback_filename.lower().endswith(PACKAGE_SUFFIXES):
            fallback_filename += ".feedpak"
        target, content_hash, bytes_read, headers = self._download_drive_file(song_id, fallback_filename)
        filename = _header_filename(headers, fallback_filename)
        self.clear_metadata_cache()
        result = {
            "ok": True,
            "song_id": song_id,
            "remoteSongId": song_id,
            "cached": True,
            "cacheState": "ready",
            "bytes": bytes_read,
        }
        result.update(self._import_into_library(target, content_hash, filename))
        return result
