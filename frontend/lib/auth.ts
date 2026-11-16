// Server-only: Google sign-in, the session cookie and the per-request user token for the backend (V3).
// Never import this from a client component (it reads server secrets).
//
// Flow (OpenID Connect authorization code + PKCE, plain node:crypto, no auth library):
//  1. /api/auth/signin makes a state, a nonce and a PKCE verifier, keeps them in a short signed cookie and sends
//     the parent to Google, asking only for "openid": no email, name or picture.
//  2. /api/auth/callback/google checks the state, swaps the code for an ID token straight from Google's token
//     endpoint over TLS (OIDC Core 3.1.3.7 allows skipping the signature check then) and checks iss, aud, exp
//     and the nonce.
//  3. Google's account id never leaves this server: it becomes a pseudonymous id, an HMAC under AUTH_SECRET, kept
//     in an httpOnly session cookie (a JWT signed with a key derived from AUTH_SECRET).
//  4. Each backend call carries X-User-Token: a 60-second JWT for that id, signed with USER_TOKEN_SECRET, which
//     only this server and the backend hold (backend auth/user_token.py).
//
// Changing AUTH_SECRET signs everyone out and gives every account a new id, so their old stories are no longer
// theirs. Only change it if it leaked.

import { createHash, createHmac, randomBytes, timingSafeEqual } from "node:crypto";

export const SESSION_COOKIE = "sw_session";
export const OAUTH_COOKIE = "sw_oauth";
export const SESSION_SECONDS = 7 * 24 * 3600;
const OAUTH_SECONDS = 600;
const USER_TOKEN_SECONDS = 60;
export const CALLBACK_PATH = "/api/auth/callback/google";

const GOOGLE_AUTHORIZE = "https://accounts.google.com/o/oauth2/v2/auth";
export const GOOGLE_TOKEN = "https://oauth2.googleapis.com/token";
const GOOGLE_ISSUERS = new Set(["https://accounts.google.com", "accounts.google.com"]);

type Claims = Record<string, unknown> & { exp: number };
export type Session = { uid: string };
export type OAuthState = { state: string; verifier: string; nonce: string; next: string };

const now = () => Math.floor(Date.now() / 1000);

// --- HS256 JWT (the only algorithm we sign or accept) ---

function hmac(key: Buffer | string, data: string): Buffer {
  return createHmac("sha256", key).update(data).digest();
}

export function signJwt(claims: Claims, key: Buffer | string): string {
  const head = Buffer.from(JSON.stringify({ alg: "HS256", typ: "JWT" })).toString("base64url");
  const body = Buffer.from(JSON.stringify(claims)).toString("base64url");
  return `${head}.${body}.${hmac(key, `${head}.${body}`).toString("base64url")}`;
}

/** The claims of a JWT we signed with `key` that hasn't expired; null for anything else. */
export function verifyJwt(token: string | undefined, key: Buffer | string, at = now()): Claims | null {
  const parts = token?.split(".");
  if (!parts || parts.length !== 3) return null;
  const [head, body, signature] = parts;
  const expected = hmac(key, `${head}.${body}`);
  const given = Buffer.from(signature, "base64url");
  if (given.length !== expected.length || !timingSafeEqual(given, expected)) return null;
  try {
    const header = JSON.parse(Buffer.from(head, "base64url").toString()) as { alg?: unknown };
    const claims = JSON.parse(Buffer.from(body, "base64url").toString()) as Claims;
    if (header.alg !== "HS256" || typeof claims.exp !== "number" || claims.exp <= at) return null;
    return claims;
  } catch {
    return null;
  }
}

// --- Keys and settings ---

/** Separate keys for separate jobs, all derived from AUTH_SECRET. */
function derivedKey(purpose: "session" | "oauth" | "uid"): Buffer | null {
  const secret = process.env.AUTH_SECRET;
  if (!secret || secret.length < 32) return null;
  return hmac(secret, `storyweaver ${purpose} v1`);
}

export type GoogleConfig = { clientId: string; clientSecret: string };

export function googleConfig(): GoogleConfig | null {
  const clientId = process.env.GOOGLE_CLIENT_ID?.trim();
  const clientSecret = process.env.GOOGLE_CLIENT_SECRET?.trim();
  return clientId && clientSecret && derivedKey("oauth") ? { clientId, clientSecret } : null;
}

/** The site's own origin for Google's redirect: AUTH_URL when set (it must match Google's settings exactly). */
export function siteOrigin(request: Request): string {
  const configured = process.env.AUTH_URL?.trim().replace(/\/$/, "");
  return configured || new URL(request.url).origin;
}

// --- Session ---

export function pseudonymousId(googleSubject: string): string | null {
  const key = derivedKey("uid");
  return key ? hmac(key, `google:${googleSubject}`).toString("base64url") : null;
}

export function sessionCookieValue(uid: string, at = now()): string | null {
  const key = derivedKey("session");
  return key ? signJwt({ sub: uid, iat: at, exp: at + SESSION_SECONDS }, key) : null;
}

export function readSessionValue(value: string | undefined, at = now()): Session | null {
  const key = derivedKey("session");
  const claims = key ? verifyJwt(value, key, at) : null;
  return claims && typeof claims.sub === "string" ? { uid: claims.sub } : null;
}

/** A cookie's value from a Cookie header. */
export function cookieValue(header: string | null, name: string): string | undefined {
  for (const part of header?.split(";") ?? []) {
    const [key, ...rest] = part.trim().split("=");
    if (key === name) return decodeURIComponent(rest.join("="));
  }
  return undefined;
}

export function sessionFromRequest(request: Request): Session | null {
  return readSessionValue(cookieValue(request.headers.get("cookie"), SESSION_COOKIE));
}

export function cookie(name: string, value: string, maxAge: number, request: Request, path = "/"): string {
  const secure = siteOrigin(request).startsWith("https://") ? "; Secure" : "";
  return `${name}=${encodeURIComponent(value)}; Path=${path}; Max-Age=${maxAge}; HttpOnly; SameSite=Lax${secure}`;
}

// --- Backend calls on the user's behalf ---

/** X-User-Token for one backend call, or null when USER_TOKEN_SECRET isn't set. */
export function userToken(session: Session, at = now()): string | null {
  const secret = process.env.USER_TOKEN_SECRET;
  if (!secret) return null;
  return signJwt(
    { iss: "storyweaver-web", aud: "storyweaver-api", sub: session.uid, iat: at, exp: at + USER_TOKEN_SECONDS },
    secret,
  );
}

function problem(status: number, detail: string, code: string): Response {
  return Response.json(
    { type: "about:blank", title: status === 401 ? "Unauthorized" : "Forbidden", status, detail, code },
    { status, headers: { "content-type": "application/problem+json" } },
  );
}

/**
 * The headers a route sends to the backend for the signed-in user, or the response to send instead: 401 when
 * not signed in, 403 for a state-changing request from another site (cookies are SameSite=Lax as well).
 */
export function userHeaders(request: Request): { headers: Record<string, string> } | { response: Response } {
  if (request.method !== "GET" && request.method !== "HEAD") {
    const origin = request.headers.get("origin");
    if (origin && origin !== siteOrigin(request)) {
      return { response: problem(403, "This request came from another site.", "cross_site") };
    }
  }
  const session = sessionFromRequest(request);
  const token = session ? userToken(session) : null;
  if (!token) return { response: problem(401, "Please sign in to make and read stories.", "sign_in_required") };
  return { headers: { "X-User-Token": token } };
}

// --- Google sign-in ---

/** Only a path on this site (never "//host" or "https://..."), so sign-in can't redirect elsewhere. */
export function safeNext(next: string | null): string {
  return next && next.startsWith("/") && !next.startsWith("//") && !next.startsWith("/\\") ? next : "/";
}

export function startSignIn(request: Request, config: GoogleConfig, next: string): { url: string; cookie: string } {
  const key = derivedKey("oauth");
  if (!key) throw new Error("AUTH_SECRET is not set.");
  const flow: OAuthState = {
    state: randomBytes(24).toString("base64url"),
    verifier: randomBytes(48).toString("base64url"),
    nonce: randomBytes(24).toString("base64url"),
    next: safeNext(next),
  };
  const params = new URLSearchParams({
    client_id: config.clientId,
    redirect_uri: `${siteOrigin(request)}${CALLBACK_PATH}`,
    response_type: "code",
    scope: "openid", // only Google's account id: no email, name or picture
    state: flow.state,
    nonce: flow.nonce,
    code_challenge: createHash("sha256").update(flow.verifier).digest("base64url"),
    code_challenge_method: "S256",
    prompt: "select_account",
  });
  const value = signJwt({ ...flow, exp: now() + OAUTH_SECONDS }, key);
  return {
    url: `${GOOGLE_AUTHORIZE}?${params}`,
    cookie: cookie(OAUTH_COOKIE, value, OAUTH_SECONDS, request, CALLBACK_PATH),
  };
}

export function readOAuthState(request: Request): OAuthState | null {
  const key = derivedKey("oauth");
  const claims = key ? verifyJwt(cookieValue(request.headers.get("cookie"), OAUTH_COOKIE), key) : null;
  if (!claims) return null;
  const { state, verifier, nonce, next } = claims;
  return [state, verifier, nonce, next].every((v) => typeof v === "string")
    ? { state: state as string, verifier: verifier as string, nonce: nonce as string, next: safeNext(next as string) }
    : null;
}

/** Google's account id from an ID token received directly from Google's token endpoint; null if any check fails. */
export function googleSubject(idToken: unknown, config: GoogleConfig, nonce: string, at = now()): string | null {
  if (typeof idToken !== "string") return null;
  try {
    const claims = JSON.parse(Buffer.from(idToken.split(".")[1] ?? "", "base64url").toString()) as Record<string, unknown>;
    const ok =
      typeof claims.iss === "string" &&
      GOOGLE_ISSUERS.has(claims.iss) &&
      claims.aud === config.clientId &&
      typeof claims.exp === "number" &&
      claims.exp > at &&
      claims.nonce === nonce &&
      typeof claims.sub === "string" &&
      claims.sub.length > 0;
    return ok ? (claims.sub as string) : null;
  } catch {
    return null;
  }
}
