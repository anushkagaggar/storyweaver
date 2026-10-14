// Server-only helper: reads the backend API key from server env. Never import this from a client component.

export type CheckResult = { ok: boolean; status: number; detail: unknown };

export function backendConfig(): { baseUrl: string; apiKey: string } {
  const baseUrl = process.env.STORYWEAVER_API_URL;
  const apiKey = process.env.STORYWEAVER_API_KEY;
  if (!baseUrl || !apiKey) {
    throw new Error("STORYWEAVER_API_URL and STORYWEAVER_API_KEY must be set on the server.");
  }
  return { baseUrl: baseUrl.replace(/\/$/, ""), apiKey };
}

export async function callBackend(path: string, timeoutMs: number): Promise<CheckResult> {
  const { baseUrl, apiKey } = backendConfig();
  try {
    const response = await fetch(`${baseUrl}${path}`, {
      headers: { "X-API-Key": apiKey },
      cache: "no-store",
      signal: AbortSignal.timeout(timeoutMs),
    });
    const isJson = response.headers.get("content-type")?.includes("json") ?? false;
    const detail: unknown = isJson ? await response.json() : await response.text();
    return { ok: response.ok, status: response.status, detail };
  } catch (error) {
    const reason = error instanceof Error && error.name === "TimeoutError" ? "timed out" : "unreachable";
    return { ok: false, status: 0, detail: `Backend ${reason}.` };
  }
}

const FORWARDED_HEADERS = ["content-type", "retry-after", "location", "idempotent-replayed", "x-request-id"];

/**
 * Forward a request to the backend with the API key attached, returning the backend's status, body and the
 * headers the browser needs (problem+json, Retry-After). Network failures become a 502/504 problem response.
 */
export async function forwardToBackend(
  path: string,
  init: { method?: "GET" | "POST"; body?: string; headers?: Record<string, string>; timeoutMs?: number } = {},
): Promise<Response> {
  const { baseUrl, apiKey } = backendConfig();
  try {
    const upstream = await fetch(`${baseUrl}${path}`, {
      method: init.method ?? "GET",
      body: init.body,
      headers: { "X-API-Key": apiKey, ...(init.body ? { "Content-Type": "application/json" } : {}), ...init.headers },
      cache: "no-store",
      signal: AbortSignal.timeout(init.timeoutMs ?? 30_000),
    });
    const headers = new Headers();
    for (const name of FORWARDED_HEADERS) {
      const value = upstream.headers.get(name);
      if (value) headers.set(name, value);
    }
    return new Response(upstream.body, { status: upstream.status, headers });
  } catch (error) {
    const timedOut = error instanceof Error && error.name === "TimeoutError";
    const status = timedOut ? 504 : 502;
    return Response.json(
      { type: "about:blank", title: timedOut ? "Gateway Timeout" : "Bad Gateway", status, detail: "The story service is unreachable." },
      { status, headers: { "content-type": "application/problem+json" } },
    );
  }
}
