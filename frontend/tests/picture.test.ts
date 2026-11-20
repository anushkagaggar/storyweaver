import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { POST } from "@/app/api/pictures/route";
import {
  autoRetryDelaySeconds,
  FORM_COPY,
  problemMessage,
  summaryLabel,
  validatePicturePrompt,
} from "@/lib/story-logic";
import { MAX_PICTURE_PROMPT_LENGTH, type StorySummary } from "@/lib/story-types";

import { signedIn, stubAuthEnv } from "./signed-in";

const ROW: StorySummary = { id: "x", kind: "picture", status: "ready", title: null, created_at: "2026-11-20T10:00:00Z" };

describe("picture form logic", () => {
  it("has its own words", () => {
    expect(FORM_COPY.picture.label).toBe("What should we draw?");
    expect(FORM_COPY.picture.submit).toBe("Draw it");
    expect(FORM_COPY.story.submit).toBe("Make my story");
  });

  it("validates what to draw like the API", () => {
    expect(validatePicturePrompt("  ")).toMatch(/what to draw/);
    expect(validatePicturePrompt("a purple dragon")).toBeNull();
    expect(validatePicturePrompt("x".repeat(MAX_PICTURE_PROMPT_LENGTH + 1))).toMatch(/under 300/);
  });

  it("labels pictures in My stories", () => {
    expect(summaryLabel({ ...ROW, title: "A purple dragon" })).toBe("A purple dragon");
    expect(summaryLabel({ ...ROW, status: "queued" })).toBe("A picture being drawn…");
    expect(summaryLabel({ ...ROW, status: "failed" })).toBe("A picture that couldn't be finished");
    expect(summaryLabel({ ...ROW, kind: "story", status: "planning" })).toBe("A story being made…");
  });

  it("shows the API's words for nothing to draw, and never auto-retries when pictures are off", () => {
    const unclear = { status: 422, code: "nothing_to_draw", detail: 'Please say what to draw, for example "a purple dragon".' };
    expect(problemMessage(unclear, null)).toBe(unclear.detail);
    expect(problemMessage({ status: 503, code: "pictures_unavailable" }, 300)).toMatch(/Pictures aren't available/);
    expect(autoRetryDelaySeconds(503, 300, 0, "pictures_unavailable")).toBeNull();
    expect(autoRetryDelaySeconds(503, 5, 0, "safety_check_unavailable")).toBe(6);
  });
});

describe("POST /api/pictures", () => {
  beforeEach(stubAuthEnv);
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  function post(body: unknown, headers: Record<string, string> = {}): Promise<Response> {
    return POST(
      signedIn("http://localhost/api/pictures", {
        method: "POST",
        headers: { "content-type": "application/json", ...headers },
        body: JSON.stringify(body),
      }),
    );
  }

  it("forwards only the prompt, as the signed-in parent, with the Idempotency-Key", async () => {
    const upstream = vi.fn().mockResolvedValue(Response.json({ story_id: "s", job_id: "j", status: "ready" }, { status: 202 }));
    vi.stubGlobal("fetch", upstream);

    const response = await post({ prompt: "draw a purple dragon", extra: "dropped" }, { "Idempotency-Key": "draw-key-0001" });

    expect(response.status).toBe(202);
    const [url, init] = upstream.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("https://backend.example/v1/pictures");
    expect(JSON.parse(init.body as string)).toEqual({ prompt: "draw a purple dragon" });
    const headers = init.headers as Record<string, string>;
    expect(headers["X-API-Key"]).toBe("server-side-key");
    expect(headers["X-User-Token"]).toBeTruthy();
    expect(headers["Idempotency-Key"]).toBe("draw-key-0001");
  });

  it("refuses a parent who isn't signed in, and a body without a prompt, without calling the backend", async () => {
    const upstream = vi.fn();
    vi.stubGlobal("fetch", upstream);
    const anonymous = await POST(
      new Request("http://localhost/api/pictures", { method: "POST", body: JSON.stringify({ prompt: "a cat" }) }),
    );
    expect(anonymous.status).toBe(401);
    expect((await post({ prompt: 42 })).status).toBe(400);
    expect((await post({ prompt: "x".repeat(MAX_PICTURE_PROMPT_LENGTH * 2 + 1) })).status).toBe(400);
    expect(upstream).not.toHaveBeenCalled();
  });
});
