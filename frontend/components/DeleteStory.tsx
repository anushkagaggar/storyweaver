"use client";

import { useState } from "react";

import { deleteStory } from "@/lib/story-client";

type Phase = "idle" | "confirming" | "deleting";

/**
 * "Delete this story" with an inline confirmation (no browser dialog). Removes the story, its pages and its
 * reading voice for good, then calls `onDeleted` so the viewer stops polling and says so.
 */
export function DeleteStory({ id, onDeleted }: { id: string; onDeleted: () => void }) {
  const [phase, setPhase] = useState<Phase>("idle");
  const [error, setError] = useState<string | null>(null);

  async function confirm() {
    setPhase("deleting");
    setError(null);
    const result = await deleteStory(id);
    if (result.ok) {
      onDeleted();
      return;
    }
    setError(result.message);
    setPhase("confirming");
  }

  return (
    <div className="delete-story">
      {phase === "idle" ? (
        <button type="button" className="link-button" onClick={() => setPhase("confirming")}>
          Delete this story
        </button>
      ) : (
        <p role="group" aria-label="Confirm deleting this story">
          Delete this story and its reading voice for good?{" "}
          <button type="button" className="danger" onClick={() => void confirm()} disabled={phase === "deleting"}>
            {phase === "deleting" ? "Deleting…" : "Delete"}
          </button>{" "}
          <button type="button" className="link-button" onClick={() => setPhase("idle")} disabled={phase === "deleting"}>
            Keep it
          </button>
        </p>
      )}
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
    </div>
  );
}
