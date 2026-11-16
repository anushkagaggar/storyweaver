import { createHmac } from "node:crypto";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { GET as callback } from "@/app/api/auth/callback/google/route";
import { GET as signin } from "@/app/api/auth/signin/route";
import { GET as sharedStory } from "@/app/api/shared/[token]/route";
import { POST as createStory } from "@/app/api/stories/route";
import {
  OAUTH_COOKIE,
  SESSION_COOKIE,
  cookieValue,
  googleSubject,
  pseudonymousId,
  readSessionValue,
  safeNext,
  sessionCookieValue,
  signJwt,
  verifyJwt,
} from "@/lib/auth";
import { shareLinkLifetime } from "@/lib/story-logic";

import { UID, signedIn, stubAuthEnv } from "./signed-in";

const GOOGLE = { clientId: "client-123.apps.googleusercontent.com", clientSecret: "x" };

function payload(token: string): Record<string, unknown> {
  return JSON.parse(Buffer.from(token.split(".")[1], "base64url").toString()) as Record<string, unknown>;
}

function idToken(claims: Record<string, unknown>): string {
  return `e30.${Buffer.from(JSON.stringify(claims)).toString("base64url")}.sig`;
}

beforeEach(() => {
  stubAuthEnv();
  vi.stubEnv("GOOGLE_CLIENT_ID", GOOGLE.clientId);
  vi.stubEnv("GOOGLE_CLIENT_SECRET", "google-secret");
});
afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

describe("JWT and session", () => {
  it("verifies only our own unexpired HS256 tokens", () => {
    const token = signJwt({ sub: "x", exp: 2_000 }, "key");
    expect(verifyJwt(token, "key", 1_000)?.sub).toBe("x");
    expect(verifyJwt(token, "key", 2_000)).toBeNull(); // expired
    expect(verifyJwt(token, "other-key", 1_000)).toBeNull();
    const [head, , signature] = token.split(".");
    const changed = Buffer.from(JSON.stringify({ sub: "admin", exp: 2_000 })).toString("base64url");
    expect(verifyJwt(`${head}.${changed}.${signature}`, "key", 1_000)).toBeNull();
    const none = `${Buffer.from('{"alg":"none"}').toString("base64url")}.${changed}.`;
    expect(verifyJwt(none, "key", 1_000)).toBeNull();
    expect(verifyJwt(undefined, "key")).toBeNull();
  });

  it("keeps a session for a week and never Google's own id", () => {
    const uid = pseudonymousId("google-sub-1")!;
    expect(uid).toMatch(/^[A-Za-z0-9_-]{43}$/); // what the backend accepts as a token subject
    expect(uid).not.toContain("google-sub-1");
    expect(pseudonymousId("google-sub-1")).toBe(uid);
    const value = sessionCookieValue(uid, 1_000)!;
    expect(readSessionValue(value, 1_000 + 6 * 24 * 3600)).toEqual({ uid });
    expect(readSessionValue(value, 1_000 + 8 * 24 * 3600)).toBeNull();
  });

  it("only redirects to paths on this site after sign-in", () => {
    expect(safeNext("/story/abc")).toBe("/story/abc");
    for (const bad of ["//evil.example", "https://evil.example", "/\\evil.example", null, ""]) expect(safeNext(bad)).toBe("/");
  });
});

describe("Google ID token checks", () => {
  const good = { iss: "https://accounts.google.com", aud: GOOGLE.clientId, exp: 2_000, nonce: "n1", sub: "1234" };

  it("accepts a token for us with the right nonce", () => {
    expect(googleSubject(idToken(good), GOOGLE, "n1", 1_000)).toBe("1234");
  });

  it.each([
    ["another app", { ...good, aud: "someone-else" }],
    ["another issuer", { ...good, iss: "https://evil.example" }],
    ["expired", { ...good, exp: 999 }],
    ["a replayed nonce", { ...good, nonce: "n0" }],
    ["no subject", { ...good, sub: "" }],
  ])("refuses %s", (_, claims) => {
    expect(googleSubject(idToken(claims), GOOGLE, "n1", 1_000)).toBeNull();
  });
});

describe("sign-in flow", () => {
  it("asks Google only for openid, with PKCE, and remembers the state in a signed cookie", async () => {
    const response = await signin(new Request("http://localhost:3000/api/auth/signin?next=/story/abc"));
    expect(response.status).toBe(302);
    const location = new URL(response.headers.get("location")!);
    expect(location.origin).toBe("https://accounts.google.com");
    expect(location.searchParams.get("scope")).toBe("openid");
    expect(location.searchParams.get("code_challenge_method")).toBe("S256");
    expect(location.searchParams.get("redirect_uri")).toBe("http://localhost:3000/api/auth/callback/google");
    const setCookie = response.headers.get("set-cookie")!;
    expect(setCookie).toMatch(/HttpOnly/);
    expect(setCookie).toMatch(/SameSite=Lax/);
  });

  async function startAndReturn(state?: string) {
    const start = await signin(new Request("http://localhost:3000/api/auth/signin?next=/story/abc"));
    const sent = new URL(start.headers.get("location")!).searchParams;
    const oauth = cookieValue(start.headers.get("set-cookie"), OAUTH_COOKIE)!;
    const url = `http://localhost:3000/api/auth/callback/google?code=c1&state=${state ?? sent.get("state")}`;
    return { sent, request: new Request(url, { headers: { cookie: `${OAUTH_COOKIE}=${oauth}` } }) };
  }

  it("signs the parent in and returns them where they were going", async () => {
    const { sent, request } = await startAndReturn();
    const google = vi.fn().mockResolvedValue(
      Response.json({ id_token: idToken({ iss: "accounts.google.com", aud: GOOGLE.clientId, exp: 4e9, nonce: sent.get("nonce"), sub: "g-42" }) }),
    );
    vi.stubGlobal("fetch", google);

    const response = await callback(request);

    expect(response.status).toBe(302);
    expect(response.headers.get("location")).toBe("http://localhost:3000/story/abc");
    const body = (google.mock.calls[0][1] as RequestInit).body as URLSearchParams;
    expect(body.get("code_verifier")).toBeTruthy(); // PKCE
    const cookies = response.headers.getSetCookie();
    const session = cookies.find((c) => c.startsWith(`${SESSION_COOKIE}=`))!;
    expect(readSessionValue(decodeURIComponent(session.split(";")[0].split("=")[1]))).toEqual({ uid: pseudonymousId("g-42") });
  });

  it("refuses a callback whose state doesn't match (login CSRF)", async () => {
    const { request } = await startAndReturn("forged-state");
    const google = vi.fn();
    vi.stubGlobal("fetch", google);
    const response = await callback(request);
    expect(response.headers.get("location")).toBe("http://localhost:3000/?signin=failed");
    expect(google).not.toHaveBeenCalled();
  });
});

describe("story routes as the signed-in parent", () => {
  it("sends a short-lived user token signed with USER_TOKEN_SECRET, and no IP", async () => {
    const upstream = vi.fn().mockResolvedValue(Response.json({ story_id: "s", job_id: "j", status: "queued" }, { status: 202 }));
    vi.stubGlobal("fetch", upstream);

    const response = await createStory(
      signedIn("http://localhost:3000/api/stories", {
        method: "POST",
        headers: { "content-type": "application/json", "x-forwarded-for": "203.0.113.7" },
        body: JSON.stringify({ prompt: "a sleepy owl" }),
      }),
    );

    expect(response.status).toBe(202);
    const headers = (upstream.mock.calls[0][1] as RequestInit).headers as Record<string, string>;
    const token = headers["X-User-Token"];
    const [head, body, signature] = token.split(".");
    const expected = createHmac("sha256", "a-test-user-token-secret-0123456789").update(`${head}.${body}`).digest("base64url");
    expect(signature).toBe(expected);
    const claims = payload(token);
    expect(claims).toMatchObject({ iss: "storyweaver-web", aud: "storyweaver-api", sub: UID });
    expect((claims.exp as number) - (claims.iat as number)).toBe(60);
    expect(Object.keys(headers).map((h) => h.toLowerCase())).not.toContain("x-client-ip");
  });

  it("refuses a story from someone who isn't signed in", async () => {
    const upstream = vi.fn();
    vi.stubGlobal("fetch", upstream);
    const response = await createStory(
      new Request("http://localhost:3000/api/stories", { method: "POST", body: JSON.stringify({ prompt: "an owl" }) }),
    );
    expect(response.status).toBe(401);
    expect((await response.json()).code).toBe("sign_in_required");
    expect(upstream).not.toHaveBeenCalled();
  });

  it("refuses a signed-in request sent from another site", async () => {
    const upstream = vi.fn();
    vi.stubGlobal("fetch", upstream);
    const response = await createStory(
      signedIn("http://localhost:3000/api/stories", {
        method: "POST",
        headers: { origin: "https://evil.example" },
        body: JSON.stringify({ prompt: "an owl" }),
      }),
    );
    expect(response.status).toBe(403);
    expect(upstream).not.toHaveBeenCalled();
  });
});

describe("share links", () => {
  const TOKEN = `${"a".repeat(32)}.1800000900.${"b".repeat(32)}`;

  it("forwards a well-formed link without sign-in, and nothing else", async () => {
    const upstream = vi.fn().mockResolvedValue(Response.json({ id: "s" }));
    vi.stubGlobal("fetch", upstream);
    const ok = await sharedStory(new Request(`http://localhost/api/shared/${TOKEN}`), { params: Promise.resolve({ token: TOKEN }) });
    expect(ok.status).toBe(200);
    expect(upstream.mock.calls[0][0]).toBe(`https://backend.example/v1/shared/${TOKEN}`);
    const headers = (upstream.mock.calls[0][1] as RequestInit).headers as Record<string, string>;
    expect(headers["X-User-Token"]).toBeUndefined();

    const bad = await sharedStory(new Request("http://localhost/api/shared/x"), { params: Promise.resolve({ token: "../../v1/stories" }) });
    expect(bad.status).toBe(404);
    expect(upstream).toHaveBeenCalledTimes(1);
  });

  it("says how long a link has left, and nothing once it's gone", () => {
    const now = new Date("2026-11-16T20:00:00Z");
    expect(shareLinkLifetime("2026-11-16T20:15:00Z", now, "en-GB")).toMatch(/\(15 minutes\)$/);
    expect(shareLinkLifetime("2026-11-16T20:00:30Z", now, "en-GB")).toMatch(/\(1 minute\)$/);
    expect(shareLinkLifetime("2026-11-16T19:59:00Z", now)).toBeNull();
  });
});
