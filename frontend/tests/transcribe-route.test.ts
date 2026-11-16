import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { POST } from "@/app/api/speech/transcribe/route";
import { MAX_AUDIO_BYTES } from "@/lib/story-types";

import { signedIn, stubAuthEnv } from "./signed-in";

function audio(bytes: number, type = "audio/webm;codecs=opus"): Request {
  return signedIn("http://localhost/api/speech/transcribe", {
    method: "POST",
    headers: { "content-type": type },
    body: new Uint8Array(bytes),
  });
}

describe("POST /api/speech/transcribe", () => {
  beforeEach(stubAuthEnv);

  it("is for signed-in parents only", async () => {
    const upstream = vi.fn();
    vi.stubGlobal("fetch", upstream);
    const anonymous = new Request("http://localhost/api/speech/transcribe", {
      method: "POST",
      headers: { "content-type": "audio/webm" },
      body: new Uint8Array(10),
    });
    expect((await POST(anonymous)).status).toBe(401);
    expect(upstream).not.toHaveBeenCalled();
  });
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  it("forwards the raw recording with the API key and its original content type", async () => {
    const upstream = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ text: "a sleepy dragon" }), { status: 200, headers: { "content-type": "application/json" } }),
    );
    vi.stubGlobal("fetch", upstream);

    const response = await POST(audio(2048));

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ text: "a sleepy dragon" });
    const [url, init] = upstream.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("https://backend.example/v1/speech/transcribe");
    const headers = init.headers as Record<string, string>;
    expect(headers["X-API-Key"]).toBe("server-side-key");
    expect(headers["Content-Type"]).toBe("audio/webm;codecs=opus");
    expect((init.body as Uint8Array).byteLength).toBe(2048);
  });

  it("refuses non-audio bodies without calling the backend", async () => {
    const upstream = vi.fn();
    vi.stubGlobal("fetch", upstream);
    const response = await POST(audio(10, "application/json"));
    expect(response.status).toBe(415);
    expect(upstream).not.toHaveBeenCalled();
  });

  it("refuses a recording over the cap with a friendly 413, without calling the backend", async () => {
    const upstream = vi.fn();
    vi.stubGlobal("fetch", upstream);
    const response = await POST(audio(MAX_AUDIO_BYTES + 1));
    expect(response.status).toBe(413);
    expect((await response.json()).detail).toMatch(/too long/);
    expect(upstream).not.toHaveBeenCalled();
  });

  it("passes the backend's voice errors through", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ code: "audio_silent", detail: "We couldn't hear anything." }), {
          status: 422,
          headers: { "content-type": "application/problem+json" },
        }),
      ),
    );
    const response = await POST(audio(100));
    expect(response.status).toBe(422);
    expect((await response.json()).code).toBe("audio_silent");
  });
});
