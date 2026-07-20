from __future__ import annotations

import importlib
import io
import json
import sys
from pathlib import Path
from urllib import error, parse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import remote_library_client.feedforge as feedforge
from remote_library_client.feedforge import (
    KEY_MIGRATE_MESSAGE,
    KEY_REJECTED_MESSAGE,
    FeedForgeProvider,
    SongGoneError,
    _direct_download_url,
    _reduce_record,
    is_feedforge_url,
    normalize_feedforge_base_url,
)
from remote_library_client.google_drive import drive_file_id_from_url, is_google_drive_file_url
from remote_library_client.provider import AuthRequiredError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FAKE_KEY = "ffp_fake_test_key_000000000000"


# Shrink the API page limit (50 in production) to 3 for every test, so cursor pagination is
# exercised with tiny synthetic catalogs; never sleep in tests.
@pytest.fixture(autouse=True)
def _small_pages(monkeypatch):
    monkeypatch.setattr(feedforge, "_API_PAGE_LIMIT", 3)
    monkeypatch.setattr(FeedForgeProvider, "walk_pace_seconds", 0)


def _api_song(i: int = 0, **overrides) -> dict:
    """One synthetic, content-free record in the shape the live v1 API returns (verified
    2026-07-16) — notably `fileSizeBytes` as a JSON *string* (BigInt-serialized)."""
    record = {
        "id": f"cfake{i:019d}",
        "title": f"Song {i:03d}",
        "artist": f"Artist {i % 4}",
        "album": f"Album {i % 2}",
        "year": 2000 + i,
        "durationSec": 180 + i,
        "tuning": "E Standard Tuning",
        "version": "1.0",
        "difficultyLabel": None,
        "fileSizeBytes": str(1000 + i),
        "downloadCount": i,
        "coverUrl": f"https://feedforge.org/feedpak-covers/cfakecover{i:03d}",
        "createdAt": f"2026-06-{(i % 27) + 1:02d}T00:00:00.000Z",
        "updatedAt": f"2026-06-{(i % 27) + 1:02d}T12:00:00.000Z",
    }
    record.update(overrides)
    return record


def _songs(n: int) -> list[dict]:
    return [_api_song(i) for i in range(n)]


class _Resp:
    """Minimal urllib response stand-in: context manager + read() + headers + geturl()."""

    def __init__(self, body=b"", headers=None, url="https://feedforge.org/"):
        self._chunks = list(body) if isinstance(body, list) else [body]
        self.headers = headers or {}
        self._url = url

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _size=-1):
        return self._chunks.pop(0) if self._chunks else b""

    def geturl(self):
        return self._url


def _http_error(url: str, code: int, body: bytes = b"", headers: dict | None = None) -> error.HTTPError:
    return error.HTTPError(url, code, "error", headers or {}, io.BytesIO(body))


class FakeFeedForgeAPI(FeedForgeProvider):
    """FeedForge provider with the network stubbed at ``_urlopen``: a cursor-paginated
    ``/api/v1/songs`` (sort + updatedAfter honored, ETag/304 revalidation, Bearer-key auth),
    the tracked download POST, public cover art, and external CDN bytes. The catalog walk
    runs inline (no threads) so tests are deterministic."""

    def __init__(self, cache_dir, *, catalog=None, token=FAKE_KEY, download_payload=None,
                 file_bytes=b"PK\x03\x04fake-package", rate_limit_times=0,
                 fail_cursor_pages_times=0, me_username="fakeuser",
                 me_expires_at="2036-01-01T00:00:00.000Z", deleted=None, cf_block=False,
                 source_extra=None, **kwargs):
        source = {
            "baseUrl": "https://feedforge.org",
            "token": token,
            "label": "Fake FeedForge",
            "accountSeed": "cafe1234",
            **(source_extra or {}),
        }
        super().__init__(source, cache_dir, **kwargs)
        self.api_catalog = list(catalog or [])
        self._valid_key = FAKE_KEY
        self._download_payload = download_payload if download_payload is not None else {}
        self._file_bytes = file_bytes
        self._rate_limit_times = rate_limit_times
        # Fail this many cursor-bearing walk requests (page 2+) with a 500 — exercises the
        # walk's per-page retries and resume-from-cursor.
        self._fail_cursor_pages_times = fail_cursor_pages_times
        self.me_username = me_username
        self.me_expires_at = me_expires_at
        # Tombstones served by /api/v1/deletions: dicts with id + deletedAt.
        self.deleted = list(deleted or [])
        # When True, every feedforge.org API request gets a Cloudflare managed challenge.
        self.cf_block = cf_block
        self.calls: list[tuple[str, str]] = []
        self.api_user_agents: list[str] = []
        self.cover_request_headers: dict | None = None
        # Run walks inline so tests never race a thread.
        self._start_catalog_walk = self._walk_catalog

    # -- fake server -----------------------------------------------------

    def _catalog_sorted(self, sort: str) -> list[dict]:
        if sort == "updated":
            return sorted(self.api_catalog, key=lambda r: r["updatedAt"], reverse=True)
        return sorted(self.api_catalog, key=lambda r: r["createdAt"], reverse=True)  # newest

    def _songs_response(self, url: str, req) -> _Resp:
        qs = parse.parse_qs(parse.urlparse(url).query)
        limit = int((qs.get("limit") or ["50"])[0])
        cursor = (qs.get("cursor") or [""])[0]
        updated_after = (qs.get("updatedAfter") or [""])[0]
        if cursor and not updated_after and self._fail_cursor_pages_times > 0:
            self._fail_cursor_pages_times -= 1
            raise _http_error(url, 500, b'{"ok":false,"error":"transient"}')
        rows = self._catalog_sorted((qs.get("sort") or ["updated"])[0])
        if updated_after:
            rows = [r for r in rows if r["updatedAt"] > updated_after]
        start = 0
        if cursor:
            ids = [r["id"] for r in rows]
            start = ids.index(cursor) + 1 if cursor in ids else len(rows)
        page = rows[start:start + limit]
        etag = 'W/"' + ("-".join(r["id"][-4:] for r in page) or "empty") + f"-{len(rows)}" + '"'
        if not cursor and req.get_header("If-none-match") == etag:
            raise _http_error(url, 304)
        has_more = start + limit < len(rows)
        body = {
            "ok": True,
            "data": page,
            "pagination": {
                "limit": limit,
                "nextCursor": page[-1]["id"] if (page and has_more) else None,
                "hasMore": has_more,
            },
        }
        return _Resp(json.dumps(body).encode(), {"Content-Type": "application/json", "ETag": etag}, url)

    def _urlopen(self, req, timeout):
        url = req.full_url
        method = req.get_method()
        host = (parse.urlparse(url).hostname or "").lower()
        path = parse.urlparse(url).path
        self.calls.append((method, url))
        if host == "feedforge.org" and path.startswith("/feedpak-covers/"):
            # Public cover art — record the headers so tests can assert the key never rides.
            self.cover_request_headers = dict(req.headers)
            return _Resp(b"\x89PNG fake image", {"Content-Type": "image/png"}, url)
        if host == "www.mediafire.com":
            # A MediaFire share link serves an HTML download *page* whose button points at
            # the direct-download host; that host falls through to the CDN branch below.
            page = (
                '<a class="input popsok" aria-label="Download file"\n'
                '   href="https://download9.mediafire.com/tok123/fakekey01234567/Song.feedpak"\n'
                '   id="downloadButton" rel="nofollow">Download</a>'
            )
            return _Resp(page.encode(), {"content-type": "text/html; charset=UTF-8"}, url)
        if host == "htmlpage.example.com":
            # A host that serves a web page where a package was expected.
            return _Resp(b"<html>a download page</html>", {"content-type": "text/html"}, url)
        if host != "feedforge.org":
            # External CDN (Google Drive / Dropbox / …): stream file bytes.
            return _Resp(
                [self._file_bytes],
                {"content-type": "application/octet-stream",
                 "content-disposition": 'attachment; filename="song.feedpak"'},
                url,
            )
        self.api_user_agents.append(str(req.get_header("User-agent") or ""))
        if self.cf_block:
            raise _http_error(url, 403, b"<html>Just a moment...</html>",
                              {"Cf-Mitigated": "challenge", "Content-Type": "text/html"})
        if req.get_header("Authorization") != f"Bearer {self._valid_key}":
            raise _http_error(url, 401, json.dumps({"ok": False, "error": "Invalid key."}).encode())
        if self._rate_limit_times > 0:
            self._rate_limit_times -= 1
            raise _http_error(url, 429, b'{"ok":false,"error":"slow down"}', {"Retry-After": "0"})
        if path == "/api/v1/me" and method == "GET":
            body = {
                "ok": True,
                "data": {
                    "user": {"id": "cfakeuser0000", "username": self.me_username,
                             "displayName": self.me_username, "role": "USER"},
                    "token": {"scopes": ["catalog:read", "feedpaks:download"],
                              "expiresAt": self.me_expires_at,
                              "lastUsedAt": "2026-07-18T00:00:00.000Z"},
                },
            }
            return _Resp(json.dumps(body).encode(), {"Content-Type": "application/json"}, url)
        if path == "/api/v1/deletions" and method == "GET":
            qs = parse.parse_qs(parse.urlparse(url).query)
            after = (qs.get("deletedAfter") or [""])[0]
            rows = [t for t in self.deleted if str(t.get("deletedAt") or "") > after]
            body = {
                "ok": True,
                "data": rows,
                "pagination": {"limit": 50, "nextCursor": None, "hasMore": False},
            }
            return _Resp(json.dumps(body).encode(), {"Content-Type": "application/json"}, url)
        if path == "/api/v1/songs" and method == "GET":
            return self._songs_response(url, req)
        if path.startswith("/api/v1/songs/") and path.endswith("/download") and method == "POST":
            song_id = path.split("/")[-2]
            if song_id not in {r["id"] for r in self.api_catalog}:
                raise _http_error(url, 404, json.dumps({"ok": False, "error": "Song not found."}).encode())
            return _Resp(json.dumps(self._download_payload).encode(), {"Content-Type": "application/json"}, url)
        raise _http_error(url, 404, b'{"ok":false,"error":"no such route"}')

    def songs_requests(self) -> list[str]:
        return [u for _m, u in self.calls if "/api/v1/songs?" in u or u.endswith("/api/v1/songs")]


# ------------------------------------------------------------------- helpers / URL parsing


@pytest.mark.parametrize("url,expected", [
    ("https://feedforge.org", "https://feedforge.org"),
    ("feedforge.org", "https://feedforge.org"),
    ("https://feedforge.org/library?page=2", "https://feedforge.org"),
    ("http://localhost:3000/", "http://localhost:3000"),
    ("", "https://feedforge.org"),
    ("not a url", "https://feedforge.org"),
])
def test_normalize_feedforge_base_url(url, expected):
    assert normalize_feedforge_base_url(url) == expected


def test_is_feedforge_url():
    assert is_feedforge_url("https://feedforge.org/library")
    assert is_feedforge_url("https://www.feedforge.org")
    assert not is_feedforge_url("https://drive.google.com/drive/folders/x")
    assert not is_feedforge_url("studio.local")


@pytest.mark.parametrize("url,expected", [
    ("https://drive.google.com/file/d/FID12345678/view", "FID12345678"),
    ("https://drive.google.com/uc?export=download&id=FID12345678", "FID12345678"),
    ("https://drive.usercontent.google.com/download?id=FID12345678&export=download", "FID12345678"),
    ("https://example.com/pkg.feedpak", None),
    ("", None),
])
def test_drive_file_id_from_url(url, expected):
    assert drive_file_id_from_url(url) == expected
    assert is_google_drive_file_url(url) is (expected is not None)


@pytest.mark.parametrize("url,expect_dl1", [
    ("https://www.dropbox.com/scl/fi/abc/Song.feedpak?rlkey=k&st=s&dl=0", True),
    ("https://www.dropbox.com/scl/fi/abc/Song.feedpak?rlkey=k", True),
    ("https://drive.google.com/file/d/FID12345678/view", False),
    ("https://example.com/x.feedpak", False),
])
def test_direct_download_url_forces_dropbox_dl1(url, expect_dl1):
    out = _direct_download_url(url)
    if expect_dl1:
        assert "dl=1" in out and "dl=0" not in out
    else:
        assert out == url


def test_reduce_record_tolerates_live_api_typing():
    # fileSizeBytes is a STRING when present and null when not (verified live); year and
    # durationSec are numbers; timestamps become floats for sorting.
    reduced = _reduce_record(_api_song(1, fileSizeBytes="8178892"))
    assert reduced["sizeBytes"] == 8178892
    assert reduced["year"] == 2001
    assert reduced["createdTs"] > 0

    sparse = _reduce_record(_api_song(2, fileSizeBytes=None, year=None, durationSec=None,
                                      album="", createdAt="", updatedAt="not-a-date"))
    assert sparse["sizeBytes"] == 0
    assert sparse["year"] is None
    assert sparse["durationSec"] is None
    assert sparse["createdTs"] == 0.0
    assert sparse["updatedTs"] == 0.0


# ------------------------------------------------------------------------------ auth


def test_missing_key_raises_before_any_request(tmp_path):
    provider = FakeFeedForgeAPI(tmp_path, token="")
    with pytest.raises(AuthRequiredError, match="access key"):
        provider.describe_source()
    assert provider.calls == []


def test_legacy_credentials_source_prompts_for_key_migration(tmp_path):
    provider = FakeFeedForgeAPI(tmp_path, token="",
                                source_extra={"username": "tester", "password": "old-secret"})
    with pytest.raises(AuthRequiredError) as excinfo:
        provider.describe_source()
    assert str(excinfo.value) == KEY_MIGRATE_MESSAGE
    assert provider.calls == []


def test_rejected_key_raises_auth_required(tmp_path):
    provider = FakeFeedForgeAPI(tmp_path, token="ffp_wrong_key", catalog=_songs(3))
    with pytest.raises(AuthRequiredError) as excinfo:
        provider.describe_source()
    assert str(excinfo.value) == KEY_REJECTED_MESSAGE


def test_rate_limit_is_retried_once_via_retry_after(tmp_path):
    provider = FakeFeedForgeAPI(tmp_path, catalog=_songs(2), rate_limit_times=1)
    info = provider.describe_source()
    assert info["songCount"] == 2  # the 429 was absorbed by one Retry-After retry


# -------------------------------------------------------------------- mirror walk + refresh


def test_walk_builds_full_mirror_across_cursor_pages(tmp_path):
    provider = FakeFeedForgeAPI(tmp_path, catalog=_songs(7))  # 3 pages of 3 at the test limit

    info = provider.describe_source()

    assert info["ok"] is True
    assert info["songCount"] == 7
    assert info["server"]["protocol"] == "feedforge.v1"
    walk_requests = [u for u in provider.songs_requests() if "sort=newest" in u]
    assert len(walk_requests) == 3  # cursor-walked, one request per page
    assert sum("cursor=" in u for u in walk_requests) == 2


def test_mirror_persists_and_reloads_without_rewalking(tmp_path):
    first = FakeFeedForgeAPI(tmp_path, catalog=_songs(7))
    first.describe_source()
    assert (first.cache_dir / "catalog.json").exists()

    second = FakeFeedForgeAPI(tmp_path, catalog=_songs(7))
    songs, total = second.query_page(size=10)

    assert total == 7 and len(songs) == 7
    # The persisted mirror was loaded; the only network was ONE updatedAfter delta (a loaded
    # mirror counts as stale, which also revalidates the key after a restart) — no re-walk.
    deltas = [u for u in second.songs_requests() if "updatedAfter=" in u]
    walks = [u for u in second.songs_requests() if "sort=newest" in u]
    assert len(deltas) == 1 and len(walks) == 0


def test_delta_merges_new_and_changed_records(tmp_path):
    provider = FakeFeedForgeAPI(tmp_path, catalog=_songs(4))
    provider.describe_source()

    # The walk watermark is the real wall-clock walk start, so delta-visible changes must be
    # stamped *after* it — far-future dates keep the test independent of the actual clock.
    provider.api_catalog.append(
        _api_song(90, createdAt="2036-01-01T00:00:00.000Z", updatedAt="2036-01-01T00:00:00.000Z"))
    provider.api_catalog[0] = {**provider.api_catalog[0],
                               "title": "Renamed", "updatedAt": "2036-01-02T00:00:00.000Z"}
    with provider._mirror_lock:
        provider._synced_at = -10_000  # force the TTL check to see a stale mirror

    _songs_page, total = provider.query_page(size=10)

    assert total == 5
    assert provider._record(provider.api_catalog[0]["id"])["title"] == "Renamed"


def test_unchanged_delta_revalidates_with_etag_304(tmp_path):
    provider = FakeFeedForgeAPI(tmp_path, catalog=_songs(3))
    provider.describe_source()

    with provider._mirror_lock:
        provider._synced_at = -10_000
    provider.query_page(size=5)  # empty 200 delta -> stores the ETag
    with provider._mirror_lock:
        provider._synced_at = -10_000
    provider.query_page(size=5)  # same watermark -> If-None-Match -> 304

    conditional = [(m, u) for m, u in provider.calls
                   if "updatedAfter=" in u]
    assert len(conditional) == 2
    # The fake raises 304 only when If-None-Match matched; reaching here without error and
    # without a third merge proves the revalidation path ran. Mirror is still intact:
    assert provider.query_stats()["total_songs"] == 3


def test_completed_rewalk_drops_ghost_records(tmp_path):
    provider = FakeFeedForgeAPI(tmp_path, catalog=_songs(5))
    provider.describe_source()
    assert provider.query_stats()["total_songs"] == 5

    removed = provider.api_catalog.pop(0)  # deleted on FeedForge; updatedAfter will never say so
    with provider._mirror_lock:
        provider._synced_at = -10_000
        provider._full_walk_wall = 1.0  # long past full_resync_seconds -> re-walk due

    provider.query_page(size=10)

    assert provider._record(removed["id"]) is None
    assert provider.query_stats()["total_songs"] == 4


def test_download_404_drops_the_record(tmp_path):
    provider = FakeFeedForgeAPI(tmp_path, catalog=_songs(3))
    provider.describe_source()
    ghost_id = provider.api_catalog.pop(0)["id"]  # still mirrored, gone upstream

    with pytest.raises(SongGoneError):
        provider._post_download(ghost_id)

    assert provider._record(ghost_id) is None


# ----------------------------------------------------------- local querying over the mirror


def test_query_page_returns_syncable_shape(tmp_path):
    provider = FakeFeedForgeAPI(tmp_path, catalog=[_api_song(5, artist="Zeta Testers", title="Song Bravo",
                                                             album="Fake Album Two")])

    song = provider.query_page(size=3)[0][0]

    assert song["title"] == "Song Bravo"
    assert song["artist"] == "Zeta Testers"
    assert song["album"] == "Fake Album Two"
    assert song["syncSupport"] == "syncable"
    assert song["packageForm"] == "sloppak-zip"
    assert song["capabilities"] == ["package-download"]
    assert song["settingsKey"]
    assert song["localFilename"] == ""
    assert song["sizeBytes"] == 1005  # from the string-typed fileSizeBytes
    assert song["duration"] == 185
    assert song["year"] == 2005


def test_all_core_sorts_are_served_locally(tmp_path):
    catalog = [
        _api_song(0, artist="Alpha", title="Zulu Song", year=1990,
                  createdAt="2026-06-01T00:00:00.000Z"),
        _api_song(1, artist="Mike", title="Alpha Song", year=2020,
                  createdAt="2026-06-03T00:00:00.000Z"),
        _api_song(2, artist="Zeta", title="Mid Song", year=2005,
                  createdAt="2026-06-02T00:00:00.000Z"),
        _api_song(3, artist="Beta", title="Beta Song", year=None,
                  createdAt="2026-06-04T00:00:00.000Z"),
    ]
    provider = FakeFeedForgeAPI(tmp_path, catalog=catalog)

    def artists(sort, direction="asc"):
        songs, _total = provider.query_page(size=10, sort=sort, direction=direction)
        return [s["artist"] for s in songs]

    assert artists("artist") == ["Alpha", "Beta", "Mike", "Zeta"]
    assert artists("artist-desc") == ["Zeta", "Mike", "Beta", "Alpha"]
    titles = [s["title"] for s in provider.query_page(size=10, sort="title-desc")[0]]
    assert titles == ["Zulu Song", "Mid Song", "Beta Song", "Alpha Song"]
    assert artists("recent") == ["Beta", "Mike", "Zeta", "Alpha"]  # newest added first
    # Year-newest first; the record with no year sinks to the end (never pollutes the top).
    years = [s["year"] for s in provider.query_page(size=10, sort="year-desc")[0]]
    assert years == [2020, 2005, 1990, None]


def test_search_is_local_no_query_leaves_the_process(tmp_path):
    catalog = _songs(5) + [_api_song(50, artist="Metallica", title="Special Track")]
    provider = FakeFeedForgeAPI(tmp_path, catalog=catalog)
    provider.describe_source()
    before = len(provider.songs_requests())

    songs, total = provider.query_page(q="metallica", size=10)

    assert total == 1
    assert songs[0]["title"] == "Special Track"
    assert len(provider.songs_requests()) == before  # served from the mirror, zero API calls
    assert not any("q=" in u for u in provider.songs_requests())


def test_stats_letters_and_artists_are_restored(tmp_path):
    catalog = [
        _api_song(0, artist="Alpha Band", album="One"),
        _api_song(1, artist="Alpha Band", album="Two"),
        _api_song(2, artist="Beta Crew"),
        _api_song(3, artist="1st Wonder"),
    ]
    provider = FakeFeedForgeAPI(tmp_path, catalog=catalog)

    stats = provider.query_stats()
    assert stats["total_songs"] == 4
    assert stats["total_artists"] == 3
    assert stats["letters"] == {"A": 1, "B": 1, "#": 1}

    artists, total = provider.query_artists(size=10)
    assert total == 3
    alpha = next(a for a in artists if a["name"] == "Alpha Band")
    assert alpha["song_count"] == 2
    assert alpha["album_count"] == 2
    assert {album["name"] for album in alpha["albums"]} == {"One", "Two"}

    only_a, total_a = provider.query_artists(letter="a", size=10)
    assert total_a == 1 and only_a[0]["name"] == "Alpha Band"


def test_query_page_marks_downloaded_song_as_local(tmp_path):
    local_root = tmp_path / "dlc"
    provider = FakeFeedForgeAPI(tmp_path / "cache",
                                catalog=[_api_song(5, artist="Zeta Testers", title="Song Bravo")],
                                local_library_root=local_root)
    name = "Zeta Testers - Song Bravo.feedpak"
    target = local_root / provider._source_folder_name() / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"downloaded")

    song = provider.query_page(size=3)[0][0]

    relative = f"{provider._source_folder_name()}/{name}"
    assert song["localFilename"] == relative
    assert song["playFilename"] == relative
    assert song["filename"] == relative


def test_partial_mirror_reports_at_least_total(tmp_path):
    provider = FakeFeedForgeAPI(tmp_path, catalog=_songs(4))
    # Simulate mid-walk: some records present, walk not yet complete.
    provider._start_catalog_walk = lambda: None
    with provider._mirror_lock:
        provider._records = {r["id"]: _reduce_record(r) for r in provider.api_catalog[:2]}
        provider._mirror_complete = False
    provider._first_page_event.set()

    _songs_page, total = provider.query_page(size=10)

    assert total > 2  # more is coming; the grid keeps paginating until the walk settles


def test_mirror_completed_by_another_instance_is_adopted(tmp_path):
    """A cold instance whose own walk never ran (or died) must pick up a mirror that another
    instance — an earlier process, or an add-time sibling — completed and persisted, even
    though its own first look found no file yet (the original stuck-at-50 bug)."""
    watcher = FakeFeedForgeAPI(tmp_path, catalog=_songs(7))
    watcher._start_catalog_walk = lambda: None  # this instance never walks
    watcher._first_page_event.set()

    songs, _total = watcher.query_page(size=10)
    assert songs == []  # nothing local yet, and no walk of its own

    sibling = FakeFeedForgeAPI(tmp_path, catalog=_songs(7))
    sibling.describe_source()  # walks (inline) + persists the complete mirror

    songs, total = watcher.query_page(size=10)

    assert watcher._mirror_complete is True
    assert total == 7 and len(songs) == 7  # adopted from disk, no walk of its own


def test_walk_resumes_from_cursor_after_transient_failures(tmp_path):
    """A walk that dies mid-catalog resumes from its checkpointed cursor on the next attempt
    instead of re-fetching from page 1 (re-spending the rate-limit budget)."""
    provider = FakeFeedForgeAPI(
        tmp_path, catalog=_songs(7),
        fail_cursor_pages_times=FeedForgeProvider.walk_page_attempts,  # exhaust page-2 retries
    )

    provider.describe_source()  # first walk attempt: page 1 lands, page 2 keeps failing

    assert provider._mirror_complete is False
    assert provider._walk_cursor  # checkpointed mid-catalog
    with provider._mirror_lock:
        provider._walk_failed_at = 0.0  # skip the cooloff; a real one just waits 60s

    _songs_page, total = provider.query_page(size=10)  # second attempt resumes + completes

    assert provider._mirror_complete is True
    assert total == 7
    first_page_walks = [
        u for _m, u in provider.calls
        if "sort=newest" in u and "cursor=" not in u and "limit=1" not in u
    ]
    assert len(first_page_walks) == 1  # page 1 was never re-fetched


# ------------------------------------------------- honest UA + /me + deletions (v0.7.1)


def test_api_requests_use_the_honest_user_agent(tmp_path):
    provider = FakeFeedForgeAPI(tmp_path, catalog=_songs(3))
    provider.describe_source()

    assert provider.api_user_agents  # /me probe + walk pages
    assert all(ua.startswith("feedback-remote-library-client/") for ua in provider.api_user_agents)
    assert not any("Mozilla" in ua for ua in provider.api_user_agents)  # the masquerade is gone


def test_cloudflare_challenge_fails_with_a_clear_message(tmp_path):
    provider = FakeFeedForgeAPI(tmp_path, catalog=_songs(3), cf_block=True)
    with pytest.raises(RuntimeError, match="report it to the FeedForge dev"):
        provider.describe_source()


def test_me_identity_flows_into_describe(tmp_path):
    provider = FakeFeedForgeAPI(tmp_path, catalog=_songs(3), me_username="melody")

    info = provider.describe_source()

    assert info["accountUsername"] == "melody"
    assert info["sourceName"] == "FeedForge (melody)"
    assert info["keyExpiryWarning"] == ""  # far-future key: no warning


def test_key_expiry_warning_when_within_thirty_days(tmp_path):
    from datetime import datetime, timedelta, timezone

    soon = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat().replace("+00:00", "Z")
    provider = FakeFeedForgeAPI(tmp_path, catalog=_songs(2), me_expires_at=soon)

    info = provider.describe_source()

    assert "expires in 9 day" in info["keyExpiryWarning"] or "expires in 10 day" in info["keyExpiryWarning"]
    assert "Connected apps" in info["keyExpiryWarning"]


def test_deletions_feed_drops_records_on_refresh(tmp_path):
    provider = FakeFeedForgeAPI(tmp_path, catalog=_songs(5))
    provider.describe_source()
    assert provider.query_stats()["total_songs"] == 5

    goner = provider.api_catalog.pop(0)
    provider.deleted.append({"id": goner["id"], "deletedAt": "2036-01-01T00:00:00.000Z"})
    with provider._mirror_lock:
        provider._synced_at = -10_000  # force the TTL check to see a stale mirror

    _songs_page, total = provider.query_page(size=10)

    assert total == 4
    assert provider._record(goner["id"]) is None
    deletion_calls = [u for _m, u in provider.calls if "deletedAfter=" in u]
    assert deletion_calls  # the tombstone feed was actually consulted


def test_deletions_watermark_persists_and_reloads(tmp_path):
    first = FakeFeedForgeAPI(tmp_path, catalog=_songs(3))
    first.describe_source()
    raw = json.loads((first.cache_dir / "catalog.json").read_text(encoding="utf-8"))
    assert raw["deletionsWatermark"]  # set at walk completion, persisted

    second = FakeFeedForgeAPI(tmp_path, catalog=_songs(3))
    second.query_page(size=5)  # loads the mirror, delta+deletions refresh

    assert second._deletions_watermark
    deletion_calls = [u for _m, u in second.calls if "deletedAfter=" in u]
    assert len(deletion_calls) == 1  # the reload refresh polled the feed once


# --------------------------------------------------------------------------- artwork


def test_get_art_fetches_cover_without_the_bearer_key(tmp_path):
    provider = FakeFeedForgeAPI(tmp_path, catalog=[_api_song(1)])
    provider.describe_source()

    response = provider.get_art(_api_song(1)["id"])

    assert response is not None
    assert provider.cover_request_headers is not None
    assert not any(k.lower() == "authorization" for k in provider.cover_request_headers)
    # Second call is served from the art cache (no new cover request).
    covers_before = len([u for _m, u in provider.calls if "/feedpak-covers/" in u])
    assert provider.get_art(_api_song(1)["id"]) is not None
    assert len([u for _m, u in provider.calls if "/feedpak-covers/" in u]) == covers_before


# --------------------------------------------------------------------------- download


def test_do_sync_resolves_drive_link_and_imports(tmp_path):
    local_root = tmp_path / "dlc"
    local_root.mkdir()
    imported = []

    def importer(path, root):
        imported.append(path)
        return {"libraryImportState": "indexed", "libraryFilename": path.relative_to(root).as_posix()}

    provider = FakeFeedForgeAPI(
        tmp_path / "cache",
        catalog=[_api_song(5, artist="Zeta Testers", title="Song Bravo")],
        download_payload={"ok": True, "url": "https://drive.google.com/file/d/FID12345678/view"},
        local_library_root=local_root,
        library_importer=importer,
    )
    provider.describe_source()

    result = provider._do_sync(_api_song(5)["id"])

    assert result["ok"] is True
    assert result["playbackSource"] == "library-folder"
    assert result["libraryImportState"] == "indexed"
    # Imported under the mirror record's deterministic "Artist - Title.feedpak" name.
    assert result["localFilename"].endswith("Zeta Testers - Song Bravo.feedpak")
    assert len(imported) == 1
    assert any(m == "POST" and "/download" in u for m, u in provider.calls)


def test_do_sync_handles_dropbox_url(tmp_path):
    provider = FakeFeedForgeAPI(
        tmp_path / "cache",
        catalog=[_api_song(5)],
        download_payload={"ok": True, "url": "https://www.dropbox.com/scl/fi/x/Song.feedpak?rlkey=k&dl=0"},
    )
    provider.describe_source()

    result = provider._do_sync(_api_song(5)["id"])

    assert result["ok"] is True
    assert result["bytes"] > 0
    assert any("dl=1" in u for _m, u in provider.calls)  # fetched the direct form


def test_do_sync_routes_proton_links_through_the_proton_module(tmp_path, monkeypatch):
    seen = {}

    def fake_download(provider, url, fallback_filename):
        seen["url"] = url
        seen["filename"] = fallback_filename
        target = provider.cache_dir / fallback_filename
        target.write_bytes(b"PK\x03\x04proton")
        return target, "hash", 12

    monkeypatch.setattr(feedforge.proton_drive, "download_share_package", fake_download)
    provider = FakeFeedForgeAPI(
        tmp_path / "cache",
        catalog=[_api_song(5, artist="Zeta Testers", title="Song Bravo")],
        download_payload={"ok": True, "url": "https://drive.proton.me/urls/FAKETOKEN0#pw"},
    )
    provider.describe_source()

    result = provider._do_sync(_api_song(5)["id"])

    assert result["ok"] is True
    assert seen["url"] == "https://drive.proton.me/urls/FAKETOKEN0#pw"
    assert seen["filename"] == "Zeta Testers - Song Bravo.feedpak"


def test_do_sync_proton_without_native_deps_degrades_clearly(tmp_path, monkeypatch):
    def raising(*_args, **_kwargs):
        raise ImportError("No module named 'pysequoia'")

    monkeypatch.setattr(feedforge.proton_drive, "download_share_package", raising)
    provider = FakeFeedForgeAPI(
        tmp_path / "cache",
        catalog=[_api_song(5)],
        download_payload={"ok": True, "url": "https://drive.proton.me/urls/FAKETOKEN0#pw"},
    )
    provider.describe_source()

    with pytest.raises(RuntimeError, match="bcrypt \\+ pysequoia"):
        provider._do_sync(_api_song(5)["id"])


def test_do_sync_routes_mediafire_links_through_the_mediafire_module(tmp_path):
    local_root = tmp_path / "dlc"
    local_root.mkdir()
    provider = FakeFeedForgeAPI(
        tmp_path / "cache",
        catalog=[_api_song(5, artist="Zeta Testers", title="Song Bravo")],
        download_payload={"ok": True,
                          "url": "https://www.mediafire.com/file/fakekey01234567/Song.feedpak/file"},
        local_library_root=local_root,
        library_importer=lambda path, root: {"libraryImportState": "indexed",
                                             "libraryFilename": path.relative_to(root).as_posix()},
    )
    provider.describe_source()

    result = provider._do_sync(_api_song(5)["id"])

    assert result["ok"] is True
    # Imported under the deterministic mirror-record name, not the CDN's filename.
    assert result["localFilename"].endswith("Zeta Testers - Song Bravo.feedpak")
    urls = [u for _m, u in provider.calls]
    assert any(u.startswith("https://www.mediafire.com/file/") for u in urls)  # the share page
    assert any(u.startswith("https://download9.mediafire.com/") for u in urls)  # the scraped direct URL


def test_do_sync_unsupported_host_fails_loudly_without_fetching(tmp_path):
    provider = FakeFeedForgeAPI(
        tmp_path / "cache", catalog=[_api_song(5)],
        download_payload={"ok": True, "url": "https://mega.nz/file/abc123#key"},
    )
    provider.describe_source()

    with pytest.raises(RuntimeError, match="mega.nz.*does not support"):
        provider._do_sync(_api_song(5)["id"])

    assert not any("mega.nz" in u for _m, u in provider.calls)  # refused before any fetch


def test_do_sync_direct_package_link_on_unknown_host_still_streams(tmp_path):
    # A URL whose path plainly names a package is downloadable from any host — the
    # unsupported-host guard must not regress the catalog's direct-link tail.
    provider = FakeFeedForgeAPI(
        tmp_path / "cache", catalog=[_api_song(5)],
        download_payload={"ok": True, "url": "https://files.example.com/paks/Fake%20Song.feedpak"},
    )
    provider.describe_source()

    result = provider._do_sync(_api_song(5)["id"])

    assert result["ok"] is True
    assert result["bytes"] > 0


def test_do_sync_html_where_package_expected_fails_loudly(tmp_path):
    provider = FakeFeedForgeAPI(
        tmp_path / "cache", catalog=[_api_song(5)],
        download_payload={"ok": True, "url": "https://htmlpage.example.com/Fake.feedpak"},
    )
    provider.describe_source()

    with pytest.raises(RuntimeError, match="web page instead of a package"):
        provider._do_sync(_api_song(5)["id"])


def test_do_sync_raises_when_no_download_url(tmp_path):
    provider = FakeFeedForgeAPI(tmp_path / "cache", catalog=[_api_song(5)], download_payload={"ok": False})
    provider.describe_source()
    with pytest.raises(RuntimeError, match="download link"):
        provider._do_sync(_api_song(5)["id"])


def test_sync_song_is_non_blocking_then_plays(tmp_path):
    local_root = tmp_path / "dlc"
    local_root.mkdir()
    provider = FakeFeedForgeAPI(
        tmp_path / "cache",
        catalog=[_api_song(5, artist="Zeta Testers", title="Song Bravo")],
        download_payload={"ok": True, "url": "https://drive.google.com/file/d/FID12345678/view"},
        local_library_root=local_root,
        library_importer=lambda path, root: {"libraryImportState": "indexed"},
    )
    provider.describe_source()
    provider._start_background_sync = provider._background_sync  # run inline, deterministically

    first = provider.sync_song(_api_song(5)["id"])
    assert first["cacheState"] == "downloading"
    assert "filename" not in first

    second = provider.sync_song(_api_song(5)["id"])
    assert second["playbackSource"] == "library-folder"
    assert second["localFilename"].endswith("Zeta Testers - Song Bravo.feedpak")


def test_active_downloads_reports_downloading_then_ready(tmp_path):
    local_root = tmp_path / "dlc"
    local_root.mkdir()
    provider = FakeFeedForgeAPI(
        tmp_path / "cache",
        catalog=[_api_song(5, artist="Zeta Testers", title="Song Bravo")],
        download_payload={"ok": True, "url": "https://drive.google.com/file/d/FID12345678/view"},
        local_library_root=local_root,
        library_importer=lambda path, root: {"libraryImportState": "indexed"},
    )
    provider.describe_source()
    provider._start_background_sync = lambda song_id: None  # hold in the downloading state

    provider.sync_song(_api_song(5)["id"])
    downloading = provider.active_downloads()
    assert downloading[0]["status"] == "downloading"
    assert downloading[0]["providerId"] == provider.id
    assert downloading[0]["title"] == "Zeta Testers – Song Bravo"

    provider._background_sync(_api_song(5)["id"])
    ready = provider.active_downloads()
    assert ready[0]["status"] == "ready"
    assert ready[0]["localFilename"].endswith("Zeta Testers - Song Bravo.feedpak")


# ------------------------------------------------------------------------- route wiring


def _stub_api(monkeypatch, *, catalog=None, raise_auth=False, me_username="stubuser",
              me_expires_at="2036-01-01T00:00:00.000Z"):
    catalog = catalog if catalog is not None else _songs(7)
    api_calls: list[dict] = []

    def fake_api_get(self, path, params=None, *, etag="", timeout=30.0, retry_after_cap=None):
        api_calls.append(dict(params or {}))
        if raise_auth:
            raise AuthRequiredError(KEY_REJECTED_MESSAGE)
        if path.endswith("/me"):
            return {
                "ok": True,
                "data": {"user": {"username": me_username, "displayName": me_username},
                         "token": {"scopes": [], "expiresAt": me_expires_at,
                                   "lastUsedAt": "2026-07-18T00:00:00.000Z"}},
            }, 'W/"me"'
        if path.endswith("/deletions"):
            return {"ok": True, "data": [],
                    "pagination": {"limit": 50, "nextCursor": None, "hasMore": False}}, 'W/"del"'
        params = params or {}
        rows = sorted(catalog, key=lambda r: r["createdAt"], reverse=True)
        if params.get("updatedAfter"):
            rows = [r for r in rows if r["updatedAt"] > params["updatedAfter"]]
        limit = int(params.get("limit") or 50)
        start = 0
        cursor = params.get("cursor") or ""
        if cursor:
            ids = [r["id"] for r in rows]
            start = ids.index(cursor) + 1 if cursor in ids else len(rows)
        page = rows[start:start + limit]
        has_more = start + limit < len(rows)
        return {
            "ok": True,
            "data": page,
            "pagination": {"limit": limit,
                           "nextCursor": page[-1]["id"] if (page and has_more) else None,
                           "hasMore": has_more},
        }, 'W/"stub"'

    monkeypatch.setattr(FeedForgeProvider, "_api_get", fake_api_get)
    monkeypatch.setattr(FeedForgeProvider, "_start_catalog_walk", FeedForgeProvider._walk_catalog)
    return api_calls


def _setup_routes(tmp_path, registered=None):
    routes = importlib.reload(importlib.import_module("routes"))
    app = FastAPI()
    routes.setup(app, {
        "config_dir": tmp_path / "config",
        "register_library_provider": (
            (lambda provider, replace=False: registered.setdefault(provider.id, provider))
            if registered is not None else (lambda provider, replace=False: None)
        ),
        "get_sloppak_cache_dir": lambda: tmp_path / "cache",
        "get_dlc_dir": lambda: None,
    })
    return routes, TestClient(app)


def test_add_feedforge_source_with_key_registers_and_hides_it(tmp_path, monkeypatch):
    _stub_api(monkeypatch)
    registered = {}
    _routes, client = _setup_routes(tmp_path, registered)

    added = client.post("/api/plugins/remote_library_client/sources", json={
        "type": "feedforge.v1", "token": FAKE_KEY, "label": "My FeedForge",
    })
    status = client.get("/api/plugins/remote_library_client/status")

    assert added.status_code == 200
    source = added.json()["source"]
    assert source["type"] == "feedforge.v1"
    assert source["songCount"] == 7
    assert "token" not in source  # the key is a secret and never surfaces
    assert source["hasToken"] is True
    assert source["accountSeed"]
    provider_id = added.json()["provider"]["id"]
    assert provider_id.startswith("feedforge:")
    assert provider_id in registered
    feedforge_status = [item for item in status.json()["sources"] if item.get("type") == "feedforge.v1"]
    assert feedforge_status and feedforge_status[0]["online"] is True
    assert "token" not in feedforge_status[0]


def test_add_registers_the_instance_that_walked(tmp_path, monkeypatch):
    """The provider registered for core browsing must be the SAME instance whose describe
    walked the catalog — a second cold instance is exactly the stuck-at-50 split-brain bug."""
    api_calls = _stub_api(monkeypatch)
    registered = {}
    _routes, client = _setup_routes(tmp_path, registered)

    added = client.post("/api/plugins/remote_library_client/sources", json={
        "type": "feedforge.v1", "token": FAKE_KEY,
    })
    provider = registered[added.json()["provider"]["id"]]

    assert provider._mirror_complete is True  # the registered instance holds the walked mirror
    calls_before = len(api_calls)
    songs, total = provider.query_page(size=10)
    assert total == 7 and len(songs) == 7
    assert len(api_calls) == calls_before  # served from its own mirror — no cold re-walk


def test_status_reports_syncing_while_walk_incomplete(tmp_path, monkeypatch):
    _stub_api(monkeypatch)
    # Freeze the walk before it starts: the mirror stays incomplete, as mid-walk in real life.
    monkeypatch.setattr(FeedForgeProvider, "_start_catalog_walk", lambda self: None)
    monkeypatch.setattr(FeedForgeProvider, "describe_wait_seconds", 0.0)
    _routes, client = _setup_routes(tmp_path, registered={})

    added = client.post("/api/plugins/remote_library_client/sources", json={
        "type": "feedforge.v1", "token": FAKE_KEY,
    })
    status = client.get("/api/plugins/remote_library_client/status").json()["sources"]

    assert added.status_code == 200
    card = next(item for item in status if item.get("type") == "feedforge.v1")
    assert "Syncing" in card["message"]
    assert card["online"] is True


def test_default_labels_track_the_account_identity(tmp_path, monkeypatch):
    """A default label upgrades to "FeedForge (username)" once /me exposes the identity; a
    label the user typed is never touched."""
    _stub_api(monkeypatch)
    routes, client = _setup_routes(tmp_path, registered={})
    routes._store.upsert_source({
        "type": "feedforge.v1", "providerId": "feedforge:one:aaa", "baseUrl": "https://feedforge.org",
        "token": FAKE_KEY, "accountSeed": "aaaa1111", "label": "FeedForge", "enabled": True,
    })
    routes._store.upsert_source({
        "type": "feedforge.v1", "providerId": "feedforge:two:bbb", "baseUrl": "https://feedforge.org",
        "token": FAKE_KEY, "accountSeed": "bbbb2222", "label": "My Custom Name", "enabled": True,
    })

    status = client.get("/api/plugins/remote_library_client/status").json()["sources"]

    by_id = {item.get("providerId"): item for item in status}
    assert by_id["feedforge:one:aaa"]["label"] == "FeedForge (stubuser)"  # default upgraded
    assert by_id["feedforge:one:aaa"]["username"] == "stubuser"
    assert by_id["feedforge:two:bbb"]["label"] == "My Custom Name"  # custom preserved


def test_status_shows_key_expiry_warning_after_sync(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    soon = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat().replace("+00:00", "Z")
    _stub_api(monkeypatch, me_expires_at=soon)
    _routes, client = _setup_routes(tmp_path, registered={})

    client.post("/api/plugins/remote_library_client/sources", json={
        "type": "feedforge.v1", "token": FAKE_KEY,
    })
    status = client.get("/api/plugins/remote_library_client/status").json()["sources"]

    card = next(item for item in status if item.get("type") == "feedforge.v1")
    assert "expires" in card["message"]  # walk complete, so the expiry warning gets the slot
    assert card["online"] is True


def test_add_feedforge_without_key_is_401_with_guidance(tmp_path, monkeypatch):
    _stub_api(monkeypatch)
    _routes, client = _setup_routes(tmp_path)

    added = client.post("/api/plugins/remote_library_client/sources", json={"type": "feedforge.v1"})

    assert added.status_code == 401
    assert "access key" in added.json()["detail"]


def test_add_feedforge_rejected_key_is_401(tmp_path, monkeypatch):
    _stub_api(monkeypatch, raise_auth=True)
    _routes, client = _setup_routes(tmp_path)

    added = client.post("/api/plugins/remote_library_client/sources", json={
        "type": "feedforge.v1", "token": "ffp_bad",
    })

    assert added.status_code == 401


def test_legacy_credentials_source_migrates_to_a_key(tmp_path, monkeypatch):
    """A stored pre-API source (username/password) prompts for a key on status, keeps its
    providerId through the PATCH that supplies one, and sheds the obsolete password."""
    _stub_api(monkeypatch)
    routes, client = _setup_routes(tmp_path, registered={})
    legacy = {
        "type": "feedforge.v1",
        "providerId": "feedforge:legacy:abc123",
        "baseUrl": "https://feedforge.org",
        "username": "tester",
        "password": "old-secret",
        "label": "My FeedForge",
        "enabled": True,
    }
    routes._store.upsert_source(legacy)

    status = client.get("/api/plugins/remote_library_client/status").json()["sources"]
    card = next(item for item in status if item.get("providerId") == "feedforge:legacy:abc123")
    assert card["authRequired"] is True
    assert "access keys" in card["message"]
    assert "password" not in card and "token" not in card

    patched = client.patch(
        "/api/plugins/remote_library_client/sources/feedforge%3Alegacy%3Aabc123",
        json={"token": FAKE_KEY},
    )
    assert patched.status_code == 200

    status = client.get("/api/plugins/remote_library_client/status").json()["sources"]
    card = next(item for item in status if item.get("providerId") == "feedforge:legacy:abc123")
    assert card["online"] is True
    assert card["hasToken"] is True
    assert card["songCount"] == 7
    stored = next(item for item in routes._store.list_sources()
                  if item.get("providerId") == "feedforge:legacy:abc123")
    assert "password" not in stored  # the obsolete secret is dropped at migration
    # The key's account (from /me) is authoritative over the legacy stored username — the
    # pasted key may even belong to a different account than the old login did.
    assert stored["username"] == "stubuser"
    assert stored["label"] == "My FeedForge"  # the user-typed label is never touched
