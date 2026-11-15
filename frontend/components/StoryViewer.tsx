"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import { DeleteStory } from "@/components/DeleteStory";
import { StoryBook } from "@/components/StoryBook";
import { fetchStory } from "@/lib/story-client";
import { FINAL_STATUSES, hasPendingMedia, mergeMediaUrls, pollDelayMs, statusHint, statusMessage } from "@/lib/story-logic";
import type { Story } from "@/lib/story-types";

type View =
  | { kind: "loading" }
  | { kind: "story"; story: Story }
  | { kind: "error"; message: string; final: boolean }
  | { kind: "deleted" };

// Narration and pictures finish after the text, so a ready story is polled a while longer (about 10 minutes at most).
const MAX_MEDIA_POLLS = 120;

/**
 * Polls /api/stories/{id} (plan §6.4: every 1.5 s with backoff) until the story is ready or failed, then keeps
 * polling until every page's narration and picture has arrived.
 */
export function StoryViewer({ id }: { id: string }) {
  const [view, setView] = useState<View>({ kind: "loading" });
  const latest = useRef<Story | null>(null);
  // Set once the story is deleted, so an in-flight poll does not bring it back.
  const deleted = useRef(false);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    async function poll(attempt: number) {
      const result = await fetchStory(id);
      if (cancelled || deleted.current) return;
      if (result.ok) {
        const story = mergeMediaUrls(latest.current, result.value);
        latest.current = story;
        setView({ kind: "story", story });
        if (FINAL_STATUSES.has(story.status) && !(hasPendingMedia(story) && attempt < MAX_MEDIA_POLLS)) return;
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

  function onDeleted() {
    deleted.current = true;
    setView({ kind: "deleted" });
  }

  if (view.kind === "deleted") {
    return (
      <div className="story-status" role="status">
        <h1>Story deleted</h1>
        <p>The story, its pages, its pictures and its reading voice are gone for good.</p>
        <Link href="/">Make a new story</Link>
      </div>
    );
  }

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
        <DeleteStory id={id} onDeleted={onDeleted} />
      </>
    );
  }

  if (view.kind === "story" && view.story.status === "failed") {
    return (
      <div className="story-status failed" role="alert">
        <h1>{statusMessage("failed")}</h1>
        <p>{view.story.error?.detail ?? "Please try a different idea."}</p>
        <Link href="/">Try another idea</Link>
        <DeleteStory id={id} onDeleted={onDeleted} />
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
