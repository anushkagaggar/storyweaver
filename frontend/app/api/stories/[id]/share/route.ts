import { userHeaders } from "@/lib/auth";
import { forwardToBackend } from "@/lib/backend";
import { UUID_PATTERN } from "@/lib/story-logic";

export const dynamic = "force-dynamic";

/** POST /api/stories/{id}/share -> a 15-minute read-only link (backend auth/share_links.py). Owner only. */
export async function POST(request: Request, { params }: { params: Promise<{ id: string }> }): Promise<Response> {
  const { id } = await params;
  if (!UUID_PATTERN.test(id)) {
    return Response.json(
      { type: "about:blank", title: "Not Found", status: 404, detail: "Story not found." },
      { status: 404, headers: { "content-type": "application/problem+json" } },
    );
  }
  const user = userHeaders(request);
  if ("response" in user) return user.response;
  return forwardToBackend(`/v1/stories/${id}/share`, { method: "POST", headers: user.headers, timeoutMs: 15_000 });
}
