import { SESSION_COOKIE, cookie, siteOrigin } from "@/lib/auth";

export const dynamic = "force-dynamic";

/** POST /api/auth/signout (a form button): forget the session cookie and go home. */
export async function POST(request: Request): Promise<Response> {
  const origin = request.headers.get("origin");
  if (origin && origin !== siteOrigin(request)) return new Response("Forbidden", { status: 403 });
  return new Response(null, {
    status: 303,
    headers: { location: `${siteOrigin(request)}/`, "set-cookie": cookie(SESSION_COOKIE, "", 0, request), "cache-control": "no-store" },
  });
}
