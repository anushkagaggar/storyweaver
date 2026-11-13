// Pure helpers for the story UI (no React, no fetch) so they can be unit-tested.

import {
  MAX_PROMPT_LENGTH,
  MIN_PROMPT_LENGTH,
  type Problem,
  type Scene,
  type Story,
  type StoryStatus,
} from "@/lib/story-types";

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

/** True while any page's narration is still being made, so the viewer keeps polling after the text appears. */
export function hasPendingAudio(story: Story): boolean {
  return story.status === "ready" && story.scenes.some((scene) => scene.audio_status === "pending");
}

/**
 * Each poll returns freshly signed audio URLs. Swapping the URL of a page that is already playing would restart
 * it, so a page that was ready keeps the URL it had.
 */
export function mergeAudioUrls(previous: Story | null, next: Story): Story {
  if (!previous) return next;
  const known = new Map<number, string>();
  for (const scene of previous.scenes) {
    if (scene.audio_status === "ready" && scene.audio_url) known.set(scene.idx, scene.audio_url);
  }
  return {
    ...next,
    scenes: next.scenes.map((scene: Scene) => {
      const url = known.get(scene.idx);
      return scene.audio_status === "ready" && url ? { ...scene, audio_url: url } : scene;
    }),
  };
}

/** What to show in place of the player for a page without playable audio. Null when there's nothing to say. */
export function audioNote(scene: Scene): string | null {
  if (scene.audio_status === "pending") return "Getting the reading voice ready…";
  if (scene.audio_status === "failed") return "Sorry, we couldn't make the reading voice for this page.";
  return null;
}

/** Recorder formats in order of preference: Chrome/Firefox record webm or ogg (Opus), Safari records mp4 (AAC). */
export const RECORDER_MIME_TYPES = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/mp4"] as const;

/** The first format the browser can record, or undefined to let it choose its default. */
export function pickRecorderMimeType(isSupported: (type: string) => boolean): string | undefined {
  return RECORDER_MIME_TYPES.find((type) => isSupported(type));
}

/** Why the microphone couldn't start, as a sentence a parent can act on. `name` is the DOMException name. */
export function microphoneMessage(name: string | undefined): string {
  switch (name) {
    case "NotAllowedError":
    case "SecurityError":
      return "We need your permission to use the microphone. Allow it in your browser, or type your idea instead.";
    case "NotFoundError":
    case "OverconstrainedError":
      return "We couldn't find a microphone. Please type your idea instead.";
    case "NotReadableError":
    case "AbortError":
      return "The microphone is busy in another app. Close it and try again, or type your idea.";
    default:
      return "The microphone isn't working. Please type your idea instead.";
  }
}

/** The backend's voice errors already carry a parent-facing sentence in `detail`; fall back by status. */
export function transcribeMessage(problem: Problem): string {
  if (problem.detail && problem.status !== 502 && problem.status !== 504) return problem.detail;
  if (problem.status === 413) return "That recording is too big. Please keep it short.";
  return "Voice input isn't working right now. Please type your idea instead.";
}

/** m:ss for the recorder's timer. */
export function formatClock(seconds: number): string {
  const whole = Math.max(0, Math.floor(seconds));
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
}
