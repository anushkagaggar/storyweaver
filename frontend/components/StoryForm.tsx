"use client";

import { useRouter } from "next/navigation";
import { type FormEvent, useEffect, useId, useRef, useState } from "react";

import { MicRecorder } from "@/components/MicRecorder";
import { createPicture, createStory } from "@/lib/story-client";
import {
  autoRetryDelaySeconds,
  busyCountdownMessage,
  FORM_COPY,
  validatePicturePrompt,
  validatePrompt,
} from "@/lib/story-logic";
import {
  MAX_PICTURE_PROMPT_LENGTH,
  MAX_PROMPT_LENGTH,
  SCENE_CHOICES,
  type SceneChoice,
  type StoryKind,
} from "@/lib/story-types";

export function StoryForm() {
  const router = useRouter();
  const promptId = useId();
  const [prompt, setPrompt] = useState("");
  // A whole story, or one picture ("draw a purple dragon", typed or said into the microphone).
  const [kind, setKind] = useState<StoryKind>("story");
  const [scenes, setScenes] = useState<SceneChoice>(5);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  // Seconds left before the form resubmits a "busy" idea on its own; null when not waiting.
  const [busyWait, setBusyWait] = useState<number | null>(null);
  // One key per distinct request: a double-click or a retry of the same idea never creates two stories.
  const attempt = useRef<{
    key: string;
    signature: string;
    kind: StoryKind;
    prompt: string;
    scenes: SceneChoice;
  } | null>(null);
  const autoRetries = useRef(0);
  // The message to show if the parent cancels the wait.
  const busyMessage = useRef("");

  async function send() {
    const current = attempt.current;
    if (!current) return;
    setSubmitting(true);
    setError(null);
    const result =
      current.kind === "picture"
        ? await createPicture(current.prompt, current.key)
        : await createStory(current.prompt, current.scenes, current.key);
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
    const problem = kind === "picture" ? validatePicturePrompt(prompt) : validatePrompt(prompt);
    if (problem) {
      setError(problem);
      return;
    }
    const trimmed = prompt.trim();
    const signature = `${kind}|${trimmed}|${scenes}`;
    if (attempt.current?.signature !== signature) {
      attempt.current = { key: crypto.randomUUID(), signature, kind, prompt: trimmed, scenes };
    }
    autoRetries.current = 0;
    await send();
  }

  const copy = FORM_COPY[kind];
  const maxLength = kind === "picture" ? MAX_PICTURE_PROMPT_LENGTH : MAX_PROMPT_LENGTH;

  return (
    <form className="story-form" onSubmit={onSubmit} noValidate>
      <fieldset disabled={submitting}>
        <legend>What shall we make?</legend>
        <label className="choice">
          <input type="radio" name="kind" value="story" checked={kind === "story"} onChange={() => setKind("story")} />
          A story
        </label>
        <label className="choice">
          <input
            type="radio"
            name="kind"
            value="picture"
            checked={kind === "picture"}
            onChange={() => setKind("picture")}
          />
          A picture
        </label>
      </fieldset>

      <label htmlFor={promptId}>{copy.label}</label>
      <textarea
        id={promptId}
        value={prompt}
        onChange={(e) => setPrompt(e.target.value)}
        maxLength={maxLength}
        rows={kind === "picture" ? 2 : 3}
        placeholder={copy.placeholder}
        aria-describedby={`${promptId}-count`}
        disabled={submitting}
        required
      />
      <p id={`${promptId}-count`} className="hint">
        {prompt.length}/{maxLength}
        {kind === "picture" && " · You can also say it: \"draw a purple dragon\""}
      </p>

      <MicRecorder disabled={submitting} onTranscript={(text) => setPrompt(text.slice(0, maxLength))} />

      {kind === "story" && (
        <fieldset disabled={submitting}>
          <legend>How many pages?</legend>
          {SCENE_CHOICES.map((n) => (
            <label key={n} className="choice">
              <input type="radio" name="scenes" value={n} checked={scenes === n} onChange={() => setScenes(n)} />
              {n} pages
            </label>
          ))}
        </fieldset>
      )}

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
        {busyWait !== null ? "Waiting for the storyteller…" : submitting ? copy.submitting : copy.submit}
      </button>
    </form>
  );
}
