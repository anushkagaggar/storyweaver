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

/** GET /api/stories/{id} -> backend GET /v1/stories/{id}. The id is checked so nothing else is forwarded. */
export async function GET(_request: Request, { params }: Params): Promise<Response> {
  const { id } = await params;
  if (!UUID_PATTERN.test(id)) return notFound();
  return forwardToBackend(`/v1/stories/${id}`, { timeoutMs: 15_000 });
}

/**
 * DELETE /api/stories/{id} -> backend DELETE /v1/stories/{id}: the story, its pages and its audio.
 * As with reading, the story's link is the only credential (there are no accounts; docs/dpia.md).
 */
export async function DELETE(_request: Request, { params }: Params): Promise<Response> {
  const { id } = await params;
  if (!UUID_PATTERN.test(id)) return notFound();
  return forwardToBackend(`/v1/stories/${id}`, { method: "DELETE", timeoutMs: 30_000 });
}
