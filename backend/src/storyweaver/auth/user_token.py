"""The signed-in user, as vouched for by the Vercel server (V3).

Parents sign in with Google on the website. Google's account id never reaches us: the Vercel server turns it
into a pseudonymous id (an HMAC under a key only it holds) and, for each API call, sends a short-lived JWT in
`X-User-Token`, signed with `USER_TOKEN_SECRET` (HS256), which only Vercel and this server hold:

    {"iss": "storyweaver-web", "aud": "storyweaver-api", "sub": "<pseudonymous id>", "iat": ..., "exp": ...}

A caller with only the API key (the MCP server, scripts) sends no token and acts as one separate owner,
`OPERATOR`. It can't read users' stories, and users can't read its stories; a leaked API key therefore doesn't
expose anyone's stories, and can't impersonate a user without `USER_TOKEN_SECRET`.
"""

import re

import jwt

ISSUER = "storyweaver-web"
AUDIENCE = "storyweaver-api"
OPERATOR = "operator"  # the owner for API-key-only callers; never a valid token subject (see SUBJECT)
SUBJECT = re.compile(r"^[A-Za-z0-9_-]{32,64}$")
MAX_LIFETIME_SECONDS = 300  # tokens are minted per request; a long-lived one is a mistake or a forgery
LEEWAY_SECONDS = 10  # clock skew between Vercel and the Space


class InvalidUserTokenError(Exception):
    """The token is missing a claim, expired, forged, or not meant for us."""


class SignInRequiredError(Exception):
    """A request's X-User-Token can't be trusted (invalid, or sign-in isn't configured here): 401."""


def verify_user_token(token: str, secret: str) -> str:
    """The token's subject (the user's pseudonymous id), or InvalidUserTokenError."""
    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=["HS256"],  # never taken from the token's header
            audience=AUDIENCE,
            issuer=ISSUER,
            leeway=LEEWAY_SECONDS,
            options={"require": ["exp", "iat", "sub", "iss", "aud"]},
        )
    except jwt.PyJWTError as exc:
        raise InvalidUserTokenError(str(exc)) from exc
    subject = claims["sub"]
    if not isinstance(subject, str) or not SUBJECT.fullmatch(subject):
        raise InvalidUserTokenError("Invalid subject.")
    if claims["exp"] - claims["iat"] > MAX_LIFETIME_SECONDS:
        raise InvalidUserTokenError("Token lifetime too long.")
    return subject
