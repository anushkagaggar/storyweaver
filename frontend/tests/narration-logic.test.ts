import { describe, expect, it } from "vitest";

import {
  audioNote,
  formatClock,
  hasPendingAudio,
  mergeAudioUrls,
  microphoneMessage,
  pickRecorderMimeType,
  transcribeMessage,
} from "@/lib/story-logic";
import type { Scene, Story } from "@/lib/story-types";

function scene(idx: number, audio_status: Scene["audio_status"], audio_url: string | null = null): Scene {
  return { idx, text: "Once upon a time.", lines: [], image_url: null, audio_url, audio_status };
}

function story(scenes: Scene[], status: Story["status"] = "ready"): Story {
  return {
    id: "s",
    status,
    age_band: "4-6",
    num_scenes: scenes.length,
    title: "T",
    moral: null,
    setting: null,
    characters: [],
    scenes,
    notice: null,
    tts_engine: "kokoro",
    error: null,
    created_at: "",
    updated_at: "",
  };
}

describe("hasPendingAudio", () => {
  it("is true only for a ready story with a page still being narrated", () => {
    expect(hasPendingAudio(story([scene(1, "ready", "u"), scene(2, "pending")]))).toBe(true);
    expect(hasPendingAudio(story([scene(1, "ready", "u"), scene(2, "failed")]))).toBe(false);
    expect(hasPendingAudio(story([scene(1, "none")]))).toBe(false);
    expect(hasPendingAudio(story([scene(1, "pending")], "checking"))).toBe(false);
  });
});

describe("mergeAudioUrls", () => {
  it("keeps the URL of a page that was already ready, so a playing page isn't restarted", () => {
    const before = story([scene(1, "ready", "https://b/1?sig=old"), scene(2, "pending")]);
    const after = story([scene(1, "ready", "https://b/1?sig=new"), scene(2, "ready", "https://b/2?sig=new")]);
    const merged = mergeAudioUrls(before, after);
    expect(merged.scenes.map((x) => x.audio_url)).toEqual(["https://b/1?sig=old", "https://b/2?sig=new"]);
  });

  it("returns the new story when there is no earlier one", () => {
    const first = story([scene(1, "pending")]);
    expect(mergeAudioUrls(null, first)).toBe(first);
  });
});

describe("audioNote", () => {
  it("explains pending and failed pages and says nothing otherwise", () => {
    expect(audioNote(scene(1, "pending"))).toMatch(/ready/);
    expect(audioNote(scene(1, "failed"))).toMatch(/couldn't/);
    expect(audioNote(scene(1, "none"))).toBeNull();
    expect(audioNote(scene(1, "ready", "u"))).toBeNull();
  });
});

describe("pickRecorderMimeType", () => {
  it("prefers webm/Opus, then Safari's mp4, and returns undefined when nothing is supported", () => {
    expect(pickRecorderMimeType(() => true)).toBe("audio/webm;codecs=opus");
    expect(pickRecorderMimeType((t) => t === "audio/mp4")).toBe("audio/mp4");
    expect(pickRecorderMimeType(() => false)).toBeUndefined();
  });
});

describe("microphoneMessage", () => {
  it("gives an actionable message per error and a typed-input fallback", () => {
    expect(microphoneMessage("NotAllowedError")).toMatch(/permission/);
    expect(microphoneMessage("NotFoundError")).toMatch(/find a microphone/);
    expect(microphoneMessage("NotReadableError")).toMatch(/busy/);
    expect(microphoneMessage(undefined)).toMatch(/type your idea/);
  });
});

describe("transcribeMessage", () => {
  it("shows the backend's parent-facing detail, but not for gateway failures", () => {
    expect(transcribeMessage({ status: 422, detail: "We couldn't hear anything." })).toBe("We couldn't hear anything.");
    expect(transcribeMessage({ status: 502, detail: "The story service is unreachable." })).toMatch(/Voice input/);
    expect(transcribeMessage({ status: 413 })).toMatch(/too big/);
    expect(transcribeMessage({ status: 500 })).toMatch(/type your idea/);
  });
});

describe("formatClock", () => {
  it("formats m:ss", () => {
    expect(formatClock(0)).toBe("0:00");
    expect(formatClock(9.9)).toBe("0:09");
    expect(formatClock(65)).toBe("1:05");
    expect(formatClock(-1)).toBe("0:00");
  });
});
