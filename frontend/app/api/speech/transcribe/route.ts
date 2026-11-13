import { forwardToBackend } from "@/lib/backend";
import { BodyTooLargeError, readCapped } from "@/lib/read-capped";
import { MAX_AUDIO_BYTES } from "@/lib/story-types";

export const dynamic = "force-dynamic";
// Whisper is fast, but the local fallback may load its model first.
export const maxDuration = 60;

function problem(status: number, title: string, detail: string, code: string): Response {
  return Response.json(
    { type: "about:blank", title, status, detail, code },
    { status, headers: { "content-type": "application/problem+json" } },
  );
}

/**
 * POST /api/speech/transcribe -> backend POST /v1/speech/transcribe.
 * The browser sends the raw recording (Content-Type: audio/*). It is held in memory only, forwarded once and
 * never stored or logged. The size cap sits below Vercel's 4.5 MB request limit so a too-long recording gets a
 * clear message instead of a platform error.
 */
export async function POST(request: Request): Promise<Response> {
  const type = request.headers.get("content-type") ?? "";
  if (!type.toLowerCase().startsWith("audio/")) {
    return problem(415, "Unsupported Media Type", "Please send a voice recording.", "unsupported_media_type");
  }
  let audio: Uint8Array;
  try {
    audio = await readCapped(request, MAX_AUDIO_BYTES);
  } catch (error) {
    if (error instanceof BodyTooLargeError) {
      return problem(413, "Payload Too Large", "That recording is too long. Please keep your idea under a minute.", "audio_too_large");
    }
    return problem(400, "Bad Request", "We couldn't read that recording. Please try again.", "audio_unreadable");
  }
  return forwardToBackend("/v1/speech/transcribe", {
    method: "POST",
    body: audio,
    headers: { "Content-Type": type },
    timeoutMs: 55_000,
  });
}
