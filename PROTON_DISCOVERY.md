# Proton Drive public-folder — discovery report

Branch: `explore/proton-drive`. Status: **feasibility VERIFIED live** against a real public
share. This documents the protocol + crypto so a `proton-public.v1` provider type can be built
on the existing `BaseLibraryProvider` / `PROVIDER_TYPES` seam (same pattern as Google Drive).

> **Secret handling:** a Proton public-share link is `https://drive.proton.me/urls/<token>#<pw>`.
> The `<token>` is semi-public (it's in the URL, sent to the server). The `<pw>` after `#` is the
> **URL password** — the decryption secret, never sent to the server. Treat it like a token:
> store it per-source (like the Remote Library Server token), never log it, and strip it from
> API responses (`_public_source`).

## What was verified live (end to end)

- **Anonymous SRP auth works.** No account, no login — just the token + the fragment password.
- **Full metadata decryption works** — recovered real `.feedpak` filenames from the E2EE share
  (`Artist-Title.feedpak` convention on the tested share — note it differs from Google Drive's
  `Artist - Album - Title`, so the Proton parser needs its own filename handling).
- **Content decryption works end to end (verified during the build).** The shipped provider
  downloaded + decrypted a real song's content blocks against the live share and reassembled a
  **valid `.feedpak` (a ZIP archive)** — `pysequoia` handles the SEIPD packets. Each block is a bare
  SEIPD packet (first byte `0xd2` = new-format tag 18); prepending the file's `ContentKeyPacket`
  (a PKESK) reconstructs a decryptable OpenPGP message.

## Protocol (anonymous public share)

API base `https://drive.proton.me/api`. `/info` and `/auth` need only a browser `User-Agent`; the
data endpoints additionally require `x-pm-appversion: web-drive@<ver>` (e.g. `web-drive@5.2.0`).

1. `GET /drive/urls/{token}/info` → SRP challenge: `{Modulus (PGP-signed), ServerEphemeral,
   UrlPasswordSalt, SRPSession, Version, Flags}`. `Flags` bit encodes generated vs custom password.
2. `POST /drive/urls/{token}/auth` `{ClientEphemeral, ClientProof, SRPSession}` → **anonymous
   session** `{UID, AccessToken, ServerProof, Share, ...}`. Verify `ServerProof`. Authed calls send
   `x-pm-uid: {UID}` + `Authorization: Bearer {AccessToken}`.
3. `GET /drive/urls/{token}` → `{Token}`: the share **and** root-folder node material together —
   `{ShareKey, SharePassphrase, SharePasswordSalt, NodeKey, NodePassphrase, NodeHashKey, LinkID, ...}`.
   **Required** — the auth response's `Share` carries only the share half (`ShareKey`/`SharePassphrase`/
   `SharePasswordSalt`/`LinkID`), *not* the root `NodeKey`/`NodePassphrase`. (`/folders/{LinkID}` and
   `/links/{LinkID}` both 404 for a public share — do not use them.)
4. `GET /drive/urls/{token}/folders/{LinkID}/children?Page&PageSize` → `{Links: [...]}`: each child
   `{LinkID, Type, Name (enc), NodeKey, NodePassphrase, Hash (HMAC), Size, MIMEType, FileProperties, ...}`.
   **The per-file content key is `FileProperties.ContentKeyPacket`** (a PKESK), captured here — not on
   the revision below.
5. `GET /drive/urls/{token}/files/{LinkID}?FromBlockIndex&PageSize` → the active revision with
   `Blocks[]` (paginate by `FromBlockIndex`), each block `{Index, URL, BareURL, Hash, Token, ...}`.
   **Fetch each block from `URL`** (the full storage URL; a bare `BareURL` fetch 400s) — the bytes are a
   standalone SEIPD packet. Decrypt `ContentKeyPacket ‖ block` with the file node key; reassemble by
   `Index`.

## Crypto chain (verified)

- **SRP-6a** via `proton-client` (pip, pure-Python): extract the modulus from the signed `Modulus`
  (verify its signature in production with a bundled Proton pubkey — do NOT rely on `python-gnupg`/gpg),
  `User(url_password, modulus)` → `get_challenge()` = ClientEphemeral, `process_challenge(salt, B)`
  = ClientProof, `verify_session(ServerProof)`.
- **bcrypt URL-password** → `urlPassphrase`: `bcrypt.hashpw(pw, b"$2y$10$" + bcrypt_b64_encode(salt)[:22])[29:]`
  (`bcrypt_b64_encode` is `proton.srp.util`). This is the password for the SharePassphrase PGP message.
- **Key hierarchy** (OpenPGP, Curve25519): urlPassphrase → decrypt `SharePassphrase` → unlock
  `ShareKey` → decrypt root `NodePassphrase` → unlock root `NodeKey`. **A child's `Name` and
  `NodePassphrase` are encrypted to the PARENT node key** (so the parent lists children); the child's
  own `NodeKey` (unlocked via its passphrase) is for the child's **contents** only. `Hash` is an
  HMAC of the name under the parent `NodeHashKey`.
- **Content** (download): `ContentKeyPacket` (PKESK to the file NodeKey) → per-file session key; each
  block is a SEIPD packet under that session key; a signed manifest over the block hashes.

## Dependencies (the real cost)

FeedBack **supports plugin deps** first-class: a plugin `requirements.txt` is `pip install`ed to a
persistent `pip_packages` dir on load (cached by hash), and native wheels are already used in the
ecosystem (`feedback-ultrastar-import` ships `numpy`). FeedBack bundles **Python 3.12**, which has
prebuilt wheels for everything below.

- **SRP + bcrypt:** `proton-client` (or reimplement — it's ~1 file of SRP + the bcrypt helper) +
  `cryptography` (native wheel, ubiquitous). Bundle Proton's modulus pubkey for verification instead
  of `python-gnupg` (avoids the gpg binary).
- **OpenPGP:** **`pysequoia`** (Rust, native wheel). `PGPy` is out — it's unmaintained and **breaks on
  Python 3.13+** (imports the removed `imghdr` stdlib module), and it can't do the AES-GCM/**SEIPDv2**
  packets Proton switched new content to in June 2026. `pysequoia` handles both SEIPD versions and the
  split ContentKeyPacket/SEIPD form cleanly.

Net: Proton moves the plugin off "zero dependencies," but onto a **supported, precedented mechanism**
with mature 3.12 wheels. Caveats: locked-down/read-only deploys can't install deps (core degrades the
plugin with a warning); an exotic platform with no `pysequoia` wheel would fail to load the type.

## Build plan

`proton-public.v1` `GoogleDrive`-style provider: store `{token, urlPassword}` per source (password =
secret, stripped from responses); `describe_source` = auth + list to get a count; `query_page` =
list children + decrypt names (filename-parsed metadata, Proton convention); `sync_song` = the
existing **background-download** pattern (Proton downloads are internet-slow too, so the 250ms
sync-song cap applies — reuse `active_downloads()` + the poller), fetching + decrypting the file's
blocks. Reuses the type picker, the `BaseLibraryProvider` seam, and the local-import machinery.

## Risks / open items

- **Undocumented, moving API** — `x-pm-appversion` is required and Proton bumps/eventually rejects old
  values; they migrate crypto (SEIPDv2). Standing maintenance/breakage liability.
- **Anti-abuse rate-limiting** — rapid *repeated* auth gets throttled (hit during discovery). Mitigated
  in the build by caching the session (`AccessToken`/`ExpiresIn`) + the decrypted catalog and reusing the
  registered provider across status polls; a full auth→list→download run completes cleanly.
- **Custom-password shares** — the tested share used a generated password; password-protected shares
  concatenate the user-typed password (both the SRP gate and the bcrypt derivation) — handle the flag.
- **Provenance** — the protocol is reverse-engineered from Proton's GPL-3.0 WebClients (compatible with
  this repo's AGPL-3.0, but noted).
