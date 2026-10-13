// Server-only helper: reads the backend API key from server env. Never import this from a client component.

export type CheckResult = { ok: boolean; status: number; detail: unknown };

function backendConfig(): { baseUrl: string; apiKey: string } {
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
