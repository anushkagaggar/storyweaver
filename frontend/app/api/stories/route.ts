import { userHeaders } from "@/lib/auth";
import { forwardToBackend } from "@/lib/backend";
import { MAX_PROMPT_LENGTH, SCENE_CHOICES } from "@/lib/story-types";

export const dynamic = "force-dynamic";

const IDEMPOTENCY_KEY = /^[A-Za-z0-9._:-]{8,128}$/;

function badRequest(detail: string): Response {
  return Response.json(
    { type: "about:blank", title: "Bad Request", status: 400, detail },
    { status: 400, headers: { "content-type": "application/problem+json" } },
  );
}

/** GET /api/stories -> backend GET /v1/stories: the signed-in parent's own stories, newest first. */
export async function GET(request: Request): Promise<Response> {
  const user = userHeaders(request);
  if ("response" in user) return user.response;
  return forwardToBackend("/v1/stories?limit=20", { headers: user.headers, timeoutMs: 15_000 });
}

/** POST /api/stories -> backend POST /v1/stories, as the signed-in parent. Only the fields the API accepts go. */
export async function POST(request: Request): Promise<Response> {
  const user = userHeaders(request);
  if ("response" in user) return user.response;
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return badRequest("The request body must be JSON.");
  }
  const { prompt, num_scenes } = (body ?? {}) as { prompt?: unknown; num_scenes?: unknown };
  if (typeof prompt !== "string" || prompt.length > MAX_PROMPT_LENGTH * 2) {
    return badRequest("A story idea is required.");
  }
  const scenes = SCENE_CHOICES.find((n) => n === num_scenes) ?? SCENE_CHOICES[0];

  const key = request.headers.get("idempotency-key");
  const headers: Record<string, string> = { ...user.headers };
  if (key && IDEMPOTENCY_KEY.test(key)) headers["Idempotency-Key"] = key;

  return forwardToBackend("/v1/stories", {
    method: "POST",
    body: JSON.stringify({ prompt, num_scenes: scenes }),
    headers,
  });
}
