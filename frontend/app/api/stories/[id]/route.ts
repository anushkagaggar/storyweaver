import { userHeaders } from "@/lib/auth";
import { forwardToBackend } from "@/lib/backend";
import { UUID_PATTERN } from "@/lib/story-logic";

export const dynamic = "force-dynamic";

type Params = { params: Promise<{ id: string }> };

function notFound(): Response {
  return Response.json(
    { type: "about:blank", title: "Not Found", status: 404, detail: "Story not found." },
    { status: 404, headers: { "content-type": "application/problem+json" } },
  );
}

/** GET /api/stories/{id} -> backend GET /v1/stories/{id}. Only the owner gets it; anyone else gets 404. */
export async function GET(request: Request, { params }: Params): Promise<Response> {
  const { id } = await params;
  if (!UUID_PATTERN.test(id)) return notFound();
  const user = userHeaders(request);
  if ("response" in user) return user.response;
  return forwardToBackend(`/v1/stories/${id}`, { headers: user.headers, timeoutMs: 15_000 });
}

/** DELETE /api/stories/{id} -> backend DELETE /v1/stories/{id}: the story, its pages, pictures and audio. Owner only. */
export async function DELETE(request: Request, { params }: Params): Promise<Response> {
  const { id } = await params;
  if (!UUID_PATTERN.test(id)) return notFound();
  const user = userHeaders(request);
  if ("response" in user) return user.response;
  return forwardToBackend(`/v1/stories/${id}`, { method: "DELETE", headers: user.headers, timeoutMs: 30_000 });
}
