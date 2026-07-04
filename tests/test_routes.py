from __future__ import annotations

import importlib
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_add_source_registers_library_provider(tmp_path, monkeypatch):
    routes = importlib.import_module("routes")
    routes = importlib.reload(routes)
    registered = {}
    unregistered = []

    def probe(base_url: str) -> dict:
        assert base_url == "https://studio.example.test:8765"
        return {
            "ok": True,
            "sourceId": "direct_studio",
            "sourceName": "Studio Source",
            "songCount": 12,
            "server": {"protocol": "slopsmith-direct-library.v1"},
        }

    monkeypatch.setattr(routes, "_probe_source", probe)
    app = FastAPI()
    routes.setup(app, {
        "config_dir": tmp_path / "config",
        "register_library_provider": lambda provider, replace=False: registered.setdefault(provider.id, provider),
        "unregister_library_provider": lambda provider_id: unregistered.append(provider_id),
        "get_sloppak_cache_dir": lambda: tmp_path / "cache",
    })
    client = TestClient(app)

    added = client.post("/api/plugins/remote_library_client/sources", json={
        "baseUrl": "https://studio.example.test/",
        "label": "Studio",
    })
    status = client.get("/api/plugins/remote_library_client/status")

    assert added.status_code == 200
    provider_id = added.json()["provider"]["id"]
    assert provider_id in registered
    assert registered[provider_id].kind == "remote"
    assert registered[provider_id].capabilities == ("library.read", "art.read", "song.sync")
    assert status.json()["sources"][0]["providerId"] == provider_id
    assert status.json()["sources"][0]["online"] is True
    assert status.json()["sources"][0]["enabled"] is True
    assert status.json()["sources"][0]["lastSuccessfulContactAt"]

    removed = client.delete(f"/api/plugins/remote_library_client/sources/{provider_id}")
    assert removed.status_code == 200
    assert provider_id in unregistered


def test_add_source_discovers_default_port_and_protocol(tmp_path, monkeypatch):
    routes = importlib.import_module("routes")
    routes = importlib.reload(routes)
    probe_calls = []

    def probe(base_url: str) -> dict:
        probe_calls.append(base_url)
        if base_url == "http://studio.local:8765":
            raise RuntimeError("connection refused")
        assert base_url == "https://studio.local:8765"
        return {
            "ok": True,
            "sourceId": "studio",
            "sourceName": "Studio",
            "songCount": 42,
            "server": {"protocol": "slopsmith-direct-library.v1"},
        }

    monkeypatch.setattr(routes, "_probe_source", probe)
    app = FastAPI()
    routes.setup(app, {
        "config_dir": tmp_path / "config",
        "register_library_provider": lambda provider, replace=False: None,
        "get_sloppak_cache_dir": lambda: tmp_path / "cache",
    })
    client = TestClient(app)

    added = client.post("/api/plugins/remote_library_client/sources", json={"baseUrl": "studio.local"})

    assert added.status_code == 200
    assert added.json()["source"]["baseUrl"] == "https://studio.local:8765"
    assert added.json()["source"]["songCount"] == 42
    assert probe_calls == ["http://studio.local:8765", "https://studio.local:8765"]


def test_add_source_tries_explicit_url_before_default_port(tmp_path, monkeypatch):
    routes = importlib.import_module("routes")
    routes = importlib.reload(routes)
    probe_calls = []

    def probe(base_url: str) -> dict:
        probe_calls.append(base_url)
        assert base_url == "https://example.ngrok-free.app"
        return {
            "ok": True,
            "sourceId": "ngrok",
            "sourceName": "Ngrok Source",
            "songCount": 7,
            "server": {"protocol": "slopsmith-direct-library.v1"},
        }

    monkeypatch.setattr(routes, "_probe_source", probe)
    app = FastAPI()
    routes.setup(app, {
        "config_dir": tmp_path / "config",
        "register_library_provider": lambda provider, replace=False: None,
        "get_sloppak_cache_dir": lambda: tmp_path / "cache",
    })
    client = TestClient(app)

    added = client.post("/api/plugins/remote_library_client/sources", json={"baseUrl": "https://example.ngrok-free.app"})

    assert added.status_code == 200
    assert added.json()["source"]["baseUrl"] == "https://example.ngrok-free.app"
    assert probe_calls == ["https://example.ngrok-free.app"]


def test_add_source_strips_credentials_from_base_url(tmp_path, monkeypatch):
    routes = importlib.import_module("routes")
    routes = importlib.reload(routes)
    probe_calls = []

    def probe(base_url: str) -> dict:
        probe_calls.append(base_url)
        assert base_url == "https://studio.local:9443"
        return {
            "ok": True,
            "sourceId": "studio",
            "sourceName": "Studio",
            "songCount": 42,
            "server": {"protocol": "slopsmith-direct-library.v1"},
        }

    monkeypatch.setattr(routes, "_probe_source", probe)
    app = FastAPI()
    routes.setup(app, {
        "config_dir": tmp_path / "config",
        "register_library_provider": lambda provider, replace=False: None,
        "get_sloppak_cache_dir": lambda: tmp_path / "cache",
    })
    client = TestClient(app)

    added = client.post("/api/plugins/remote_library_client/sources", json={
        "baseUrl": "https://user:secret@studio.local:9443",
    })

    assert added.status_code == 200
    assert added.json()["source"]["baseUrl"] == "https://studio.local:9443"
    assert "secret" not in str(added.json())
    assert probe_calls == ["https://studio.local:9443"]


def test_add_source_explicit_url_falls_back_to_default_port(tmp_path, monkeypatch):
    routes = importlib.import_module("routes")
    routes = importlib.reload(routes)
    probe_calls = []

    def probe(base_url: str) -> dict:
        probe_calls.append(base_url)
        if base_url == "https://studio.local":
            raise RuntimeError("connection refused")
        assert base_url == "https://studio.local:8765"
        return {
            "ok": True,
            "sourceId": "studio",
            "sourceName": "Studio",
            "songCount": 42,
            "server": {"protocol": "slopsmith-direct-library.v1"},
        }

    monkeypatch.setattr(routes, "_probe_source", probe)
    app = FastAPI()
    routes.setup(app, {
        "config_dir": tmp_path / "config",
        "register_library_provider": lambda provider, replace=False: None,
        "get_sloppak_cache_dir": lambda: tmp_path / "cache",
    })
    client = TestClient(app)

    added = client.post("/api/plugins/remote_library_client/sources", json={"baseUrl": "https://studio.local"})

    assert added.status_code == 200
    assert added.json()["source"]["baseUrl"] == "https://studio.local:8765"
    assert probe_calls == ["https://studio.local", "https://studio.local:8765"]


def test_add_source_does_not_save_when_unreachable(tmp_path, monkeypatch):
    routes = importlib.import_module("routes")
    routes = importlib.reload(routes)

    def probe(base_url: str) -> dict:
        raise RuntimeError(f"no route to {base_url}")

    monkeypatch.setattr(routes, "_probe_source", probe)
    app = FastAPI()
    routes.setup(app, {
        "config_dir": tmp_path / "config",
        "register_library_provider": lambda provider, replace=False: None,
        "get_sloppak_cache_dir": lambda: tmp_path / "cache",
    })
    client = TestClient(app)

    added = client.post("/api/plugins/remote_library_client/sources", json={"baseUrl": "studio.local"})
    status = client.get("/api/plugins/remote_library_client/status")

    assert added.status_code == 400
    assert "Could not connect" in added.json()["detail"]
    assert status.json()["sources"] == []


def test_add_source_does_not_save_when_registration_fails(tmp_path, monkeypatch):
    routes = importlib.import_module("routes")
    routes = importlib.reload(routes)

    def probe(base_url: str) -> dict:
        return {
            "ok": True,
            "sourceId": "direct_studio",
            "sourceName": "Studio Source",
            "songCount": 12,
            "server": {"protocol": "slopsmith-direct-library.v1"},
        }

    monkeypatch.setattr(routes, "_probe_source", probe)
    app = FastAPI()
    routes.setup(app, {
        "config_dir": tmp_path / "config",
        "register_library_provider": lambda provider, replace=False: (_ for _ in ()).throw(RuntimeError("boom")),
        "get_sloppak_cache_dir": lambda: tmp_path / "cache",
    })
    client = TestClient(app)

    added = client.post("/api/plugins/remote_library_client/sources", json={"baseUrl": "https://studio.example.test"})
    status = client.get("/api/plugins/remote_library_client/status")

    assert added.status_code == 400
    assert status.json()["sources"] == []
    assert routes._providers == {}


def test_add_source_failure_scrubs_probe_error_details(tmp_path, monkeypatch):
    routes = importlib.import_module("routes")
    routes = importlib.reload(routes)

    def probe(base_url: str) -> dict:
        raise RuntimeError(f"failed at https://user:secret@studio.local/private/log and {tmp_path / 'token.txt'}")

    monkeypatch.setattr(routes, "_probe_source", probe)
    app = FastAPI()
    routes.setup(app, {
        "config_dir": tmp_path / "config",
        "register_library_provider": lambda provider, replace=False: None,
        "get_sloppak_cache_dir": lambda: tmp_path / "cache",
    })
    client = TestClient(app)

    added = client.post("/api/plugins/remote_library_client/sources", json={"baseUrl": "https://studio.local"})

    assert added.status_code == 400
    detail = added.json()["detail"]
    assert "secret" not in detail
    assert str(tmp_path) not in detail
    assert "<redacted>" in detail
    assert "<path>" in detail


def test_status_scrubs_probe_error_details(tmp_path, monkeypatch):
    routes = importlib.import_module("routes")
    routes = importlib.reload(routes)
    source = {
        "providerId": "direct:studio:test",
        "baseUrl": "https://studio.local",
        "sourceId": "studio",
        "sourceName": "Studio",
        "label": "Studio",
        "enabled": True,
    }

    def probe(base_url: str) -> dict:
        raise RuntimeError(f"failed at https://user:secret@studio.local/private/log and {tmp_path / 'token.txt'}")

    monkeypatch.setattr(routes, "_probe_source", probe)
    app = FastAPI()
    routes.setup(app, {
        "config_dir": tmp_path / "config",
        "register_library_provider": lambda provider, replace=False: None,
        "get_sloppak_cache_dir": lambda: tmp_path / "cache",
    })
    routes._store.upsert_source(source)
    client = TestClient(app)

    status = client.get("/api/plugins/remote_library_client/status")

    assert status.status_code == 200
    message = status.json()["sources"][0]["message"]
    assert "secret" not in message
    assert str(tmp_path) not in message
    assert "<redacted>" in message
    assert "<path>" in message


def test_disable_source_unregisters_and_skips_status_probe(tmp_path, monkeypatch):
    routes = importlib.import_module("routes")
    routes = importlib.reload(routes)
    registered = {}
    unregistered = []
    probe_calls = []

    def probe(base_url: str) -> dict:
        probe_calls.append(base_url)
        return {
            "ok": True,
            "sourceId": "direct_studio",
            "sourceName": "Studio Source",
            "songCount": 12,
            "server": {"protocol": "slopsmith-direct-library.v1"},
        }

    monkeypatch.setattr(routes, "_probe_source", probe)
    app = FastAPI()
    routes.setup(app, {
        "config_dir": tmp_path / "config",
        "register_library_provider": lambda provider, replace=False: registered.setdefault(provider.id, provider),
        "unregister_library_provider": lambda provider_id: unregistered.append(provider_id),
        "get_sloppak_cache_dir": lambda: tmp_path / "cache",
    })
    client = TestClient(app)

    added = client.post("/api/plugins/remote_library_client/sources", json={"baseUrl": "https://studio.example.test"})
    provider_id = added.json()["provider"]["id"]
    disabled = client.patch(f"/api/plugins/remote_library_client/sources/{provider_id}", json={"enabled": False})
    probe_calls.clear()
    status = client.get("/api/plugins/remote_library_client/status")

    assert disabled.status_code == 200
    assert provider_id in unregistered
    assert status.json()["sources"][0]["enabled"] is False
    assert status.json()["sources"][0]["online"] is False
    assert status.json()["sources"][0]["message"] == "Disabled"
    assert probe_calls == []

    enabled = client.patch(f"/api/plugins/remote_library_client/sources/{provider_id}", json={"enabled": True})
    assert enabled.status_code == 200
    assert enabled.json()["source"]["enabled"] is True


def test_enable_source_does_not_save_when_registration_fails(tmp_path, monkeypatch):
    routes = importlib.import_module("routes")
    routes = importlib.reload(routes)
    source = {
        "providerId": "direct:studio:test",
        "baseUrl": "https://studio.example.test",
        "sourceId": "studio",
        "sourceName": "Studio",
        "label": "Studio",
        "enabled": False,
    }
    app = FastAPI()
    routes.setup(app, {
        "config_dir": tmp_path / "config",
        "register_library_provider": lambda provider, replace=False: (_ for _ in ()).throw(RuntimeError("boom")),
        "get_sloppak_cache_dir": lambda: tmp_path / "cache",
    })
    routes._store.upsert_source(source)
    client = TestClient(app)

    enabled = client.patch("/api/plugins/remote_library_client/sources/direct:studio:test", json={"enabled": True})
    saved = routes._store.list_sources()[0]

    assert enabled.status_code == 400
    assert saved["enabled"] is False
    assert routes._providers == {}


def test_source_patch_updates_nam_tone_sync_setting_and_provider(tmp_path, monkeypatch):
    routes = importlib.import_module("routes")
    routes = importlib.reload(routes)
    registered = {}

    def probe(base_url: str) -> dict:
        return {
            "ok": True,
            "sourceId": "direct_studio",
            "sourceName": "Studio Source",
            "songCount": 12,
            "capabilities": ["library.read", "art.read", "song.sync", "nam-tone-sync.read"],
            "namToneSync": {"enabled": True},
            "server": {"protocol": "slopsmith-direct-library.v1"},
        }

    monkeypatch.setattr(routes, "_probe_source", probe)
    app = FastAPI()
    routes.setup(app, {
        "config_dir": tmp_path / "config",
        "register_library_provider": lambda provider, replace=False: registered.__setitem__(provider.id, provider),
        "get_sloppak_cache_dir": lambda: tmp_path / "cache",
    })
    client = TestClient(app)

    added = client.post("/api/plugins/remote_library_client/sources", json={"baseUrl": "https://studio.example.test"})
    provider_id = added.json()["provider"]["id"]
    updated = client.patch(f"/api/plugins/remote_library_client/sources/{provider_id}", json={"syncNamToneAssets": True})

    assert updated.status_code == 200
    assert updated.json()["source"]["syncNamToneAssets"] is True
    assert updated.json()["source"]["namToneSyncAvailable"] is True
    assert registered[provider_id].source["syncNamToneAssets"] is True