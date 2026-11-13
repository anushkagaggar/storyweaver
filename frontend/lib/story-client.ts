// Browser-side calls to our own /api routes (never the backend directly; the API key stays on the server).

import { parseRetryAfter, problemMessage } from "@/lib/story-logic";
import type { CreateStoryResponse, Problem, SceneChoice, Story } from "@/lib/story-types";

export type Failure = { ok: false; message: string; status: number; retryAfter: number | null };
export type Result<T> = { ok: true; value: T } | Failure;

async function failure(response: Response): Promise<Failure> {
  let problem: Problem = { status: response.status };
  try {
    problem = { ...((await response.json()) as Problem), status: response.status };
  } catch {
    // Non-JSON error body: fall back to the status code.
  }
  const retryAfter = parseRetryAfter(response.headers.get("retry-after"));
  return { ok: false, message: problemMessage(problem, retryAfter), status: response.status, retryAfter };
}

export async function createStory(
  prompt: string,
  numScenes: SceneChoice,
  idempotencyKey: string,
): Promise<Result<CreateStoryResponse>> {
  try {
    const response = await fetch("/api/stories", {
      method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": idempotencyKey },
      body: JSON.stringify({ prompt, num_scenes: numScenes }),
    });
    if (!response.ok) return failure(response);
    return { ok: true, value: (await response.json()) as CreateStoryResponse };
  } catch {
    return { ok: false, message: "We couldn't reach the storyteller. Check your connection and try again.", status: 0, retryAfter: null };
  }
}

export async function fetchStory(id: string): Promise<Result<Story>> {
  try {
    const response = await fetch(`/api/stories/${encodeURIComponent(id)}`, { cache: "no-store" });
    if (!response.ok) return failure(response);
    return { ok: true, value: (await response.json()) as Story };
  } catch {
    return { ok: false, message: "Reconnecting…", status: 0, retryAfter: null };
  }
}
