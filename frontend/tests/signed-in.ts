// Test helpers: a signed-in parent's request, as the browser would send it after Google sign-in.

import { vi } from "vitest";

import { SESSION_COOKIE, sessionCookieValue } from "@/lib/auth";

export const UID = "u".repeat(43);

export function stubAuthEnv(): void {
  vi.stubEnv("STORYWEAVER_API_URL", "https://backend.example");
  vi.stubEnv("STORYWEAVER_API_KEY", "server-side-key");
  vi.stubEnv("AUTH_SECRET", "a-test-auth-secret-that-is-long-enough-0123");
  vi.stubEnv("USER_TOKEN_SECRET", "a-test-user-token-secret-0123456789");
}

export function signedIn(url: string, init: RequestInit = {}, uid = UID): Request {
  const headers = new Headers(init.headers);
  headers.set("cookie", `${SESSION_COOKIE}=${sessionCookieValue(uid)}`);
  return new Request(url, { ...init, headers });
}
