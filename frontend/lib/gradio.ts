// Server-only: calls the backend's Gradio GPU endpoints (ADR-0001). Never import from a client component.
//
// Gradio's HTTP API is two steps: POST /gradio_api/call/<name> returns an event id, then
// GET /gradio_api/call/<name>/<event_id> streams server-sent events ending in `complete` or `error`.
// HF_TOKEN (optional) is sent so ZeroGPU charges GPU time to the token owner's quota instead of
// the anonymous per-IP quota.

import { backendConfig, type CheckResult } from "@/lib/backend";

type GradioEvent = { event: string; data: string };

function lastEvent(stream: string): GradioEvent | null {
  let current: GradioEvent | null = null;
  let last: GradioEvent | null = null;
  for (const line of stream.split(/\r?\n/)) {
    if (line.startsWith("event:")) current = { event: line.slice(6).trim(), data: "" };
    else if (line.startsWith("data:") && current) {
      current.data = line.slice(5).trim();
      last = current;
    }
  }
  return last;
}

function safeJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

export async function callGradioApi(name: string, timeoutMs: number): Promise<CheckResult> {
  const { baseUrl, apiKey } = backendConfig();
  const headers: Record<string, string> = { "X-API-Key": apiKey };
  const hfToken = process.env.HF_TOKEN;
  if (hfToken) headers.Authorization = `Bearer ${hfToken}`;
  const signal = AbortSignal.timeout(timeoutMs);

  try {
    const start = await fetch(`${baseUrl}/gradio_api/call/${name}`, {
      method: "POST",
      headers: { ...headers, "Content-Type": "application/json" },
      body: JSON.stringify({ data: [] }),
      cache: "no-store",
      signal,
    });
    if (!start.ok) return { ok: false, status: start.status, detail: await start.text() };
    const { event_id: eventId } = (await start.json()) as { event_id?: string };
    if (!eventId) return { ok: false, status: 502, detail: "Gradio did not return an event id." };

    const result = await fetch(`${baseUrl}/gradio_api/call/${name}/${eventId}`, {
      headers,
      cache: "no-store",
      signal,
    });
    const event = lastEvent(await result.text());
    if (event?.event === "complete") {
      const output = safeJson(event.data);
      const value = Array.isArray(output) ? output[0] : output;
      const ok = typeof value === "object" && value !== null && (value as { ok?: unknown }).ok === true;
      return { ok, status: ok ? 200 : 503, detail: value };
    }
    return { ok: false, status: 502, detail: event ? safeJson(event.data) : "No result from Gradio." };
  } catch (error) {
    const reason = error instanceof Error && error.name === "TimeoutError" ? "timed out" : "unreachable";
    return { ok: false, status: 0, detail: `Backend ${reason}.` };
  }
}
