// Browser-side calls to our own /api routes (never the backend directly; the API key stays on the server).

import { deleteMessage, parseRetryAfter, problemMessage, transcribeMessage } from "@/lib/story-logic";
import type {
  CreateStoryResponse,
  Problem,
  SceneChoice,
  ShareLink,
  SharedStory,
  Story,
  StorySummary,
  Transcript,
} from "@/lib/story-types";

export type Failure = { ok: false; message: string; status: number; retryAfter: number | null; code?: string };
export type Result<T> = { ok: true; value: T } | Failure;

async function failure(response: Response): Promise<Failure> {
  let problem: Problem = { status: response.status };
  try {
    problem = { ...((await response.json()) as Problem), status: response.status };
  } catch {
    // Non-JSON error body: fall back to the status code.
  }
  const retryAfter = parseRetryAfter(response.headers.get("retry-after"));
  return { ok: false, message: problemMessage(problem, retryAfter), status: response.status, retryAfter, code: problem.code };
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

/** One picture from words like "draw a purple dragon"; read it like a story (fetchStory). */
export async function createPicture(prompt: string, idempotencyKey: string): Promise<Result<CreateStoryResponse>> {
  try {
    const response = await fetch("/api/pictures", {
      method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": idempotencyKey },
      body: JSON.stringify({ prompt }),
    });
    if (!response.ok) return failure(response);
    return { ok: true, value: (await response.json()) as CreateStoryResponse };
  } catch {
    return { ok: false, message: "We couldn't reach the storyteller. Check your connection and try again.", status: 0, retryAfter: null };
  }
}

async function getJson<T>(path: string, reconnecting = "Reconnecting…"): Promise<Result<T>> {
  try {
    const response = await fetch(path, { cache: "no-store" });
    if (!response.ok) return failure(response);
    return { ok: true, value: (await response.json()) as T };
  } catch {
    return { ok: false, message: reconnecting, status: 0, retryAfter: null };
  }
}

export function fetchStory(id: string): Promise<Result<Story>> {
  return getJson<Story>(`/api/stories/${encodeURIComponent(id)}`);
}

/** A story through its owner's share link (no sign-in). 404 once the link has expired. */
export function fetchSharedStory(token: string): Promise<Result<SharedStory>> {
  return getJson<SharedStory>(`/api/shared/${encodeURIComponent(token)}`);
}

/** The signed-in parent's stories, newest first. */
export async function listStories(): Promise<Result<StorySummary[]>> {
  const result = await getJson<{ stories: StorySummary[] }>("/api/stories", "We couldn't load your stories.");
  return result.ok ? { ok: true, value: result.value.stories } : result;
}

/** Make a new 15-minute read-only link to one of your stories. */
export async function shareStory(id: string): Promise<Result<ShareLink>> {
  try {
    const response = await fetch(`/api/stories/${encodeURIComponent(id)}/share`, { method: "POST" });
    if (!response.ok) return failure(response);
    return { ok: true, value: (await response.json()) as ShareLink };
  } catch {
    return { ok: false, message: "We couldn't reach the storyteller. Check your connection and try again.", status: 0, retryAfter: null };
  }
}

/** Send a recording for transcription. The audio goes to our own /api route, which holds the API key. */
export async function transcribeAudio(recording: Blob): Promise<Result<Transcript>> {
  try {
    const response = await fetch("/api/speech/transcribe", {
      method: "POST",
      headers: { "Content-Type": recording.type || "audio/webm" },
      body: recording,
    });
    if (!response.ok) {
      let problem: Problem = { status: response.status };
      try {
        problem = { ...((await response.json()) as Problem), status: response.status };
      } catch {
        // Non-JSON error body: fall back to the status code.
      }
      return { ok: false, message: transcribeMessage(problem), status: response.status, retryAfter: parseRetryAfter(response.headers.get("retry-after")) };
    }
    return { ok: true, value: (await response.json()) as Transcript };
  } catch {
    return { ok: false, message: "We couldn't reach the storyteller. Check your connection and try again.", status: 0, retryAfter: null };
  }
}

/** Delete a story for good. A 404 counts as success: the story is gone either way. */
export async function deleteStory(id: string): Promise<Result<null>> {
  try {
    const response = await fetch(`/api/stories/${encodeURIComponent(id)}`, { method: "DELETE" });
    if (response.ok || response.status === 404) return { ok: true, value: null };
    let problem: Problem = { status: response.status };
    try {
      problem = { ...((await response.json()) as Problem), status: response.status };
    } catch {
      // Non-JSON error body: fall back to the status code.
    }
    const retryAfter = parseRetryAfter(response.headers.get("retry-after"));
    return { ok: false, message: deleteMessage(problem), status: response.status, retryAfter };
  } catch {
    return { ok: false, message: "We couldn't reach the storyteller. Check your connection and try again.", status: 0, retryAfter: null };
  }
}
