import { describe, expect, it } from "vitest";

import {
  FINAL_STATUSES,
  parseRetryAfter,
  pollDelayMs,
  problemMessage,
  statusHint,
  statusMessage,
  UUID_PATTERN,
  validatePrompt,
} from "@/lib/story-logic";

describe("pollDelayMs", () => {
  it("starts at 1.5 s and backs off to at most 8 s", () => {
    expect(pollDelayMs(0)).toBe(1500);
    expect(pollDelayMs(1)).toBe(1875);
    expect(pollDelayMs(5)).toBeGreaterThan(pollDelayMs(4));
    expect(pollDelayMs(50)).toBe(8000);
    expect(pollDelayMs(-3)).toBe(1500);
  });
});

describe("statusMessage", () => {
  it("has a friendly message for every status", () => {
    for (const status of ["queued", "planning", "checking", "ready", "failed"] as const) {
      expect(statusMessage(status).length).toBeGreaterThan(5);
    }
    expect(FINAL_STATUSES.has("ready")).toBe(true);
    expect(FINAL_STATUSES.has("checking")).toBe(false);
  });
});

describe("statusHint", () => {
  it("explains the safety-check wait only while checking", () => {
    expect(statusHint("checking")).toMatch(/safety check.*up to a minute/);
    expect(statusHint("planning")).toMatch(/under a minute/);
  });
});

describe("validatePrompt", () => {
  it("accepts a normal idea and rejects empty or too long ones", () => {
    expect(validatePrompt("a dragon who is scared of the dark")).toBeNull();
    expect(validatePrompt("   ")).toMatch(/a little more/);
    expect(validatePrompt("x".repeat(501))).toMatch(/under 500/);
  });
});

describe("problemMessage", () => {
  it("shows the safety message for unsafe ideas", () => {
    const message = problemMessage(
      { status: 422, code: "unsafe_input", detail: "This story idea can't be used. Try a gentle, kind idea." },
      null,
    );
    expect(message).toBe("This story idea can't be used. Try a gentle, kind idea.");
  });

  it("tells the parent how long to wait when busy", () => {
    expect(problemMessage({ status: 429 }, 37)).toBe("The storyteller is busy right now. Please try again in 37 seconds.");
    expect(problemMessage({ status: 503 }, null)).toMatch(/paused for a moment/);
    expect(problemMessage({ status: 404 }, null)).toBe("We couldn't find that story.");
    expect(problemMessage({ status: 500 }, null)).toMatch(/Something went wrong/);
  });
});

describe("parseRetryAfter and UUID_PATTERN", () => {
  it("parses seconds and validates story ids", () => {
    expect(parseRetryAfter("12")).toBe(12);
    expect(parseRetryAfter("1.2")).toBe(2);
    expect(parseRetryAfter(null)).toBeNull();
    expect(parseRetryAfter("soon")).toBeNull();
    expect(UUID_PATTERN.test("56983979-3898-4355-95c3-29144bfdc9d3")).toBe(true);
    expect(UUID_PATTERN.test("../../v1/jobs")).toBe(false);
  });
});
