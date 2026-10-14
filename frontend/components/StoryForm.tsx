"use client";

import { useRouter } from "next/navigation";
import { type FormEvent, useId, useRef, useState } from "react";

import { createStory } from "@/lib/story-client";
import { validatePrompt } from "@/lib/story-logic";
import { MAX_PROMPT_LENGTH, SCENE_CHOICES, type SceneChoice } from "@/lib/story-types";

export function StoryForm() {
  const router = useRouter();
  const promptId = useId();
  const [prompt, setPrompt] = useState("");
  const [scenes, setScenes] = useState<SceneChoice>(5);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  // One key per distinct request: a double-click or a retry of the same idea never creates two stories.
  const attempt = useRef<{ key: string; signature: string } | null>(null);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const problem = validatePrompt(prompt);
    if (problem) {
      setError(problem);
      return;
    }
    const signature = `${prompt.trim()}|${scenes}`;
    if (attempt.current?.signature !== signature) {
      attempt.current = { key: crypto.randomUUID(), signature };
    }
    setSubmitting(true);
    setError(null);
    const result = await createStory(prompt.trim(), scenes, attempt.current.key);
    if (result.ok) {
      router.push(`/story/${result.value.story_id}`);
      return;
    }
    setError(result.message);
    setSubmitting(false);
  }

  return (
    <form className="story-form" onSubmit={onSubmit} noValidate>
      <label htmlFor={promptId}>What should tonight&apos;s story be about?</label>
      <textarea
        id={promptId}
        value={prompt}
        onChange={(e) => setPrompt(e.target.value)}
        maxLength={MAX_PROMPT_LENGTH}
        rows={3}
        placeholder="A little dragon who is scared of the dark"
        aria-describedby={`${promptId}-count`}
        disabled={submitting}
        required
      />
      <p id={`${promptId}-count`} className="hint">
        {prompt.length}/{MAX_PROMPT_LENGTH}
      </p>

      <fieldset disabled={submitting}>
        <legend>How many pages?</legend>
        {SCENE_CHOICES.map((n) => (
          <label key={n} className="choice">
            <input type="radio" name="scenes" value={n} checked={scenes === n} onChange={() => setScenes(n)} />
            {n} pages
          </label>
        ))}
      </fieldset>

      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      <button type="submit" disabled={submitting}>
        {submitting ? "Starting your story…" : "Make my story"}
      </button>
    </form>
  );
}
