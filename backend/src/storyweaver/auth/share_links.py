"""Share links (V3): a story's owner can let anyone with the link read it for 15 minutes.

The link carries its own expiry, fixed when the owner made it and covered by an HMAC, so it is stateless:
forwarding or reopening it never extends it, and a deleted story's links simply find nothing. The key is
derived from `USER_TOKEN_SECRET`, so a holder of only the API key (MCP, scripts) can't mint links to users'
stories.

Token: `<story id as 32 hex>.<expiry, unix seconds>.<32 chars of base64url HMAC-SHA256>`, URL-safe as is.
"""

import base64
import hashlib
import hmac
import re
import time
import uuid
from dataclasses import dataclass

TOKEN = re.compile(r"^([0-9a-f]{32})\.([0-9]{10})\.([A-Za-z0-9_-]{32})$")
_KEY_LABEL = b"storyweaver share links v1"


@dataclass(frozen=True)
class SharedStory:
    story_id: uuid.UUID
    expires_at: int  # unix seconds

    def seconds_left(self, now: float) -> int:
        return max(0, int(self.expires_at - now))


class ShareLinks:
    def __init__(self, secret: str, ttl_seconds: int) -> None:
        self._key = hmac.new(secret.encode(), _KEY_LABEL, hashlib.sha256).digest()
        self._ttl = ttl_seconds

    def _sign(self, payload: str) -> str:
        digest = hmac.new(self._key, payload.encode(), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).decode().rstrip("=")[:32]

    def make(self, story_id: uuid.UUID, now: float | None = None) -> tuple[str, SharedStory]:
        expires_at = int(now if now is not None else time.time()) + self._ttl
        payload = f"{story_id.hex}.{expires_at}"
        return f"{payload}.{self._sign(payload)}", SharedStory(story_id, expires_at)

    def read(self, token: str, now: float | None = None) -> SharedStory | None:
        """The story a valid, unexpired link points at; None for anything else (no reason given)."""
        match = TOKEN.fullmatch(token)
        if match is None:
            return None
        story_hex, expiry, signature = match.groups()
        if not hmac.compare_digest(signature, self._sign(f"{story_hex}.{expiry}")):
            return None
        shared = SharedStory(uuid.UUID(hex=story_hex), int(expiry))
        current = now if now is not None else time.time()
        # A valid signature can only carry an expiry we set, so it is never more than one TTL ahead.
        return shared if current < shared.expires_at <= current + self._ttl else None
