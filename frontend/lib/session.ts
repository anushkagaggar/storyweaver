// Server components only: the signed-in parent from the session cookie (lib/auth.ts).

import { cookies } from "next/headers";

import { readSessionValue, SESSION_COOKIE, type Session } from "@/lib/auth";

export async function currentSession(): Promise<Session | null> {
  const store = await cookies();
  return readSessionValue(store.get(SESSION_COOKIE)?.value);
}
