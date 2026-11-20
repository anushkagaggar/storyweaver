import { userHeaders } from "@/lib/auth";
import { forwardToBackend } from "@/lib/backend";
import { MAX_PICTURE_PROMPT_LENGTH } from "@/lib/story-types";

export const dynamic = "force-dynamic";

const IDEMPOTENCY_KEY = /^[A-Za-z0-9._:-]{8,128}$/;

function badRequest(detail: string): Response {
  return Response.json(
    { type: "about:blank", title: "Bad Request", status: 400, detail },
    { status: 400, headers: { "content-type": "application/problem+json" } },
  );
}

/**
 * POST /api/pictures -> backend POST /v1/pictures, as the signed-in parent: one picture from words like
 * "draw a purple dragon" (typed, or said and transcribed). It is read like a story, at /api/stories/{id}.
 */
export async function POST(request: Request): Promise<Response> {
  const user = userHeaders(request);
  if ("response" in user) return user.response;
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return badRequest("The request body must be JSON.");
  }
  const { prompt } = (body ?? {}) as { prompt?: unknown };
  if (typeof prompt !== "string" || prompt.length > MAX_PICTURE_PROMPT_LENGTH * 2) {
    return badRequest("Tell us what to draw.");
  }

  const key = request.headers.get("idempotency-key");
  const headers: Record<string, string> = { ...user.headers };
  if (key && IDEMPOTENCY_KEY.test(key)) headers["Idempotency-Key"] = key;

  return forwardToBackend("/v1/pictures", { method: "POST", body: JSON.stringify({ prompt }), headers });
}
