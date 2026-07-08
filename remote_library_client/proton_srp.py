# SPDX-License-Identifier: AGPL-3.0-or-later
"""Minimal Proton SRP-6a client for anonymous Drive public-share authentication.

This reimplements only the handshake surface the Proton provider needs — the client
``get_challenge`` / ``process_challenge`` / ``verify_session`` exchange, the bcrypt URL-
password key-stretch (``computeKeyPassword``), and extracting the modulus from the signed
challenge — so the plugin depends on ``bcrypt`` alone rather than the full ``proton-client``
package (which also pulls in ``python-gnupg`` — needing the ``gpg`` binary — ``pyopenssl``,
and ``requests``, none of which this plugin uses).

The wire protocol is Proton's; this is interoperability-compatible with, and structurally
follows, ``proton-python-client`` (GPLv3, compatible with this repo's AGPL-3.0). Its output
is cross-checked byte-for-byte against that reference in ``tests/test_proton_srp.py``.

Notable Proton-specific quirks reproduced here:
- all big-integer <-> byte conversions are **little-endian**;
- the hash is an *expanded* SHA-512 (four SHA-512 digests over ``data || 0x00..0x03``);
- passwords are pre-stretched with bcrypt (the ``$2y$`` variant) before the SRP hash.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os

import bcrypt

# bcrypt's non-standard base64 alphabet (``./`` lead) vs. RFC 4648's (``+/`` tail).
_STD_B64 = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
_BCRYPT_B64 = b"./ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
_B64_TO_BCRYPT = bytes.maketrans(_STD_B64, _BCRYPT_B64)


def _pm_digest(data: bytes) -> bytes:
    """Proton's expanded SHA-512: ``SHA512(data||0) || SHA512(data||1) || ... || SHA512(data||3)`` (256 bytes)."""
    return b"".join(hashlib.sha512(data + bytes([i])).digest() for i in range(4))


def _bcrypt_b64(data: bytes) -> bytes:
    return base64.b64encode(data).translate(_B64_TO_BCRYPT)


def _to_bytes(value: int) -> bytes:
    return value.to_bytes((value.bit_length() + 7) // 8, "little")


def _to_int(value: bytes) -> int:
    return int.from_bytes(value, "little")


def _hash_to_int(*parts: bytes | int) -> int:
    buffer = bytearray()
    for part in parts:
        if part is None:
            continue
        buffer += _to_bytes(part) if isinstance(part, int) else part
    return _to_int(_pm_digest(bytes(buffer)))


def _hash_password(password: bytes, salt: bytes, modulus: bytes) -> bytes:
    """Proton auth v3/v4 password hash: bcrypt-stretch the password (salt suffixed with
    ``proton`` and bcrypt-b64 encoded), then expand-hash the crypt string with the modulus."""
    salt = (salt + b"proton")[:16]
    encoded_salt = _bcrypt_b64(salt)[:22]
    hashed = bcrypt.hashpw(password, b"$2y$10$" + encoded_salt)
    return _pm_digest(hashed + modulus)


def extract_modulus(signed_modulus: str) -> bytes:
    """Return the raw modulus bytes from Proton's PGP-clearsigned ``Modulus`` challenge field.

    The value is authenticated by TLS to ``drive.proton.me``; the OpenPGP clearsign signature
    is defense-in-depth we do not additionally verify (that would require bundling and
    tracking Proton's modulus-signing key).
    """
    text = str(signed_modulus or "").replace("\r\n", "\n")
    try:
        payload = text.split("\n\n", 1)[1].split("\n-----BEGIN PGP SIGNATURE", 1)[0].strip()
    except IndexError as exc:
        raise ValueError("malformed SRP modulus challenge") from exc
    return base64.b64decode(payload)


def compute_key_password(url_password: str, share_password_salt: bytes) -> str:
    """Proton ``computeKeyPassword``: bcrypt key-stretch of the URL password with the share
    salt, returning the 31-char bcrypt hash tail — the passphrase for the share PGP message.

    Note this uses ``SharePasswordSalt`` (decryption) with **no** ``proton`` suffix, distinct
    from the SRP handshake's ``UrlPasswordSalt`` (access) handled in :func:`_hash_password`.
    """
    encoded_salt = _bcrypt_b64(share_password_salt)[:22]
    return bcrypt.hashpw(url_password.encode(), b"$2y$10$" + encoded_salt)[29:].decode()


class SRPUser:
    """A single anonymous Proton SRP-6a handshake against a public-share token.

    Usage mirrors ``proton.srp.User``::

        user = SRPUser(url_password, extract_modulus(info["Modulus"]))
        client_ephemeral = user.get_challenge()
        client_proof = user.process_challenge(salt, server_ephemeral)
        ...  # POST the proof, then:
        user.verify_session(server_proof)  # must be True before trusting the session
    """

    def __init__(
        self,
        password: str,
        modulus_bytes: bytes,
        g_hex: bytes = b"2",
        secret_a: bytes | None = None,
    ) -> None:
        if not isinstance(password, str) or not password:
            raise ValueError("password must be a non-empty string")
        if secret_a is not None and len(secret_a) != 32:
            raise ValueError("secret_a must be exactly 32 bytes")
        self._password = password.encode()
        self.N = _to_int(modulus_bytes)
        self.g = int(g_hex, 16)
        width = (self.N.bit_length() + 7) // 8
        # k = H(g, N), each padded to the modulus width (little-endian).
        self._k = _to_int(_pm_digest(self.g.to_bytes(width, "little") + self.N.to_bytes(width, "little")))
        # Secret ephemeral: a random 256-bit value with the top bit forced set (injectable for tests).
        self._a = _to_int(secret_a) if secret_a is not None else (_to_int(os.urandom(32)) | (1 << 255))
        self._A = pow(self.g, self._a, self.N)
        self._expected_server_proof: bytes | None = None
        self._authenticated = False

    def get_challenge(self) -> bytes:
        """The client ephemeral ``A`` (little-endian bytes) to send as ``ClientEphemeral``."""
        return _to_bytes(self._A)

    def process_challenge(self, salt: bytes, server_ephemeral: bytes) -> bytes | None:
        """Compute the client proof ``M1`` from the server's salt + ephemeral ``B``.

        Returns ``None`` on an SRP-6a safety-check failure (``B ≡ 0 (mod N)`` or ``u == 0``).
        """
        server_b = _to_int(server_ephemeral)
        if server_b % self.N == 0:
            return None
        scramble = _hash_to_int(self._A, server_b)
        if scramble == 0:
            return None
        x = _to_int(_hash_password(self._password, salt, _to_bytes(self.N)))
        verifier = pow(self.g, x, self.N)
        session = pow(server_b - self._k * verifier, self._a + scramble * x, self.N)
        session_key = _to_bytes(session)
        client_proof = _pm_digest(_to_bytes(self._A) + _to_bytes(server_b) + session_key)
        self._expected_server_proof = _pm_digest(_to_bytes(self._A) + client_proof + session_key)
        return client_proof

    def verify_session(self, server_proof: bytes) -> bool:
        """Validate the server's proof (constant-time). Sets/returns :attr:`authenticated`."""
        self._authenticated = bool(self._expected_server_proof) and hmac.compare_digest(
            self._expected_server_proof, server_proof
        )
        return self._authenticated

    @property
    def authenticated(self) -> bool:
        return self._authenticated
