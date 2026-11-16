import {
  CALLBACK_PATH,
  GOOGLE_TOKEN,
  OAUTH_COOKIE,
  SESSION_COOKIE,
  SESSION_SECONDS,
  cookie,
  googleConfig,
  googleSubject,
  pseudonymousId,
  readOAuthState,
  sessionCookieValue,
  siteOrigin,
} from "@/lib/auth";

export const dynamic = "force-dynamic";

/** Back to the home page with a reason, clearing the one-time sign-in cookie. */
function giveUp(request: Request, reason: "cancelled" | "failed"): Response {
  const headers = new Headers({ location: `${siteOrigin(request)}/?signin=${reason}`, "cache-control": "no-store" });
  headers.append("set-cookie", cookie(OAUTH_COOKIE, "", 0, request, CALLBACK_PATH));
  return new Response(null, { status: 302, headers });
}

/** GET /api/auth/callback/google: Google sends the parent back here after they sign in (lib/auth.ts, step 2). */
export async function GET(request: Request): Promise<Response> {
  const config = googleConfig();
  const params = new URL(request.url).searchParams;
  if (params.get("error")) return giveUp(request, "cancelled");
  const flow = readOAuthState(request);
  const code = params.get("code");
  if (!config || !flow || !code || params.get("state") !== flow.state) return giveUp(request, "failed");

  let idToken: unknown;
  try {
    const response = await fetch(GOOGLE_TOKEN, {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({
        code,
        client_id: config.clientId,
        client_secret: config.clientSecret,
        redirect_uri: `${siteOrigin(request)}${CALLBACK_PATH}`,
        grant_type: "authorization_code",
        code_verifier: flow.verifier,
      }),
      cache: "no-store",
      signal: AbortSignal.timeout(10_000),
    });
    if (!response.ok) return giveUp(request, "failed");
    idToken = ((await response.json()) as { id_token?: unknown }).id_token;
  } catch {
    return giveUp(request, "failed");
  }

  const subject = googleSubject(idToken, config, flow.nonce);
  const uid = subject ? pseudonymousId(subject) : null;
  const session = uid ? sessionCookieValue(uid) : null;
  if (!session) return giveUp(request, "failed");

  const headers = new Headers({ location: `${siteOrigin(request)}${flow.next}`, "cache-control": "no-store" });
  headers.append("set-cookie", cookie(OAUTH_COOKIE, "", 0, request, CALLBACK_PATH));
  headers.append("set-cookie", cookie(SESSION_COOKIE, session, SESSION_SECONDS, request));
  return new Response(null, { status: 302, headers });
}
