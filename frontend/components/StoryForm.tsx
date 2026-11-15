"use client";

import { useRouter } from "next/navigation";
import { type FormEvent, useEffect, useId, useRef, useState } from "react";

import { MicRecorder } from "@/components/MicRecorder";
import { createStory } from "@/lib/story-client";
import { autoRetryDelaySeconds, busyCountdownMessage, validatePrompt } from "@/lib/story-logic";
import { MAX_PROMPT_LENGTH, SCENE_CHOICES, type SceneChoice } from "@/lib/story-types";

export function StoryForm() {
  const router = useRouter();
  const promptId = useId();
  const [prompt, setPrompt] = useState("");
  const [scenes, setScenes] = useState<SceneChoice>(5);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  // Seconds left before the form resubmits a "busy" idea on its own; null when not waiting.
  const [busyWait, setBusyWait] = useState<number | null>(null);
  // One key per distinct request: a double-click or a retry of the same idea never creates two stories.
  const attempt = useRef<{ key: string; signature: string; prompt: string; scenes: SceneChoice } | null>(null);
  const autoRetries = useRef(0);
  // The message to show if the parent cancels the wait.
  const busyMessage = useRef("");

  async function send() {
    const current = attempt.current;
    if (!current) return;
    setSubmitting(true);
    setError(null);
    const result = await createStory(current.prompt, current.scenes, current.key);
    if (result.ok) {
      router.push(`/story/${result.value.story_id}`);
      return;
    }
    // The backend refuses busy ideas before storing anything, so resending the same Idempotency-Key is safe.
    const wait = autoRetryDelaySeconds(result.status, result.retryAfter, autoRetries.current, result.code);
    if (wait !== null) {
      autoRetries.current += 1;
      busyMessage.current = result.message;
      setBusyWait(wait);
      return;
    }
    setError(result.message);
    setSubmitting(false);
  }

  // Count down once a second, then resubmit. Cleanup stops the timer on cancel or unmount.
  useEffect(() => {
    if (busyWait === null) return;
    const timer = setTimeout(() => {
      if (busyWait <= 1) {
        setBusyWait(null);
        void send();
      } else {
        setBusyWait(busyWait - 1);
      }
    }, 1000);
    return () => clearTimeout(timer);
    // `send` reads only refs and stable setters, so it is safe to leave out.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [busyWait]);

  function cancelWait() {
    setBusyWait(null);
    setError(busyMessage.current);
    setSubmitting(false);
  }

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const problem = validatePrompt(prompt);
    if (problem) {
      setError(problem);
      return;
    }
    const trimmed = prompt.trim();
    const signature = `${trimmed}|${scenes}`;
    if (attempt.current?.signature !== signature) {
      attempt.current = { key: crypto.randomUUID(), signature, prompt: trimmed, scenes };
    }
    autoRetries.current = 0;
    await send();
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

      <MicRecorder disabled={submitting} onTranscript={(text) => setPrompt(text.slice(0, MAX_PROMPT_LENGTH))} />

      <fieldset disabled={submitting}>
        <legend>How many pages?</legend>
        {SCENE_CHOICES.map((n) => (
          <label key={n} className="choice">
            <input type="radio" name="scenes" value={n} checked={scenes === n} onChange={() => setScenes(n)} />
            {n} pages
          </label>
        ))}
      </fieldset>

      {busyWait !== null && (
        <p className="hint" aria-live="off">
          {busyCountdownMessage(busyWait)}{" "}
          <button type="button" className="link-button" onClick={cancelWait}>
            Cancel
          </button>
        </p>
      )}
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      <button type="submit" disabled={submitting}>
        {busyWait !== null ? "Waiting for the storyteller…" : submitting ? "Starting your story…" : "Make my story"}
      </button>
    </form>
  );
}
