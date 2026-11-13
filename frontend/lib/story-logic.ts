// Pure helpers for the story UI (no React, no fetch) so they can be unit-tested.

import { MAX_PROMPT_LENGTH, MIN_PROMPT_LENGTH, type Problem, type StoryStatus } from "@/lib/story-types";

/** Plan §6.4: poll every 1.5 s, backing off gently, never slower than every 8 s. */
export function pollDelayMs(attempt: number): number {
  return Math.round(Math.min(1500 * 1.25 ** Math.max(0, attempt), 8000));
}

export const FINAL_STATUSES: ReadonlySet<StoryStatus> = new Set(["ready", "failed"]);

export function statusMessage(status: StoryStatus): string {
  switch (status) {
    case "queued":
      return "Waiting for the storyteller…";
    case "planning":
      return "Writing your story…";
    case "checking":
      return "Making sure it's just right for little ears…";
    case "ready":
      return "Your story is ready!";
    case "failed":
      return "This story couldn't be finished.";
  }
}

/** The smaller line under the status heading: what to expect while waiting. */
export function statusHint(status: StoryStatus): string {
  if (status === "checking") {
    // The free-tier safety model allows only a few checks a minute, so this step can wait up to about a minute.
    return "Every story gets a safety check before you see it. When lots of stories are being made, this can take up to a minute.";
  }
  return "This usually takes under a minute, sometimes two when it's busy.";
}

/** Client-side check that mirrors the API, so obvious mistakes never leave the browser. */
export function validatePrompt(raw: string): string | null {
  const prompt = raw.trim().replace(/\s+/g, " ");
  if (prompt.length < MIN_PROMPT_LENGTH) return "Tell us a little more about your story idea.";
  if (prompt.length > MAX_PROMPT_LENGTH) return `Please keep your idea under ${MAX_PROMPT_LENGTH} characters.`;
  return null;
}

/** Turn an API problem response into a sentence a parent can act on. */
export function problemMessage(problem: Problem, retryAfterSeconds: number | null): string {
  const wait = retryAfterSeconds && retryAfterSeconds > 0 ? ` Please try again in ${retryAfterSeconds} seconds.` : "";
  if (problem.code === "unsafe_input") {
    return problem.detail ?? "This story idea can't be used. Try a gentle, kind idea.";
  }
  switch (problem.status) {
    case 422:
      return "Please check your story idea and try again.";
    case 429:
      return `The storyteller is busy right now.${wait || " Please try again shortly."}`;
    case 503:
      return `Story making is paused for a moment.${wait || " Please try again shortly."}`;
    case 404:
      return "We couldn't find that story.";
    default:
      return "Something went wrong. Please try again.";
  }
}

/** How many times the form resubmits a busy (429/503) idea on its own before handing back to the parent. */
export const MAX_AUTO_RETRIES = 3;
const DEFAULT_BUSY_WAIT_SECONDS = 20;
const MAX_BUSY_WAIT_SECONDS = 90;

/**
 * Seconds to wait before resubmitting automatically, or null when the parent should decide.
 * Only "busy" answers qualify (429 quota, 503 safety check briefly unavailable); a refused or invalid idea never
 * does. One second is added to Retry-After so the per-minute window has really moved on.
 */
export function autoRetryDelaySeconds(status: number, retryAfterSeconds: number | null, retriesSoFar: number): number | null {
  if (status !== 429 && status !== 503) return null;
  if (retriesSoFar >= MAX_AUTO_RETRIES) return null;
  const wait = retryAfterSeconds && retryAfterSeconds > 0 ? retryAfterSeconds + 1 : DEFAULT_BUSY_WAIT_SECONDS;
  return Math.min(wait, MAX_BUSY_WAIT_SECONDS);
}

export function busyCountdownMessage(secondsLeft: number): string {
  const unit = secondsLeft === 1 ? "second" : "seconds";
  return `Lots of stories are being made right now. We'll try again for you in ${secondsLeft} ${unit}…`;
}

/** Seconds from a Retry-After header (we only send delta-seconds). */
export function parseRetryAfter(value: string | null): number | null {
  if (!value) return null;
  const seconds = Number(value);
  return Number.isFinite(seconds) && seconds >= 0 ? Math.ceil(seconds) : null;
}

export const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
