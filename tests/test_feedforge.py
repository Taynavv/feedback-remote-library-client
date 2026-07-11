from __future__ import annotations

import importlib
import json
import sys
from http.cookiejar import Cookie
from pathlib import Path
from urllib import parse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import remote_library_client.feedforge as feedforge
from remote_library_client.feedforge import (
    FeedForgeProvider,
    _direct_download_url,
    is_feedforge_url,
    normalize_feedforge_base_url,
    parse_library_html,
)
from remote_library_client.google_drive import drive_file_id_from_url, is_google_drive_file_url
from remote_library_client.provider import AuthRequiredError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# Shrink FeedForge's page size (25 in production) to 3 for every test, so multi-page lazy
# mapping is exercised with tiny synthetic catalogs.
@pytest.fixture(autouse=True)
def _small_pages(monkeypatch):
    monkeypatch.setattr(feedforge, "_PAGE_SIZE", 3)


# Synthetic, content-free fixtures mirroring the real feedforge.org /library *table* (verified
# live 2026-07-10): each song is a <tr> with facet links (?artist= / ?album= / ?tuning= / ?year=,
# the param appearing after ?page=N) and a plain <td> duration. Obviously-fake ids/names — never
# a real song or account.
FAKE_CARDS = [
    # (song_id, title, artist, album, tuning, year, duration)
    ("cfake000000000000000000b", "Song Bravo", "Zeta Testers", "Fake Album Two", "Drop D", "2021", "3:45"),
    ("cfake000000000000000000a", "Song Alpha", "Alpha Testers", "Fake Album One", "E Standard", "2019", "2:58"),
    ("cfake000000000000000000c", "Song &amp; Charlie", "Alpha Testers", "", "", "", ""),
]


def _cards(n):
    """n obviously-fake card tuples, for exercising multi-page pagination."""
    return [
        (f"cfake{i:019d}", f"Song {i:03d}", f"Artist {i % 4}", f"Album {i % 2}", "E Standard", str(2000 + i), "3:00")
        for i in range(n)
    ]


def _facet(param, value):
    if not value:
        return '<span class="muted">-</span>'
    return f'<a class="linked-cell" href="/library?page=1&amp;{param}={parse.quote_plus(value)}">{value}</a>'


def _row_html(song_id, title, artist, album, tuning, year, duration):
    return (
        "<tr>"
        '<td><span class="download-button-wrap"><button class="download-icon-button"></button></span></td>'
        f'<td><a class="cover-thumb" href="/songs/{song_id}"><img src="/feedpak-covers/{song_id}cover?dpl=x"/></a></td>'
        f'<td>{_facet("artist", artist)}</td>'
        f'<td><a class="song-title" href="/songs/{song_id}">{title}</a></td>'
        f'<td class="album-cell">{_facet("album", album)}</td>'
        f'<td>{_facet("tuning", tuning)}</td>'
        f'<td>{_facet("year", year)}</td>'
        f"<td>{duration or '-'}</td>"
        "</tr>"
    )


def _library_html(cards):
    header = (
        "<thead><tr><th>Art</th><th>Artist</th><th>Title</th><th>Album</th>"
        "<th>Tuning</th><th>Year</th><th>Duration</th></tr></thead>"
    )
    body = "".join(_row_html(*card) for card in cards)
    return f'<html><body><table class="library-table">{header}<tbody>{body}</tbody></table></body></html>'


def _session_cookie():
    return Cookie(
        version=0, name="__Secure-next-auth.session-token", value="fake-session-token",
        port=None, port_specified=False, domain="feedforge.org", domain_specified=True,
        domain_initial_dot=False, path="/", path_specified=True, secure=True, expires=None,
        discard=False, comment=None, comment_url=None, rest={}, rfc2109=False,
    )


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


class FakeFeedForge(FeedForgeProvider):
    """FeedForge provider with the network stubbed: NextAuth handshake, a paginated + searchable
    /library table served from an in-memory `catalog`, download-resolve JSON, and file bytes."""

    def __init__(self, cache_dir, *, catalog=None, session_valid=True, download_payload=None,
                 drive_bytes=b"PK\x03\x04fake-package", logout_first=False, **kwargs):
        super().__init__(
            {"baseUrl": "https://feedforge.org", "username": "tester", "password": "pw", "label": "Fake FeedForge"},
            cache_dir, **kwargs,
        )
        self.empty_page_backoff_seconds = 0  # no real sleeping in tests
        self._catalog = list(catalog or [])
        self._session_valid = session_valid
        self._download_payload = download_payload if download_payload is not None else {}
        self._drive_bytes = drive_bytes
        self._logout_first = logout_first
        self._authed = False
        self._library_calls = 0
        self.calls: list[tuple[str, str]] = []

    def _urlopen(self, req, timeout):
        url = req.full_url
        method = req.get_method()
        path = url[len(self.base_url):] if url.startswith(self.base_url) else url
        self.calls.append((method, path))
        if path.startswith("/api/auth/csrf"):
            return _Resp(json.dumps({"csrfToken": "fake-csrf"}).encode(), {"content-type": "application/json"}, url)
        if path.startswith("/api/auth/callback/credentials"):
            if self._session_valid:
                self._cookies.set_cookie(_session_cookie())
                self._authed = True
            return _Resp(json.dumps({"url": self.base_url}).encode(), {"content-type": "application/json"}, url)
        if path.startswith("/api/auth/session"):
            body = {"user": {"name": "tester"}} if self._authed else {}
            return _Resp(json.dumps(body).encode(), {"content-type": "application/json"}, url)
        if path.startswith("/library"):
            self._library_calls += 1
            if self._logout_first and self._library_calls == 1:
                return _Resp(b"<html>please log in</html>", {"content-type": "text/html"}, self.base_url + "/login")
            qs = parse.parse_qs(parse.urlparse(url).query)
            pagenum = int((qs.get("page") or ["1"])[0])
            q = (qs.get("q") or [""])[0].strip().lower()
            rows = self._catalog
            if q:
                rows = [r for r in rows if q in r[1].lower() or q in r[2].lower()]  # title / artist
            size = feedforge._PAGE_SIZE
            start = (pagenum - 1) * size
            return _Resp(_library_html(rows[start:start + size]).encode(), {"content-type": "text/html"}, url)
        if "/download" in path and method == "POST":
            return _Resp(json.dumps(self._download_payload).encode(), {"content-type": "application/json"}, url)
        # External CDN (Google Drive or Dropbox): stream file bytes.
        return _Resp(
            [self._drive_bytes],
            {"content-type": "application/octet-stream", "content-disposition": 'attachment; filename="song.feedpak"'},
            url,
        )

    def library_pages_fetched(self):
        return sorted({int(path.split("page=")[1].split("&")[0])
                       for _m, path in self.calls if path.startswith("/library")})


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


def test_parse_library_html_extracts_card_fields():
    cards = parse_library_html(_library_html(FAKE_CARDS))

    assert [card["song_id"] for card in cards] == [
        "cfake000000000000000000b", "cfake000000000000000000a", "cfake000000000000000000c",
    ]
    bravo = cards[0]
    assert bravo["title"] == "Song Bravo"
    assert bravo["artist"] == "Zeta Testers"
    assert bravo["album"] == "Fake Album Two"
    assert bravo["tuning"] == "Drop D"
    assert bravo["year"] == 2021
    assert bravo["duration"] == 3 * 60 + 45  # "3:45" -> seconds
    # HTML entity unescaped; missing facets (artist present, album/tuning absent) degrade cleanly.
    assert cards[2]["title"] == "Song & Charlie"
    assert cards[2]["artist"] == "Alpha Testers"
    assert cards[2]["album"] == ""
    assert cards[2]["duration"] is None


def test_parse_library_html_skips_rows_without_song_title():
    html = "<table><tbody><tr><td>not a song row</td></tr>" + _row_html(*FAKE_CARDS[0]) + "</tbody></table>"
    cards = parse_library_html(html)
    assert len(cards) == 1
    assert cards[0]["song_id"] == "cfake000000000000000000b"


# ------------------------------------------------------------------------------ login


def test_login_drives_nextauth_handshake_and_sets_session(tmp_path):
    provider = FakeFeedForge(tmp_path)
    assert not provider._has_session()

    provider._login()

    assert provider._has_session()
    called = [path for _method, path in provider.calls]
    assert any(p.startswith("/api/auth/csrf") for p in called)
    assert any(p.startswith("/api/auth/callback/credentials") for p in called)
    assert any(p.startswith("/api/auth/session") for p in called)


def test_login_rejects_bad_credentials(tmp_path):
    provider = FakeFeedForge(tmp_path, session_valid=False)
    with pytest.raises(AuthRequiredError):
        provider._login()


def test_missing_credentials_raise_before_any_request(tmp_path):
    provider = FakeFeedForge(tmp_path)
    provider.username = ""
    with pytest.raises(AuthRequiredError):
        provider._login()


# -------------------------------------------------------------------- lazy query_page


def test_query_page_returns_syncable_shape(tmp_path):
    provider = FakeFeedForge(tmp_path, catalog=[FAKE_CARDS[0]])

    song = provider.query_page(size=3)[0][0]

    assert song["title"] == "Song Bravo"
    assert song["artist"] == "Zeta Testers"
    assert song["album"] == "Fake Album Two"
    assert song["syncSupport"] == "syncable"
    assert song["packageForm"] == "sloppak-zip"
    assert song["capabilities"] == ["package-download"]
    assert song["settingsKey"]
    assert song["localFilename"] == ""


def test_query_page_is_lazy_only_fetches_the_viewed_window(tmp_path):
    # 30-song catalog (10 pages of 3); viewing page 0 must NOT scrape the whole catalog.
    provider = FakeFeedForge(tmp_path, catalog=_cards(30))

    songs, _total = provider.query_page(page=0, size=3)

    assert [s["title"] for s in songs] == ["Song 000", "Song 001", "Song 002"]
    assert provider.library_pages_fetched() == [1]  # only FeedForge page 1 fetched


def test_query_page_maps_window_across_ff_pages(tmp_path):
    # A core page/size that straddles two FeedForge pages fetches exactly those two.
    provider = FakeFeedForge(tmp_path, catalog=_cards(30))

    songs, _total = provider.query_page(page=1, size=4)  # songs [4..8) -> ff pages 2 and 3

    assert [s["title"] for s in songs] == ["Song 004", "Song 005", "Song 006", "Song 007"]
    assert provider.library_pages_fetched() == [2, 3]


def test_query_page_paginates_the_whole_catalog(tmp_path):
    provider = FakeFeedForge(tmp_path, catalog=_cards(7))
    seen = []
    page = 0
    while True:
        songs, _total = provider.query_page(page=page, size=3)
        if not songs:
            break
        seen.extend(s["song_id"] for s in songs)
        page += 1

    assert len(seen) == 7 and len(set(seen)) == 7  # every song, no dupes


def test_query_page_search_passes_q_through_server_side(tmp_path):
    catalog = _cards(9) + [("cfakeZZZ", "Special Track", "Metallica", "Ride", "Drop D", "1984", "6:37")]
    provider = FakeFeedForge(tmp_path, catalog=catalog)

    songs, _total = provider.query_page(q="metallica", size=3)

    assert [s["title"] for s in songs] == ["Special Track"]
    # The request carried ?q=; the fake filtered server-side (we didn't scrape + filter locally).
    assert any("q=metallica" in path for _m, path in provider.calls)


def test_query_page_total_settles_at_the_end(tmp_path):
    provider = FakeFeedForge(tmp_path, catalog=_cards(7))

    _first, total_first = provider.query_page(page=0, size=3)
    last_songs, total_last = provider.query_page(page=2, size=3)  # songs [6..9) -> only 1 left

    assert total_first > 3           # at-least estimate signals a next page while more remain
    assert len(last_songs) == 1
    assert total_last == 7           # settles to the exact count at the end


def test_query_page_marks_downloaded_song_as_local(tmp_path):
    local_root = tmp_path / "dlc"
    provider = FakeFeedForge(tmp_path / "cache", catalog=[FAKE_CARDS[0]], local_library_root=local_root)
    name = "Zeta Testers - Song Bravo.feedpak"
    target = local_root / provider._source_folder_name() / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"downloaded")

    song = provider.query_page(size=3)[0][0]

    relative = f"{provider._source_folder_name()}/{name}"
    assert song["localFilename"] == relative
    assert song["playFilename"] == relative
    assert song["filename"] == relative


def test_page_fetch_is_cached(tmp_path):
    provider = FakeFeedForge(tmp_path, catalog=_cards(3))
    provider.query_page(size=3)
    provider.query_page(size=3)  # same window again

    assert provider.library_pages_fetched() == [1]  # served from cache the second time


def test_fetch_page_retries_a_transient_empty_page(tmp_path):
    # A page that momentarily returns empty must be retried, not treated as the end.
    provider = FakeFeedForge(tmp_path, catalog=_cards(3))
    seq = iter(["", "", _library_html(_cards(3))])  # empty, empty, then real content
    provider._authed_html = lambda _path: next(seq)

    cards = provider._fetch_page_cards(1)
    assert len(cards) == 3


def test_authed_html_relogs_in_on_logout_redirect(tmp_path):
    provider = FakeFeedForge(tmp_path, catalog=[FAKE_CARDS[0]], logout_first=True)

    songs, _total = provider.query_page(size=3)

    assert [s["title"] for s in songs] == ["Song Bravo"]  # bounced to /login, re-logged-in, succeeded
    logins = [p for _m, p in provider.calls if p.startswith("/api/auth/callback")]
    assert len(logins) >= 2


# --------------------------------------------------- catalog total + degraded aggregates


def test_catalog_total_binary_searches_the_size(tmp_path):
    provider = FakeFeedForge(tmp_path, catalog=_cards(7))  # 3 pages: 3 + 3 + 1

    assert provider._catalog_total() == 7
    # Bounded probing (exponential + binary search), not a page-by-page scrape of all 3+ pages.
    assert len(provider.library_pages_fetched()) <= 5


def test_catalog_total_zero_for_empty_catalog(tmp_path):
    provider = FakeFeedForge(tmp_path, catalog=[])
    assert provider._catalog_total() == 0


def test_describe_source_reports_type_and_total(tmp_path):
    provider = FakeFeedForge(tmp_path, catalog=_cards(7))

    info = provider.describe_source()

    assert info["ok"] is True
    assert info["songCount"] == 7
    assert info["server"]["protocol"] == "feedforge.v1"


def test_query_stats_reports_total_and_degrades_letters(tmp_path):
    provider = FakeFeedForge(tmp_path, catalog=_cards(7))

    stats = provider.query_stats()

    assert stats["total_songs"] == 7
    assert stats["letters"] == {}          # A-Z rail degraded (would need the full catalog)
    assert stats["total_artists"] == 0


def test_query_artists_is_empty(tmp_path):
    provider = FakeFeedForge(tmp_path, catalog=_cards(7))
    assert provider.query_artists() == ([], 0)


# --------------------------------------------------------------------------- download


def _browse(provider):
    # Populate the card cache the way a real click does (browse, then sync).
    provider.query_page(size=50)


def test_do_sync_resolves_drive_link_and_imports(tmp_path):
    local_root = tmp_path / "dlc"
    local_root.mkdir()
    imported = []

    def importer(path, root):
        imported.append(path)
        return {"libraryImportState": "indexed", "libraryFilename": path.relative_to(root).as_posix()}

    provider = FakeFeedForge(
        tmp_path / "cache",
        catalog=[FAKE_CARDS[0]],
        download_payload={"ok": True, "url": "https://drive.google.com/file/d/FID12345678/view"},
        local_library_root=local_root,
        library_importer=importer,
    )
    _browse(provider)

    result = provider._do_sync("cfake000000000000000000b")

    assert result["ok"] is True
    assert result["playbackSource"] == "library-folder"
    assert result["libraryImportState"] == "indexed"
    # Imported under the browsed card's deterministic "Artist - Title.feedpak" name.
    assert result["localFilename"].endswith("Zeta Testers - Song Bravo.feedpak")
    assert len(imported) == 1
    assert any(method == "POST" and "/download" in path for method, path in provider.calls)


def test_do_sync_handles_dropbox_url(tmp_path):
    provider = FakeFeedForge(
        tmp_path / "cache",
        catalog=[FAKE_CARDS[0]],
        download_payload={"ok": True, "url": "https://www.dropbox.com/scl/fi/x/Song.feedpak?rlkey=k&dl=0"},
    )
    _browse(provider)

    result = provider._do_sync("cfake000000000000000000b")

    assert result["ok"] is True
    assert result["bytes"] > 0
    assert any("dl=1" in path for _method, path in provider.calls)  # fetched the direct form


def test_do_sync_names_file_from_url_when_not_browsed(tmp_path):
    # Syncing a song that wasn't browsed (no cached card) still imports under a meaningful name
    # derived from the resolved download URL.
    local_root = tmp_path / "dlc"
    local_root.mkdir()
    provider = FakeFeedForge(
        tmp_path / "cache",
        catalog=[],
        download_payload={"ok": True, "url": "https://www.dropbox.com/scl/fi/x/Cool-Band-Cool-Song.feedpak?dl=0"},
        local_library_root=local_root,
        library_importer=lambda path, root: {"libraryImportState": "indexed"},
    )

    result = provider._do_sync("some-unbrowsed-id")

    assert result["localFilename"].endswith("Cool-Band-Cool-Song.feedpak")


def test_do_sync_raises_when_no_download_url(tmp_path):
    provider = FakeFeedForge(tmp_path / "cache", catalog=[FAKE_CARDS[0]], download_payload={"ok": False})
    _browse(provider)
    with pytest.raises(RuntimeError, match="download link"):
        provider._do_sync("cfake000000000000000000b")


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


def test_sync_song_is_non_blocking_then_plays(tmp_path):
    local_root = tmp_path / "dlc"
    local_root.mkdir()
    provider = FakeFeedForge(
        tmp_path / "cache",
        catalog=[FAKE_CARDS[0]],
        download_payload={"ok": True, "url": "https://drive.google.com/file/d/FID12345678/view"},
        local_library_root=local_root,
        library_importer=lambda path, root: {"libraryImportState": "indexed"},
    )
    _browse(provider)
    provider._start_background_sync = provider._background_sync  # run inline, deterministically

    first = provider.sync_song("cfake000000000000000000b")
    assert first["cacheState"] == "downloading"
    assert "filename" not in first

    second = provider.sync_song("cfake000000000000000000b")
    assert second["playbackSource"] == "library-folder"
    assert second["localFilename"].endswith("Zeta Testers - Song Bravo.feedpak")


def test_active_downloads_reports_downloading_then_ready(tmp_path):
    local_root = tmp_path / "dlc"
    local_root.mkdir()
    provider = FakeFeedForge(
        tmp_path / "cache",
        catalog=[FAKE_CARDS[0]],
        download_payload={"ok": True, "url": "https://drive.google.com/file/d/FID12345678/view"},
        local_library_root=local_root,
        library_importer=lambda path, root: {"libraryImportState": "indexed"},
    )
    _browse(provider)
    provider._start_background_sync = lambda song_id: None  # hold in the downloading state

    provider.sync_song("cfake000000000000000000b")
    downloading = provider.active_downloads()
    assert downloading[0]["status"] == "downloading"
    assert downloading[0]["providerId"] == provider.id
    assert downloading[0]["title"]

    provider._background_sync("cfake000000000000000000b")
    ready = provider.active_downloads()
    assert ready[0]["status"] == "ready"
    assert ready[0]["localFilename"].endswith("Zeta Testers - Song Bravo.feedpak")


# ------------------------------------------------------------------------- route wiring


def _stub_network(monkeypatch, *, catalog=None, raise_auth=False):
    catalog = catalog if catalog is not None else _cards(7)

    def fake_authed_html(self, path):
        if raise_auth:
            raise AuthRequiredError("FeedForge rejected the username or password")
        qs = parse.parse_qs(parse.urlparse(path).query)
        pagenum = int((qs.get("page") or ["1"])[0])
        size = feedforge._PAGE_SIZE
        return _library_html(catalog[(pagenum - 1) * size:pagenum * size])

    monkeypatch.setattr(FeedForgeProvider, "_ensure_session", lambda self: None)
    monkeypatch.setattr(FeedForgeProvider, "_authed_html", fake_authed_html)
    monkeypatch.setattr(FeedForgeProvider, "empty_page_backoff_seconds", 0)


def test_add_feedforge_source_registers_and_hides_password(tmp_path, monkeypatch):
    routes = importlib.reload(importlib.import_module("routes"))
    _stub_network(monkeypatch)
    registered = {}
    app = FastAPI()
    routes.setup(app, {
        "config_dir": tmp_path / "config",
        "register_library_provider": lambda provider, replace=False: registered.setdefault(provider.id, provider),
        "get_sloppak_cache_dir": lambda: tmp_path / "cache",
        "get_dlc_dir": lambda: None,
    })
    client = TestClient(app)

    added = client.post("/api/plugins/remote_library_client/sources", json={
        "type": "feedforge.v1", "username": "tester", "password": "s3cret", "label": "My FeedForge",
    })
    status = client.get("/api/plugins/remote_library_client/status")

    assert added.status_code == 200
    source = added.json()["source"]
    assert source["type"] == "feedforge.v1"
    assert source["songCount"] == 7
    assert source["username"] == "tester"
    assert "password" not in source  # the secret never surfaces
    assert source["hasPassword"] is True
    provider_id = added.json()["provider"]["id"]
    assert provider_id.startswith("feedforge:")
    assert provider_id in registered
    feedforge_status = [item for item in status.json()["sources"] if item.get("type") == "feedforge.v1"]
    assert feedforge_status and feedforge_status[0]["online"] is True
    assert "password" not in feedforge_status[0]


def test_add_feedforge_rejects_bad_credentials_with_401(tmp_path, monkeypatch):
    routes = importlib.reload(importlib.import_module("routes"))
    _stub_network(monkeypatch, raise_auth=True)
    app = FastAPI()
    routes.setup(app, {
        "config_dir": tmp_path / "config",
        "register_library_provider": lambda provider, replace=False: None,
        "get_sloppak_cache_dir": lambda: tmp_path / "cache",
        "get_dlc_dir": lambda: None,
    })
    client = TestClient(app)

    added = client.post("/api/plugins/remote_library_client/sources", json={
        "type": "feedforge.v1", "username": "tester", "password": "wrong",
    })

    assert added.status_code == 401
