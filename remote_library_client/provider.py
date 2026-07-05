from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import sqlite3
import threading
import time
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from urllib import error, parse, request

from fastapi.responses import Response

LibraryImporter = Callable[[Path, Path], dict | None]
MAX_JSON_RESPONSE_BYTES = 10 * 1024 * 1024
MAX_BINARY_RESPONSE_BYTES = 256 * 1024 * 1024
MAX_PACKAGE_RESPONSE_BYTES = 2 * 1024 * 1024 * 1024
MAX_ERROR_RESPONSE_BYTES = 64 * 1024


class RedirectBlockedError(RuntimeError):
    """Raised when a server tries to redirect the client to a different internal host."""


class AuthRequiredError(RuntimeError):
    """Raised when the remote server rejects a request for lack of a valid auth token (HTTP 401)."""


def _host_resolves_to_internal(host: str) -> bool:
    """True if the host is unresolvable or resolves to a loopback/private/link-local address.

    These are the SSRF pivot targets we refuse to follow a redirect to. Resolving the name
    (rather than trusting its text) also defends against DNS-based redirect pivots.
    """
    if not host:
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError, ValueError):
        return True
    for info in infos:
        raw = str(info[4][0]).split("%", 1)[0]
        try:
            addr = ipaddress.ip_address(raw)
        except ValueError:
            return True
        if (
            addr.is_loopback
            or addr.is_private
            or addr.is_link_local
            or addr.is_reserved
            or addr.is_multicast
            or addr.is_unspecified
        ):
            return True
    return False


def _redirect_is_blocked(origin_host: str, allow_unsafe: bool, newurl: str) -> bool:
    """A redirect is blocked only when it pivots to a *different* host that is internal.

    The originally-configured host is always allowed (the whole tool exists to talk to
    LAN/localhost servers), and so are same-host redirects (e.g. scheme upgrades, ngrok).
    """
    if allow_unsafe:
        return False
    target_host = (parse.urlparse(newurl).hostname or "").lower()
    if target_host == (origin_host or "").lower():
        return False
    return _host_resolves_to_internal(target_host)


class _GuardedRedirectHandler(request.HTTPRedirectHandler):
    """Follows redirects like urllib's default, but refuses to pivot to a different internal host."""

    def __init__(self, origin_host: str, allow_unsafe: bool) -> None:
        super().__init__()
        self._origin_host = (origin_host or "").lower()
        self._allow_unsafe = allow_unsafe

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if _redirect_is_blocked(self._origin_host, self._allow_unsafe, newurl):
            raise RedirectBlockedError(
                "server tried to redirect the request to an internal host; "
                "enable 'Allow unsafe redirects' for this source to permit it"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _read_limited(handle, limit: int) -> bytes:
    chunks = []
    total = 0
    while True:
        chunk = handle.read(min(1024 * 1024, limit + 1 - total))
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise RuntimeError("remote response exceeded size limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _read_error_detail(exc: error.HTTPError) -> str:
    try:
        detail = exc.read(MAX_ERROR_RESPONSE_BYTES + 1)
    except Exception:
        return str(exc)
    if len(detail) > MAX_ERROR_RESPONSE_BYTES:
        return "remote error response exceeded size limit"
    return detail.decode("utf-8", errors="replace") or str(exc)


def _remote_error(exc: error.HTTPError) -> RuntimeError:
    detail = _read_error_detail(exc)
    if exc.code == 401:
        return AuthRequiredError(detail or "authentication required")
    return RuntimeError(detail or str(exc))


def provider_id_for_source(source_id: str, base_url: str) -> str:
    raw = source_id or base_url
    slug = re.sub(r"[^A-Za-z0-9_.:-]+", "-", raw).strip("-_.:")[:80]
    digest = hashlib.sha1(base_url.encode("utf-8")).hexdigest()[:10]
    return f"direct:{slug or 'source'}:{digest}"


def sanitize_filename(value: str, fallback: str = "remote-song") -> str:
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", value).strip(" ._")
    return name or fallback


def safe_path_segment(value: str | None, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "-", str(value or ""))
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .-_")
    return (cleaned[:80].rstrip(" .-_") or fallback)


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _header_filename(headers: dict, fallback: str) -> str:
    disposition = headers.get("content-disposition") or headers.get("Content-Disposition") or ""
    match = re.search(r"filename\*=UTF-8''([^;]+)", disposition, re.I)
    if match:
        return sanitize_filename(parse.unquote(match.group(1)), fallback)
    match = re.search(r'filename="?([^";]+)"?', disposition)
    if match:
        return sanitize_filename(match.group(1), fallback)
    return sanitize_filename(fallback, "remote-song.psarc")


def _fnv1a_base36(value: str) -> str:
    h = 2166136261
    for char in str(value or ""):
        h ^= ord(char)
        h = (h * 16777619) & 0xFFFFFFFF
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    if h == 0:
        return "0"
    out = ""
    while h:
        h, rem = divmod(h, 36)
        out = digits[rem] + out
    return out


def playback_settings_key(filename: str, song_format: str = "") -> str:
    suffix = Path(filename or "").suffix.lower()
    inferred_format = "psarc" if suffix == ".psarc" else "sloppak" if suffix in {".sloppak", ".zip"} else "unknown"
    source_kind = str(song_format or inferred_format)[:40] or "unknown"
    seed = str(filename or "unknown")
    suffix = _fnv1a_base36(f"{source_kind}:{seed}").rjust(7, "0")[-7:]
    return f"settings-v1-{suffix}"


def _public_error_message(exc: Exception) -> str:
    message = str(exc) or exc.__class__.__name__
    message = re.sub(r"(https?://)[^/@\s]+@", r"\1<redacted>@", message, flags=re.I)
    message = re.sub(r"[A-Za-z]:[\\/][^\s\"'<>]+", "<path>", message)
    message = re.sub(r"(?<![:/])/[^\s\"'<>]+", "<path>", message)
    message = re.sub(r"\s+", " ", message).strip()
    return message[:200] or exc.__class__.__name__


def _query_filter_values(values) -> list[str]:
    normalized = []
    for value in values or []:
        item = str(value).strip()
        if item:
            normalized.append(item[:120])
        if len(normalized) >= 50:
            break
    return normalized


def _validate_base_url(base_url: str) -> str:
    normalized = str(base_url or "").rstrip("/")
    parsed = parse.urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("baseUrl must be an http(s) URL")
    return normalized


def _safe_int(value, default: int = 0) -> int:
    # Server-supplied counts/totals may be missing or non-numeric; never let them raise.
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class DirectLibraryProvider:
    kind = "remote"
    capabilities = ("library.read", "art.read", "song.sync")
    metadata_cache_ttl_seconds = 300
    metadata_cache_max_entries = 256

    def __init__(
        self,
        source: dict,
        cache_dir: Path,
        local_library_root: Path | None = None,
        library_importer: LibraryImporter | None = None,
        nam_config_dir: Path | None = None,
    ) -> None:
        self.source = dict(source)
        self.base_url = _validate_base_url(source.get("baseUrl") or "")
        self.allow_unsafe_redirects = bool(source.get("allowUnsafeRedirects"))
        origin_host = parse.urlparse(self.base_url).hostname or ""
        self._opener = request.build_opener(_GuardedRedirectHandler(origin_host, self.allow_unsafe_redirects))
        # Optional bearer-token auth. Prefer the Authorization header; fall back to a
        # ?token= query param only for non-ASCII tokens, which HTTP headers cannot carry.
        self.token = str(source.get("token") or "").strip()
        self._auth_header = f"Bearer {self.token}" if self.token and self.token.isascii() else ""
        self._auth_query = {} if (not self.token or self.token.isascii()) else {"token": self.token}
        self.id = str(source.get("providerId") or provider_id_for_source(source.get("sourceId") or "", self.base_url))
        self.label = str(source.get("label") or source.get("sourceName") or self.base_url)
        self.cache_dir = Path(cache_dir) / sanitize_filename(self.id.replace(":", "_"))
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.local_library_root = Path(local_library_root) if local_library_root else None
        self.library_importer = library_importer
        self.nam_config_dir = Path(nam_config_dir) if nam_config_dir else None
        self._metadata_cache: dict[tuple[str, tuple[tuple[str, str], ...]], tuple[float, dict]] = {}
        self._metadata_cache_lock = threading.RLock()

    def _urlopen(self, req, timeout):
        # Routes every request through the redirect-guarding opener (see _GuardedRedirectHandler).
        return self._opener.open(req, timeout=timeout)

    def _headers(self) -> dict:
        headers = {"ngrok-skip-browser-warning": "true"}
        if self._auth_header:
            headers["Authorization"] = self._auth_header
        return headers

    def _url(self, path: str, params: dict | None = None) -> str:
        merged = {**self._auth_query, **(params or {})}
        query = f"?{parse.urlencode(merged)}" if merged else ""
        return f"{self.base_url}{path}{query}"

    def _json(self, path: str, params: dict | None = None, timeout: float = 20) -> dict:
        req = request.Request(self._url(path, params), headers=self._headers())
        try:
            with self._urlopen(req, timeout=timeout) as response:
                return json.loads(_read_limited(response, MAX_JSON_RESPONSE_BYTES).decode("utf-8") or "{}")
        except error.HTTPError as exc:
            raise _remote_error(exc) from exc

    def _metadata_cache_key(self, path: str, params: dict | None = None) -> tuple[str, tuple[tuple[str, str], ...]]:
        normalized_params = tuple(sorted((str(key), str(value)) for key, value in (params or {}).items()))
        return path, normalized_params

    def _json_cached(self, path: str, params: dict | None = None, timeout: float = 20) -> dict:
        key = self._metadata_cache_key(path, params)
        now = time.monotonic()
        with self._metadata_cache_lock:
            cached = self._metadata_cache.get(key)
            if cached and now - cached[0] <= self.metadata_cache_ttl_seconds:
                return copy.deepcopy(cached[1])
            if cached:
                self._metadata_cache.pop(key, None)

        payload = self._json(path, params, timeout=timeout)

        with self._metadata_cache_lock:
            if len(self._metadata_cache) >= self.metadata_cache_max_entries:
                oldest_key = min(self._metadata_cache, key=lambda item: self._metadata_cache[item][0])
                self._metadata_cache.pop(oldest_key, None)
            self._metadata_cache[key] = (now, copy.deepcopy(payload))
        return payload

    def clear_metadata_cache(self) -> None:
        with self._metadata_cache_lock:
            self._metadata_cache.clear()

    def _bytes(self, path: str, params: dict | None = None) -> tuple[bytes, str, dict]:
        req = request.Request(self._url(path, params), headers=self._headers())
        try:
            with self._urlopen(req, timeout=120) as response:
                return (
                    _read_limited(response, MAX_BINARY_RESPONSE_BYTES),
                    response.headers.get("content-type") or "application/octet-stream",
                    dict(response.headers),
                )
        except error.HTTPError as exc:
            raise _remote_error(exc) from exc

    def _download_to_cache(self, path: str, fallback_filename: str) -> tuple[Path, str, int, dict]:
        req = request.Request(self._url(path), headers=self._headers())
        tmp_path = None
        try:
            with self._urlopen(req, timeout=120) as response:
                headers = dict(response.headers)
                filename = _header_filename(headers, fallback_filename)
                target = self.cache_dir / filename
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp_path = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
                digest = hashlib.sha256()
                bytes_read = 0
                with tmp_path.open("wb") as handle:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        bytes_read += len(chunk)
                        if bytes_read > MAX_PACKAGE_RESPONSE_BYTES:
                            raise RuntimeError("remote package response exceeded size limit")
                        digest.update(chunk)
                        handle.write(chunk)
                tmp_path.replace(target)
                return target, digest.hexdigest(), bytes_read, headers
        except error.HTTPError as exc:
            raise _remote_error(exc) from exc
        finally:
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _copy_file_atomic(self, source: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            with source.open("rb") as src, tmp_path.open("wb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
            tmp_path.replace(target)
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _art_cache_paths(self, song_id: str) -> tuple[Path, Path]:
        art_dir = self.cache_dir / "art"
        art_dir.mkdir(parents=True, exist_ok=True)
        safe_id = sanitize_filename(song_id, "remote-art")
        return art_dir / f"{safe_id}.bin", art_dir / f"{safe_id}.json"

    def _read_cached_art(self, song_id: str) -> tuple[bytes, str] | None:
        content_path, metadata_path = self._art_cache_paths(song_id)
        if not content_path.exists():
            return None
        media_type = "image/png"
        if metadata_path.exists():
            try:
                metadata = json.loads(metadata_path.read_text())
                media_type = str(metadata.get("mediaType") or media_type)
            except Exception:
                pass
        try:
            return content_path.read_bytes(), media_type
        except OSError:
            return None

    def _write_cached_art(self, song_id: str, content: bytes, media_type: str) -> None:
        content_path, metadata_path = self._art_cache_paths(song_id)
        try:
            content_path.write_bytes(content)
            metadata_path.write_text(json.dumps({"mediaType": media_type}))
        except OSError:
            pass

    def _source_folder_name(self) -> str:
        return safe_path_segment(self.source.get("sourceId") or self.label or self.id, "remote-source")

    def _library_target(self, filename: str, content_hash: str) -> tuple[Path, str] | None:
        if not self.local_library_root or not self.local_library_root.exists() or not self.local_library_root.is_dir():
            return None
        target_dir = self.local_library_root / self._source_folder_name()
        target_dir.mkdir(parents=True, exist_ok=True)
        safe_name = sanitize_filename(Path(filename).name, "remote-song.psarc")
        target = target_dir / safe_name
        if target.exists() and _sha256_file(target) == content_hash:
            return target, target.relative_to(self.local_library_root).as_posix()
        stem = target.stem or "remote-song"
        suffix = target.suffix or ".psarc"
        for index in range(1, 1000):
            candidate = target if index == 1 else target_dir / f"{stem}-{index}{suffix}"
            if not candidate.exists():
                return candidate, candidate.relative_to(self.local_library_root).as_posix()
            if _sha256_file(candidate) == content_hash:
                return candidate, candidate.relative_to(self.local_library_root).as_posix()
        raise RuntimeError("unable to allocate a unique local library filename")

    def _write_atomic(self, target: Path, content: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            tmp_path.write_bytes(content)
            tmp_path.replace(target)
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _nam_db_path(self) -> Path | None:
        return self.nam_config_dir / "nam_tone.db" if self.nam_config_dir else None

    def _nam_models_dir(self) -> Path | None:
        return self.nam_config_dir / "nam_models" if self.nam_config_dir else None

    def _nam_irs_dir(self) -> Path | None:
        return self.nam_config_dir / "nam_irs" if self.nam_config_dir else None

    def _safe_child(self, root: Path, name: str | None) -> Path | None:
        if not name:
            return None
        root_resolved = root.resolve()
        path = (root / name).resolve()
        try:
            path.relative_to(root_resolved)
        except ValueError:
            return None
        return path

    def _asset_path_from_url(self, url: str) -> str:
        parsed = parse.urlparse(str(url or ""))
        if parsed.scheme or parsed.netloc:
            return parsed.path or "/"
        return str(url or "")

    def _hash_matches(self, content: bytes, expected: str | None) -> bool:
        if not expected:
            return True
        actual = _sha256_bytes(content)
        normalized = str(expected).lower().removeprefix("sha256:")
        return actual == normalized

    def _allocate_nam_asset_path(
        self, root: Path, name: str, content: bytes, expected_hash: str | None
    ) -> tuple[Path, str, bool]:
        target = self._safe_child(root, name)
        if target is None:
            safe_name = sanitize_filename(Path(name).name, "nam-asset")
            target = root / safe_name
        target.parent.mkdir(parents=True, exist_ok=True)
        content_hash = _sha256_bytes(content)
        if target.exists():
            if _sha256_file(target) == content_hash:
                return target, target.relative_to(root).as_posix(), False
            suffix = target.suffix
            stem = target.stem or "nam-asset"
            short_hash = (str(expected_hash or content_hash).removeprefix("sha256:") or content_hash)[:10]
            target = target.with_name(f"{stem}-{short_hash}{suffix}")
        if target.exists() and _sha256_file(target) == content_hash:
            return target, target.relative_to(root).as_posix(), False
        self._write_atomic(target, content)
        return target, target.relative_to(root).as_posix(), True

    def _download_nam_asset(self, asset: dict | None, asset_type: str) -> tuple[str, bool]:
        if not asset:
            return "", False
        root = self._nam_models_dir() if asset_type == "model" else self._nam_irs_dir()
        if root is None:
            raise RuntimeError("NAM Tone config directory is unavailable")
        root.mkdir(parents=True, exist_ok=True)
        url = asset.get("url") or ""
        name = str(asset.get("name") or Path(self._asset_path_from_url(url)).name or f"asset.{asset_type}")
        content, _media_type, _headers = self._bytes(self._asset_path_from_url(url))
        expected_hash = asset.get("sha256")
        if not self._hash_matches(content, expected_hash):
            raise RuntimeError(f"Downloaded {asset_type} asset hash did not match: {name}")
        _target, local_name, wrote = self._allocate_nam_asset_path(root, name, content, expected_hash)
        return local_name, wrote

    def _ensure_nam_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS presets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                model_file TEXT,
                ir_file TEXT,
                input_gain REAL NOT NULL DEFAULT 1.0,
                output_gain REAL NOT NULL DEFAULT 0.5,
                gate_threshold REAL NOT NULL DEFAULT -60.0,
                settings_json TEXT NOT NULL DEFAULT '{}'
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tone_mappings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,
                tone_key TEXT NOT NULL,
                preset_id INTEGER NOT NULL,
                UNIQUE(filename, tone_key),
                FOREIGN KEY (preset_id) REFERENCES presets(id)
            )
        """)

    def _remote_preset_identity(self, preset: dict) -> str:
        return f"{self.id}:{preset.get('ref') or preset.get('name') or 'preset'}"

    def _find_imported_preset_id(self, conn: sqlite3.Connection, remote_id: str) -> int | None:
        rows = conn.execute("SELECT id, settings_json FROM presets").fetchall()
        for preset_id, settings_json in rows:
            try:
                settings = json.loads(settings_json or "{}")
            except json.JSONDecodeError:
                continue
            metadata = settings.get("remoteLibraryClient") if isinstance(settings, dict) else None
            if isinstance(metadata, dict) and metadata.get("remoteId") == remote_id:
                return int(preset_id)
        return None

    def _remote_owned_preset_ids(self, conn: sqlite3.Connection) -> list[int]:
        preset_ids = []
        rows = conn.execute("SELECT id, settings_json FROM presets").fetchall()
        remote_prefix = f"{self.id}:"
        for preset_id, settings_json in rows:
            try:
                settings = json.loads(settings_json or "{}")
            except json.JSONDecodeError:
                continue
            metadata = settings.get("remoteLibraryClient") if isinstance(settings, dict) else None
            if isinstance(metadata, dict) and str(metadata.get("remoteId") or "").startswith(remote_prefix):
                preset_ids.append(int(preset_id))
        return preset_ids

    def _delete_remote_owned_mappings(self, conn: sqlite3.Connection, mapping_keys: list[str]) -> None:
        preset_ids = self._remote_owned_preset_ids(conn)
        if not mapping_keys or not preset_ids:
            return
        filename_placeholders = ",".join("?" for _ in mapping_keys)
        preset_placeholders = ",".join("?" for _ in preset_ids)
        conn.execute(
            "DELETE FROM tone_mappings "
            f"WHERE filename IN ({filename_placeholders}) AND preset_id IN ({preset_placeholders})",
            (*mapping_keys, *preset_ids),
        )

    def _unique_preset_name(self, conn: sqlite3.Connection, preferred: str) -> str:
        base = preferred.strip() or "Remote NAM preset"
        existing = {row[0] for row in conn.execute("SELECT name FROM presets").fetchall()}
        if base not in existing:
            return base
        for index in range(2, 1000):
            candidate = f"{base} {index}"
            if candidate not in existing:
                return candidate
        raise RuntimeError("unable to allocate a unique NAM preset name")

    def _install_nam_tone_sync(self, payload: dict, local_filename: str) -> dict:
        db_path = self._nam_db_path()
        if db_path is None:
            return {"ok": False, "skipped": True, "reason": "NAM Tone config directory is unavailable"}
        db_path.parent.mkdir(parents=True, exist_ok=True)
        presets = list(payload.get("presets") or [])
        mappings = list(payload.get("mappings") or [])
        preset_id_by_ref: dict[str, int] = {}
        assets_imported = 0
        assets_reused = 0
        mapping_keys = []
        if local_filename:
            mapping_keys.append(playback_settings_key(local_filename))
            mapping_keys.append(local_filename)
        for key in (
            payload.get("targetSettingsKey"),
            payload.get("settingsKey"),
            payload.get("sourceSettingsKey"),
        ):
            key = str(key or "")
            if key and key not in mapping_keys:
                mapping_keys.append(key)
        with closing(sqlite3.connect(db_path)) as conn:
            self._ensure_nam_schema(conn)
            for preset in presets:
                model_file, model_wrote = self._download_nam_asset(preset.get("modelFile"), "model")
                ir_file, ir_wrote = self._download_nam_asset(preset.get("irFile"), "ir")
                assets_imported += int(bool(model_file and model_wrote)) + int(bool(ir_file and ir_wrote))
                assets_reused += int(bool(model_file and not model_wrote)) + int(bool(ir_file and not ir_wrote))
                settings = dict(preset.get("settings") or {})
                remote_id = self._remote_preset_identity(preset)
                settings["remoteLibraryClient"] = {
                    "remoteId": remote_id,
                    "sourceId": self.source.get("sourceId") or "",
                    "sourceName": self.label,
                    "remotePresetRef": preset.get("ref") or "",
                }
                existing_id = self._find_imported_preset_id(conn, remote_id)
                name = str(preset.get("name") or "Remote NAM preset")
                if existing_id:
                    existing_name = conn.execute("SELECT name FROM presets WHERE id = ?", (existing_id,)).fetchone()[0]
                    conn.execute(
                        "UPDATE presets SET name = ?, model_file = ?, ir_file = ?, input_gain = ?, "
                        "output_gain = ?, gate_threshold = ?, settings_json = ? WHERE id = ?",
                        (
                            existing_name,
                            model_file,
                            ir_file,
                            preset.get("inputGain", 1.0),
                            preset.get("outputGain", 0.5),
                            preset.get("gateThreshold", -60.0),
                            json.dumps(settings),
                            existing_id,
                        ),
                    )
                    preset_id_by_ref[str(preset.get("ref") or "")] = existing_id
                else:
                    display_name = self._unique_preset_name(conn, f"{self.label} / {name}")
                    cursor = conn.execute(
                        "INSERT INTO presets "
                        "(name, model_file, ir_file, input_gain, output_gain, gate_threshold, settings_json) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            display_name,
                            model_file,
                            ir_file,
                            preset.get("inputGain", 1.0),
                            preset.get("outputGain", 0.5),
                            preset.get("gateThreshold", -60.0),
                            json.dumps(settings),
                        ),
                    )
                    preset_id_by_ref[str(preset.get("ref") or "")] = int(cursor.lastrowid)
            mappings_imported = 0
            self._delete_remote_owned_mappings(conn, mapping_keys)
            for mapping in mappings:
                tone_key = str(mapping.get("toneKey") or "")
                preset_id = preset_id_by_ref.get(str(mapping.get("presetRef") or ""))
                if not tone_key or not preset_id:
                    continue
                for mapping_key in mapping_keys:
                    conn.execute(
                        "INSERT OR REPLACE INTO tone_mappings (filename, tone_key, preset_id) VALUES (?, ?, ?)",
                        (mapping_key, tone_key, preset_id),
                    )
                mappings_imported += 1
            conn.commit()
        return {
            "ok": True,
            "skipped": False,
            "presetsImported": len(preset_id_by_ref),
            "mappingsImported": mappings_imported,
            "mappingKey": mapping_keys[0] if mapping_keys else "",
            "mappingKeys": mapping_keys,
            "assetsImported": assets_imported,
            "assetsReused": assets_reused,
            "warnings": list(payload.get("warnings") or []),
        }

    def sync_nam_tones(self, song_id: str, local_filename: str) -> dict:
        if not self.source.get("syncNamToneAssets"):
            return {"ok": True, "skipped": True, "reason": "disabled"}
        if not local_filename:
            return {"ok": False, "skipped": True, "reason": "song was not imported into the local library"}
        try:
            payload = self._json(f"/songs/{parse.quote(song_id)}/nam-tone-sync", timeout=20)
        except RuntimeError as exc:
            raw_detail = str(exc)
            if "disabled" in raw_detail or "404" in raw_detail or "not found" in raw_detail:
                return {"ok": False, "skipped": True, "reason": _public_error_message(exc)}
            raise
        if payload.get("schema") != "slopsmith.nam-tone-sync.v1":
            return {"ok": False, "skipped": True, "reason": "unsupported NAM tone sync manifest"}
        return self._install_nam_tone_sync(payload, local_filename)

    def _remote_query_params(
        self,
        *,
        page: int,
        size: int,
        sort: str,
        direction: str,
        q: str = "",
        **kwargs,
    ) -> dict:
        params = {
            "q": str(q or "")[:1000],
            "page": max(0, int(page or 0)),
            "pageSize": max(1, min(100, int(size or 24))),
            "sort": sort or "artist",
            "direction": direction or "asc",
        }
        if kwargs.get("format_filter"):
            params["format"] = kwargs["format_filter"]
        for key in ("arrangements_has", "arrangements_lacks", "stems_has", "stems_lacks", "tunings"):
            values = _query_filter_values(kwargs.get(key))
            if values:
                params[key] = ",".join(values)
        has_lyrics = kwargs.get("has_lyrics")
        if has_lyrics is not None:
            params["has_lyrics"] = str(int(bool(has_lyrics)))
        return params

    def _normalize_song(self, song: dict) -> dict:
        remote_id = str(song.get("remoteSongId") or song.get("songId") or song.get("id") or "")
        title = song.get("title") or remote_id or "Remote song"
        package_form = song.get("packageForm") or ""
        song_format = song.get("format") or ("sloppak" if "sloppak" in package_form else "psarc")
        stem_ids = list(song.get("stem_ids") or song.get("stemIds") or [])
        stem_count = song.get("stem_count", song.get("stemCount"))
        if stem_count is None:
            stem_count = len(stem_ids)
        stem_count = _safe_int(stem_count, len(stem_ids))
        return {
            **song,
            "filename": remote_id,
            "song_id": remote_id,
            "remote_id": remote_id,
            "remoteSongId": remote_id,
            "libraryProviderId": self.id,
            "provider": self.id,
            "sourceId": song.get("sourceId") or self.source.get("sourceId"),
            "sourceName": self.label,
            "title": title,
            "artist": song.get("artist") or "Unknown artist",
            "album": song.get("album") or "",
            "format": song_format,
            "stem_count": stem_count,
            "stem_ids": stem_ids,
            "localFilename": "",
            "local_filename": "",
            "playFilename": "",
            "arrangements": list(song.get("arrangements") or []),
            "has_lyrics": bool(song.get("has_lyrics") or song.get("hasLyrics")),
            "tuning": song.get("tuning") or song.get("tuningName") or song.get("tuning_name") or "",
            "tuning_name": song.get("tuning_name") or song.get("tuningName") or song.get("tuning") or "",
            "sizeBytes": song.get("sizeBytes") or song.get("size_bytes") or 0,
        }

    def _normalize_artist_payload(self, artists: list[dict]) -> list[dict]:
        normalized_artists = []
        for artist in artists:
            albums = []
            for album in artist.get("albums") or []:
                songs = [self._normalize_song(song) for song in album.get("songs") or []]
                albums.append({**album, "songs": songs})
            normalized_artists.append({**artist, "albums": albums})
        return normalized_artists

    def query_page(self, page: int = 0, size: int = 24, sort: str = "artist", direction: str = "asc", **kwargs):
        if kwargs.get("favorites_only"):
            return [], 0
        payload = self._json_cached(
            "/songs",
            self._remote_query_params(page=page, size=size, sort=sort, direction=direction, **kwargs),
        )
        songs = [self._normalize_song(song) for song in payload.get("songs") or []]
        return songs, _safe_int(payload.get("total"), len(songs))

    def query_artists(self, letter: str = "", page: int = 0, size: int = 50, **kwargs):
        if kwargs.get("favorites_only"):
            return [], 0
        params = self._remote_query_params(page=page, size=size, sort="artist", direction="asc", **kwargs)
        if letter:
            params["letter"] = letter
        payload = self._json_cached("/artists", params)
        return self._normalize_artist_payload(payload.get("artists") or []), _safe_int(payload.get("total_artists"), 0)

    def query_stats(self, **kwargs) -> dict:
        if kwargs.get("favorites_only"):
            return {"total_songs": 0, "total_artists": 0, "letters": {}}
        payload = self._json_cached(
            "/stats",
            self._remote_query_params(page=0, size=1, sort="artist", direction="asc", **kwargs),
        )
        return {
            "total_songs": _safe_int(payload.get("total_songs"), 0),
            "total_artists": _safe_int(payload.get("total_artists"), 0),
            "letters": dict(payload.get("letters") or {}),
        }

    def tuning_names(self) -> dict:
        payload = self._json_cached("/tuning-names")
        tunings = payload.get("tunings")
        return {"tunings": tunings if isinstance(tunings, list) else []}

    def get_art(self, song_id: str):
        cached = self._read_cached_art(song_id)
        if cached:
            content, media_type = cached
            return Response(content=content, media_type=media_type, headers={"Cache-Control": "public, max-age=86400"})
        try:
            content, media_type, _headers = self._bytes(f"/songs/{parse.quote(song_id)}/art")
        except RuntimeError as exc:
            if "404" in str(exc) or "artwork not found" in str(exc) or "song not found" in str(exc):
                return None
            raise
        self._write_cached_art(song_id, content, media_type)
        return Response(content=content, media_type=media_type, headers={"Cache-Control": "public, max-age=86400"})

    def sync_song(self, song_id: str) -> dict:
        fallback_filename = sanitize_filename(song_id) + ".psarc"
        target, content_hash, bytes_read, headers = self._download_to_cache(
            f"/songs/{parse.quote(song_id)}/package",
            fallback_filename,
        )
        filename = _header_filename(headers, fallback_filename)
        library_target = self._library_target(filename, content_hash)
        library_path = None
        local_filename = ""
        library_import_result = None
        library_import_error = ""
        if library_target:
            library_path, local_filename = library_target
            wrote_library = False
            if not library_path.exists() or _sha256_file(library_path) != content_hash:
                self._copy_file_atomic(target, library_path)
                wrote_library = True
            if self.library_importer and self.local_library_root:
                try:
                    library_import_result = self.library_importer(library_path, self.local_library_root)
                except Exception as exc:
                    library_import_error = _public_error_message(exc)
                    if wrote_library:
                        try:
                            library_path.unlink(missing_ok=True)
                        except OSError:
                            pass
                    library_path = None
                    local_filename = ""
        self.clear_metadata_cache()
        result = {
            "ok": True,
            "song_id": song_id,
            "remoteSongId": song_id,
            "cached": True,
            "cacheState": "ready",
            "bytes": bytes_read,
        }
        if library_path and local_filename:
            result.update({
                "filename": local_filename,
                "localFilename": local_filename,
                "local_filename": local_filename,
                "playFilename": local_filename,
                "libraryRelativePath": local_filename,
                "libraryImportState": "indexed" if library_import_result else "staged",
                "playbackSource": "library-folder",
            })
            if library_import_result:
                result.update(library_import_result)
        else:
            result["playbackSource"] = "remote-cache"
            if library_import_error:
                result["libraryImportState"] = "failed"
                result["libraryImportError"] = library_import_error
        if self.source.get("syncNamToneAssets"):
            try:
                result["toneSync"] = self.sync_nam_tones(song_id, local_filename)
            except Exception as exc:
                result["toneSync"] = {"ok": False, "skipped": False, "error": _public_error_message(exc)}
        return result