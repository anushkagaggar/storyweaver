import { forwardToBackend } from "@/lib/backend";
import { SHARE_TOKEN_PATTERN } from "@/lib/story-logic";

export const dynamic = "force-dynamic";

/**
 * GET /api/shared/{token} -> backend GET /v1/shared/{token}: a story through its owner's share link. Needs no
 * sign-in; the link stops working 15 minutes after the owner made it, however often it is opened or forwarded.
 */
export async function GET(_request: Request, { params }: { params: Promise<{ token: string }> }): Promise<Response> {
  const { token } = await params;
  if (!SHARE_TOKEN_PATTERN.test(token)) {
    return Response.json(
      { type: "about:blank", title: "Not Found", status: 404, detail: "This link has expired or the story was deleted. Ask for a new link." },
      { status: 404, headers: { "content-type": "application/problem+json" } },
    );
  }
  return forwardToBackend(`/v1/shared/${token}`, { timeoutMs: 15_000 });
}
