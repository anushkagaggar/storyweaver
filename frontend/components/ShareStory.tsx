"use client";

import { useState } from "react";

import { shareStory } from "@/lib/story-client";
import { shareLinkLifetime } from "@/lib/story-logic";
import type { ShareLink } from "@/lib/story-types";

/**
 * "Share this story": makes a read-only link that anyone can open for 15 minutes from now. Opening or forwarding
 * it never makes it last longer; press the button again for a fresh link.
 */
export function ShareStory({ id }: { id: string }) {
  const [link, setLink] = useState<ShareLink | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  async function make() {
    setBusy(true);
    setError(null);
    setCopied(false);
    const result = await shareStory(id);
    setBusy(false);
    if (result.ok) setLink(result.value);
    else setError(result.message);
  }

  const url = link ? `${window.location.origin}/s/${link.token}` : "";
  const lifetime = link ? shareLinkLifetime(link.expires_at) : null;

  async function copy() {
    try {
      await navigator.clipboard.writeText(url);
      setCopied(true);
    } catch {
      setCopied(false); // the link is still selectable in the box
    }
  }

  return (
    <div className="share-story">
      <button type="button" className="secondary" onClick={() => void make()} disabled={busy}>
        {busy ? "Making a link…" : link ? "Make a new link" : "Share this story"}
      </button>
      {link && lifetime && (
        <div className="share-link">
          <label htmlFor="share-url">Anyone with this link can read the story {lifetime}. It won&apos;t last longer, even if it&apos;s passed on.</label>
          <input id="share-url" readOnly value={url} onFocus={(event) => event.currentTarget.select()} />
          <button type="button" className="link-button" onClick={() => void copy()}>
            {copied ? "Copied" : "Copy link"}
          </button>
        </div>
      )}
      {link && !lifetime && <p className="hint">That link has expired. Make a new one to share again.</p>}
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
    </div>
  );
}
