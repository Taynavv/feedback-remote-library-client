# SPDX-License-Identifier: AGPL-3.0-or-later
"""iroh peer-to-peer transport for the ``iroh-library.v1`` source type.

Lets a client reach a home-hosted Remote Library Server **by its iroh ID** (an EndpointId /
"Library ID"), with **no port forwarding** — the server dials outbound to iroh's relay/discovery
network, so it's reachable from anywhere. We tunnel the *exact same* Remote Library Server HTTP
protocol over an iroh QUIC bidirectional stream, so nothing about the protocol or the bearer-token
auth changes; only the transport does.

Mechanism (proven end to end before porting): ``http.client`` speaks HTTP over an iroh ``BiStream``
through a tiny socket adapter; the server side pipes each accepted stream to its own local HTTP
server. iroh runs on a background asyncio loop; this module bridges the sync provider code to it.

``iroh`` is a native dependency, imported **lazily** — the plugin (and the other source types) load
without it; it's only needed when an ``iroh-library.v1`` source is actually used.
"""
from __future__ import annotations

import asyncio
import base64
import http.client
import io
import threading
import time
from urllib import error as urllib_error
from urllib import parse

from remote_library_client.provider import (
    BaseLibraryProvider,
    DirectLibraryProvider,
    LibraryImporter,
    _public_error_message,
    provider_id_for_source,
)


def _decode_direct_song_id(song_id: str) -> str | None:
    """Recover the library-relative filename a Remote Library Server encodes into a song id
    (``song_<urlsafe-b64(relative_name)>``) — used only to label background downloads."""
    raw = str(song_id or "")
    if not raw.startswith("song_"):
        return None
    encoded = raw[5:]
    try:
        padding = "=" * (-len(encoded) % 4)
        return base64.urlsafe_b64decode((encoded + padding).encode("ascii")).decode("utf-8")
    except Exception:
        return None

ALPN = b"feedback/rls/1"
_DEFAULT_TIMEOUT = 120.0


def _iroh():
    """Import iroh lazily so the plugin loads without the native dependency present."""
    import iroh

    return iroh


def normalize_iroh_id(value: str) -> str:
    """Trim a pasted Library ID (an iroh EndpointTicket, or a bare EndpointId hex)."""
    return str(value or "").strip()


def is_iroh_id(value: str) -> bool:
    """A plausible iroh Library ID: an EndpointTicket (``endpoint…``) or a 64-hex EndpointId."""
    raw = normalize_iroh_id(value)
    if raw.lower().startswith("endpoint"):
        return True
    return len(raw) == 64 and all(c in "0123456789abcdefABCDEF" for c in raw)


class _IrohRuntime:
    """A shared iroh Endpoint on a dedicated asyncio loop in a background thread, plus a cache of
    live connections keyed by Library ID. One instance per process (the client only dials out)."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, name="iroh-client-loop", daemon=True).start()
        self._endpoint = self._run(self._bind(), timeout=60)
        self._conns: dict[str, object] = {}
        self._lock = threading.Lock()

    def _run(self, coro, timeout: float = _DEFAULT_TIMEOUT):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    async def _bind(self):
        iroh = _iroh()
        secret = iroh.SecretKey.generate()  # ephemeral: the client identity only dials outbound
        options = iroh.EndpointOptions(preset=iroh.preset_n0(), secret_key=secret.to_bytes(), alpns=[ALPN])
        return await iroh.Endpoint.bind(options)

    def _addr_for(self, iroh_id: str):
        iroh = _iroh()
        value = normalize_iroh_id(iroh_id)
        if value.lower().startswith("endpoint"):
            return iroh.EndpointTicket.from_string(value).endpoint_addr()
        # A bare EndpointId relies on iroh discovery to resolve the current address/relay.
        return iroh.EndpointAddr(iroh.EndpointId.from_string(value))

    def _connection(self, iroh_id: str, timeout: float):
        with self._lock:
            conn = self._conns.get(iroh_id)
        if conn is not None:
            return conn
        conn = self._run(self._endpoint.connect(self._addr_for(iroh_id), ALPN), timeout=timeout)
        with self._lock:
            self._conns[iroh_id] = conn
        return conn

    def drop(self, iroh_id: str) -> None:
        with self._lock:
            self._conns.pop(iroh_id, None)

    def open_stream(self, iroh_id: str, timeout: float):
        """Open a fresh bidirectional stream to the server, re-dialing once if the cached
        connection is dead. Returns (send_stream, recv_stream)."""
        conn = self._connection(iroh_id, timeout)
        try:
            bi = self._run(conn.open_bi(), timeout=timeout)
        except Exception:
            self.drop(iroh_id)
            conn = self._connection(iroh_id, timeout)
            bi = self._run(conn.open_bi(), timeout=timeout)
        return _IrohSocket(self, bi.send(), bi.recv(), timeout)

    def write(self, send, data, timeout: float):
        self._run(send.write_all(bytes(data)), timeout=timeout)

    def finish(self, send, timeout: float):
        self._run(send.finish(), timeout=timeout)

    def read(self, recv, size: int, timeout: float):
        return self._run(recv.read(size), timeout=timeout)


_runtime: _IrohRuntime | None = None
_runtime_lock = threading.Lock()


def _get_runtime() -> _IrohRuntime:
    global _runtime
    with _runtime_lock:
        if _runtime is None:
            _runtime = _IrohRuntime()
        return _runtime


class _IrohSocket:
    """A minimal blocking socket over one iroh BiStream, so ``http.client`` can use it directly."""

    def __init__(self, runtime: _IrohRuntime, send, recv, timeout: float) -> None:
        self._rt = runtime
        self._send, self._recv = send, recv
        self._timeout = timeout
        self._buf = b""

    def sendall(self, data) -> None:
        self._rt.write(self._send, data, self._timeout)

    def finish_send(self) -> None:
        self._rt.finish(self._send, self._timeout)

    def recv(self, size: int) -> bytes:
        if not self._buf:
            self._buf = self._rt.read(self._recv, max(size, 65536), self._timeout) or b""
        out, self._buf = self._buf[:size], self._buf[size:]
        return out

    def makefile(self, mode: str = "rb", buffering=None, **kwargs):
        return io.BufferedReader(_RawIroh(self))

    def settimeout(self, _timeout) -> None:  # http.client may call these; no-ops for our stream
        pass

    def setsockopt(self, *_args) -> None:
        pass

    def close(self) -> None:
        pass


class _RawIroh(io.RawIOBase):
    def __init__(self, sock: _IrohSocket) -> None:
        self._sock = sock

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        data = self._sock.recv(len(buffer))
        buffer[: len(data)] = data
        return len(data)


def http_over_iroh(iroh_id: str, req, timeout: float = _DEFAULT_TIMEOUT):
    """Perform a urllib ``Request`` over iroh and return an ``http.client`` response (which is
    urllib-response-compatible: context manager, ``read``, ``status``, ``headers``). Raises
    ``urllib.error.HTTPError`` on a >=400 status so ``DirectLibraryProvider``'s error handling
    (401 -> AuthRequiredError, etc.) works unchanged."""
    runtime = _get_runtime()
    parsed = parse.urlparse(req.full_url)
    path = (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")
    method = req.get_method()
    headers = dict(req.header_items())
    headers.setdefault("Host", "iroh")
    headers["Connection"] = "close"  # one request/response per stream; server closes after

    def attempt():
        sock = runtime.open_stream(iroh_id, timeout)
        http_conn = http.client.HTTPConnection("iroh")
        http_conn.sock = sock
        http_conn.request(method, path, body=req.data, headers=headers)
        sock.finish_send()  # request fully sent -> EOF so the server flushes it to the app
        return http_conn.getresponse()

    try:
        response = attempt()
    except urllib_error.HTTPError:
        raise
    except Exception:
        runtime.drop(iroh_id)  # dead cached connection -> re-dial once
        try:
            response = attempt()
        except Exception as exc:
            raise urllib_error.URLError(f"iroh transport error: {exc}") from exc

    if response.status >= 400:
        raise urllib_error.HTTPError(req.full_url, response.status, response.reason, response.headers, response)
    return response


class IrohLibraryProvider(DirectLibraryProvider):
    """A Remote Library Server reached over iroh (``iroh-library.v1``).

    Identical to :class:`DirectLibraryProvider` in every way — same protocol, same bearer-token
    auth — except the transport: :meth:`_urlopen` tunnels each request over an iroh stream to the
    server's Library ID instead of making an HTTP-over-TCP call.
    """

    type = "iroh-library.v1"

    def __init__(
        self,
        source: dict,
        cache_dir,
        local_library_root=None,
        library_importer: LibraryImporter | None = None,
        nam_config_dir=None,
    ) -> None:
        iroh_id = normalize_iroh_id(source.get("irohId") or source.get("baseUrl") or "")
        if not is_iroh_id(iroh_id):
            raise ValueError("not a valid iroh Library ID")
        self._iroh_id = iroh_id
        # A synthetic base URL: only its path is used (to build /source, /songs, …). It is never
        # dialed — _urlopen routes everything over iroh — so no SSRF guard/origin host applies.
        base_url = "http://iroh.invalid"
        provider_id = str(
            source.get("providerId") or provider_id_for_source(f"iroh_{iroh_id[:24]}", iroh_id, prefix="iroh")
        )
        # Bypass DirectLibraryProvider.__init__'s http(s) base-URL validation (the id isn't a URL).
        BaseLibraryProvider.__init__(
            self,
            {**source, "providerId": provider_id},
            cache_dir,
            origin_host="",
            allow_unsafe_redirects=False,
            local_library_root=local_library_root,
            library_importer=library_importer,
            nam_config_dir=nam_config_dir,
        )
        self.base_url = base_url
        self.label = str(source.get("label") or source.get("sourceName") or f"iroh library {iroh_id[:12]}")
        # Same bearer-token handling as the direct type (the header is tunnelled and the server
        # checks it exactly as before).
        self.token = str(source.get("token") or "").strip()
        self._auth_header = f"Bearer {self.token}" if self.token and self.token.isascii() else ""
        self._auth_query = {} if (not self.token or self.token.isascii()) else {"token": self.token}
        # iroh reaches the server over the internet (relay/hole-punch), so a package download can
        # far exceed FeedBack core's ~250 ms sync-song cap. Download in the background like the
        # Google Drive / Proton types: return immediately, and the song plays on the next click.
        self._sync_lock = threading.Lock()
        self._downloads: dict[str, dict] = {}

    def _urlopen(self, req, timeout=_DEFAULT_TIMEOUT):
        return http_over_iroh(self._iroh_id, req, timeout=timeout)

    # -- non-blocking sync (mirrors google_drive.py / proton_drive.py) ----

    def sync_song(self, song_id: str) -> dict:
        ready = self._local_ready(song_id)
        if ready:
            return ready
        with self._sync_lock:
            entry = self._downloads.get(song_id)
            already_running = bool(entry and entry.get("status") == "downloading")
            if not already_running:
                self._downloads[song_id] = {
                    "status": "downloading", "title": self._download_label(song_id), "at": time.monotonic(),
                }
        if not already_running:
            threading.Thread(target=self._background_sync, args=(song_id,), daemon=True).start()
        return {
            "ok": True, "song_id": song_id, "remoteSongId": song_id,
            "cached": False, "cacheState": "downloading", "message": "Downloading over iroh…",
        }

    def _background_sync(self, song_id: str) -> None:
        title = self._download_label(song_id)
        try:
            # The real work: the direct-protocol package download + import + NAM sync, over iroh.
            result = DirectLibraryProvider.sync_song(self, song_id)
            entry = {"status": "ready", "title": title, "at": time.monotonic(), "result": result}
        except Exception as exc:  # noqa: BLE001 — record and allow a retry on the next click
            entry = {"status": "error", "title": title, "at": time.monotonic(),
                     "message": _public_error_message(exc)}
        with self._sync_lock:
            self._downloads[song_id] = entry

    def _local_ready(self, song_id: str) -> dict | None:
        # A finished background sync from this session plays immediately on the next click.
        with self._sync_lock:
            entry = self._downloads.get(song_id)
        if entry and entry.get("status") == "ready":
            result = entry.get("result") or {}
            if result.get("filename"):
                return result
        return None

    def _download_label(self, song_id: str) -> str:
        name = _decode_direct_song_id(song_id) or song_id
        stem = name.rsplit("/", 1)[-1]
        for suffix in (".sloppak", ".feedpak", ".psarc", ".zip"):
            if stem.lower().endswith(suffix):
                stem = stem[: -len(suffix)]
                break
        return stem or song_id

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
                    "providerId": self.id, "songId": song_id,
                    "title": entry.get("title") or song_id, "status": entry.get("status") or "downloading",
                }
                if entry.get("status") == "ready":
                    item["localFilename"] = (entry.get("result") or {}).get("filename") or ""
                elif entry.get("status") == "error":
                    item["message"] = entry.get("message") or ""
                items.append(item)
        return items
