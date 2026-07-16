# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import secrets
from datetime import datetime, timezone
from pathlib import Path
from urllib import parse

from fastapi import HTTPException

from remote_library_client.feedforge import (
    CATALOG_SYNCING_MESSAGE,
    KEY_MIGRATE_MESSAGE,
    KEY_REQUIRED_MESSAGE,
    FeedForgeProvider,
    is_feedforge_url,
    normalize_feedforge_base_url,
)
from remote_library_client.google_drive import (
    GoogleDrivePublicFolderProvider,
    is_google_drive_folder_url,
    parse_drive_folder_id,
)
from remote_library_client.iroh_transport import (
    IrohLibraryProvider,
    IrohUnreachableError,
    is_iroh_id,
    normalize_iroh_id,
)
from remote_library_client.proton_drive import (
    ProtonPublicShareProvider,
    is_proton_share_url,
    parse_proton_share_url,
)
from remote_library_client.provider import (
    AuthRequiredError,
    BaseLibraryProvider,
    DirectLibraryProvider,
    _public_error_message,
    _safe_int,
    provider_id_for_source,
)
from remote_library_client.store import RemoteLibraryClientStore

_store: RemoteLibraryClientStore | None = None
_register_provider = None
_unregister_provider = None
_cache_dir: Path | None = None
_get_dlc_dir = None
_extract_meta = None
_meta_db = None
_config_dir: Path | None = None
_providers: dict[str, BaseLibraryProvider] = {}
DEFAULT_SOURCE_PORT = 8765

# Registered library-provider types, keyed by the source's stored `type`. New remote
# backends (Google Drive, ...) register here; a source with no `type` is the original
# Remote Library Server (`slopsmith-direct-library.v1`) for backward compatibility.
PROVIDER_TYPES = {
    DirectLibraryProvider.type: DirectLibraryProvider,
    GoogleDrivePublicFolderProvider.type: GoogleDrivePublicFolderProvider,
    ProtonPublicShareProvider.type: ProtonPublicShareProvider,
    IrohLibraryProvider.type: IrohLibraryProvider,
    FeedForgeProvider.type: FeedForgeProvider,
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _source_enabled(source: dict) -> bool:
    return source.get("enabled") is not False


def _format_base_url(scheme: str, netloc: str, *, add_default_port: bool = True) -> str:
    parsed = parse.urlparse(f"{scheme}://{netloc}")
    if not parsed.hostname:
        raise ValueError("baseUrl must include a host")
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = parsed.port or (DEFAULT_SOURCE_PORT if add_default_port else None)
    return f"{scheme}://{host}{f':{port}' if port else ''}".rstrip("/")


def _candidate_base_urls(value: str) -> list[str]:
    raw = str(value or "").strip().rstrip("/")
    if not raw:
        raise ValueError("Enter a server URL or hostname")
    candidates = []
    if "://" in raw:
        parsed = parse.urlparse(raw)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("baseUrl must be an http(s) URL")
        candidates.append(_format_base_url(parsed.scheme, parsed.netloc, add_default_port=False))
        if parsed.port is None:
            candidates.append(_format_base_url(parsed.scheme, parsed.netloc))
    else:
        raw = raw.lstrip("/")
        if "/" in raw:
            raise ValueError("baseUrl hostname cannot include a path")
        for scheme in ("http", "https"):
            candidates.append(_format_base_url(scheme, raw))
    unique_candidates = []
    for candidate in candidates:
        if candidate not in unique_candidates:
            unique_candidates.append(candidate)
    return unique_candidates


def _probe_first_available(value: str, token: str = "") -> tuple[str, dict]:
    errors = []
    for base_url in _candidate_base_urls(value):
        try:
            return base_url, _probe_source(base_url, token)
        except AuthRequiredError:
            # The server answered — it just needs a token. Stop laddering and surface that.
            raise
        except Exception as exc:
            errors.append(f"{base_url}: {exc}")
    detail = "Could not connect to a Remote Library Server."
    if errors:
        detail = f"{detail} Tried: {'; '.join(errors)}"
    raise ValueError(detail)


def _source_cache_dir() -> Path:
    root = _cache_dir or (_store.root / "cache")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _source_type(source: dict) -> str:
    return str(source.get("type") or DirectLibraryProvider.type)


def _provider_for_source(source: dict) -> BaseLibraryProvider:
    local_root = _get_dlc_dir() if callable(_get_dlc_dir) else None
    provider_cls = PROVIDER_TYPES.get(_source_type(source), DirectLibraryProvider)
    return provider_cls(source, _source_cache_dir(), local_root, _import_library_file, _config_dir)


def _import_library_file(package_path: Path, local_root: Path) -> dict | None:
    if not callable(_extract_meta) or _meta_db is None or not hasattr(_meta_db, "put"):
        return None
    metadata = _extract_meta(package_path)
    stat = package_path.stat()
    try:
        filename = package_path.relative_to(local_root).as_posix()
    except ValueError:
        filename = package_path.name
    _meta_db.put(filename, stat.st_mtime, stat.st_size, metadata)
    return {"libraryImportState": "indexed", "libraryFilename": filename}


def _register_provider_instance(
    provider: BaseLibraryProvider, source: dict, *, replace: bool = True
) -> BaseLibraryProvider | None:
    """Register an already-built provider, keeping its warm state (catalog mirror, sessions).

    Constructing a *second* instance from the same source and registering that instead leaves
    the registered one cold — for FeedForge that meant an unregistered throwaway thread walked
    the whole catalog while the provider actually serving core browsed an empty mirror."""
    if not _source_enabled(source):
        _unregister_source_provider(source.get("providerId") or "")
        return None
    if callable(_register_provider):
        _register_provider(provider, replace=replace)
    _providers[provider.id] = provider
    return provider


def _register_source_provider(source: dict, *, replace: bool = True) -> BaseLibraryProvider | None:
    if not _source_enabled(source):
        _unregister_source_provider(source.get("providerId") or "")
        return None
    return _register_provider_instance(_provider_for_source(source), source, replace=replace)


def _unregister_source_provider(provider_id: str) -> None:
    _providers.pop(provider_id, None)
    if callable(_unregister_provider):
        try:
            _unregister_provider(provider_id)
        except ValueError:
            pass


def _probe_source(base_url: str, token: str = "") -> dict:
    probe = DirectLibraryProvider({
        "baseUrl": base_url,
        "providerId": provider_id_for_source("probe", base_url),
        "label": base_url,
        "token": token,
    }, _source_cache_dir())
    return probe._json("/source", timeout=3)


def _source_from_payload(base_url: str, payload: dict, label: str = "") -> dict:
    source_id = str(payload.get("sourceId") or "")
    provider_id = provider_id_for_source(source_id, base_url)
    source_name = str(payload.get("sourceName") or label or base_url)
    return {
        "providerId": provider_id,
        "baseUrl": base_url,
        "sourceId": source_id,
        "sourceName": source_name,
        "label": label or source_name,
        "protocol": (payload.get("server") or {}).get("protocol") or "slopsmith-direct-library.v1",
        "songCount": _safe_int(payload.get("songCount"), 0),
        "remoteCapabilities": list(payload.get("capabilities") or []),
        "namToneSyncAvailable": bool((payload.get("namToneSync") or {}).get("enabled"))
            or "nam-tone-sync.read" in set(payload.get("capabilities") or []),
        "authRequired": bool((payload.get("auth") or {}).get("required")),
        "enabled": True,
        "lastSuccessfulContactAt": _utc_now_iso(),
    }


def _save_checked_source(source: dict, payload: dict) -> dict:
    updated = {
        **source,
        **_source_from_payload(source.get("baseUrl") or "", payload, source.get("label") or ""),
        "enabled": _source_enabled(source),
        "syncNamToneAssets": bool(source.get("syncNamToneAssets")),
        "allowUnsafeRedirects": bool(source.get("allowUnsafeRedirects")),
        "token": str(source.get("token") or ""),
    }
    old_provider_id = source.get("providerId") or ""
    new_provider_id = updated.get("providerId") or ""
    if old_provider_id and old_provider_id != new_provider_id:
        _store.remove_source(old_provider_id)
        _unregister_source_provider(old_provider_id)
    provider = _register_source_provider(updated, replace=True)
    _store.upsert_source(updated)
    return {
        **updated,
        "registered": bool(provider and provider.id in _providers),
        "online": bool(payload.get("ok", True)),
        "message": "",
    }


def _google_source(seed: dict) -> dict:
    """Build (or refresh) a Google Drive public-folder source from user input or a stored
    record: normalize the folder URL, construct the provider, and enumerate the folder to
    validate reachability and count songs. Raises if the folder is not reachable."""
    folder_id = parse_drive_folder_id(seed.get("baseUrl") or seed.get("folderId") or "")
    if not folder_id:
        raise ValueError("not a recognizable Google Drive folder URL")
    base_url = f"https://drive.google.com/drive/folders/{folder_id}"
    source = {
        **seed,
        "type": GoogleDrivePublicFolderProvider.type,
        "baseUrl": base_url,
        "folderId": folder_id,
        "providerId": seed.get("providerId")
        or provider_id_for_source(f"gdrive_{folder_id}", base_url, prefix="gdrive"),
        "enabled": _source_enabled(seed),
        "syncNamToneAssets": False,
        "allowUnsafeRedirects": bool(seed.get("allowUnsafeRedirects")),
        "token": "",
    }
    provider = _provider_for_source(source)
    info = provider.describe_source()
    label = str(seed.get("label") or "").strip()
    source.update({
        "sourceId": info["sourceId"],
        "sourceName": info["sourceName"],
        "label": label or seed.get("label") or info["sourceName"],
        "protocol": GoogleDrivePublicFolderProvider.type,
        "songCount": _safe_int(info.get("songCount"), 0),
        "remoteCapabilities": list(info.get("capabilities") or []),
        "namToneSyncAvailable": False,
        "authRequired": False,
        "lastSuccessfulContactAt": _utc_now_iso(),
    })
    return source


def _save_checked_google_source(source: dict) -> dict:
    updated = _google_source(source)
    provider = _register_source_provider(updated, replace=True)
    _store.upsert_source(updated)
    return {
        **updated,
        "registered": bool(provider and provider.id in _providers),
        "online": True,
        "message": "",
    }


def _proton_source(seed: dict, provider: BaseLibraryProvider | None = None) -> dict:
    """Build (or refresh) a Proton public-share source: parse the token + URL password from the
    pasted link, then authenticate + list to validate reachability and count songs. The URL
    password is a secret — stored as ``urlPassword``, never placed in the displayable ``baseUrl``.

    Pass an already-registered ``provider`` to reuse its cached SRP session — Proton rate-limits
    repeated anonymous auth, so a fresh handshake on every status poll would risk a throttle.
    """
    parsed = parse_proton_share_url(seed.get("baseUrl") or "")
    token = str(seed.get("shareToken") or (parsed[0] if parsed else "")).strip()
    password = str(seed.get("urlPassword") or (parsed[1] if parsed else "")).strip()
    if not token:
        raise ValueError("not a recognizable Proton public-share URL")
    if not password:
        raise ValueError("paste the full Proton share link, including the password after '#'")
    base_url = f"https://drive.proton.me/urls/{token}"
    source = {
        **seed,
        "type": ProtonPublicShareProvider.type,
        "baseUrl": base_url,
        "shareToken": token,
        "urlPassword": password,
        "providerId": seed.get("providerId")
        or provider_id_for_source(f"proton_{token}", base_url, prefix="proton"),
        "enabled": _source_enabled(seed),
        "syncNamToneAssets": False,
        "allowUnsafeRedirects": bool(seed.get("allowUnsafeRedirects")),
        "token": "",
    }
    info = (provider or _provider_for_source(source)).describe_source()
    label = str(seed.get("label") or "").strip()
    source.update({
        "sourceId": info["sourceId"],
        "sourceName": info["sourceName"],
        "label": label or seed.get("label") or info["sourceName"],
        "protocol": ProtonPublicShareProvider.type,
        "songCount": _safe_int(info.get("songCount"), 0),
        "remoteCapabilities": list(info.get("capabilities") or []),
        "namToneSyncAvailable": False,
        "authRequired": False,
        "lastSuccessfulContactAt": _utc_now_iso(),
    })
    return source


def _save_checked_proton_source(source: dict) -> dict:
    # Reuse the registered provider (and its cached session) when present; only build + register
    # a fresh one when the source is not yet registered (e.g. first add, or after a restart).
    existing = _providers.get(source.get("providerId") or "")
    updated = _proton_source(source, provider=existing)
    provider = existing or _register_source_provider(updated, replace=True)
    _store.upsert_source(updated)
    return {
        **updated,
        "registered": bool(provider and provider.id in _providers),
        "online": True,
        "message": "",
    }


def _iroh_source(seed: dict) -> dict:
    """Build (or refresh) a Remote Server (iroh) source: validate the Library ID, then reach the
    server *over iroh* and read ``/source`` to validate + capture its metadata. It speaks the exact
    same protocol as the direct server type (token auth, NAM tones, artwork) — only the transport
    differs, so almost everything is reused from :class:`DirectLibraryProvider`."""
    iroh_id = normalize_iroh_id(seed.get("irohId") or seed.get("baseUrl") or "")
    if not is_iroh_id(iroh_id):
        raise ValueError("not a valid iroh Library ID")
    token = str(seed.get("token") or "").strip()
    source = {
        **seed,
        "type": IrohLibraryProvider.type,
        "irohId": iroh_id,
        "baseUrl": f"iroh://{iroh_id[:16]}…",  # display-only; the real identity is irohId
        "providerId": seed.get("providerId")
        or provider_id_for_source(f"iroh_{iroh_id[:24]}", iroh_id, prefix="iroh"),
        "enabled": _source_enabled(seed),
        "syncNamToneAssets": bool(seed.get("syncNamToneAssets")),
        "allowUnsafeRedirects": False,
        "token": token,
    }
    # Use the short status-probe budget (not a long default) so an offline server surfaces promptly
    # here — on add/refresh and on every /status poll — instead of blocking on iroh discovery.
    payload = _provider_for_source(source).probe_source()
    capabilities = set(payload.get("capabilities") or [])
    # Validate the handshake: any live iroh endpoint on this ALPN answers, so make sure it's actually
    # a Remote Library Server (not, say, a leftover test peer) — otherwise it silently browses empty.
    if "library.read" not in capabilities and not (payload.get("server") or {}).get("protocol"):
        raise ValueError("This iroh endpoint is not a Remote Library Server.")
    label = str(seed.get("label") or "").strip()
    source.update({
        "sourceId": str(payload.get("sourceId") or ""),
        "sourceName": str(payload.get("sourceName") or label or "iroh library"),
        "label": label or seed.get("label") or payload.get("sourceName") or "iroh library",
        "protocol": IrohLibraryProvider.type,
        "songCount": _safe_int(payload.get("songCount"), 0),
        "remoteCapabilities": list(payload.get("capabilities") or []),
        "namToneSyncAvailable": bool((payload.get("namToneSync") or {}).get("enabled"))
        or "nam-tone-sync.read" in capabilities,
        "authRequired": bool((payload.get("auth") or {}).get("required")),
        "lastSuccessfulContactAt": _utc_now_iso(),
    })
    return source


def _save_checked_iroh_source(source: dict) -> dict:
    updated = _iroh_source(source)
    provider = _register_source_provider(updated, replace=True)
    _store.upsert_source(updated)
    return {
        **updated,
        "registered": bool(provider and provider.id in _providers),
        "online": True,
        "message": "",
    }


def _feedforge_source(seed: dict, provider: BaseLibraryProvider | None = None) -> tuple[dict, BaseLibraryProvider]:
    """Build (or refresh) a FeedForge source from a user-created **access key** (the ``token``
    field — a secret, stripped from every API response by ``_public_source``), validating it
    against the v1 API and counting songs from the catalog mirror. A legacy credentials-era
    source (username/password, no key) raises ``AuthRequiredError`` so its card prompts for a
    key; the stored password is dropped the moment a key takes over.

    Returns ``(source, provider)`` — **the provider that performed the describe**, so callers
    register that exact instance. Pass an already-registered ``provider`` to reuse its catalog
    mirror; building a fresh instance per call would kick a from-scratch catalog walk each
    time (and once left the registered provider cold while a throwaway thread walked)."""
    base_url = normalize_feedforge_base_url(seed.get("baseUrl") or "")
    token = str(seed.get("token") or "").strip()
    if not token:
        legacy = bool(seed.get("password") or seed.get("username"))
        raise AuthRequiredError(KEY_MIGRATE_MESSAGE if legacy else KEY_REQUIRED_MESSAGE)
    host = parse.urlparse(base_url).hostname or "feedforge.org"
    # The v1 API exposes no account identity (no whoami endpoint — raised with the FeedForge
    # dev), so mint a per-source random seed once: it keeps providerId (and the local import
    # folder) stable across key rotations, and two different accounts distinct. Legacy sources
    # keep their username-derived providerId.
    account_seed = str(seed.get("accountSeed") or "").strip()
    if not account_seed and not seed.get("providerId"):
        account_seed = secrets.token_hex(4)
    ident = account_seed or str(seed.get("username") or "").strip()
    source = {
        **seed,
        "type": FeedForgeProvider.type,
        "baseUrl": base_url,
        "token": token,
        "accountSeed": account_seed,
        "providerId": seed.get("providerId")
        or provider_id_for_source(
            f"feedforge_{host}_{ident}" if ident else f"feedforge_{host}", base_url, prefix="feedforge"
        ),
        "enabled": _source_enabled(seed),
        "syncNamToneAssets": False,
        "allowUnsafeRedirects": bool(seed.get("allowUnsafeRedirects")),
    }
    source.pop("password", None)  # the credentials era is over; never keep an unused secret
    provider = provider or _provider_for_source(source)
    info = provider.describe_source()
    label = str(seed.get("label") or "").strip()
    source.update({
        "sourceId": info["sourceId"],
        "sourceName": info["sourceName"],
        "label": label or seed.get("label") or info["sourceName"],
        "protocol": FeedForgeProvider.type,
        "songCount": _safe_int(info.get("songCount"), 0),
        "remoteCapabilities": list(info.get("capabilities") or []),
        "namToneSyncAvailable": False,
        "authRequired": False,
        "lastSuccessfulContactAt": _utc_now_iso(),
    })
    return source, provider


def _save_checked_feedforge_source(source: dict) -> dict:
    # Reuse the registered provider (and its catalog mirror) when present; on first
    # registration, register the SAME instance that just described — never a second cold one.
    existing = _providers.get(source.get("providerId") or "")
    updated, provider = _feedforge_source(source, provider=existing)
    if not existing:
        provider = _register_provider_instance(provider, updated)
    _store.upsert_source(updated)
    # While the initial walk is still filling the mirror, say so on the card — the count is an
    # at-least value that grows on each refresh until the walk completes.
    syncing = bool(provider and getattr(provider, "catalog_syncing", False))
    return {
        **updated,
        "registered": bool(provider and provider.id in _providers),
        "online": True,
        "message": CATALOG_SYNCING_MESSAGE if syncing else "",
    }


def _provider_payload(provider: BaseLibraryProvider | None) -> dict | None:
    if not provider:
        return None
    return {"id": provider.id, "label": provider.label}


def _public_source(source: dict) -> dict:
    # Never echo stored secrets back to the browser: the Remote Library Server bearer `token`,
    # the Proton share `urlPassword` (the URL fragment), and the FeedForge account `password`.
    # Expose only whether each is set (the FeedForge `username` is not a secret and is kept).
    public = {key: value for key, value in source.items() if key not in ("token", "urlPassword", "password")}
    public["hasToken"] = bool(str(source.get("token") or "").strip())
    public["hasPassword"] = bool(str(source.get("password") or "").strip())
    return public


def setup(app, context):
    global _store, _register_provider, _unregister_provider, _cache_dir
    global _get_dlc_dir, _extract_meta, _meta_db, _config_dir
    _config_dir = Path(context["config_dir"])
    _store = RemoteLibraryClientStore(_config_dir)
    _register_provider = context.get("register_library_provider")
    _unregister_provider = context.get("unregister_library_provider")
    _get_dlc_dir = context.get("get_dlc_dir")
    _extract_meta = context.get("extract_meta")
    _meta_db = context.get("meta_db")
    cache_factory = context.get("get_sloppak_cache_dir")
    _cache_dir = Path(cache_factory()) / "remote_library_client" if callable(cache_factory) else _store.root / "cache"
    for source in _store.list_sources():
        try:
            _register_source_provider(source, replace=True)
        except Exception:
            continue

    @app.get("/api/plugins/remote_library_client/settings")
    def get_settings():
        data = _store.load()
        return {"sources": [_public_source(item) for item in data.get("sources") or []]}

    @app.get("/api/plugins/remote_library_client/downloads")
    def downloads():
        # In-progress / recently-finished background song downloads, for the screen's
        # progress poller (Google Drive sources download out-of-band; see google_drive.py).
        items = []
        for provider in list(_providers.values()):
            reporter = getattr(provider, "active_downloads", None)
            if not callable(reporter):
                continue
            try:
                items.extend(reporter())
            except Exception:
                continue
        return {"downloads": items}

    @app.get("/api/plugins/remote_library_client/status")
    def status():
        sources = []
        for source in _store.list_sources():
            provider_id = source.get("providerId") or ""
            item = {
                **source,
                "enabled": _source_enabled(source),
                "registered": provider_id in _providers,
                "online": False,
                "message": "",
            }
            if not _source_enabled(source):
                item["message"] = "Disabled"
                sources.append(_public_source(item))
                continue
            if _source_type(source) == GoogleDrivePublicFolderProvider.type:
                try:
                    item.update(_save_checked_google_source(source))
                except Exception as exc:
                    item["message"] = _public_error_message(exc)
                sources.append(_public_source(item))
                continue
            if _source_type(source) == ProtonPublicShareProvider.type:
                try:
                    item.update(_save_checked_proton_source(source))
                except Exception as exc:
                    item["message"] = _public_error_message(exc)
                sources.append(_public_source(item))
                continue
            if _source_type(source) == IrohLibraryProvider.type:
                try:
                    item.update(_save_checked_iroh_source(source))
                except AuthRequiredError:
                    item["authRequired"] = True
                    item["message"] = (
                        "Access token rejected" if str(source.get("token") or "").strip() else "Access token required"
                    )
                except IrohUnreachableError as exc:
                    # Offline / unreachable server: keep the card in the Offline state (online stays
                    # False) with a clear message that screen.js renders, rather than a raw transport
                    # error or a source that just looks empty.
                    item["online"] = False
                    item["message"] = _public_error_message(exc)
                except Exception as exc:
                    item["message"] = _public_error_message(exc)
                sources.append(_public_source(item))
                continue
            if _source_type(source) == FeedForgeProvider.type:
                try:
                    item.update(_save_checked_feedforge_source(source))
                except AuthRequiredError as exc:
                    # Carries the actionable text: paste/replace a key, or (for a legacy
                    # credentials-era source) migrate to one.
                    item["authRequired"] = True
                    item["message"] = _public_error_message(exc)
                except Exception as exc:
                    item["message"] = _public_error_message(exc)
                sources.append(_public_source(item))
                continue
            try:
                payload = _probe_source(source.get("baseUrl") or "", source.get("token") or "")
                item.update(_save_checked_source(source, payload))
            except AuthRequiredError:
                item["authRequired"] = True
                item["message"] = (
                    "Access token rejected" if str(source.get("token") or "").strip() else "Access token required"
                )
            except Exception as exc:
                item["message"] = _public_error_message(exc)
            sources.append(_public_source(item))
        return {"sources": sources, "providerSupport": callable(_register_provider)}

    @app.post("/api/plugins/remote_library_client/sources")
    def add_source(data: dict):
        raw_url = data.get("baseUrl") or data.get("url") or ""
        source_type = str(data.get("type") or "").strip()
        # Honor the explicit type from the add form's picker; fall back to URL auto-detection
        # for API callers that omit it.
        use_proton = source_type == ProtonPublicShareProvider.type or (
            not source_type and is_proton_share_url(raw_url)
        )
        if use_proton:
            try:
                source = _proton_source({
                    "baseUrl": raw_url,
                    "label": str(data.get("label") or "").strip(),
                    "allowUnsafeRedirects": bool(data.get("allowUnsafeRedirects")),
                })
                provider = _register_source_provider(source, replace=True)
                _store.upsert_source(source)
                return {"ok": True, "source": _public_source(source), "provider": _provider_payload(provider)}
            except Exception as exc:
                raise HTTPException(status_code=400, detail=_public_error_message(exc)) from exc
        use_google = source_type == GoogleDrivePublicFolderProvider.type or (
            not source_type and is_google_drive_folder_url(raw_url)
        )
        if use_google:
            try:
                source = _google_source({
                    "baseUrl": raw_url,
                    "label": str(data.get("label") or "").strip(),
                    "allowUnsafeRedirects": bool(data.get("allowUnsafeRedirects")),
                })
                provider = _register_source_provider(source, replace=True)
                _store.upsert_source(source)
                return {"ok": True, "source": _public_source(source), "provider": _provider_payload(provider)}
            except Exception as exc:
                raise HTTPException(status_code=400, detail=_public_error_message(exc)) from exc
        use_iroh = source_type == IrohLibraryProvider.type or (not source_type and is_iroh_id(raw_url))
        if use_iroh:
            token = str(data.get("token") or "").strip()
            try:
                source = _iroh_source({
                    "irohId": raw_url,
                    "label": str(data.get("label") or "").strip(),
                    "syncNamToneAssets": bool(data.get("syncNamToneAssets")),
                    "token": token,
                })
                provider = _register_source_provider(source, replace=True)
                _store.upsert_source(source)
                return {"ok": True, "source": _public_source(source), "provider": _provider_payload(provider)}
            except AuthRequiredError as exc:
                message = "The access token was rejected." if token else "This server requires an access token."
                raise HTTPException(status_code=401, detail=message) from exc
            except Exception as exc:
                raise HTTPException(status_code=400, detail=_public_error_message(exc)) from exc
        use_feedforge = source_type == FeedForgeProvider.type or (not source_type and is_feedforge_url(raw_url))
        if use_feedforge:
            try:
                source, described_provider = _feedforge_source({
                    "baseUrl": raw_url,  # empty -> defaults to https://feedforge.org
                    "token": str(data.get("token") or "").strip(),
                    "label": str(data.get("label") or "").strip(),
                })
                # Register the instance that just described (its catalog walk is already
                # running/complete) — a second cold instance would re-walk from scratch.
                provider = _register_provider_instance(described_provider, source, replace=True)
                _store.upsert_source(source)
                return {"ok": True, "source": _public_source(source), "provider": _provider_payload(provider)}
            except AuthRequiredError as exc:
                raise HTTPException(status_code=401, detail=_public_error_message(exc)) from exc
            except Exception as exc:
                raise HTTPException(status_code=400, detail=_public_error_message(exc)) from exc
        token = str(data.get("token") or "").strip()
        try:
            base_url, payload = _probe_first_available(raw_url, token)
            label = str(data.get("label") or "").strip()
            source = {
                **_source_from_payload(base_url, payload, label),
                "syncNamToneAssets": bool(data.get("syncNamToneAssets")),
                "allowUnsafeRedirects": bool(data.get("allowUnsafeRedirects")),
                "token": token,
            }
            provider = _register_source_provider(source, replace=True)
            _store.upsert_source(source)
            return {"ok": True, "source": _public_source(source), "provider": _provider_payload(provider)}
        except AuthRequiredError as exc:
            message = "The access token was rejected." if token else "This server requires an access token."
            raise HTTPException(status_code=401, detail=message) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=_public_error_message(exc)) from exc

    @app.post("/api/plugins/remote_library_client/sources/{provider_id:path}/refresh")
    def refresh_source(provider_id: str):
        source = next((item for item in _store.list_sources() if item.get("providerId") == provider_id), None)
        if not source:
            raise HTTPException(status_code=404, detail="source not found")
        if not _source_enabled(source):
            disabled = {**source, "enabled": False, "online": False, "message": "Disabled"}
            return {"ok": True, "source": _public_source(disabled)}
        if _source_type(source) == GoogleDrivePublicFolderProvider.type:
            try:
                updated = _save_checked_google_source(source)
                provider = _providers.get(updated.get("providerId") or "")
                return {"ok": True, "source": _public_source(updated), "provider": _provider_payload(provider)}
            except Exception as exc:
                raise HTTPException(status_code=400, detail=_public_error_message(exc)) from exc
        if _source_type(source) == ProtonPublicShareProvider.type:
            try:
                updated = _save_checked_proton_source(source)
                provider = _providers.get(updated.get("providerId") or "")
                return {"ok": True, "source": _public_source(updated), "provider": _provider_payload(provider)}
            except Exception as exc:
                raise HTTPException(status_code=400, detail=_public_error_message(exc)) from exc
        if _source_type(source) == IrohLibraryProvider.type:
            try:
                updated = _save_checked_iroh_source(source)
                provider = _providers.get(updated.get("providerId") or "")
                return {"ok": True, "source": _public_source(updated), "provider": _provider_payload(provider)}
            except AuthRequiredError as exc:
                message = "Access token rejected" if str(source.get("token") or "").strip() else "Access token required"
                raise HTTPException(status_code=401, detail=message) from exc
            except Exception as exc:
                raise HTTPException(status_code=400, detail=_public_error_message(exc)) from exc
        if _source_type(source) == FeedForgeProvider.type:
            try:
                updated = _save_checked_feedforge_source(source)
                provider = _providers.get(updated.get("providerId") or "")
                return {"ok": True, "source": _public_source(updated), "provider": _provider_payload(provider)}
            except AuthRequiredError as exc:
                raise HTTPException(status_code=401, detail=_public_error_message(exc)) from exc
            except Exception as exc:
                raise HTTPException(status_code=400, detail=_public_error_message(exc)) from exc
        try:
            payload = _probe_source(source.get("baseUrl") or "", source.get("token") or "")
            updated = _save_checked_source(source, payload)
            provider_id = updated.get("providerId") or ""
            provider = _providers.get(provider_id)
            return {"ok": True, "source": _public_source(updated), "provider": _provider_payload(provider)}
        except AuthRequiredError as exc:
            message = "Access token rejected" if str(source.get("token") or "").strip() else "Access token required"
            raise HTTPException(status_code=401, detail=message) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=_public_error_message(exc)) from exc

    @app.patch("/api/plugins/remote_library_client/sources/{provider_id:path}")
    def update_source(provider_id: str, data: dict):
        source = next((item for item in _store.list_sources() if item.get("providerId") == provider_id), None)
        if not source:
            raise HTTPException(status_code=404, detail="source not found")
        allowed = {"enabled", "syncNamToneAssets", "allowUnsafeRedirects", "token"}
        if not any(key in data for key in allowed):
            raise HTTPException(status_code=400, detail="no supported source settings provided")
        updated = dict(source)
        if "enabled" in data:
            updated["enabled"] = bool(data.get("enabled"))
        if "syncNamToneAssets" in data:
            updated["syncNamToneAssets"] = bool(data.get("syncNamToneAssets"))
        if "allowUnsafeRedirects" in data:
            updated["allowUnsafeRedirects"] = bool(data.get("allowUnsafeRedirects"))
        if "token" in data:
            updated["token"] = str(data.get("token") or "").strip()
        if _source_enabled(updated):
            try:
                provider = _register_source_provider(updated, replace=True)
            except Exception as exc:
                raise HTTPException(status_code=400, detail=_public_error_message(exc)) from exc
            _store.upsert_source(updated)
            return {"ok": True, "source": _public_source(updated), "provider": _provider_payload(provider)}
        _store.upsert_source(updated)
        _unregister_source_provider(provider_id)
        return {"ok": True, "source": _public_source(updated), "provider": None}

    @app.delete("/api/plugins/remote_library_client/sources/{provider_id:path}")
    def remove_source(provider_id: str):
        removed = _store.remove_source(provider_id)
        _unregister_source_provider(provider_id)
        if not removed:
            raise HTTPException(status_code=404, detail="source not found")
        return {"ok": True, "providerId": provider_id}

    return app