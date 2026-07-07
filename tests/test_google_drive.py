from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from remote_library_client.google_drive import (
    GoogleDrivePublicFolderProvider,
    is_google_drive_folder_url,
    parse_drive_folder_id,
    parse_feedpak_filename,
)

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Synthetic, content-free fixtures: fake Drive file ids + obviously-fake names, never
# real songs or real share links. The .txt entry must be ignored (not a package file).
FAKE_FILES = [
    ("FID000000000001", "Zeta Testers - Second Fake Album - Song Bravo.feedpak"),
    ("FID000000000002", "Alpha Testers - First Fake Album - Song Alpha.feedpak"),
    ("FID000000000003", "Alpha Testers - First Fake Album - Song &amp; Ampersand.feedpak"),
    ("FID000000000004", "readme-not-a-song.txt"),
]


def _folder_html(files: list[tuple[str, str]]) -> str:
    entries = "".join(
        f'<div class="flip-entry" id="entry-{file_id}" role="link">'
        f'<div class="flip-entry-info">'
        f'<a href="https://drive.google.com/file/d/{file_id}/view?usp=drive_web" target="_blank"></a>'
        f'<div class="flip-entry-title">{name}</div>'
        f"</div></div>"
        for file_id, name in files
    )
    return f'<html><body><div class="flip-entries">{entries}</div></body></html>'


class _FakeHTTPResponse:
    """Minimal stand-in for a urllib response: context manager + read() + headers dict."""

    def __init__(self, chunks: list[bytes], headers: dict):
        self._chunks = list(chunks)
        self.headers = headers

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _size: int = -1) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""


class FakeGoogleProvider(GoogleDrivePublicFolderProvider):
    """Google provider with the network stubbed: canned folder HTML and canned responses."""

    def __init__(self, cache_dir, *, folder_html="", responses=None, **kwargs):
        source = {
            "baseUrl": "https://drive.google.com/drive/folders/FAKEFOLDERID0001",
            "label": "Fake Drive",
        }
        super().__init__(source, cache_dir, **kwargs)
        self._folder_html = folder_html
        self._responses = list(responses or [])
        self.opened: list[str] = []

    def _fetch_folder_html(self) -> str:
        return self._folder_html

    def _urlopen(self, req, timeout):
        self.opened.append(req.full_url)
        return self._responses.pop(0)


# --------------------------------------------------------------------------- URL parsing


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://drive.google.com/drive/folders/1AbCdEf_Ghi-Jkl", "1AbCdEf_Ghi-Jkl"),
        ("https://drive.google.com/drive/u/0/folders/1AbCdEf_Ghi-Jkl?usp=sharing", "1AbCdEf_Ghi-Jkl"),
        ("https://drive.google.com/open?id=1AbCdEf_Ghi-Jkl", "1AbCdEf_Ghi-Jkl"),
        ("https://studio.local:8765", None),
        ("not a url at all", None),
    ],
)
def test_parse_drive_folder_id(url, expected):
    assert parse_drive_folder_id(url) == expected


def test_is_google_drive_folder_url():
    assert is_google_drive_folder_url("https://drive.google.com/drive/folders/1AbCdEf_Ghi-Jkl")
    assert is_google_drive_folder_url("https://drive.google.com/open?id=1AbCdEf_Ghi-Jkl")
    # A Drive *file* link (no folder id) and a non-Drive host are not folder sources.
    assert not is_google_drive_folder_url("https://drive.google.com/file/d/1AbCdEf_Ghi-Jkl/view")
    assert not is_google_drive_folder_url("https://studio.local:8765")


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Artist - Album - Title.feedpak", ("Artist", "Album", "Title")),
        ("Artist - Title.feedpak", ("Artist", "", "Title")),
        ("Just A Title.feedpak", ("Unknown artist", "", "Just A Title")),
        ("Band - Multi - Part - Album Name.feedpak", ("Band", "Multi - Part", "Album Name")),
        ("Legacy Song.sloppak", ("Unknown artist", "", "Legacy Song")),
    ],
)
def test_parse_feedpak_filename(name, expected):
    assert parse_feedpak_filename(name) == expected


# --------------------------------------------------------------------------- enumeration


def test_enumerate_parses_sorts_and_skips_non_packages(tmp_path):
    provider = FakeGoogleProvider(tmp_path, folder_html=_folder_html(FAKE_FILES))

    songs, total = provider.query_page(size=50)

    # The .txt entry is excluded; the other three are songs.
    assert total == 3
    # Sorted by (artist, album, title); the ampersand entity is unescaped.
    assert [song["title"] for song in songs] == ["Song & Ampersand", "Song Alpha", "Song Bravo"]
    assert songs[0]["artist"] == "Alpha Testers"
    assert songs[0]["album"] == "First Fake Album"
    assert songs[0]["format"] == "sloppak"
    assert songs[0]["song_id"] == "FID000000000003"
    assert songs[0]["libraryProviderId"] == provider.id


def test_query_page_paginates(tmp_path):
    provider = FakeGoogleProvider(tmp_path, folder_html=_folder_html(FAKE_FILES))

    first, total = provider.query_page(page=0, size=2)
    second, _ = provider.query_page(page=1, size=2)

    assert total == 3
    assert len(first) == 2
    assert len(second) == 1


def test_search_filters_across_artist_album_title(tmp_path):
    provider = FakeGoogleProvider(tmp_path, folder_html=_folder_html(FAKE_FILES))

    songs, total = provider.query_page(q="zeta", size=50)

    assert total == 1
    assert songs[0]["artist"] == "Zeta Testers"


def test_query_artists_groups_by_artist_and_album(tmp_path):
    provider = FakeGoogleProvider(tmp_path, folder_html=_folder_html(FAKE_FILES))

    artists, total = provider.query_artists(size=50)

    assert total == 2
    assert [artist["name"] for artist in artists] == ["Alpha Testers", "Zeta Testers"]
    alpha = artists[0]
    assert alpha["song_count"] == 2
    assert alpha["album_count"] == 1
    assert alpha["albums"][0]["name"] == "First Fake Album"


def test_query_stats_counts_songs_artists_and_letters(tmp_path):
    provider = FakeGoogleProvider(tmp_path, folder_html=_folder_html(FAKE_FILES))

    stats = provider.query_stats()

    assert stats["total_songs"] == 3
    assert stats["total_artists"] == 2
    assert stats["letters"] == {"A": 1, "Z": 1}


def test_enumeration_is_cached(tmp_path):
    provider = FakeGoogleProvider(tmp_path, folder_html=_folder_html(FAKE_FILES))
    calls = {"n": 0}
    original = provider._fetch_folder_html

    def counting():
        calls["n"] += 1
        return original()

    provider._fetch_folder_html = counting
    provider.query_page(size=50)
    provider.query_stats()
    provider.query_artists(size=50)

    assert calls["n"] == 1  # one fetch, served from the metadata cache thereafter


def test_get_art_and_tuning_names_degrade_gracefully(tmp_path):
    provider = FakeGoogleProvider(tmp_path, folder_html=_folder_html(FAKE_FILES))

    assert provider.get_art("FID000000000001") is None
    assert provider.tuning_names() == {"tunings": []}


def test_describe_source_reports_type_and_count(tmp_path):
    provider = FakeGoogleProvider(tmp_path, folder_html=_folder_html(FAKE_FILES))

    info = provider.describe_source()

    assert info["ok"] is True
    assert info["songCount"] == 3
    assert info["server"]["protocol"] == "google-drive-public.v1"
    assert info["capabilities"] == ["library.read", "song.sync"]


# ------------------------------------------------------------------------------- download


def test_sync_song_downloads_and_imports_into_library(tmp_path):
    local_root = tmp_path / "dlc"
    local_root.mkdir()
    imported = []

    def importer(path, root):
        imported.append((path, root))
        return {"libraryImportState": "indexed", "libraryFilename": path.relative_to(root).as_posix()}

    name = "Fake Band - Fake Album - Fake Song.feedpak"
    response = _FakeHTTPResponse(
        [b"fake-package-bytes"],
        {"content-type": "application/octet-stream", "content-disposition": f'attachment; filename="{name}"'},
    )
    provider = FakeGoogleProvider(
        tmp_path / "cache",
        folder_html=_folder_html([("FID000000000001", name)]),
        responses=[response],
        local_library_root=local_root,
        library_importer=importer,
    )

    result = provider.sync_song("FID000000000001")

    assert result["ok"] is True
    assert result["playbackSource"] == "library-folder"
    assert result["libraryImportState"] == "indexed"
    assert result["localFilename"].endswith(name)
    assert (local_root / "Fake Drive" / name).read_bytes() == b"fake-package-bytes"
    assert len(imported) == 1


def test_sync_song_falls_back_to_cache_without_local_root(tmp_path):
    name = "Fake Band - Fake Album - Fake Song.feedpak"
    response = _FakeHTTPResponse(
        [b"fake-package-bytes"],
        {"content-type": "application/octet-stream", "content-disposition": f'attachment; filename="{name}"'},
    )
    provider = FakeGoogleProvider(
        tmp_path / "cache",
        folder_html=_folder_html([("FID000000000001", name)]),
        responses=[response],
    )

    result = provider.sync_song("FID000000000001")

    assert result["ok"] is True
    assert result["playbackSource"] == "remote-cache"
    assert result["bytes"] == len(b"fake-package-bytes")


def test_download_uses_confirm_flow_for_large_file(tmp_path):
    name = "Big Band - Big Album - Big Song.feedpak"
    interstitial = (
        '<html><body><form id="download-form" method="get" '
        'action="https://drive.usercontent.google.com/download">'
        '<input type="hidden" name="id" value="BIGFILEID000001">'
        '<input type="hidden" name="export" value="download">'
        '<input type="hidden" name="confirm" value="t">'
        '<input type="hidden" name="uuid" value="abc-123"></form></body></html>'
    )
    responses = [
        _FakeHTTPResponse([interstitial.encode()], {"content-type": "text/html; charset=utf-8"}),
        _FakeHTTPResponse(
            [b"big-fake-bytes"],
            {"content-type": "application/octet-stream", "content-disposition": f'attachment; filename="{name}"'},
        ),
    ]
    provider = FakeGoogleProvider(tmp_path / "cache", responses=responses)

    target, content_hash, size, _headers = provider._download_drive_file("BIGFILEID000001", "fallback.feedpak")

    assert target.read_bytes() == b"big-fake-bytes"
    assert size == len(b"big-fake-bytes")
    # Second request went to the confirmed usercontent URL carrying the confirm token.
    assert "drive.usercontent.google.com/download" in provider.opened[1]
    assert "confirm=t" in provider.opened[1]


def test_confirmed_download_url_raises_on_quota(tmp_path):
    provider = FakeGoogleProvider(tmp_path / "cache")
    html = "<html><body>Too many users have viewed or downloaded this file recently.</body></html>"

    with pytest.raises(RuntimeError, match="rate-limited"):
        provider._confirmed_download_url(html, "FID000000000001")


def test_confirmed_download_url_builds_from_form(tmp_path):
    provider = FakeGoogleProvider(tmp_path / "cache")
    html = (
        '<form id="download-form" action="https://drive.usercontent.google.com/download">'
        '<input type="hidden" name="id" value="FID000000000001">'
        '<input type="hidden" name="confirm" value="t"></form>'
    )

    url = provider._confirmed_download_url(html, "FID000000000001")

    assert url.startswith("https://drive.usercontent.google.com/download?")
    assert "id=FID000000000001" in url
    assert "confirm=t" in url


# ----------------------------------------------------------------------------- route wiring


def test_add_google_source_registers_provider(tmp_path, monkeypatch):
    routes = importlib.reload(importlib.import_module("routes"))
    monkeypatch.setattr(
        GoogleDrivePublicFolderProvider,
        "_fetch_folder_html",
        lambda self: _folder_html([("FID000000000001", "Band - Album - Song.feedpak")]),
    )
    registered = {}
    app = FastAPI()
    routes.setup(app, {
        "config_dir": tmp_path / "config",
        "register_library_provider": lambda provider, replace=False: registered.setdefault(provider.id, provider),
        "get_sloppak_cache_dir": lambda: tmp_path / "cache",
        "get_dlc_dir": lambda: None,
    })
    client = TestClient(app)

    added = client.post(
        "/api/plugins/remote_library_client/sources",
        json={"baseUrl": "https://drive.google.com/drive/folders/FOLDERID0001234"},
    )
    status = client.get("/api/plugins/remote_library_client/status")

    assert added.status_code == 200
    source = added.json()["source"]
    assert source["type"] == "google-drive-public.v1"
    assert source["songCount"] == 1
    assert "token" not in source  # secret never surfaces (there is none, but the contract holds)
    provider_id = added.json()["provider"]["id"]
    assert provider_id.startswith("gdrive:")
    assert provider_id in registered
    google_status = [item for item in status.json()["sources"] if item.get("type") == "google-drive-public.v1"]
    assert google_status and google_status[0]["online"] is True
