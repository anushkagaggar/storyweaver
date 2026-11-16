"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { listStories } from "@/lib/story-client";
import type { StorySummary } from "@/lib/story-types";

/** The signed-in parent's recent stories. Only they can see this list. */
export function MyStories() {
  const [stories, setStories] = useState<StorySummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void listStories().then((result) => {
      if (cancelled) return;
      if (result.ok) setStories(result.value);
      else setError(result.message);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  if (error) return <p className="hint">{error}</p>;
  if (!stories || stories.length === 0) return null;
  return (
    <section className="my-stories" aria-labelledby="my-stories">
      <h2 id="my-stories">Your stories</h2>
      <ul>
        {stories.map((story) => (
          <li key={story.id}>
            <Link href={`/story/${story.id}`}>{story.title ?? (story.status === "failed" ? "A story that couldn't be finished" : "A story being made…")}</Link>{" "}
            <span className="hint">{new Date(story.created_at).toLocaleDateString(undefined, { day: "numeric", month: "short" })}</span>
          </li>
        ))}
      </ul>
      <p className="hint">Stories are deleted automatically 30 days after they were made.</p>
    </section>
  );
}
