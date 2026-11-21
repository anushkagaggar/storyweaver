import { describe, expect, it } from "vitest";

import {
  autoRetryDelaySeconds,
  busyCountdownMessage,
  FINAL_STATUSES,
  MAX_AUTO_RETRIES,
  parseRetryAfter,
  pollDelayMs,
  problemMessage,
  statusHint,
  statusMessage,
  UUID_PATTERN,
  validatePrompt,
  waitPhrase,
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

  it("says a used-up day in words, not in seconds", () => {
    expect(problemMessage({ status: 429 }, 300)).toBe("The storyteller is busy right now. Please try again in about 5 minutes.");
    expect(problemMessage({ status: 429 }, 50_400)).toBe("The storyteller is busy right now. Please try again in about 14 hours.");
    const detail = "We've made all the stories we can for today. Please come back tomorrow.";
    expect(problemMessage({ status: 429, code: "safety_check_unavailable", detail }, 50_400)).toBe(detail);
    expect(waitPhrase(37)).toBe("37 seconds");
    expect(waitPhrase(3600)).toBe("about 1 hour");
  });

  it("shows the backend's own words for the per-visitor limit", () => {
    const detail = "That's all the stories for today. Please come back tomorrow for more.";
    expect(problemMessage({ status: 429, code: "visitor_limit", detail }, 3600)).toBe(detail);
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

describe("autoRetryDelaySeconds", () => {
  it("waits Retry-After plus a second for busy answers", () => {
    expect(autoRetryDelaySeconds(429, 37, 0)).toBe(38);
    expect(autoRetryDelaySeconds(503, 30, 1)).toBe(31);
  });

  it("falls back to a default wait and caps long waits", () => {
    expect(autoRetryDelaySeconds(429, null, 0)).toBe(20);
    expect(autoRetryDelaySeconds(429, 0, 0)).toBe(20);
    expect(autoRetryDelaySeconds(429, 600, 0)).toBe(90);
  });

  it("never retries refused, invalid or failed requests", () => {
    for (const status of [0, 400, 404, 422, 500, 502, 504]) {
      expect(autoRetryDelaySeconds(status, 10, 0)).toBeNull();
    }
  });

  it("gives up after the retry limit", () => {
    expect(autoRetryDelaySeconds(429, 10, MAX_AUTO_RETRIES - 1)).toBe(11);
    expect(autoRetryDelaySeconds(429, 10, MAX_AUTO_RETRIES)).toBeNull();
  });

  it("never retries a quota that is used up for today", () => {
    expect(autoRetryDelaySeconds(429, 3600, 0)).toBeNull();
    expect(autoRetryDelaySeconds(429, 50_400, 0, "safety_check_unavailable")).toBeNull();
  });

  it("never retries a visitor's own story limit, which is an hour or a day away", () => {
    expect(autoRetryDelaySeconds(429, 1800, 0, "visitor_limit")).toBeNull();
  });
});

describe("busyCountdownMessage", () => {
  it("counts down in words a parent understands", () => {
    expect(busyCountdownMessage(12)).toMatch(/try again for you in 12 seconds/);
    expect(busyCountdownMessage(1)).toMatch(/in 1 second…$/);
  });
});
