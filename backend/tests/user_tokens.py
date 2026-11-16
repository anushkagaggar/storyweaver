"""Tokens like the ones the Vercel server signs for a signed-in user (frontend/lib/auth.ts)."""

import time
from typing import Any

import jwt

from storyweaver.auth.user_token import AUDIENCE, ISSUER

USER_TOKEN_SECRET = "test-user-token-secret-0123456789abcdef"  # pragma: allowlist secret
ALICE = "a" * 43  # pseudonymous ids look like 43 chars of base64url (an HMAC-SHA256)
BOB = "b" * 43


def user_token(sub: str = ALICE, secret: str = USER_TOKEN_SECRET, **overrides: Any) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {"iss": ISSUER, "aud": AUDIENCE, "sub": sub, "iat": now, "exp": now + 60}
    claims.update(overrides)
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, secret, algorithm="HS256")


def as_user(sub: str = ALICE, **headers: str) -> dict[str, str]:
    return {"X-User-Token": user_token(sub), **headers}
