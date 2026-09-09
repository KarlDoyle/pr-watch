"""App JWT signing + installation-access-token exchange for the GitHub App.

Two-step auth a GitHub App uses to call the API as an installation:

  1. Sign a short-lived JWT with the App's RS256 private key, `iss` = App ID.
     GitHub's API: https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-json-web-token-jwt-for-a-github-app
  2. Exchange that JWT for an installation access token (~1h TTL) via
     ``POST /app/installations/{installation_id}/access_tokens``.
     https://docs.github.com/en/rest/apps/apps#create-an-installation-access-token-for-an-app

RS256 signing needs real RSA private-key operations, which the standard
library does not provide. Rather than pull that requirement into
``pr_watch.py`` (deliberately zero-dependency), it is isolated here behind
an optional import: the core CLI/polling mode is completely unaffected
whether or not `cryptography` is installed. Install it only if you're
running the GitHub App / webhook mode:

    pip install cryptography
    # or: uv sync --extra github-app
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable
from dataclasses import dataclass

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    _HAS_CRYPTOGRAPHY = True
except ImportError:  # pragma: no cover - exercised via _HAS_CRYPTOGRAPHY branch in tests
    _HAS_CRYPTOGRAPHY = False


class AuthError(Exception):
    """Raised for any App-auth failure (missing dep, bad key, bad response)."""


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def generate_app_jwt(
    app_id: str,
    private_key_pem: bytes,
    *,
    now: int | None = None,
    ttl_s: int = 540,  # GitHub's max is 10 minutes; stay under it
) -> str:
    """Build and sign the App-level JWT used to mint installation tokens.

    ``now`` is injectable for deterministic tests. Backdates ``iat`` by 60s
    per GitHub's guidance, to tolerate clock drift between this host and
    GitHub's servers.
    """
    if not _HAS_CRYPTOGRAPHY:
        raise AuthError(
            "generate_app_jwt() requires the 'cryptography' package "
            "(pip install cryptography, or `uv sync --extra github-app`)."
        )
    issued = int(now if now is not None else time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {"iat": issued - 60, "exp": issued + ttl_s, "iss": str(app_id)}
    signing_input = (
        f"{_b64url(json.dumps(header, separators=(',', ':')).encode('utf-8'))}."
        f"{_b64url(json.dumps(payload, separators=(',', ':')).encode('utf-8'))}"
    )
    try:
        key = serialization.load_pem_private_key(private_key_pem, password=None)
        signature = key.sign(signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    except ValueError as exc:
        raise AuthError(f"Could not load/sign with the App private key: {exc}") from exc
    return f"{signing_input}.{_b64url(signature)}"


@dataclass
class InstallationToken:
    token: str
    expires_at: str  # ISO-8601, as returned by GitHub


HttpJsonFn = Callable[..., dict]


def get_installation_token(
    app_jwt: str,
    installation_id: str,
    *,
    http_json: HttpJsonFn,
) -> InstallationToken:
    """Exchange an App JWT for an installation access token.

    ``http_json`` is injected (normally ``pr_watch.http_json``) so this
    stays testable without real network access and without importing
    ``pr_watch`` at module load time (keeps the dependency direction
    explicit: github_app -> pr_watch, never the reverse).
    """
    headers = {
        "Authorization": f"Bearer {app_jwt}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "pr-watch-github-app",
    }
    url = f"https://api.github.com/app/installations/{installation_id}/access_tokens"
    data = http_json(url, method="POST", headers=headers)
    token = data.get("token")
    expires_at = data.get("expires_at")
    if not token or not expires_at:
        raise AuthError(f"Unexpected installation-token response: {data!r}")
    return InstallationToken(token=token, expires_at=expires_at)
