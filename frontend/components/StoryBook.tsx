"use client";

import { useCallback, useEffect, useState } from "react";

import { audioNote, imageNote } from "@/lib/story-logic";
import type { Story } from "@/lib/story-types";

/** One scene per page: its picture, its text and a reading-voice player, with previous/next buttons and arrows. */
export function StoryBook({ story }: { story: Story }) {
  const [page, setPage] = useState(0);
  const last = story.scenes.length - 1;
  // The page whose narration starts by itself, because the previous page's narration just finished.
  const [autoPlayIdx, setAutoPlayIdx] = useState<number | null>(null);
  const go = useCallback(
    (delta: number) => {
      setAutoPlayIdx(null);
      setPage((p) => Math.min(last, Math.max(0, p + delta)));
    },
    [last],
  );

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key === "ArrowRight") go(1);
      if (event.key === "ArrowLeft") go(-1);
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [go]);

  const scene = story.scenes.at(page);
  if (!scene) return null;
  const note = audioNote(scene);
  const picture = imageNote(scene);
  const next = story.scenes.at(page + 1);

  return (
    <article className="storybook" aria-roledescription="storybook">
      <header>
        <h1>{story.title}</h1>
        {story.setting && <p className="setting">{story.setting}</p>}
      </header>

      <section className="page" aria-live="polite" aria-label={`Page ${page + 1} of ${last + 1}`}>
        <p className="page-number">
          Page {page + 1} of {last + 1}
        </p>
        {scene.image_url ? (
          // Presigned, short-lived URLs from our own bucket: next/image's optimiser would add nothing here.
          // eslint-disable-next-line @next/next/no-img-element
          <img
            key={scene.image_url}
            className={scene.image_status === "placeholder" ? "page-image placeholder" : "page-image"}
            src={scene.image_url}
            // The picture shows what the text says; a placeholder is decorative.
            alt={scene.image_status === "placeholder" ? "" : `Picture for page ${page + 1}`}
          />
        ) : (
          scene.image_status === "pending" && <div className="page-image drawing" aria-hidden="true" />
        )}
        {picture && (
          <p className="hint image-note" role="status">
            {picture}
          </p>
        )}
        <p className="page-text">{scene.text}</p>
        {scene.audio_url ? (
          // Browsers only let audio start after a tap, so page 1 waits for Play; later pages continue by themselves.
          <audio
            key={scene.idx}
            className="page-audio"
            controls
            preload="metadata"
            src={scene.audio_url}
            aria-label={`Listen to page ${page + 1}`}
            autoPlay={autoPlayIdx === scene.idx}
            onEnded={() => {
              if (!next) return;
              setAutoPlayIdx(next.idx);
              setPage((p) => p + 1);
            }}
          />
        ) : (
          note && (
            <p className="hint audio-note" role="status">
              {note}
            </p>
          )
        )}
      </section>

      <nav className="page-nav" aria-label="Pages">
        <button type="button" onClick={() => go(-1)} disabled={page === 0}>
          ← Back
        </button>
        <button type="button" onClick={() => go(1)} disabled={page === last}>
          Next →
        </button>
      </nav>

      {page === last && story.moral && (
        <p className="moral">
          <strong>The moral:</strong> {story.moral}
        </p>
      )}
    </article>
  );
}
