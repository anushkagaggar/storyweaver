"use client";

import { useEffect, useRef, useState, useSyncExternalStore } from "react";

import { transcribeAudio } from "@/lib/story-client";
import { formatClock, microphoneMessage, pickRecorderMimeType } from "@/lib/story-logic";
import { MAX_AUDIO_BYTES, MAX_RECORDING_SECONDS } from "@/lib/story-types";

type Phase = "idle" | "recording" | "transcribing";

// Browser capabilities never change while the page is open. The server snapshot is false, so server and client
// render the same markup first and the button appears after hydration.
const subscribeNever = () => () => {};
const canRecord = () => typeof MediaRecorder !== "undefined" && Boolean(navigator.mediaDevices?.getUserMedia);
const cannotRecord = () => false;

/**
 * Record a story idea by voice and hand the transcript to the form (the parent can edit it before sending).
 * The recording lives in memory only: it is sent once for transcription and never stored. The microphone is
 * released as soon as recording stops, and the whole thing is hidden when the browser can't record.
 */
export function MicRecorder({ onTranscript, disabled }: { onTranscript: (text: string) => void; disabled?: boolean }) {
  const supported = useSyncExternalStore(subscribeNever, canRecord, cannotRecord);
  const [phase, setPhase] = useState<Phase>("idle");
  const [seconds, setSeconds] = useState(0);
  const [message, setMessage] = useState<string | null>(null);
  const recorder = useRef<MediaRecorder | null>(null);
  const stream = useRef<MediaStream | null>(null);
  const ticker = useRef<ReturnType<typeof setInterval> | null>(null);
  const discard = useRef(false);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      discard.current = true;
      if (recorder.current?.state === "recording") recorder.current.stop();
      release();
    };
  }, []);

  function release() {
    if (ticker.current) clearInterval(ticker.current);
    ticker.current = null;
    stream.current?.getTracks().forEach((track) => track.stop());
    stream.current = null;
  }

  async function start() {
    setMessage(null);
    let media: MediaStream;
    try {
      media = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (error) {
      setMessage(microphoneMessage(error instanceof DOMException ? error.name : undefined));
      return;
    }
    stream.current = media;
    discard.current = false;
    const mimeType = pickRecorderMimeType((type) => MediaRecorder.isTypeSupported(type));
    const chunks: Blob[] = [];
    const rec = new MediaRecorder(media, { ...(mimeType ? { mimeType } : {}), audioBitsPerSecond: 32_000 });
    recorder.current = rec;
    rec.ondataavailable = (event) => {
      if (event.data.size > 0) chunks.push(event.data);
    };
    rec.onstop = () => {
      release();
      if (discard.current || !mounted.current) return;
      const blob = new Blob(chunks, { type: rec.mimeType || mimeType || "audio/webm" });
      void send(blob);
    };
    rec.start();
    const startedAt = Date.now();
    setSeconds(0);
    setPhase("recording");
    ticker.current = setInterval(() => {
      const elapsed = (Date.now() - startedAt) / 1000;
      setSeconds(elapsed);
      if (elapsed >= MAX_RECORDING_SECONDS && rec.state === "recording") rec.stop();
    }, 250);
  }

  async function send(blob: Blob) {
    if (blob.size === 0) {
      setMessage("We didn't get any sound. Please try again.");
      setPhase("idle");
      return;
    }
    if (blob.size > MAX_AUDIO_BYTES) {
      setMessage("That recording is too long. Please keep your idea under a minute.");
      setPhase("idle");
      return;
    }
    setPhase("transcribing");
    const result = await transcribeAudio(blob);
    if (!mounted.current) return;
    setPhase("idle");
    if (result.ok) {
      setMessage("Here's what we heard. Change anything that isn't right, then make your story.");
      onTranscript(result.value.text);
    } else {
      setMessage(result.message);
    }
  }

  function stop() {
    if (recorder.current?.state === "recording") recorder.current.stop();
  }

  function cancel() {
    discard.current = true;
    if (recorder.current?.state === "recording") recorder.current.stop();
    setPhase("idle");
    setMessage(null);
  }

  if (!supported) return null;

  return (
    <div className="mic">
      {phase === "idle" && (
        <button type="button" className="secondary" onClick={() => void start()} disabled={disabled}>
          🎤 Say your idea
        </button>
      )}
      {phase === "recording" && (
        <>
          <button type="button" onClick={stop}>
            ⏹ Done · {formatClock(seconds)}
          </button>
          <button type="button" className="link-button" onClick={cancel}>
            Cancel
          </button>
        </>
      )}
      {phase === "transcribing" && (
        <p className="hint" aria-busy="true">
          Listening to your recording…
        </p>
      )}
      <p className="hint mic-message" role="status" aria-live="polite">
        {phase === "recording" ? "Recording… speak your story idea, then press Done." : message}
      </p>
    </div>
  );
}
