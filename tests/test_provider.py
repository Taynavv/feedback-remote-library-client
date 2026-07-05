from __future__ import annotations

import json
import sqlite3
from urllib import error

import pytest

from remote_library_client.provider import (
    MAX_ERROR_RESPONSE_BYTES,
    MAX_JSON_RESPONSE_BYTES,
    AuthRequiredError,
    DirectLibraryProvider,
    RedirectBlockedError,
    _GuardedRedirectHandler,
    _header_filename,
    _host_resolves_to_internal,
    _redirect_is_blocked,
    _sha256_bytes,
    playback_settings_key,
    provider_id_for_source,
)

SONGS = [
    {
        "sourceId": "direct_studio",
        "remoteSongId": "song-one",
        "title": "Clean Tone",
        "artist": "The Fixtures",
        "album": "Bench",
        "format": "psarc",
        "packageForm": "psarc-file",
        "arrangements": [{"name": "Lead"}],
        "has_lyrics": True,
        "tuning": "E Standard",
    },
    {
        "sourceId": "direct_studio",
        "remoteSongId": "song-two",
        "title": "Heavy Tone",
        "artist": "The Fixtures",
        "album": "Bench",
        "format": "sloppak",
        "packageForm": "sloppak-zip",
        "arrangements": [{"name": "Rhythm"}],
        "has_lyrics": False,
        "stem_count": 4,
        "stem_ids": ["drums", "bass", "guitar", "vocals"],
        "tuning": "Drop D",
    },
]


class FakeProvider(DirectLibraryProvider):
    def __init__(
        self, tmp_path, local_library_root=None, library_importer=None, source_extra=None, nam_config_dir=None
    ):
        source = {
            "baseUrl": "https://studio.example.test",
            "providerId": provider_id_for_source("direct_studio", "https://studio.example.test"),
            "sourceId": "direct_studio",
            "label": "Studio",
        }
        source.update(source_extra or {})
        super().__init__(source, tmp_path, local_library_root, library_importer, nam_config_dir)
        self.json_calls: list[tuple[str, dict]] = []
        self.bytes_calls: list[str] = []

    def _json(self, path: str, params: dict | None = None, timeout: float = 20) -> dict:
        params = params or {}
        self.json_calls.append((path, dict(params)))
        if path.startswith("/songs/"):
            if path.endswith("/nam-tone-sync"):
                return {
                    "schema": "slopsmith.nam-tone-sync.v1",
                    "sourceId": "direct_studio",
                    "remoteSongId": "song-one",
                    "sourceFilename": "song-one.psarc",
                    "mappings": [{"toneKey": "Clean", "presetRef": "preset:clean"}],
                    "presets": [{
                        "ref": "preset:clean",
                        "name": "Clean NAM",
                        "modelFile": {
                            "name": "clean.nam",
                            "sizeBytes": len(b'{"model":"clean"}'),
                            "sha256": "sha256:ad5beddb785715813f7466bc58c6b6a2e4b2391743485d2ea805dd4ffdaf4428",
                            "url": "/songs/song-one/nam-tone-assets/model/clean.nam",
                        },
                        "irFile": {
                            "name": "room.wav",
                            "sizeBytes": len(b"RIFF-room"),
                            "sha256": "sha256:4dc883e3c126726807dc5a6b035fbcac6739613c9d977e27377ed0b49dc55b7a",
                            "url": "/songs/song-one/nam-tone-assets/ir/room.wav",
                        },
                        "inputGain": 1.25,
                        "outputGain": 0.75,
                        "gateThreshold": -55.0,
                        "settings": {"cab": "open"},
                    }],
                    "warnings": [],
                }
            song_id = path.split("/", 2)[-1]
            song = next((item for item in SONGS if item["remoteSongId"] == song_id), None)
            if not song:
                raise RuntimeError('{"detail":"song not found"}')
            return dict(song)
        if path == "/artists":
            return {
                "artists": [{
                    "name": "The Fixtures",
                    "album_count": 1,
                    "song_count": len(SONGS),
                    "albums": [{"name": "Bench", "songs": SONGS}],
                }],
                "total_artists": 1,
                "query": {"filtersApplied": True},
            }
        if path == "/stats":
            return {
                "total_songs": len(SONGS),
                "total_artists": 1,
                "letters": {"T": 1},
                "query": {"filtersApplied": True},
            }
        if path == "/tuning-names":
            return {"tunings": [
                {"name": "Drop D", "sort_key": 0, "count": 1},
                {"name": "E Standard", "sort_key": 0, "count": 1},
            ]}
        if path != "/songs":
            raise AssertionError(path)
        q = params.get("q") or ""
        songs = [song for song in SONGS if q.lower() in song["title"].lower()]
        arrangements_has = {item for item in str(params.get("arrangements_has") or "").split(",") if item}
        stems_has = {item for item in str(params.get("stems_has") or "").split(",") if item}
        stems_lacks = {item for item in str(params.get("stems_lacks") or "").split(",") if item}
        tunings = {item for item in str(params.get("tunings") or "").split(",") if item}
        if arrangements_has:
            songs = [
                song for song in songs
                if arrangements_has.intersection({item.get("name") for item in song.get("arrangements") or []})
            ]
        if stems_has:
            songs = [song for song in songs if stems_has.issubset(set(song.get("stem_ids") or []))]
        if stems_lacks:
            songs = [song for song in songs if not stems_lacks.intersection(set(song.get("stem_ids") or []))]
        if tunings:
            songs = [song for song in songs if song.get("tuning") in tunings]
        page_size = int(params.get("pageSize") or len(songs) or 1)
        page = int(params.get("page") or 0)
        offset = page * page_size
        return {
            "songs": songs[offset:offset + page_size],
            "total": len(songs),
            "nextCursor": str(offset + page_size) if offset + page_size < len(songs) else None,
            "query": {"filtersApplied": True},
        }

    def _bytes(self, path: str, params: dict | None = None):
        self.bytes_calls.append(path)
        if path.endswith("/nam-tone-assets/model/clean.nam"):
            return b'{"model":"clean"}', "application/json", {}
        if path.endswith("/nam-tone-assets/ir/room.wav"):
            return b"RIFF-room", "audio/wav", {}
        if path.endswith("/art"):
            return b"art-bytes", "image/png", {}
        if path.endswith("/package"):
            song_id = path.split("/")[-2]
            content = b"package-one" if song_id == "song-one" else b"package-two"
            return (
                content,
                "application/octet-stream",
                {"content-disposition": f'attachment; filename="{song_id}.psarc"'},
            )
        raise AssertionError(path)

    def _download_to_cache(self, path: str, fallback_filename: str):
        content, _media_type, headers = self._bytes(path)
        filename = _header_filename(headers, fallback_filename)
        target = self.cache_dir / filename
        self._write_atomic(target, content)
        return target, _sha256_bytes(content), len(content), headers


class StreamingResponse:
    def __init__(self, chunks, headers):
        self.chunks = list(chunks)
        self.headers = headers

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, _size=-1):
        return self.chunks.pop(0) if self.chunks else b""

    def close(self):
        self.chunks.clear()


def test_query_page_filters_and_normalizes(tmp_path):
    provider = FakeProvider(tmp_path)

    songs, total = provider.query_page(q="tone", size=10, arrangements_has=["Lead"], tunings=["E Standard"])

    assert total == 1
    assert songs[0]["filename"] == "song-one"
    assert songs[0]["song_id"] == "song-one"
    assert songs[0]["libraryProviderId"] == provider.id
    assert songs[0]["artist"] == "The Fixtures"


def test_remote_query_params_caps_filter_lists(tmp_path):
    provider = FakeProvider(tmp_path)

    params = provider._remote_query_params(
        page=0,
        size=10,
        sort="artist",
        direction="asc",
        q="x" * 1200,
        tunings=[f"Tuning {index} {'x' * 200}" for index in range(60)],
    )

    tunings = params["tunings"].split(",")
    assert len(params["q"]) == 1000
    assert len(tunings) == 50
    assert all(len(item) <= 120 for item in tunings)


def test_provider_rejects_non_http_base_url(tmp_path):
    with pytest.raises(ValueError, match=r"baseUrl must be an http\(s\) URL"):
        DirectLibraryProvider(
            {
                "baseUrl": "file:///tmp/remote-library",
                "providerId": "direct:bad:test",
                "sourceId": "bad",
                "label": "Bad",
            },
            tmp_path,
        )


def test_repeated_metadata_queries_use_cache(tmp_path):
    provider = FakeProvider(tmp_path)

    first_songs, first_total = provider.query_page(q="tone", size=10)
    second_songs, second_total = provider.query_page(q="tone", size=10)
    provider.query_stats()
    provider.query_stats()
    provider.tuning_names()
    provider.tuning_names()

    assert first_total == second_total == 2
    assert first_songs == second_songs
    assert [path for path, _params in provider.json_calls].count("/songs") == 1
    assert [path for path, _params in provider.json_calls].count("/stats") == 1
    assert [path for path, _params in provider.json_calls].count("/tuning-names") == 1


def test_metadata_cache_can_be_cleared(tmp_path):
    provider = FakeProvider(tmp_path)

    provider.query_page(q="tone", size=10)
    provider.clear_metadata_cache()
    provider.query_page(q="tone", size=10)

    assert [path for path, _params in provider.json_calls].count("/songs") == 2


def test_query_page_preserves_stem_metadata_and_filters(tmp_path):
    provider = FakeProvider(tmp_path)

    songs, total = provider.query_page(q="tone", size=10, stems_has=["drums"], stems_lacks=["piano"])

    assert total == 1
    assert songs[0]["filename"] == "song-two"
    assert songs[0]["stem_count"] == 4
    assert songs[0]["stem_ids"] == ["drums", "bass", "guitar", "vocals"]


def test_query_page_does_not_scan_local_library_for_matching_package(tmp_path):
    local_root = tmp_path / "dlc"
    local_root.mkdir()
    (local_root / "local-song-one.psarc").write_bytes(b"package-one")
    provider = FakeProvider(tmp_path / "cache", local_root)
    provider._library_target = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("query_page must not inspect the local library")
    )

    songs, total = provider.query_page(q="clean", size=10)

    assert total == 1
    assert songs[0]["localFilename"] == ""
    assert songs[0]["local_filename"] == ""
    assert songs[0]["playFilename"] == ""


def test_artist_stats_and_tunings(tmp_path):
    provider = FakeProvider(tmp_path)

    artists, total_artists = provider.query_artists(size=10)
    stats = provider.query_stats()
    tunings = provider.tuning_names()

    assert total_artists == 1
    assert artists[0]["name"] == "The Fixtures"
    assert artists[0]["album_count"] == 1
    assert stats == {"total_songs": 2, "total_artists": 1, "letters": {"T": 1}}
    assert {item["name"] for item in tunings["tunings"]} == {"E Standard", "Drop D"}


def test_art_proxy_returns_response(tmp_path):
    provider = FakeProvider(tmp_path)

    response = provider.get_art("song-one")

    assert response.body == b"art-bytes"
    assert response.media_type == "image/png"


def test_missing_remote_art_returns_none(tmp_path):
    provider = FakeProvider(tmp_path)

    def missing_art(path: str, params: dict | None = None):
        if path.endswith("/art"):
            raise RuntimeError('{"detail":"artwork not found"}')
        raise AssertionError(path)

    provider._bytes = missing_art

    assert provider.get_art("song-one") is None


def test_sync_downloads_to_plugin_cache(tmp_path):
    provider = FakeProvider(tmp_path)

    result = provider.sync_song("song-one")

    cached = tmp_path / provider.cache_dir.name / "song-one.psarc"

    assert result["ok"] is True
    assert result["playbackSource"] == "remote-cache"
    assert result["cached"] is True
    assert result["cacheState"] == "ready"
    assert "cachedPath" not in result
    assert "libraryPath" not in result
    assert cached.read_bytes() == b"package-one"


def test_package_download_streams_to_cache(tmp_path, monkeypatch):
    provider = DirectLibraryProvider(
        {
            "baseUrl": "https://studio.example.test",
            "providerId": "direct:studio:test",
            "sourceId": "studio",
            "label": "Studio",
        },
        tmp_path,
    )
    opened = []

    def fake_urlopen(req, timeout=120):
        opened.append((req.full_url, timeout))
        return StreamingResponse(
            [b"package-", b"one"],
            {"content-disposition": 'attachment; filename="song-one.psarc"'},
        )

    monkeypatch.setattr(provider, "_urlopen", fake_urlopen)

    target, content_hash, byte_count, headers = provider._download_to_cache("/songs/song-one/package", "fallback.psarc")

    assert opened == [("https://studio.example.test/songs/song-one/package", 120)]
    assert target.name == "song-one.psarc"
    assert target.read_bytes() == b"package-one"
    assert content_hash == _sha256_bytes(b"package-one")
    assert byte_count == len(b"package-one")
    assert headers["content-disposition"].endswith('"song-one.psarc"')


def test_package_download_size_is_limited(tmp_path, monkeypatch):
    provider = DirectLibraryProvider(
        {
            "baseUrl": "https://studio.example.test",
            "providerId": "direct:studio:test",
            "sourceId": "studio",
            "label": "Studio",
        },
        tmp_path,
    )

    def fake_urlopen(req, timeout=120):
        return StreamingResponse(
            [b"package-", b"one"],
            {"content-disposition": 'attachment; filename="song-one.psarc"'},
        )

    monkeypatch.setattr(provider, "_urlopen", fake_urlopen)
    monkeypatch.setattr("remote_library_client.provider.MAX_PACKAGE_RESPONSE_BYTES", len(b"package") - 1)

    with pytest.raises(RuntimeError, match="package response exceeded size limit"):
        provider._download_to_cache("/songs/song-one/package", "fallback.psarc")

    assert not any(provider.cache_dir.glob("*.tmp"))
    assert not (provider.cache_dir / "song-one.psarc").exists()


def test_json_response_size_is_limited(tmp_path, monkeypatch):
    provider = DirectLibraryProvider(
        {
            "baseUrl": "https://studio.example.test",
            "providerId": "direct:studio:test",
            "sourceId": "studio",
            "label": "Studio",
        },
        tmp_path,
    )

    def fake_urlopen(req, timeout=20):
        return StreamingResponse([b"{" + b"x" * MAX_JSON_RESPONSE_BYTES], {})

    monkeypatch.setattr(provider, "_urlopen", fake_urlopen)

    with pytest.raises(RuntimeError, match="response exceeded size limit"):
        provider._json("/source")


def test_http_error_body_size_is_limited(tmp_path, monkeypatch):
    provider = DirectLibraryProvider(
        {
            "baseUrl": "https://studio.example.test",
            "providerId": "direct:studio:test",
            "sourceId": "studio",
            "label": "Studio",
        },
        tmp_path,
    )

    def fake_urlopen(req, timeout=20):
        raise error.HTTPError(
            req.full_url,
            500,
            "boom",
            hdrs=None,
            fp=StreamingResponse([b"x" * (MAX_ERROR_RESPONSE_BYTES + 1)], {}),
        )

    monkeypatch.setattr(provider, "_urlopen", fake_urlopen)

    with pytest.raises(RuntimeError, match="error response exceeded size limit"):
        provider._json("/source")


def test_sync_imports_to_local_library_when_configured(tmp_path):
    local_root = tmp_path / "dlc"
    local_root.mkdir()
    provider = FakeProvider(tmp_path / "cache", local_root)

    result = provider.sync_song("song-one")

    assert result["ok"] is True
    assert result["playbackSource"] == "library-folder"
    assert result["filename"] == "direct_studio/song-one.psarc"
    assert result["localFilename"] == "direct_studio/song-one.psarc"
    assert result["playFilename"] == "direct_studio/song-one.psarc"
    assert "libraryPath" not in result
    assert (local_root / "direct_studio" / "song-one.psarc").read_bytes() == b"package-one"


def test_sync_imports_enabled_nam_tone_assets_and_mappings(tmp_path):
    local_root = tmp_path / "dlc"
    local_root.mkdir()
    provider = FakeProvider(
        tmp_path / "cache",
        local_root,
        source_extra={"syncNamToneAssets": True},
        nam_config_dir=tmp_path / "config",
    )

    result = provider.sync_song("song-one")

    tone_sync = result["toneSync"]
    assert tone_sync["ok"] is True
    assert tone_sync["skipped"] is False
    assert tone_sync["presetsImported"] == 1
    assert tone_sync["mappingsImported"] == 1
    assert tone_sync["mappingKey"] == playback_settings_key("direct_studio/song-one.psarc")
    assert (tmp_path / "config" / "nam_models" / "clean.nam").read_bytes() == b'{"model":"clean"}'
    assert (tmp_path / "config" / "nam_irs" / "room.wav").read_bytes() == b"RIFF-room"
    conn = sqlite3.connect(tmp_path / "config" / "nam_tone.db")
    preset = conn.execute(
        "SELECT id, name, model_file, ir_file, input_gain, output_gain, gate_threshold, settings_json FROM presets"
    ).fetchone()
    mappings = conn.execute("SELECT filename, tone_key, preset_id FROM tone_mappings ORDER BY filename").fetchall()
    conn.close()
    assert preset[1:7] == ("Studio / Clean NAM", "clean.nam", "room.wav", 1.25, 0.75, -55.0)
    settings = json.loads(preset[7])
    assert settings["cab"] == "open"
    assert settings["remoteLibraryClient"]["remotePresetRef"] == "preset:clean"
    assert mappings == [
        ("direct_studio/song-one.psarc", "Clean", preset[0]),
        (playback_settings_key("direct_studio/song-one.psarc"), "Clean", preset[0]),
    ]


def test_sync_removes_stale_remote_nam_mappings_without_deleting_local_mappings(tmp_path):
    local_root = tmp_path / "dlc"
    local_root.mkdir()
    provider = FakeProvider(
        tmp_path / "cache",
        local_root,
        source_extra={"syncNamToneAssets": True},
        nam_config_dir=tmp_path / "config",
    )

    first = provider.sync_song("song-one")
    local_filename = first["filename"]
    settings_key = playback_settings_key(local_filename)
    conn = sqlite3.connect(tmp_path / "config" / "nam_tone.db")
    conn.execute(
        "INSERT INTO presets (name, model_file, ir_file, settings_json) VALUES (?, ?, ?, ?)",
        ("Local Preset", "local.nam", "local.wav", "{}"),
    )
    local_preset_id = conn.execute("SELECT id FROM presets WHERE name = ?", ("Local Preset",)).fetchone()[0]
    conn.execute(
        "INSERT INTO tone_mappings (filename, tone_key, preset_id) VALUES (?, ?, ?)",
        (local_filename, "Local", local_preset_id),
    )
    conn.commit()
    conn.close()

    original_json = provider._json

    def no_remote_mappings(path: str, params: dict | None = None, timeout: float = 20) -> dict:
        if path.endswith("/nam-tone-sync"):
            return {
                "schema": "slopsmith.nam-tone-sync.v1",
                "sourceId": "direct_studio",
                "remoteSongId": "song-one",
                "sourceFilename": "song-one.psarc",
                "mappings": [],
                "presets": [],
                "warnings": [],
            }
        return original_json(path, params, timeout)

    provider._json = no_remote_mappings
    second = provider.sync_song("song-one")

    assert second["toneSync"]["mappingsImported"] == 0
    conn = sqlite3.connect(tmp_path / "config" / "nam_tone.db")
    mappings = conn.execute("SELECT filename, tone_key, preset_id FROM tone_mappings ORDER BY tone_key").fetchall()
    conn.close()
    assert mappings == [(local_filename, "Local", local_preset_id)]
    assert all(row[0] != settings_key for row in mappings)


def test_sync_skips_nam_tone_assets_when_source_setting_disabled(tmp_path):
    local_root = tmp_path / "dlc"
    local_root.mkdir()
    provider = FakeProvider(tmp_path / "cache", local_root, nam_config_dir=tmp_path / "config")

    result = provider.sync_song("song-one")

    assert "toneSync" not in result
    assert "/songs/song-one/nam-tone-sync" not in [path for path, _params in provider.json_calls]


def test_sync_reports_nam_tone_errors_without_failing_song_sync(tmp_path):
    local_root = tmp_path / "dlc"
    local_root.mkdir()
    provider = FakeProvider(
        tmp_path / "cache",
        local_root,
        source_extra={"syncNamToneAssets": True},
        nam_config_dir=tmp_path / "config",
    )
    original_json = provider._json

    def broken_json(path: str, params: dict | None = None, timeout: float = 20) -> dict:
        if path.endswith("/nam-tone-sync"):
            raise RuntimeError(f"manifest exploded at {tmp_path / 'config' / 'nam_tone.db'}")
        return original_json(path, params, timeout)

    provider._json = broken_json

    result = provider.sync_song("song-one")

    assert result["ok"] is True
    assert result["filename"] == "direct_studio/song-one.psarc"
    assert result["toneSync"] == {"ok": False, "skipped": False, "error": "manifest exploded at <path>"}


def test_sync_scrubs_nam_tone_skip_reasons(tmp_path):
    provider = FakeProvider(
        tmp_path / "cache",
        source_extra={"syncNamToneAssets": True},
        nam_config_dir=tmp_path / "config",
    )

    def missing_sync(path: str, params: dict | None = None, timeout: float = 20) -> dict:
        if path.endswith("/nam-tone-sync"):
            raise RuntimeError(f"not found at {tmp_path / 'config' / 'nam_tone.db'}")
        raise AssertionError(path)

    provider._json = missing_sync

    result = provider.sync_nam_tones("song-one", "direct_studio/song-one.psarc")

    assert result == {"ok": False, "skipped": True, "reason": "not found at <path>"}


def test_sync_scrubs_library_import_error_paths(tmp_path):
    local_root = tmp_path / "dlc"
    local_root.mkdir()

    def broken_importer(package_path, _root):
        raise RuntimeError(f"cannot index {package_path}")

    provider = FakeProvider(tmp_path / "cache", local_root, broken_importer)

    result = provider.sync_song("song-one")

    assert result["ok"] is True
    assert result["playbackSource"] == "remote-cache"
    assert result["libraryImportState"] == "failed"
    assert result["libraryImportError"] == "cannot index <path>"
    assert str(tmp_path) not in result["libraryImportError"]


def test_sync_indexes_local_library_file_when_importer_available(tmp_path):
    local_root = tmp_path / "dlc"
    local_root.mkdir()
    imported = []

    def import_library_file(package_path, root):
        imported.append((package_path, root))
        return {"libraryImportState": "indexed", "libraryFilename": package_path.relative_to(root).as_posix()}

    provider = FakeProvider(tmp_path / "cache", local_root, import_library_file)

    result = provider.sync_song("song-one")

    assert imported == [(local_root / "direct_studio" / "song-one.psarc", local_root)]
    assert result["libraryImportState"] == "indexed"
    assert result["libraryFilename"] == "direct_studio/song-one.psarc"


def test_sync_allocates_unique_local_library_name_on_content_conflict(tmp_path):
    local_root = tmp_path / "dlc"
    target_dir = local_root / "direct_studio"
    target_dir.mkdir(parents=True)
    (target_dir / "song-one.psarc").write_bytes(b"different")
    provider = FakeProvider(tmp_path / "cache", local_root)

    result = provider.sync_song("song-one")

    assert result["filename"] == "direct_studio/song-one-2.psarc"
    assert (target_dir / "song-one.psarc").read_bytes() == b"different"
    assert (target_dir / "song-one-2.psarc").read_bytes() == b"package-one"


def test_sync_surfaces_package_download_errors(tmp_path):
    provider = FakeProvider(tmp_path)

    def missing_package(path: str, params: dict | None = None):
        if path.endswith("/package"):
            raise RuntimeError('{"detail":"package not found"}')
        raise AssertionError(path)

    provider._bytes = missing_package

    with pytest.raises(RuntimeError, match="package not found"):
        provider.sync_song("song-one")


def test_host_resolves_to_internal_flags_private_loopback_and_unresolvable():
    for host in ("127.0.0.1", "10.0.0.1", "192.168.1.5", "169.254.169.254", "::1", ""):
        assert _host_resolves_to_internal(host) is True
    for host in ("8.8.8.8", "93.184.216.34"):
        assert _host_resolves_to_internal(host) is False


def test_redirect_is_blocked_only_on_pivot_to_a_different_internal_host():
    origin = "studio.example.test"
    assert _redirect_is_blocked(origin, False, "http://169.254.169.254/latest/meta-data") is True
    assert _redirect_is_blocked(origin, False, "http://127.0.0.1:9999/admin") is True
    # Same host (scheme upgrade / path change) stays allowed.
    assert _redirect_is_blocked(origin, False, "https://studio.example.test/other") is False
    # A different but public host is not an internal pivot.
    assert _redirect_is_blocked(origin, False, "http://8.8.8.8/") is False
    # Opting out disables the guard entirely.
    assert _redirect_is_blocked(origin, True, "http://127.0.0.1/") is False


def test_guarded_redirect_handler_raises_on_internal_pivot():
    handler = _GuardedRedirectHandler("studio.example.test", allow_unsafe=False)
    with pytest.raises(RedirectBlockedError):
        handler.redirect_request(None, None, 302, "Found", {}, "http://169.254.169.254/")


def test_provider_blocks_unsafe_redirects_by_default(tmp_path):
    guarded = DirectLibraryProvider(
        {"baseUrl": "https://studio.example.test", "providerId": "direct:studio:test", "label": "Studio"},
        tmp_path,
    )
    assert guarded.allow_unsafe_redirects is False

    opted_out = DirectLibraryProvider(
        {
            "baseUrl": "https://studio.example.test",
            "providerId": "direct:studio:test",
            "label": "Studio",
            "allowUnsafeRedirects": True,
        },
        tmp_path,
    )
    assert opted_out.allow_unsafe_redirects is True


def _auth_provider(tmp_path, token):
    return DirectLibraryProvider(
        {
            "baseUrl": "https://studio.example.test",
            "providerId": "direct:studio:test",
            "label": "Studio",
            "token": token,
        },
        tmp_path,
    )


def test_provider_sends_bearer_token_header(tmp_path):
    provider = _auth_provider(tmp_path, "s3cret")
    captured = {}

    def fake_open(req, timeout=20):
        captured["auth"] = req.get_header("Authorization")
        captured["url"] = req.full_url
        return StreamingResponse([b"{}"], {})

    provider._urlopen = fake_open
    provider._json("/source")

    assert captured["auth"] == "Bearer s3cret"
    assert "token=" not in captured["url"]


def test_provider_non_ascii_token_falls_back_to_query_param(tmp_path):
    provider = _auth_provider(tmp_path, "tökén")
    captured = {}

    def fake_open(req, timeout=20):
        captured["auth"] = req.get_header("Authorization")
        captured["url"] = req.full_url
        return StreamingResponse([b"{}"], {})

    provider._urlopen = fake_open
    provider._json("/source")

    assert captured["auth"] is None
    assert "token=" in captured["url"]


def test_provider_raises_auth_required_on_401(tmp_path):
    provider = _auth_provider(tmp_path, "")

    def fake_open(req, timeout=20):
        raise error.HTTPError(
            req.full_url,
            401,
            "Unauthorized",
            hdrs=None,
            fp=StreamingResponse([b'{"detail":"invalid or missing auth token"}'], {}),
        )

    provider._urlopen = fake_open

    with pytest.raises(AuthRequiredError):
        provider._json("/source")