from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# bcrypt is a runtime dependency of the Proton provider; skip cleanly where it is absent.
pytest.importorskip("bcrypt")

from remote_library_client import proton_srp as srp  # noqa: E402


def _rand_modulus() -> bytes:
    """A random 256-byte (little-endian) modulus: high bit set (~2048-bit) and odd."""
    raw = bytearray(os.urandom(256))
    raw[-1] |= 0x80
    raw[0] |= 0x01
    return bytes(raw)


def _server_side(password: str, modulus_bytes: bytes, salt: bytes):
    """A minimal SRP-6a *server* built from the same primitives, for a self-contained round trip.

    The shared secret is derived by a different formula than the client's, so agreement verifies
    the client math end to end (the authoritative byte-for-byte check against Proton's own client
    lives in :func:`test_matches_proton_client_reference`)."""
    modulus = srp._to_int(modulus_bytes)
    generator = 2
    width = (modulus.bit_length() + 7) // 8
    multiplier = srp._to_int(
        srp._pm_digest(generator.to_bytes(width, "little") + modulus.to_bytes(width, "little"))
    )
    x = srp._to_int(srp._hash_password(password.encode(), salt, srp._to_bytes(modulus)))
    verifier = pow(generator, x, modulus)
    secret_b = (srp._to_int(os.urandom(32)) | 1)
    server_ephemeral = (multiplier * verifier + pow(generator, secret_b, modulus)) % modulus
    return modulus, verifier, secret_b, server_ephemeral


def test_full_handshake_authenticates_correct_password():
    modulus = _rand_modulus()
    salt = os.urandom(16)
    password = "correct-horse-battery"
    n, verifier, secret_b, server_ephemeral = _server_side(password, modulus, salt)

    user = srp.SRPUser(password, modulus)
    client_ephemeral = srp._to_int(user.get_challenge())
    client_proof = user.process_challenge(salt, srp._to_bytes(server_ephemeral))

    # The server verifies the client proof from its own view of the shared secret...
    scramble = srp._hash_to_int(client_ephemeral, server_ephemeral)
    shared = pow(client_ephemeral * pow(verifier, scramble, n), secret_b, n)
    session_key = srp._to_bytes(shared)
    expected_client_proof = srp._pm_digest(
        srp._to_bytes(client_ephemeral) + srp._to_bytes(server_ephemeral) + session_key
    )
    assert client_proof == expected_client_proof

    # ...and the client verifies the server's proof in turn (mutual authentication).
    server_proof = srp._pm_digest(srp._to_bytes(client_ephemeral) + client_proof + session_key)
    assert user.verify_session(server_proof) is True
    assert user.authenticated is True


def test_wrong_password_does_not_authenticate():
    modulus = _rand_modulus()
    salt = os.urandom(16)
    n, verifier, secret_b, server_ephemeral = _server_side("the-real-password", modulus, salt)

    user = srp.SRPUser("a-different-password", modulus)
    client_ephemeral = srp._to_int(user.get_challenge())
    client_proof = user.process_challenge(salt, srp._to_bytes(server_ephemeral))

    scramble = srp._hash_to_int(client_ephemeral, server_ephemeral)
    shared = pow(client_ephemeral * pow(verifier, scramble, n), secret_b, n)
    server_proof = srp._pm_digest(srp._to_bytes(client_ephemeral) + client_proof + srp._to_bytes(shared))
    # The client computed a different secret, so neither proof matches.
    assert user.verify_session(server_proof) is False
    assert user.authenticated is False


def test_process_challenge_rejects_srp_safety_violation():
    # B ≡ 0 (mod N) must be refused (returns None), per the SRP-6a safety check.
    modulus = _rand_modulus()
    user = srp.SRPUser("pw", modulus)
    assert user.process_challenge(os.urandom(16), srp._to_bytes(0)) is None


def test_compute_key_password_is_deterministic_bcrypt_tail():
    salt = os.urandom(16)
    first = srp.compute_key_password("hunter2", salt)
    second = srp.compute_key_password("hunter2", salt)
    assert first == second  # deterministic for a given (password, salt)
    assert srp.compute_key_password("hunter3", salt) != first  # sensitive to the password
    assert len(first) == 31  # the bcrypt hash tail (60-char crypt string minus the 29-char prefix)


def test_extract_modulus_parses_clearsigned_payload():
    import base64

    payload = base64.b64encode(b"\x01\x02\x03\x04modulus-bytes").decode()
    signed = (
        "-----BEGIN PGP SIGNED MESSAGE-----\n"
        "Hash: SHA256\n\n"
        f"{payload}\n"
        "-----BEGIN PGP SIGNATURE-----\n"
        "iQEzBAEBCAAd...\n"
        "-----END PGP SIGNATURE-----\n"
    )
    assert srp.extract_modulus(signed) == base64.b64decode(payload)


def test_matches_proton_client_reference():
    # Authoritative cross-check: identical output to proton-python-client for the same inputs
    # and the same secret ephemeral. Skipped where the reference package is not installed
    # (it is not a runtime/CI dependency — it only validates the reimplementation locally).
    ref = pytest.importorskip("proton.srp")
    for _ in range(20):
        password = os.urandom(8).hex()
        modulus = _rand_modulus()
        salt = os.urandom(16)
        server_ephemeral = _rand_modulus()
        secret_a = os.urandom(32)
        mine = srp.SRPUser(password, modulus, secret_a=secret_a)
        theirs = ref.User(password, modulus, bytes_a=secret_a)
        assert mine.get_challenge() == theirs.get_challenge()
        assert mine.process_challenge(salt, server_ephemeral) == theirs.process_challenge(salt, server_ephemeral)
