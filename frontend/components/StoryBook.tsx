"use client";

import { useCallback, useEffect, useState } from "react";

import { audioNote } from "@/lib/story-logic";
import type { Story } from "@/lib/story-types";

/** One scene per page with previous/next buttons, keyboard arrows and a reading-voice player (pictures: V3). */
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
