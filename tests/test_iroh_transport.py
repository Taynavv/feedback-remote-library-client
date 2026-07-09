from __future__ import annotations

import asyncio
import base64
import http.server
import importlib
import json
import socketserver
import sys
import threading
import time
import types
from pathlib import Path
from urllib.parse import urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from remote_library_client.iroh_transport import (  # noqa: E402
    IrohLibraryProvider,
    IrohUnreachableError,
    _decode_direct_song_id,
    is_iroh_id,
    normalize_iroh_id,
)
from remote_library_client.provider import DirectLibraryProvider  # noqa: E402

FAKE_ID = "endpoint" + "a" * 48  # a plausible-looking (fake) EndpointTicket


# --------------------------------------------------------------- parsing (no iroh needed)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("endpointabc123def", True),
        ("a" * 64, True),  # a bare 64-hex EndpointId
        ("AbCdEf01" * 8, True),
        ("http://studio.local:8765", False),
        ("too-short", False),
        ("", False),
    ],
)
def test_is_iroh_id(value, expected):
    assert is_iroh_id(value) is expected


def test_normalize_iroh_id_trims():
    assert normalize_iroh_id("  endpointXYZ \n") == "endpointXYZ"


def test_decode_direct_song_id():
    relative = "Some Artist - Some Title.sloppak"
    song_id = "song_" + base64.urlsafe_b64encode(relative.encode()).decode().rstrip("=")
    assert _decode_direct_song_id(song_id) == relative
    assert _decode_direct_song_id("not-a-song-id") is None


# --------------------------------------------------------- provider construction (no iroh)


def test_provider_rejects_invalid_id(tmp_path):
    with pytest.raises(ValueError):
        IrohLibraryProvider({"irohId": "nope"}, tmp_path)


def test_provider_shape(tmp_path):
    provider = IrohLibraryProvider({"irohId": FAKE_ID, "token": "secret", "label": "Studio"}, tmp_path)
    assert provider.type == "iroh-library.v1"
    assert provider.token == "secret"
    assert provider._auth_header == "Bearer secret"
    assert provider.id.startswith("iroh:")
    assert provider.base_url.startswith("http://iroh")  # synthetic, never dialed


def test_download_label_decodes_song_id(tmp_path):
    provider = IrohLibraryProvider({"irohId": FAKE_ID}, tmp_path)
    song_id = "song_" + base64.urlsafe_b64encode(b"Band/My Song.sloppak").decode().rstrip("=")
    assert provider._download_label(song_id) == "My Song"


def test_addr_for_accepts_bare_endpoint_id():
    """Regression: a bare EndpointId (the *stable* Library ID) must build a valid EndpointAddr so it
    can be dialed via discovery. The old code called ``EndpointAddr(id)`` and raised TypeError, so
    only the volatile full ticket worked — which is why the shared ID appeared to keep changing."""
    pytest.importorskip("iroh")
    from remote_library_client.iroh_transport import _get_runtime

    runtime = _get_runtime()
    bare_id = runtime._endpoint.id().to_bytes().hex()  # a real, valid 64-hex EndpointId
    assert len(bare_id) == 64
    addr = runtime._addr_for(bare_id)  # must not raise
    assert addr is not None


# ---------------------------------------------- non-blocking sync (no iroh; download mocked)


def test_sync_song_returns_downloading_immediately(tmp_path, monkeypatch):
    import remote_library_client.iroh_transport as transport

    class _NoThread:  # don't actually spawn the background download
        def __init__(self, *a, **k):
            pass

        def start(self):
            pass

    monkeypatch.setattr(transport.threading, "Thread", _NoThread)
    provider = IrohLibraryProvider({"irohId": FAKE_ID}, tmp_path)

    result = provider.sync_song("song_1")
    assert result["cacheState"] == "downloading"
    assert "filename" not in result
    downloading = provider.active_downloads()
    assert downloading and downloading[0]["status"] == "downloading"
    assert downloading[0]["providerId"] == provider.id


def test_background_sync_completes_and_replays(tmp_path, monkeypatch):
    # The real work (the direct-protocol download over iroh) is stubbed; we test the orchestration.
    monkeypatch.setattr(
        DirectLibraryProvider, "sync_song",
        lambda self, song_id: {"ok": True, "filename": "src/Song.sloppak", "playbackSource": "library-folder"},
    )
    provider = IrohLibraryProvider({"irohId": FAKE_ID}, tmp_path)

    provider._background_sync("song_1")
    ready = provider._local_ready("song_1")
    assert ready and ready["filename"] == "src/Song.sloppak"
    # ...and a subsequent click plays it immediately.
    assert provider.sync_song("song_1")["filename"] == "src/Song.sloppak"
    reported = provider.active_downloads()
    assert reported[0]["status"] == "ready" and reported[0]["localFilename"] == "src/Song.sloppak"


# ------------------------------------------------------- route wiring (no iroh; probe mocked)


def test_add_iroh_source_registers_and_hides_token(tmp_path, monkeypatch):
    routes = importlib.reload(importlib.import_module("routes"))
    # Stub the /source probe so the route needs no real iroh connection.
    monkeypatch.setattr(
        IrohLibraryProvider, "_json",
        lambda self, path, params=None, timeout=20: {
            "ok": True, "sourceId": "studio", "sourceName": "Studio", "songCount": 7,
            "capabilities": ["library.read", "song.sync"], "auth": {"required": True},
        },
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
        json={"type": "iroh-library.v1", "baseUrl": FAKE_ID, "token": "super-secret"},
    )

    assert added.status_code == 200
    source = added.json()["source"]
    assert source["type"] == "iroh-library.v1"
    assert source["songCount"] == 7
    assert source["irohId"] == FAKE_ID
    assert "token" not in source  # the bearer token is a secret and must not surface
    assert added.json()["provider"]["id"].startswith("iroh:")
    assert added.json()["provider"]["id"] in registered


# ------------------------------------ offline / unreachable handling (no iroh; transport mocked)


def test_run_bounds_and_cancels_on_timeout():
    """``_run`` must return promptly when a coroutine overruns its timeout AND actually cancel the
    pending work — so a dial to an offline peer can't leak onto the shared loop and wedge later calls.
    This is the mechanism that turns a would-be hang into a prompt "offline"."""
    from remote_library_client.iroh_transport import _IrohRuntime

    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    fake = types.SimpleNamespace(_loop=loop)  # _run only needs self._loop
    cancelled = threading.Event()

    async def hang():
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    try:
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            _IrohRuntime._run(fake, hang(), timeout=0.2)
        assert time.monotonic() - started < 5  # returned promptly, not after the coroutine's 30s
        assert cancelled.wait(2)  # the coroutine was cancelled, not left running on the loop
    finally:
        loop.call_soon_threadsafe(loop.stop)


def test_status_reports_unreachable_iroh_source(tmp_path, monkeypatch):
    """A previously-added iroh source whose server goes offline must show an Offline card with a
    clear message (screen.js renders `message`), not hang the status call or look normal-but-empty."""
    routes = importlib.reload(importlib.import_module("routes"))
    ok_payload = {
        "ok": True, "sourceId": "studio", "sourceName": "Studio", "songCount": 3,
        "capabilities": ["library.read", "song.sync"],
    }
    # Reachable when first added ...
    monkeypatch.setattr(
        IrohLibraryProvider, "_json",
        lambda self, path, params=None, timeout=20: ok_payload,
    )
    app = FastAPI()
    routes.setup(app, {
        "config_dir": tmp_path / "config",
        "register_library_provider": lambda provider, replace=False: None,
        "get_sloppak_cache_dir": lambda: tmp_path / "cache",
        "get_dlc_dir": lambda: None,
    })
    client = TestClient(app)
    added = client.post(
        "/api/plugins/remote_library_client/sources",
        json={"type": "iroh-library.v1", "baseUrl": FAKE_ID},
    )
    assert added.status_code == 200

    # ... then the server goes offline: the /source probe raises IrohUnreachableError.
    def offline(self, path, params=None, timeout=20):
        raise IrohUnreachableError("Server unreachable over iroh — it may be offline.")

    monkeypatch.setattr(IrohLibraryProvider, "_json", offline)

    body = client.get("/api/plugins/remote_library_client/status").json()
    assert body["sources"], "the added source should still be listed"
    src = body["sources"][0]
    assert src["online"] is False
    assert "unreachable" in src["message"].lower()
    assert "token" not in src  # secrets are still stripped on the offline path


# --------------------------------------------- real-iroh loopback integration (gated on iroh)


def test_iroh_provider_browses_over_real_iroh(tmp_path):
    iroh = pytest.importorskip("iroh")

    songs = [
        {"remoteSongId": "song_1", "title": "Kryptonite", "artist": "3 Doors Down",
         "format": "sloppak", "packageForm": "sloppak-zip", "syncSupport": "syncable",
         "status": "remote-only", "capabilities": ["package-download"], "settingsKey": "settings-v1-a", "stem_ids": []},
    ]

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            path = urlparse(self.path).path
            body = json.dumps(
                {"ok": True, "sourceId": "s", "sourceName": "iroh test", "songCount": 1}
                if path == "/source" else {"songs": songs, "total": 1}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_a):
            pass

    stub = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    local_port = stub.server_address[1]
    threading.Thread(target=stub.serve_forever, daemon=True).start()

    # A minimal server-side tunnel (the shape iroh_tunnel.py implements), inline for the test.
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()

    def run(coro, timeout=60):
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)

    async def build():
        secret = iroh.SecretKey.generate()
        opts = iroh.EndpointOptions(preset=iroh.preset_n0(), secret_key=secret.to_bytes(), alpns=[b"feedback/rls/1"])
        return await iroh.Endpoint.bind(opts)

    endpoint = run(build())
    run(endpoint.online(), 45)

    async def pipe(bi):
        recv, send = bi.recv(), bi.send()
        reader, writer = await asyncio.open_connection("127.0.0.1", local_port)

        async def i2t():
            try:
                while (chunk := await recv.read(65536)):
                    writer.write(chunk)
                    await writer.drain()
            finally:
                writer.write_eof()

        async def t2i():
            try:
                while (chunk := await reader.read(65536)):
                    await send.write_all(chunk)
            finally:
                await send.finish()

        await asyncio.gather(i2t(), t2i(), return_exceptions=True)
        writer.close()

    async def accept():
        while True:
            incoming = await endpoint.accept_next()
            if incoming is None:
                break
            accepting = await incoming.accept()
            conn = await accepting.connect()
            while True:
                try:
                    bi = await conn.accept_bi()
                except Exception:
                    break
                asyncio.ensure_future(pipe(bi))

    asyncio.run_coroutine_threadsafe(accept(), loop)
    ticket = str(iroh.EndpointTicket.from_addr(endpoint.addr()))

    provider = IrohLibraryProvider({"irohId": ticket, "sourceId": "s"}, tmp_path)
    result, total = provider.query_page(size=50)
    assert total == 1
    assert result[0]["title"] == "Kryptonite"
    assert result[0]["libraryProviderId"] == provider.id


def test_unreachable_bare_id_fails_fast(tmp_path):
    """A syntactically valid but unreachable bare EndpointId must raise IrohUnreachableError within a
    bounded time (the dial is capped at _CONNECT_TIMEOUT) — not hang on discovery for minutes. This is
    the real end-to-end guard behind the prompt "offline" state."""
    pytest.importorskip("iroh")
    from remote_library_client.iroh_transport import _CONNECT_TIMEOUT

    provider = IrohLibraryProvider({"irohId": "b" * 64}, tmp_path)  # valid hex, (almost certainly) no peer
    started = time.monotonic()
    with pytest.raises(IrohUnreachableError):
        provider.probe_source()
    assert time.monotonic() - started < _CONNECT_TIMEOUT + 15  # bounded, not a 120s hang
