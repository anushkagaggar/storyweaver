import { forwardToBackend } from "@/lib/backend";
import { UUID_PATTERN } from "@/lib/story-logic";

export const dynamic = "force-dynamic";

/** GET /api/stories/{id} -> backend GET /v1/stories/{id}. The id is checked so nothing else is forwarded. */
export async function GET(_request: Request, { params }: { params: Promise<{ id: string }> }): Promise<Response> {
  const { id } = await params;
  if (!UUID_PATTERN.test(id)) {
    return Response.json(
      { type: "about:blank", title: "Not Found", status: 404, detail: "Story not found." },
      { status: 404, headers: { "content-type": "application/problem+json" } },
    );
  }
  return forwardToBackend(`/v1/stories/${id}`, { timeoutMs: 15_000 });
}
