# feedback-remote-library-client — development guide

Remote Library Client is a [FeedBack](https://github.com/got-feedback/feedBack)
plugin (id `remote_library_client`) that registers one or more
[Remote Library Server](https://github.com/Taynavv/feedback-remote-library-server)
URLs as native FeedBack **library providers**, so a remote library shows up in the
core Library source selector and its songs can be browsed, synced, and played
locally.

## Architecture

| File | Role |
|---|---|
| [routes.py](routes.py) | `setup(app, context)`: connection management (add / remove / list server URLs, health probe) and wiring the provider into FeedBack's library provider coordinator; resolves the local package cache via the `get_sloppak_cache_dir` context callback |
| [remote_library_client/provider.py](remote_library_client/provider.py) | The library-provider implementation: `query-page` / `query-artists` / `query-stats` / `tuning-names` / `get-art` / `sync-song` against a remote server, plus package download + NAM-tone asset sync into the local cache |
| [remote_library_client/store.py](remote_library_client/store.py) | Persisted list of configured servers + per-source options |
| [screen.html](screen.html) / [screen.js](screen.js) | Remote Client screen: add a base URL, per-source NAM-tone toggle, status |
| [settings.html](settings.html) | Settings surface |
| [tests/](tests) | pytest, content-free: fake servers + synthetic packages |

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
