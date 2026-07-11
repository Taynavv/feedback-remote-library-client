# feedback-remote-library-client — development guide

Remote Library Client is a [FeedBack](https://github.com/got-feedback/feedBack)
plugin (id `remote_library_client`) that registers remote libraries as native FeedBack
**library providers**, so a remote library shows up in the core Library source selector
and its songs can be browsed, synced, and played locally. Five source **types** are
supported: a [Remote Library Server](https://github.com/Taynavv/feedback-remote-library-server)
URL (the rich REST protocol), the **same server reached peer-to-peer over iroh** by a Library ID
(no port forwarding), a **FeedForge** (feedforge.org) community-catalog account, a public Google
Drive folder of package files, and an anonymous end-to-end-encrypted Proton Drive public share of
package files.

## Architecture

| File | Role |
|---|---|
| [routes.py](routes.py) | `setup(app, context)`: connection management (add / remove / list sources, health probe), a `PROVIDER_TYPES` registry that dispatches each source on its stored `type` (default = direct server; Google Drive folder URLs auto-detected), and wiring providers into FeedBack's library provider coordinator; resolves the local package cache via the `get_sloppak_cache_dir` context callback |
| [remote_library_client/provider.py](remote_library_client/provider.py) | `BaseLibraryProvider` (shared transport / cache / library-import machinery + graceful-default `get-art` / `tuning-names`) and `DirectLibraryProvider` — the Remote Library Server implementation (`query-page` / `query-artists` / `query-stats` / `tuning-names` / `get-art` / `sync-song`, package download + NAM-tone asset sync) |
| [remote_library_client/google_drive.py](remote_library_client/google_drive.py) | `GoogleDrivePublicFolderProvider` — the `google-drive-public.v1` type: enumerate a public Drive folder, parse `Artist - Album - Title.feedpak` filenames for metadata, download packages (redirect + confirm-token flow) into the local cache. Also exposes the Drive download as module functions (`drive_file_id_from_url`, `confirmed_download_url`, `download_drive_file`) reused by the FeedForge type |
| [remote_library_client/feedforge.py](remote_library_client/feedforge.py) | `FeedForgeProvider` — the `feedforge.v1` type: a FeedForge (feedforge.org) account as a provider. Drives the NextAuth **credentials** login (username + password, auto-relogin on lapse), scrapes the `/library` catalog HTML for rich metadata (title/artist/tuning/year/duration/art), and resolves each song's external download URL (`POST /api/songs/{id}/download` → `{ok,url}`, typically a Google Drive link → reuses `google_drive.py`'s Drive download path). Stdlib only (no new dependency); non-blocking background sync like Google/Proton. **Username/password only — Discord login is not supported by this plugin-only build** |
| [remote_library_client/proton_drive.py](remote_library_client/proton_drive.py) | `ProtonPublicShareProvider` — the `proton-public.v1` type: anonymous SRP auth to a Proton public share, decrypt the OpenPGP key hierarchy + folder listing (`pysequoia`), parse `Artist-Title.feedpak` filenames, download + decrypt content blocks into the local cache. Needs `bcrypt` + `pysequoia` (see `requirements.txt`) |
| [remote_library_client/proton_srp.py](remote_library_client/proton_srp.py) | Dependency-light reimplementation of Proton's SRP-6a handshake + bcrypt key-stretch, so the plugin needs only `bcrypt` (not the full `proton-client`, which drags in gpg/openssl/requests). Cross-checked against `proton-client` in tests |
| [remote_library_client/iroh_transport.py](remote_library_client/iroh_transport.py) | `IrohLibraryProvider` — the `iroh-library.v1` type: the **same Remote Library Server protocol tunnelled over an iroh P2P QUIC stream**, reached by a pasted Library ID (EndpointId). Subclasses `DirectLibraryProvider` and overrides only `_urlopen` (HTTP-over-iroh via a socket adapter + a shared background asyncio runtime); adds a non-blocking background sync. Needs `iroh` (see `requirements.txt`, lazy-imported) |
| [remote_library_client/store.py](remote_library_client/store.py) | Persisted list of configured sources + per-source options (including `type`) |
| [screen.html](screen.html) / [screen.js](screen.js) | Remote Client screen: add a server URL, iroh Library ID, FeedForge account, Drive folder link, or Proton share link, per-source NAM-tone toggle, status; Google Drive + Proton sources hide the token/NAM/redirect controls (the direct server shows all; the iroh type shows token + NAM but not redirect); the FeedForge type swaps the token field for a username + password pair and makes the URL optional (defaults to feedforge.org) |
| [settings.html](settings.html) | Settings surface |
| [tests/](tests) | pytest, content-free: fake servers, fake Drive folders, synthetic Proton key hierarchies + synthetic packages |

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
- **Proton is anonymous SRP + E2EE — the crypto chain is exact, verified live.** `proton-public.v1`
  authenticates to a public share with an anonymous **SRP-6a** handshake (`GET /drive/urls/{token}/info`
  → `POST .../auth` → session `{UID, AccessToken, Share}`), then unwinds an OpenPGP key hierarchy:
  bcrypt-stretch the URL password with **`SharePasswordSalt`** (Proton's `computeKeyPassword`, *not* the
  SRP's `UrlPasswordSalt`) → symmetric-decrypt `SharePassphrase` → unlock `ShareKey` → decrypt the root
  link's `NodePassphrase` (from `/folders/{LinkID}` or `/links/{LinkID}`) → unlock the root `NodeKey` →
  per child: decrypt its `NodePassphrase` with the **parent** node key, unlock the child `NodeKey`, and
  decrypt its **`Name` with the child's own node key** (fall back to the parent key). These field names
  and the "name decrypts with the child key" detail are verified against a real share — match them exactly.
- **Proton content = `ContentKeyPacket` + block, decrypted per block.** A file revision
  (`GET /drive/urls/{token}/files/{LinkID}?FromBlockIndex&PageSize`) has one base64 `ContentKeyPacket`
  (a PKESK to the file node key) and N `Blocks` (each a `BareURL` to a raw SEIPD packet). Each block
  decrypts by concatenating `ContentKeyPacket + block_bytes` into one OpenPGP message and decrypting with
  the file node key. Reassemble in `Index` order → the `.feedpak`. (This reconstruction is proven in a
  synthetic-key end-to-end test.)
- **The Proton URL password is a secret — like the server token.** The `#…` fragment of a share link is
  the decryption key. It is stored per source as **`urlPassword`** (never placed in the displayable
  `baseUrl`, which holds only the semi-public token) and **`_public_source` strips it** (alongside the
  server `token`) from every API response. Keep it out of responses and logs.
- **`proton_srp.py` is a deliberate reimplementation, not a shortcut.** Depending on `proton-client`
  drags in `python-gnupg` (needs the `gpg` binary), `pyopenssl`, and `requests`. So the SRP handshake +
  bcrypt helper are reimplemented against `bcrypt` alone. It reproduces Proton's exact quirks —
  **little-endian** big-int encoding and an **expanded SHA-512** (`pmhash`, four SHA-512s) — and is
  cross-checked byte-for-byte against `proton-client` in `test_proton_srp.py` (a test-only `importorskip`,
  not a runtime dep). Don't "simplify" the endianness or the hash.
- **Proton providers are reused across status polls — Proton rate-limits auth.** Unlike the Google path
  (which rebuilds a provider each status refresh), `_save_checked_proton_source` reuses the already-
  registered provider instance so its cached SRP session + catalog survive; a fresh handshake on every
  poll would risk Proton's anti-abuse throttle. The provider caches the session (to `ExpiresIn`) and the
  decrypted catalog (TTL). `x-pm-appversion` (`PROTON_APP_VERSION`) is a required, drifting header —
  bump it if listing starts failing.
- **Proton sync is non-blocking too** — same ~250ms core cap and the same background-download machinery
  as Google (`sync_song` → `active_downloads()` → the screen's `/downloads` poller → toasts; the click
  handler triggers on `proton:` provider ids as well as `gdrive:`). Metadata is filename-derived
  (`parse_proton_filename`: `Artist-Title.feedpak` underscored, or the spaced `Artist - Album - Title`).
- **Native deps are per-type and lazy.** Proton needs `bcrypt` + `pysequoia`; the iroh type needs
  `iroh`. All are imported *lazily* (the modules — and their non-crypto tests — load without them) and
  declared in `requirements.txt`. The direct-server and Google Drive types stay dependency-free, so
  they keep working on a deploy that cannot install the native wheels.
- **The iroh type tunnels the existing HTTP — the server protocol + auth are untouched.**
  `iroh-library.v1` subclasses `DirectLibraryProvider` and overrides only `_urlopen`: it opens an iroh
  `BiStream` to the stored EndpointId and speaks HTTP over it (a socket adapter feeds `http.client`),
  so every endpoint and the bearer token ride along unchanged; the server side (separate repo) just
  pipes each accepted stream to its own local HTTP server. `iroh` runs as one shared background asyncio
  runtime (endpoint + per-Library-ID connection cache) bridged to the sync provider via
  `run_coroutine_threadsafe`. The Library ID is the paste-able identity and is NOT secret; the
  per-source `token` still is (stripped by `_public_source`).
- **The Library ID is the *stable* bare EndpointId — a ticket is the volatile fallback.** `_addr_for`
  accepts both: a bare EndpointId (64-hex pubkey) → `iroh.EndpointAddr(EndpointId.from_string(id),
  None, [])`, letting **discovery** resolve the server's current address; or a full `endpoint…` ticket
  (id + embedded relay/socket addresses). Prefer the bare id — a ticket's string changes every restart
  as addresses change, so it's a poor thing to "follow." **The `EndpointAddr(id, None, [])` args are
  required** — `EndpointAddr(id)` raises `TypeError` (the original bug that made only tickets work, so
  the ID looked non-stable). The server now advertises the bare EndpointId as its Library ID.
- **`add_source` validates the iroh handshake.** After the `/source` probe, `_iroh_source` requires an
  RLS marker (`capabilities` has `library.read`, or `server.protocol` is set) — otherwise it errors
  instead of registering. Any live peer on the `feedback/rls/1` ALPN answers a connect, so a wrong id
  (e.g. a stray test peer) would otherwise register and silently browse empty.
- **The iroh sync is non-blocking (same ~250 ms core cap).** `DirectLibraryProvider.sync_song`
  downloads inline — fine on LAN, but an internet iroh hop exceeds core's sync-song cap. So
  `IrohLibraryProvider` overrides `sync_song` to background the download (`_background_sync` calls the
  direct package-download), return immediately, and play on the next click, exposing
  `active_downloads()` for the `/downloads` poller (the Google Drive / Proton pattern). The screen's
  click handler + poller treat `iroh:` provider ids like `gdrive:` / `proton:`.
- **An offline iroh server must fail *fast*, not hang — the timeouts are load-bearing.** Dialing a
  dead/unreachable peer otherwise blocks on iroh discovery for a long internal timeout (the source
  "just sits there"). `iroh_transport.py` caps the *dial* at `_CONNECT_TIMEOUT` (separate from the
  120 s `_DEFAULT_TIMEOUT` data-transfer ceiling — a download stays patient, a connect never does),
  and `_run` bounds every loop call with `asyncio.wait_for` so a timed-out connect is actually
  *cancelled*, not leaked onto the shared loop. The status `/source` probe uses the short
  `_STATUS_PROBE_TIMEOUT` via `IrohLibraryProvider.probe_source()`. Any non-HTTP transport failure
  raises `IrohUnreachableError` (message `IROH_UNREACHABLE_MESSAGE`); `routes.py` `status()` catches
  it to keep the card `online: False` with that clear `message` (screen.js already renders it), and
  core browse (`query_page`) fails fast the same way instead of hanging. `open_stream` still re-dials
  once for a stale cached connection — don't re-add an unbounded/looping re-dial on top, or the
  offline-server hang comes back.
- **FeedForge is scrape + NextAuth-credentials, username/password only (this build).** `feedforge.v1`
  drives the NextAuth login (`GET /api/auth/csrf` → form-POST `/api/auth/callback/credentials`
  `{csrfToken, username, password, callbackUrl, json:true}` → session cookie captured by the shared
  `BaseLibraryProvider` cookie jar), confirmed via `GET /api/auth/session` returning a `user` (bad
  creds still 200 but set no session). **Discord login is deliberately out of scope**: it can't be
  automated safely (self-botting) and the clean path — capture the session from a real browser — needs
  a FeedBack *core* capability to open a login window that plugins don't have today. Don't add a
  headless-browser dep or a Discord-token flow to work around it.
- **The FeedForge account `password` is a secret; the `username` is not.** Stored per source and
  stripped from every API response by `_public_source` (alongside the server `token` and Proton
  `urlPassword`); the UI only sees `hasPassword`. The `username` is kept (shown in the card). Keep the
  password out of responses and logs.
- **The session self-renews — don't force a re-login each poll.** NextAuth uses the JWT strategy here
  (forced by the presence of the credentials provider), so the cookie slides (server re-issues it on
  use). Like Proton, `_save_checked_feedforge_source` **reuses the already-registered provider** so its
  live session + scraped catalog survive across status polls; rebuilding it every poll would re-login
  needlessly and risk Cloudflare/anti-abuse throttling. App API routes (the download POST) authorize on
  the session cookie + `X-Requested-With: fetch` only — **no CSRF token** (CSRF is only on the NextAuth
  sign-in calls).
- **Listing is a scraped `<table>` — the row/facet selectors are the fragile part (verified live
  2026-07-10).** There is no JSON list API (`/api/songs` etc. 404). Each song is a `<tr>` with a
  `song-title` anchor to `/songs/{id}` and typed `linked-cell` **facet** anchors whose hrefs carry the
  value — `href="/library?…&artist=…"` / `&album=` / `&tuning=` / `&year=` (the param sits *after*
  `?page=N`, not right after `?`, so `parse_library_html` matches `[^"]*\bfacet=`, not `\?facet=`);
  duration is a plain `<td>M:SS</td>`; cover art is a `cover-thumb` `<img>` whose `/feedpak-covers/{id}`
  differs from the song id. A vibe-coded markup change breaks the parse unannounced — the selector regexes
  are constants at the top of `feedforge.py`. Verify against a live authed page; tests use synthetic
  fixtures only.
- **Browsing is LAZY — never scrape the whole catalog (it's ~2000+ songs and Cloudflare rate-limits
  rapid full scrapes).** `query_page` fetches only the FeedForge page(s) covering the requested window
  (`_PAGE_SIZE = 25`/page, 1-indexed, stable + non-overlapping order — verified), mapping core's
  `(page, size)` onto that grid and slicing. **Server-side query params ride along**: `?q=` is a real
  full-text **search** (verified — returns matching songs), `?sort=` maps via `_SORT_MAP` (only
  verified-stable sorts; unknown → default order). There is **no total on the page**, so `query_page`
  reports an *at-least* total that grows while full pages keep coming and settles exactly at the last
  (short) page — never triggering the binary search, so browsing stays lazy. The **source-card count**
  (`describe_source` / `query_stats.total_songs`) comes from `_catalog_total()` — a binary search for the
  last non-empty page (exponential probe + bisect, `attempts=1` since past-the-end empties are expected;
  ~6 s, cached `metadata_cache_ttl_seconds = 900`, provider reused across polls → runs ~once/15 min on
  add/status, not per browse). `_fetch_ff_page` caches only *non-empty* pages (an empty probe must not
  poison a later browse) and populates `_card_cache` so sync/art resolve a browsed song by id without
  re-fetching. **Tradeoff (deliberate): the A–Z letter rail and browse-by-artist degrade** —
  `query_stats.letters` is `{}` and `query_artists` returns `([], 0)`, because both need the whole
  catalog which we no longer hold. The song list + search is the supported browse path.
- **FeedForge hosts nothing — download is resolve-then-fetch, reusing the Drive path + a Dropbox
  fixup.** `sync_song` is non-blocking (same ~250 ms core cap and background machinery as Google/Proton;
  the screen's click handler + `/downloads` poller treat `feedforge:` ids like `gdrive:`). `_do_sync`
  POSTs `/api/songs/{id}/download` → `{ok, url}` (an external link — **Google Drive *or* Dropbox** in the
  wild). A Drive file → `google_drive.download_drive_file` (shared module function; the confirm-token flow
  lives in one place now); everything else → `_download_url_to_cache` after `_direct_download_url` coerces
  a Dropbox share link's `?dl=0` (an HTML *preview*) to `?dl=1` (the real file, which 302s to
  `dl.dropboxusercontent.com` — a public host the SSRF guard allows). It **imports under the deterministic
  `Artist - Title.feedpak` name** (not the CDN's Content-Disposition) so the browse-time `settingsKey`
  matches core's key for the imported file (the client↔core playback-settings-key contract).

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
pip install -r requirements.txt   # bcrypt + pysequoia — required for the Proton crypto tests to run
ruff check .   # CI gate: E, F, I rules, line-length 120
pytest -q
```

The Proton crypto tests `importorskip` `pysequoia`/`bcrypt`, so the suite still passes without
them (those tests just skip) — but install `requirements.txt` to actually exercise the Proton
provider. CI installs it so the full end-to-end crypto test runs.
