import { googleConfig, safeNext, startSignIn } from "@/lib/auth";

export const dynamic = "force-dynamic";

/** GET /api/auth/signin?next=/story/... -> Google's sign-in page (lib/auth.ts). */
export async function GET(request: Request): Promise<Response> {
  const config = googleConfig();
  if (!config) {
    return new Response("Sign-in isn't set up on this server yet.", { status: 503, headers: { "content-type": "text/plain" } });
  }
  const next = safeNext(new URL(request.url).searchParams.get("next"));
  const { url, cookie } = startSignIn(request, config, next);
  return new Response(null, { status: 302, headers: { location: url, "set-cookie": cookie, "cache-control": "no-store" } });
}
