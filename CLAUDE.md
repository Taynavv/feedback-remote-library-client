# feedback-remote-library-client — development guide

Remote Library Client is a [FeedBack](https://github.com/got-feedback/feedBack)
plugin (id `remote_library_client`) that registers remote libraries as native FeedBack
**library providers**, so a remote library shows up in the core Library source selector
and its songs can be browsed, synced, and played locally. Two source **types** are
supported: a [Remote Library Server](https://github.com/Taynavv/feedback-remote-library-server)
URL (the rich REST protocol) and a public Google Drive folder of package files.

## Architecture

| File | Role |
|---|---|
| [routes.py](routes.py) | `setup(app, context)`: connection management (add / remove / list sources, health probe), a `PROVIDER_TYPES` registry that dispatches each source on its stored `type` (default = direct server; Google Drive folder URLs auto-detected), and wiring providers into FeedBack's library provider coordinator; resolves the local package cache via the `get_sloppak_cache_dir` context callback |
| [remote_library_client/provider.py](remote_library_client/provider.py) | `BaseLibraryProvider` (shared transport / cache / library-import machinery + graceful-default `get-art` / `tuning-names`) and `DirectLibraryProvider` — the Remote Library Server implementation (`query-page` / `query-artists` / `query-stats` / `tuning-names` / `get-art` / `sync-song`, package download + NAM-tone asset sync) |
| [remote_library_client/google_drive.py](remote_library_client/google_drive.py) | `GoogleDrivePublicFolderProvider` — the `google-drive-public.v1` type: enumerate a public Drive folder, parse `Artist - Album - Title.feedpak` filenames for metadata, download packages (redirect + confirm-token flow) into the local cache |
| [remote_library_client/store.py](remote_library_client/store.py) | Persisted list of configured sources + per-source options (including `type`) |
| [screen.html](screen.html) / [screen.js](screen.js) | Remote Client screen: add a server URL or Drive folder link, per-source NAM-tone toggle, status; Google sources hide the token/NAM/redirect controls |
| [settings.html](settings.html) | Settings surface |
| [tests/](tests) | pytest, content-free: fake servers, fake Drive folders + synthetic packages |

## Load-bearing subtleties — do not "clean up" casually

- **The cache callback key is `get_sloppak_cache_dir` — an exact-string core
  contract.** `routes.py` calls `context.get("get_sloppak_cache_dir")`; FeedBack core
  registers that exact key. Renaming it to `feedpak` returns `None` and breaks song
  sync. (User-facing docs say "feedpak"; this internal key stays `sloppak`.)
- **Base-URL parsing is forgiving.** A bare host (`studio.local`) with no scheme/port
  tries `http` then `https` on port `8765`; keep that fallback ladder.
- **Format inference falls back `format` → suffix.** `provider.py` reads the server's
  `format` field first and only infers from suffix (`.sloppak` / `.zip` → `sloppak`,
  `.psarc` → `psarc`) when it is absent; both branches must stay.
- **The NAM-tone-sync schema check is an external contract.** `provider.py` refuses a
  tone manifest whose `schema != "slopsmith.nam-tone-sync.v1"`; that literal is
  produced by FeedBack's NAM-tone export and must match the server's
  `NAM_TONE_SYNC_SCHEMA`. Do not rebrand.
- **NAM-tone sync is best-effort.** A missing or failed tone asset never fails the
  song sync — the result carries a `toneSync` status object instead. Preserve that
  non-fatal path.
- **Metadata reads are defensive** (`song.get("stem_ids") or song.get("stemIds")`),
  tolerating snake_case and camelCase from either server generation. Keep both.
- **Bearer-token auth (server v0.2.1+).** A per-source `token` is sent as
  `Authorization: Bearer <token>`, with a `?token=` query fallback for non-ASCII tokens
  (HTTP headers are Latin-1 only). A remote `401` becomes `AuthRequiredError`; `add`/probe
  turn that into a prompt-for-token flow, and `/source`'s `auth.required` is stored as
  `authRequired`. **The token is a secret: `_public_source` strips it from every API
  response** (the UI only ever sees `hasToken`). Keep it out of responses and logs.
- **Redirect SSRF guard is on by default.** `_GuardedRedirectHandler` refuses a redirect
  that pivots to a *different* internal/loopback/link-local host; the per-source
  `allowUnsafeRedirects` flag opts out. The originally-configured host and same-host
  redirects are always allowed, so LAN/localhost servers keep working. Don't route
  requests around `self._urlopen` (that's what installs the guard).
- **`settingsKey` is server-owned and opaque.** Use the server's `settingsKey` /
  `sourceSettingsKey` / `targetSettingsKey` verbatim; never recompute them. The client's
  own `playback_settings_key` derives the key for the *locally-imported* file and must
  match FeedBack core's derivation — that client↔core contract (not the server's) is the
  thing to validate if NAM mappings ever fail to resolve at playback.
- **Provider types dispatch on the stored `type`.** `routes.py` keys `PROVIDER_TYPES` by
  each provider class's `type` attribute; a source with no `type` is the direct server
  (`slopsmith-direct-library.v1`) for back-compat. The add form leads with a **type picker**
  (`screen.html` `#rlc-type`); the chosen `type` is sent to `add_source`, which honors it
  (URL auto-detection via `is_google_drive_folder_url` is only a fallback for callers that
  omit it). The per-type form hides the Access token field for Google Drive. The direct
  `/source` probe path is untouched. New backends subclass `BaseLibraryProvider`, set a
  `type`, and register in `PROVIDER_TYPES`; both provider classes share the
  `(source, cache_dir, local_root, importer, nam_config_dir)` ctor shape so
  `_provider_for_source` can build any type uniformly.
- **Google folder metadata is filename-derived.** `google-drive-public.v1` has no server
  API — community folders are flat `.feedpak` dumps, so artist/album/title come from
  parsing the `Artist - Album - Title.feedpak` name (`parse_feedpak_filename`, best-effort
  and tolerant). Art / tunings / stats / NAM degrade to the base defaults. If a manifest
  convention is ever adopted, it becomes an *optional* metadata source layered on top —
  don't make filename parsing the only path.
- **Google download is stdlib, not a dependency.** Enumeration scrapes
  `embeddedfolderview`; download follows Drive's redirect to `drive.usercontent.google.com`
  (allowed precisely because the SSRF guard blocks only *internal* hosts — Google's are
  public) and streams via `_stream_response_to_cache`, with a confirm-token form fallback
  for very large files and a clear "rate-limited, try later" error on Google's ~24h
  per-file download lock. Keep it on `self._urlopen` (the guarded opener); do not add gdown
  or route around the guard.
- **Google sync is non-blocking — core caps sync-song at ~250ms.** FeedBack's capability
  bus (`capabilities.js` `COMMAND_TIMEOUTS_MS`, which omits `library`/`sync-song`) times the
  sync out at 250ms — far too short for an internet download. So `sync_song` never downloads
  inline: it returns immediately (a `cacheState: "downloading"` result), runs the download on
  a background thread, and plays on the *next* click once `_local_ready` finds the file. The
  screen polls `active_downloads()` via `/downloads` to show "Downloading…" / "Ready to play"
  toasts (the v3 song page has no per-card sync badge, so a toast is the only visible signal).
  `query_page` also reports `localFilename` for songs already in the local folder
  (`_downloaded_names`), so a downloaded song renders as a first-class local card (one-click
  play, local artwork, working overflow-menu Play). Do not reintroduce a blocking download in
  `sync_song` — it would silently fail at the 250ms wall.
- **`.feedpak` is the sloppak family.** `playback_settings_key` treats `.feedpak` like
  `.sloppak`/`.zip` (feedpak == sloppak internally). Part of the client↔core
  playback-settings-key contract above — validate against FeedBack core if feedpak NAM
  mappings ever fail.

## Rules

- **License**: AGPL-3.0-or-later. Keep contributions compatible.
- **No song content, ever**: fakes only in tests / CI.
- Match the release tag to `plugin.json`'s `version` — the release workflow fails the
  build if they disagree. `feedback_target` records the FeedBack version the plugin
  was last verified against.

## Development

```bash
python -m venv .venv
# Activate:  Windows: .venv\Scripts\activate  |  macOS/Linux: source .venv/bin/activate
pip install pytest fastapi httpx ruff
ruff check .   # CI gate: E, F, I rules, line-length 120
pytest -q
```
