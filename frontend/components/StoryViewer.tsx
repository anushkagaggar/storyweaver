"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import { DeleteStory } from "@/components/DeleteStory";
import { ShareStory } from "@/components/ShareStory";
import { StoryBook } from "@/components/StoryBook";
import { fetchSharedStory, fetchStory, type Result } from "@/lib/story-client";
import {
  FINAL_STATUSES,
  hasPendingMedia,
  mergeMediaUrls,
  msUntil,
  pollDelayMs,
  shareLinkLifetime,
  SIGN_IN_AGAIN,
  statusHint,
  statusMessage,
} from "@/lib/story-logic";
import type { SharedStory, Story } from "@/lib/story-types";

type View =
  | { kind: "loading" }
  | { kind: "story"; story: Story }
  | { kind: "error"; message: string; final: boolean; signIn?: boolean }
  | { kind: "deleted" }
  | { kind: "expired" };

// Narration and pictures finish after the text, so a ready story is polled a while longer (about 10 minutes at most).
const MAX_MEDIA_POLLS = 120;

/**
 * Polls the story (plan §6.4: every 1.5 s with backoff) until it is ready or failed, then keeps polling until every
 * page's narration and picture has arrived.
 *
 * `id`: the signed-in owner's own story, with Share and Delete. `shareToken`: a story read through a share link,
 * read-only, which closes itself when the link expires.
 */
export function StoryViewer({ id, shareToken }: { id?: string; shareToken?: string }) {
  const [view, setView] = useState<View>({ kind: "loading" });
  const [shareExpiresAt, setShareExpiresAt] = useState<string | null>(null);
  const latest = useRef<Story | null>(null);
  // Set once the story is deleted or the link expired, so an in-flight poll does not bring it back.
  const closed = useRef(false);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const load: () => Promise<Result<Story | SharedStory>> = shareToken
      ? () => fetchSharedStory(shareToken)
      : () => fetchStory(id ?? "");

    async function poll(attempt: number) {
      const result = await load();
      if (cancelled || closed.current) return;
      if (result.ok) {
        const story = mergeMediaUrls(latest.current, result.value);
        latest.current = story;
        if ("share_expires_at" in result.value) setShareExpiresAt(result.value.share_expires_at);
        setView({ kind: "story", story });
        if (FINAL_STATUSES.has(story.status) && !(hasPendingMedia(story) && attempt < MAX_MEDIA_POLLS)) return;
      } else {
        const signIn = result.status === 401;
        const final = result.status === 404 || signIn;
        setView({ kind: "error", message: signIn ? SIGN_IN_AGAIN : result.message, final, signIn });
        if (final) return;
      }
      timer = setTimeout(() => void poll(attempt + 1), pollDelayMs(attempt));
    }

    void poll(0);
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [id, shareToken]);

  // A shared story closes when its link expires: its pictures and audio stop working then anyway.
  useEffect(() => {
    if (!shareExpiresAt) return;
    const timer = setTimeout(() => {
      closed.current = true;
      setView({ kind: "expired" });
    }, msUntil(shareExpiresAt));
    return () => clearTimeout(timer);
  }, [shareExpiresAt]);

  function onDeleted() {
    closed.current = true;
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

  if (view.kind === "expired") {
    return (
      <div className="story-status" role="status">
        <h1>This link has expired</h1>
        <p>Shared stories can be read for 15 minutes. Ask whoever sent it for a new link.</p>
      </div>
    );
  }

  if (view.kind === "story" && view.story.status === "ready") {
    const lifetime = shareExpiresAt ? shareLinkLifetime(shareExpiresAt) : null;
    return (
      <>
        {shareToken && lifetime && (
          <p className="notice" role="note">
            Someone shared this story with you. You can read it {lifetime}.
          </p>
        )}
        {view.story.notice && (
          <p className="notice" role="note">
            {view.story.notice}
          </p>
        )}
        <StoryBook story={view.story} />
        {id && (
          <>
            <ShareStory id={id} />
            <p className="again">
              <Link href="/">Make another story</Link>
            </p>
            <DeleteStory id={id} onDeleted={onDeleted} />
          </>
        )}
      </>
    );
  }

  if (view.kind === "story" && view.story.status === "failed") {
    return (
      <div className="story-status failed" role="alert">
        <h1>{statusMessage("failed")}</h1>
        <p>{view.story.error?.detail ?? "Please try a different idea."}</p>
        <Link href="/">Try another idea</Link>
        {id && <DeleteStory id={id} onDeleted={onDeleted} />}
      </div>
    );
  }

  if (view.kind === "error" && view.final) {
    return (
      <div className="story-status failed" role="alert">
        <h1>{view.message}</h1>
        {view.signIn ? (
          <a href={`/api/auth/signin?next=${encodeURIComponent(`/story/${id ?? ""}`)}`}>Sign in with Google</a>
        ) : shareToken ? null : (
          <Link href="/">Make a new story</Link>
        )}
      </div>
    );
  }

  const status = view.kind === "story" ? view.story.status : "queued";
  return (
    <div className="story-status" aria-live="polite" aria-busy="true">
      <div className="spinner" aria-hidden="true" />
      <h1>{statusMessage(status)}</h1>
      <p className="hint">{view.kind === "error" ? view.message : statusHint(status)}</p>
    </div>
  );
}
