from __future__ import annotations

import base64
import hashlib
import importlib
import os
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from remote_library_client.proton_drive import (
    ProtonPublicShareProvider,
    is_proton_share_url,
    parse_proton_filename,
    parse_proton_share_url,
)
from remote_library_client.provider import sanitize_filename

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Synthetic, content-free catalog: obviously-fake link ids + names, never real songs or a real
# share. Keys are blank because these records feed the crypto-free orchestration tests.
def _record(link_id: str, name: str, size: int = 1) -> dict:
    return {"linkId": link_id, "name": name, "size": size, "nodeKey": "", "nodePassphrase": "", "contentKeyPacket": ""}


FAKE_RECORDS = [
    _record("LINK0002", "Zeta_Testers-Song Bravo.feedpak", 222),
    _record("LINK0001", "Alpha Testers - First Fake Album - Song Alpha.feedpak", 111),
]


class FakeCatalogProvider(ProtonPublicShareProvider):
    """Proton provider with the decrypt-the-catalog step stubbed — exercises the query /
    normalize / background-sync orchestration without any network or OpenPGP."""

    def __init__(self, cache_dir, *, records=None, **kwargs):
        source = {
            "baseUrl": "https://drive.proton.me/urls/FAKETOKEN01",
            "urlPassword": "fakepassword",
            "label": "Fake Proton",
        }
        super().__init__(source, cache_dir, **kwargs)
        self._records = [dict(record) for record in (records if records is not None else FAKE_RECORDS)]

    def _build_catalog(self):
        return [dict(record) for record in self._records]

    def _do_sync(self, song_id):
        # Stand in for the real download+decrypt: write fake bytes and import them.
        record = self._catalog_record(song_id)
        fallback = sanitize_filename(record["name"], "remote-song.feedpak")
        target = self.cache_dir / fallback
        target.write_bytes(b"fake-proton-bytes")
        result = {
            "ok": True, "song_id": song_id, "remoteSongId": song_id,
            "cached": True, "cacheState": "ready", "bytes": len(b"fake-proton-bytes"),
        }
        result.update(self._import_into_library(target, hashlib.sha256(b"fake-proton-bytes").hexdigest(), fallback))
        return result


# --------------------------------------------------------------------------- URL parsing


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://drive.proton.me/urls/GD9ABCDEF1#SecretPass12", ("GD9ABCDEF1", "SecretPass12")),
        ("https://drive.proton.me/urls/TOKEN123", ("TOKEN123", "")),
        ("https://drive.google.com/drive/folders/abc", None),
        ("https://studio.local:8765", None),
        ("not a url", None),
    ],
)
def test_parse_proton_share_url(url, expected):
    assert parse_proton_share_url(url) == expected


def test_is_proton_share_url():
    assert is_proton_share_url("https://drive.proton.me/urls/TOKEN#pw")
    assert is_proton_share_url("https://drive.proton.me/urls/TOKEN")  # token alone still parses
    assert not is_proton_share_url("https://drive.google.com/drive/folders/abc")
    assert not is_proton_share_url("https://studio.local:8765")


@pytest.mark.parametrize(
    "name,expected",
    [
        ("3_Doors_Down-Kryptonite.feedpak", ("3 Doors Down", "", "Kryptonite")),
        ("Derek_and_the_Dominos-Layla.feedpak", ("Derek and the Dominos", "", "Layla")),
        ("Artist - Album - Title.feedpak", ("Artist", "Album", "Title")),
        ("Artist - Title.feedpak", ("Artist", "", "Title")),
        ("JustATitle.feedpak", ("Unknown artist", "", "JustATitle")),
        ("Legacy_Song.sloppak", ("Unknown artist", "", "Legacy Song")),
    ],
)
def test_parse_proton_filename(name, expected):
    assert parse_proton_filename(name) == expected


# --------------------------------------------------------------- querying / normalization


def test_enumerate_sorts_and_shapes_songs(tmp_path):
    provider = FakeCatalogProvider(tmp_path)

    songs, total = provider.query_page(size=50)

    assert total == 2
    # Sorted by (artist, album, title): "Alpha Testers" before "Zeta Testers".
    assert [song["artist"] for song in songs] == ["Alpha Testers", "Zeta Testers"]
    assert songs[0]["title"] == "Song Alpha"
    assert songs[0]["album"] == "First Fake Album"
    assert songs[0]["song_id"] == "LINK0001"
    assert songs[0]["libraryProviderId"] == provider.id
    assert songs[0]["sizeBytes"] == 111


def test_songs_carry_syncable_shape(tmp_path):
    # Core renders a provider song as playable only when it carries the same first-class fields a
    # Remote Library Server emits (syncSupport / status / packageForm / capabilities).
    provider = FakeCatalogProvider(tmp_path)

    song = provider.query_page(size=50)[0][0]

    assert song["syncSupport"] == "syncable"
    assert song["status"] == "remote-only"
    assert song["packageForm"] == "sloppak-zip"
    assert song["capabilities"] == ["package-download"]
    assert song["settingsKey"]
    assert song["localFilename"] == ""


def test_query_page_paginates(tmp_path):
    provider = FakeCatalogProvider(tmp_path)
    first, total = provider.query_page(page=0, size=1)
    second, _ = provider.query_page(page=1, size=1)
    assert total == 2
    assert len(first) == 1 and len(second) == 1
    assert first[0]["artist"] == "Alpha Testers"


def test_search_filters_across_fields(tmp_path):
    provider = FakeCatalogProvider(tmp_path)
    songs, total = provider.query_page(q="zeta", size=50)
    assert total == 1
    assert songs[0]["artist"] == "Zeta Testers"


def test_query_artists_and_stats(tmp_path):
    provider = FakeCatalogProvider(tmp_path)

    artists, total = provider.query_artists(size=50)
    assert total == 2
    assert [artist["name"] for artist in artists] == ["Alpha Testers", "Zeta Testers"]

    stats = provider.query_stats()
    assert stats["total_songs"] == 2
    assert stats["total_artists"] == 2
    assert stats["letters"] == {"A": 1, "Z": 1}


def test_query_page_marks_downloaded_songs_as_local(tmp_path):
    local_root = tmp_path / "dlc"
    name = "Alpha Testers - First Fake Album - Song Alpha.feedpak"
    provider = FakeCatalogProvider(
        tmp_path / "cache",
        records=[_record("LINK0001", name)],
        local_library_root=local_root,
    )
    target = local_root / provider._source_folder_name() / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"downloaded")

    song = provider.query_page(size=50)[0][0]

    relative = f"{provider._source_folder_name()}/{name}"
    assert song["localFilename"] == relative
    assert song["filename"] == relative
    assert song["song_id"] == "LINK0001"


def test_catalog_is_cached(tmp_path):
    provider = FakeCatalogProvider(tmp_path)
    calls = {"n": 0}
    original = provider._build_catalog

    def counting():
        calls["n"] += 1
        return original()

    provider._build_catalog = counting
    provider.query_page(size=50)
    provider.query_stats()
    provider.query_artists(size=50)
    assert calls["n"] == 1  # one build, served from the in-memory catalog thereafter


def test_get_art_and_tuning_names_degrade_gracefully(tmp_path):
    provider = FakeCatalogProvider(tmp_path)
    assert provider.get_art("LINK0001") is None
    assert provider.tuning_names() == {"tunings": []}


def test_describe_source_reports_type_and_count(tmp_path):
    provider = FakeCatalogProvider(tmp_path)
    info = provider.describe_source()
    assert info["ok"] is True
    assert info["songCount"] == 2
    assert info["server"]["protocol"] == "proton-public.v1"
    assert info["capabilities"] == ["library.read", "song.sync"]


# ------------------------------------------------------------------ background download/sync


def test_sync_song_is_non_blocking_then_plays(tmp_path):
    local_root = tmp_path / "dlc"
    local_root.mkdir()
    name = "Fake Band - Fake Album - Fake Song.feedpak"
    provider = FakeCatalogProvider(
        tmp_path / "cache",
        records=[_record("LINK0001", name)],
        local_library_root=local_root,
        library_importer=lambda path, root: {"libraryImportState": "indexed"},
    )
    provider._start_background_sync = provider._background_sync  # run inline, deterministically

    first = provider.sync_song("LINK0001")
    assert first["cacheState"] == "downloading"
    assert "filename" not in first

    second = provider.sync_song("LINK0001")
    assert second["playbackSource"] == "library-folder"
    assert second["localFilename"].endswith(name)


def test_sync_song_dedupes_concurrent_downloads(tmp_path):
    provider = FakeCatalogProvider(tmp_path)
    spawned = []
    provider._start_background_sync = lambda song_id: spawned.append(song_id)

    provider.sync_song("LINK0001")
    provider.sync_song("LINK0001")

    assert spawned == ["LINK0001"]


def test_sync_song_plays_already_downloaded_file(tmp_path):
    local_root = tmp_path / "dlc"
    name = "Fake Band - Fake Album - Fake Song.feedpak"
    provider = FakeCatalogProvider(
        tmp_path / "cache",
        records=[_record("LINK0001", name)],
        local_library_root=local_root,
    )
    target = local_root / provider._source_folder_name() / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"already-here")

    result = provider.sync_song("LINK0001")
    assert result["cacheState"] == "ready"
    assert result["localFilename"].endswith(name)


def test_active_downloads_reports_downloading_then_ready(tmp_path):
    local_root = tmp_path / "dlc"
    local_root.mkdir()
    name = "Fake Band - Fake Album - Fake Song.feedpak"
    provider = FakeCatalogProvider(
        tmp_path / "cache",
        records=[_record("LINK0001", name)],
        local_library_root=local_root,
        library_importer=lambda path, root: {"libraryImportState": "indexed"},
    )
    provider._start_background_sync = lambda song_id: None  # hold in "downloading"

    provider.sync_song("LINK0001")
    downloading = provider.active_downloads()
    assert downloading and downloading[0]["status"] == "downloading"
    assert downloading[0]["providerId"] == provider.id

    provider._background_sync("LINK0001")
    ready = provider.active_downloads()
    assert ready[0]["status"] == "ready"
    assert ready[0]["localFilename"].endswith(name)


# ------------------------------------------------------------------------- end-to-end crypto


def _split_first_packet(data: bytes) -> tuple[bytes, bytes]:
    """Split an OpenPGP message into its first packet (the PKESK) and the rest (the SEIPD),
    so the synthetic fixture can hand them to the provider the way Proton splits ContentKeyPacket
    from a content block."""
    header = data[0]
    assert header & 0x80, "not an OpenPGP packet"
    index = 1
    if header & 0x40:  # new-format packet
        first = data[index]
        index += 1
        if first < 192:
            length = first
        elif first < 224:
            length = ((first - 192) << 8) + data[index] + 192
            index += 1
        elif first == 255:
            length = int.from_bytes(data[index:index + 4], "big")
            index += 4
        else:
            raise AssertionError("partial body lengths not supported in the fixture")
    else:  # old-format packet
        length_type = header & 0x03
        size = {0: 1, 1: 2, 2: 4}[length_type]
        length = int.from_bytes(data[index:index + size], "big")
        index += size
    end = index + length
    return data[:end], data[end:]


def test_proton_crypto_end_to_end(tmp_path):
    ps = pytest.importorskip("pysequoia")
    pytest.importorskip("bcrypt")
    from remote_library_client import proton_srp

    url_password = "generatedpw12"
    salt = os.urandom(16)
    url_passphrase = proton_srp.compute_key_password(url_password, salt)
    share_key = ps.Cert.generate("share@example.test")
    root_key = ps.Cert.generate("root@example.test")
    child_key = ps.Cert.generate("child@example.test")

    content = b"FAKE-FEEDPAK-CONTENT-" + os.urandom(400)
    content_key_packet, block = _split_first_packet(ps.encrypt(content, recipients=[child_key], armor=False))

    def armored(message) -> str:
        # pysequoia's encrypt() returns ASCII-armored bytes; Proton returns armored strings.
        return message.decode() if isinstance(message, (bytes, bytearray)) else message

    def secret_key(cert) -> str:
        # An armored *secret* key (bytes(cert) is public-only), the way Proton ships node keys.
        return ps.armor(bytes(cert.secrets), ps.ArmorKind.SecretKey)

    # The Token object from GET /drive/urls/{token}: share material + the root folder's node keys.
    material = {
        "SharePasswordSalt": base64.b64encode(salt).decode(),
        "SharePassphrase": armored(ps.encrypt(b"share-pass", passwords=[url_passphrase])),
        "ShareKey": secret_key(share_key),
        "LinkID": "ROOT0000",
        "NodePassphrase": armored(ps.encrypt(b"root-pass", recipients=[share_key])),
        "NodeKey": secret_key(root_key),
    }
    child = {
        "LinkID": "LINK0001",
        "Type": 2,
        "Size": len(content),
        "NodePassphrase": armored(ps.encrypt(b"child-pass", recipients=[root_key])),
        "NodeKey": secret_key(child_key),
        "Name": armored(ps.encrypt(b"Fake_Artist-Fake Title.feedpak", recipients=[child_key])),
        # The content key rides on the file link's FileProperties, not the revision.
        "FileProperties": {"ContentKeyPacket": base64.b64encode(content_key_packet).decode()},
    }
    bare_url = "https://storage.example.test/block/0"

    class FakeClient:
        def bootstrap(self):
            return material

        def fetch_children(self, link_id):
            return [child]

        def fetch_file_revision(self, link_id):
            return {"Blocks": [{"Index": 1, "BareURL": bare_url}]}

        def download_block(self, url):
            assert url == bare_url
            return block

    local_root = tmp_path / "dlc"
    local_root.mkdir()
    provider = ProtonPublicShareProvider(
        {"baseUrl": f"https://drive.proton.me/urls/TOK#{url_password}", "label": "E2E"},
        tmp_path / "cache",
        local_library_root=local_root,
        library_importer=lambda path, root: {"libraryImportState": "indexed"},
    )
    provider._client = FakeClient()

    # The full key hierarchy decrypts the real filename out of the E2E-encrypted listing.
    songs, total = provider.query_page(size=50)
    assert total == 1
    assert songs[0]["artist"] == "Fake Artist"
    assert songs[0]["title"] == "Fake Title"

    # ...and the content blocks decrypt + reassemble into the local library byte-for-byte.
    result = provider._do_sync("LINK0001")
    assert result["ok"] is True
    assert result["playbackSource"] == "library-folder"
    assert (local_root / "E2E" / "Fake_Artist-Fake Title.feedpak").read_bytes() == content


# ----------------------------------------------------------------------------- route wiring


def test_add_proton_source_registers_and_hides_password(tmp_path, monkeypatch):
    routes = importlib.reload(importlib.import_module("routes"))
    # Stub the decrypt-the-catalog step so the route test needs no network, crypto, or real share.
    monkeypatch.setattr(
        ProtonPublicShareProvider,
        "_build_catalog",
        lambda self: [_record("LINK0001", "Band-Song.feedpak")],
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
        json={"baseUrl": "https://drive.proton.me/urls/TOKEN123#SuperSecretPw"},
    )

    assert added.status_code == 200
    source = added.json()["source"]
    assert source["type"] == "proton-public.v1"
    assert source["songCount"] == 1
    # The URL password is a secret: it must never surface in an API response, and must not be
    # smuggled into the displayable baseUrl.
    assert "urlPassword" not in source
    assert "SuperSecretPw" not in source["baseUrl"]
    assert source["baseUrl"] == "https://drive.proton.me/urls/TOKEN123"
    provider_id = added.json()["provider"]["id"]
    assert provider_id.startswith("proton:")
    assert provider_id in registered


def test_proton_source_requires_password(tmp_path):
    routes = importlib.reload(importlib.import_module("routes"))
    app = FastAPI()
    routes.setup(app, {
        "config_dir": tmp_path / "config",
        "register_library_provider": lambda provider, replace=False: None,
        "get_sloppak_cache_dir": lambda: tmp_path / "cache",
        "get_dlc_dir": lambda: None,
    })
    client = TestClient(app)

    # A share link with no password fragment cannot decrypt anything — reject it clearly.
    rejected = client.post(
        "/api/plugins/remote_library_client/sources",
        json={"type": "proton-public.v1", "baseUrl": "https://drive.proton.me/urls/TOKEN123"},
    )
    assert rejected.status_code == 400
    assert "password" in rejected.json()["detail"].lower()
