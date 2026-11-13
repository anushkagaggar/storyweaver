import { describe, expect, it } from "vitest";

import { BodyTooLargeError, readCapped } from "@/lib/read-capped";

function post(body: BodyInit | null, headers: Record<string, string> = {}): Request {
  return new Request("http://localhost/x", { method: "POST", body, headers, duplex: "half" } as RequestInit);
}

describe("readCapped", () => {
  it("returns the whole body when it fits", async () => {
    const data = await readCapped(post(new Uint8Array([1, 2, 3, 4])), 10);
    expect([...data]).toEqual([1, 2, 3, 4]);
  });

  it("returns an empty array for no body", async () => {
    expect((await readCapped(post(null), 10)).byteLength).toBe(0);
  });

  it("rejects a declared Content-Length over the cap without reading", async () => {
    await expect(readCapped(post(new Uint8Array(5), { "content-length": "999" }), 10)).rejects.toBeInstanceOf(BodyTooLargeError);
  });

  it("stops reading a stream that grows past the cap even without a Content-Length", async () => {
    let pulled = 0;
    const stream = new ReadableStream<Uint8Array>({
      pull(controller) {
        pulled += 1;
        controller.enqueue(new Uint8Array(4));
        if (pulled > 100) controller.close();
      },
    });
    await expect(readCapped(post(stream), 10)).rejects.toBeInstanceOf(BodyTooLargeError);
    expect(pulled).toBeLessThan(10);
  });
});
