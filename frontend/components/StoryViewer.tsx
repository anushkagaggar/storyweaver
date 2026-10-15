"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { StoryBook } from "@/components/StoryBook";
import { fetchStory } from "@/lib/story-client";
import { FINAL_STATUSES, pollDelayMs, statusHint, statusMessage } from "@/lib/story-logic";
import type { Story } from "@/lib/story-types";

type View =
  | { kind: "loading" }
  | { kind: "story"; story: Story }
  | { kind: "error"; message: string; final: boolean };

/** Polls /api/stories/{id} (plan §6.4: every 1.5 s with backoff) until the story is ready or failed. */
export function StoryViewer({ id }: { id: string }) {
  const [view, setView] = useState<View>({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    async function poll(attempt: number) {
      const result = await fetchStory(id);
      if (cancelled) return;
      if (result.ok) {
        setView({ kind: "story", story: result.value });
        if (FINAL_STATUSES.has(result.value.status)) return;
      } else {
        const final = result.status === 404;
        setView({ kind: "error", message: result.message, final });
        if (final) return;
      }
      timer = setTimeout(() => void poll(attempt + 1), pollDelayMs(attempt));
    }

    void poll(0);
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [id]);

  if (view.kind === "story" && view.story.status === "ready") {
    return (
      <>
        {view.story.notice && (
          <p className="notice" role="note">
            {view.story.notice}
          </p>
        )}
        <StoryBook story={view.story} />
        <p className="again">
          <Link href="/">Make another story</Link>
        </p>
      </>
    );
  }

  if (view.kind === "story" && view.story.status === "failed") {
    return (
      <div className="story-status failed" role="alert">
        <h1>{statusMessage("failed")}</h1>
        <p>{view.story.error?.detail ?? "Please try a different idea."}</p>
        <Link href="/">Try another idea</Link>
      </div>
    );
  }

  if (view.kind === "error" && view.final) {
    return (
      <div className="story-status failed" role="alert">
        <h1>{view.message}</h1>
        <Link href="/">Make a new story</Link>
      </div>
    );
  }

  const status = view.kind === "story" ? view.story.status : "queued";
  return (
    <div className="story-status" aria-live="polite" aria-busy="true">
      <div className="spinner" aria-hidden="true" />
      <h1>{statusMessage(status)}</h1>
      <p className="hint">
        {view.kind === "error" ? view.message : statusHint(status)}
      </p>
    </div>
  );
}
