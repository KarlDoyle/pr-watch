"""Tests for github_app/auth.py — App JWT signing + installation-token
exchange. No network: the JWT is decoded and its signature verified
locally against the matching public key; the installation-token exchange
is driven through an injected `http_json` (same pattern pr_watch.py's
tests use for `urlopen`), never real HTTP.
"""

from __future__ import annotations

import base64
import json

import pytest

cryptography = pytest.importorskip("cryptography")
from cryptography.exceptions import InvalidSignature  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import padding, rsa  # noqa: E402

from github_app import auth  # noqa: E402


def _b64url_decode(segment: str) -> bytes:
    padded = segment + "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(padded)


@pytest.fixture(scope="module")
def keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return private_key, pem


def test_generate_app_jwt_has_valid_header_and_claims(keypair):
    _, pem = keypair
    token = auth.generate_app_jwt("123456", pem, now=1_700_000_000)
    header_seg, payload_seg, _sig_seg = token.split(".")

    header = json.loads(_b64url_decode(header_seg))
    payload = json.loads(_b64url_decode(payload_seg))

    assert header == {"alg": "RS256", "typ": "JWT"}
    assert payload["iss"] == "123456"
    assert payload["iat"] == 1_700_000_000 - 60
    assert payload["exp"] == 1_700_000_000 + 540


def test_generate_app_jwt_signature_verifies_against_public_key(keypair):
    private_key, pem = keypair
    public_key = private_key.public_key()
    token = auth.generate_app_jwt("123456", pem, now=1_700_000_000)
    header_seg, payload_seg, sig_seg = token.split(".")
    signing_input = f"{header_seg}.{payload_seg}".encode("ascii")
    signature = _b64url_decode(sig_seg)

    # Raises if invalid; a passing test is "did not raise".
    public_key.verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())


def test_generate_app_jwt_wrong_key_produces_unverifiable_signature(keypair):
    _, pem = keypair
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = auth.generate_app_jwt("123456", pem, now=1_700_000_000)
    header_seg, payload_seg, sig_seg = token.split(".")
    signing_input = f"{header_seg}.{payload_seg}".encode("ascii")
    signature = _b64url_decode(sig_seg)

    with pytest.raises(InvalidSignature):
        other_key.public_key().verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())


def test_generate_app_jwt_bad_key_raises_auth_error():
    with pytest.raises(auth.AuthError):
        auth.generate_app_jwt("123456", b"not a real PEM key")


def test_get_installation_token_returns_token():
    captured = {}

    def fake_http_json(url, *, method="GET", headers=None, body=None):
        captured["url"] = url
        captured["method"] = method
        captured["auth"] = headers.get("Authorization")
        return {"token": "ghs_abc123", "expires_at": "2025-10-13T10:00:00Z"}

    result = auth.get_installation_token("app.jwt.here", "999", http_json=fake_http_json)
    assert result.token == "ghs_abc123"
    assert result.expires_at == "2025-10-13T10:00:00Z"
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/app/installations/999/access_tokens")
    assert captured["auth"] == "Bearer app.jwt.here"


def test_get_installation_token_raises_on_unexpected_response():
    def fake_http_json(url, *, method="GET", headers=None, body=None):
        return {"message": "Not Found"}

    with pytest.raises(auth.AuthError):
        auth.get_installation_token("app.jwt.here", "999", http_json=fake_http_json)
