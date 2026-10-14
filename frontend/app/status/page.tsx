"use client";

import { useCallback, useEffect, useState } from "react";

type Check = { ok: boolean; status: number; detail: unknown };
type Status = { backend: Check; gpu: Check; groq: Check };
type Outcome = { status: Status | null; error: string | null };

const LABELS: Record<keyof Status, string> = { backend: "Backend", gpu: "GPU", groq: "Groq" };

async function fetchStatus(): Promise<Outcome> {
  try {
    const response = await fetch("/api/status", { cache: "no-store" });
    if (!response.ok) return { status: null, error: `Status check failed (HTTP ${response.status}).` };
    return { status: (await response.json()) as Status, error: null };
  } catch {
    return { status: null, error: "Status check failed." };
  }
}

export default function StatusPage() {
  const [status, setStatus] = useState<Status | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const applyOutcome = useCallback((outcome: Outcome) => {
    setStatus(outcome.status);
    setError(outcome.error);
    setLoading(false);
  }, []);

  const rerunChecks = useCallback(() => {
    setLoading(true);
    void fetchStatus().then(applyOutcome);
  }, [applyOutcome]);

  useEffect(() => {
    let cancelled = false;
    void fetchStatus().then((outcome) => {
      if (!cancelled) applyOutcome(outcome);
    });
    return () => {
      cancelled = true;
    };
  }, [applyOutcome]);

  return (
    <main>
      <h1>StoryWeaver</h1>
      <p>Deployment check (V0)</p>
      <ul className="checks">
        {(Object.keys(LABELS) as (keyof Status)[]).map((key) => {
          const check = status?.[key];
          const state = loading || !check ? "pending" : check.ok ? "ok" : "fail";
          return (
            <li key={key} className={state}>
              <strong>
                {LABELS[key]} {state === "ok" ? "OK" : state === "fail" ? "FAILED" : "…"}
              </strong>
              {check && !check.ok && <pre>{JSON.stringify(check.detail, null, 2)}</pre>}
            </li>
          );
        })}
      </ul>
      {error && <p role="alert">{error}</p>}
      <button onClick={rerunChecks} disabled={loading}>
        {loading ? "Checking…" : "Re-run checks"}
      </button>
    </main>
  );
}
