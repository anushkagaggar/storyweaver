"use client";

import { useCallback, useEffect, useState } from "react";

import type { Story } from "@/lib/story-types";

/** One scene per page with previous/next buttons and keyboard arrows. Text only in V1 (pictures V3, audio V2). */
export function StoryBook({ story }: { story: Story }) {
  const [page, setPage] = useState(0);
  const last = story.scenes.length - 1;
  const go = useCallback((delta: number) => setPage((p) => Math.min(last, Math.max(0, p + delta))), [last]);

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
