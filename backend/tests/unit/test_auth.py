import time
import uuid

import jwt
import pytest

from storyweaver.auth.share_links import ShareLinks
from storyweaver.auth.user_token import InvalidUserTokenError, verify_user_token
from user_tokens import ALICE, USER_TOKEN_SECRET, user_token

pytestmark = pytest.mark.unit

STORY = uuid.UUID("4b1e6b0e-2b55-4e0b-9a3c-1f0e2d3c4b5a")
FORGER_SECRET = "another-secret-of-the-same-length-000000"  # pragma: allowlist secret


def test_a_valid_token_names_the_user() -> None:
    assert verify_user_token(user_token(), USER_TOKEN_SECRET) == ALICE


@pytest.mark.parametrize(
    "token",
    [
        user_token(secret=FORGER_SECRET),  # forged
        user_token(exp=int(time.time()) - 60),  # expired
        user_token(aud="someone-else"),
        user_token(iss="someone-else"),
        user_token(sub="operator"),  # can't become the API-key caller's owner
        user_token(sub="short"),
        user_token(iat=None),  # every claim is required
        user_token(exp=int(time.time()) + 3600),  # far longer-lived than a per-request token
        "not-a-jwt",
    ],
)
def test_bad_tokens_are_refused(token: str) -> None:
    with pytest.raises(InvalidUserTokenError):
        verify_user_token(token, USER_TOKEN_SECRET)


def test_the_algorithm_is_never_taken_from_the_token() -> None:
    unsigned = jwt.encode(
        {"iss": "storyweaver-web", "aud": "storyweaver-api", "sub": ALICE, "iat": 1, "exp": 2**40},
        key=None,
        algorithm="none",
    )
    with pytest.raises(InvalidUserTokenError):
        verify_user_token(unsigned, USER_TOKEN_SECRET)


def test_a_share_link_works_until_it_expires_and_is_never_renewed() -> None:
    links = ShareLinks(USER_TOKEN_SECRET, ttl_seconds=900)
    token, shared = links.make(STORY, now=1_800_000_000)

    assert shared.expires_at == 1_800_000_900
    assert links.read(token, now=1_800_000_000) == shared
    assert links.read(token, now=1_800_000_899) == shared  # opened again (or forwarded): same expiry
    assert links.read(token, now=1_800_000_900) is None
    assert shared.seconds_left(1_800_000_600) == 300


def test_share_links_cant_be_forged_or_stretched() -> None:
    links = ShareLinks(USER_TOKEN_SECRET, ttl_seconds=900)
    token, _ = links.make(STORY, now=1_800_000_000)
    story_hex, expiry, signature = token.split(".")

    later = f"{story_hex}.{int(expiry) + 3600}.{signature}"  # a longer expiry with the old signature
    other_story = f"{uuid.uuid4().hex}.{expiry}.{signature}"
    other_key = ShareLinks("x" * 40, ttl_seconds=900).make(STORY, now=1_800_000_000)[0]
    for bad in (later, other_story, other_key, token + "x", "", "../../v1/stories"):
        assert links.read(bad, now=1_800_000_000) is None


def test_a_link_made_with_a_longer_ttl_is_refused_after_the_ttl_shrinks() -> None:
    long_lived, _ = ShareLinks(USER_TOKEN_SECRET, ttl_seconds=3600).make(STORY, now=1_800_000_000)
    assert ShareLinks(USER_TOKEN_SECRET, ttl_seconds=900).read(long_lived, now=1_800_000_000) is None
