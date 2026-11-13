import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DELETE } from "@/app/api/stories/[id]/route";
import { deleteMessage } from "@/lib/story-logic";

const ID = "4b1e6b0e-2b55-4e0b-9a3c-1f0e2d3c4b5a";

function call(id: string): Promise<Response> {
  return DELETE(new Request(`http://localhost/api/stories/${id}`, { method: "DELETE" }), {
    params: Promise.resolve({ id }),
  });
}

describe("DELETE /api/stories/[id]", () => {
  beforeEach(() => {
    vi.stubEnv("STORYWEAVER_API_URL", "https://backend.example");
    vi.stubEnv("STORYWEAVER_API_KEY", "server-side-key");
  });
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  it("forwards to the backend with the API key and passes 204 through without a body", async () => {
    const upstream = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", upstream);

    const response = await call(ID);

    expect(response.status).toBe(204);
    expect(await response.text()).toBe("");
    const [url, init] = upstream.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`https://backend.example/v1/stories/${ID}`);
    expect(init.method).toBe("DELETE");
    expect((init.headers as Record<string, string>)["X-API-Key"]).toBe("server-side-key");
  });

  it("answers 404 for an id that isn't a UUID, without calling the backend", async () => {
    const upstream = vi.fn();
    vi.stubGlobal("fetch", upstream);
    expect((await call("../../v1/admin")).status).toBe(404);
    expect(upstream).not.toHaveBeenCalled();
  });

  it("passes a 409 (story still being made) through with Retry-After", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        Response.json(
          { status: 409, code: "story_in_progress", detail: "This story is still being made." },
          { status: 409, headers: { "content-type": "application/problem+json", "retry-after": "30" } },
        ),
      ),
    );
    const response = await call(ID);
    expect(response.status).toBe(409);
    expect(response.headers.get("retry-after")).toBe("30");
  });
});

describe("deleteMessage", () => {
  it("uses the backend's sentence for 409 and 503, and a generic one otherwise", () => {
    expect(deleteMessage({ status: 409, detail: "Still being made." })).toBe("Still being made.");
    expect(deleteMessage({ status: 503, detail: "Audio couldn't be removed yet." })).toBe("Audio couldn't be removed yet.");
    expect(deleteMessage({ status: 500, detail: "boom" })).toMatch(/couldn't delete/);
  });
});
