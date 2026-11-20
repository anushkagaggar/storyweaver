import { describe, expect, it } from "vitest";

import {
  audioNote,
  formatClock,
  hasPendingMedia,
  imageNote,
  mergeMediaUrls,
  microphoneMessage,
  pickRecorderMimeType,
  transcribeMessage,
} from "@/lib/story-logic";
import type { Scene, Story } from "@/lib/story-types";

function scene(
  idx: number,
  audio_status: Scene["audio_status"],
  audio_url: string | null = null,
  image: Pick<Scene, "image_status" | "image_url"> = { image_status: "none", image_url: null },
): Scene {
  return { idx, text: "Once upon a time.", lines: [], ...image, audio_url, audio_status };
}

function story(scenes: Scene[], status: Story["status"] = "ready"): Story {
  return {
    id: "s",
    kind: "story",
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

const drawn = (url: string): Pick<Scene, "image_status" | "image_url"> => ({ image_status: "ready", image_url: url });
const drawing = { image_status: "pending", image_url: null } as const;

describe("hasPendingMedia", () => {
  it("is true only for a ready story with a page still being narrated or drawn", () => {
    expect(hasPendingMedia(story([scene(1, "ready", "u"), scene(2, "pending")]))).toBe(true);
    expect(hasPendingMedia(story([scene(1, "ready", "u"), scene(2, "failed")]))).toBe(false);
    expect(hasPendingMedia(story([scene(1, "none")]))).toBe(false);
    expect(hasPendingMedia(story([scene(1, "pending")], "checking"))).toBe(false);
    expect(hasPendingMedia(story([scene(1, "ready", "u", drawing)]))).toBe(true);
    expect(hasPendingMedia(story([scene(1, "none", null, drawn("p"))]))).toBe(false);
  });
});

describe("mergeMediaUrls", () => {
  it("keeps the URL of a page that was already ready, so a playing page isn't restarted", () => {
    const before = story([scene(1, "ready", "https://b/1?sig=old"), scene(2, "pending")]);
    const after = story([scene(1, "ready", "https://b/1?sig=new"), scene(2, "ready", "https://b/2?sig=new")]);
    const merged = mergeMediaUrls(before, after);
    expect(merged.scenes.map((x) => x.audio_url)).toEqual(["https://b/1?sig=old", "https://b/2?sig=new"]);
  });

  it("keeps a shown picture's URL so it doesn't reload, but takes a new one when the picture changes", () => {
    const before = story([scene(1, "none", null, drawn("https://b/i1?sig=old")), scene(2, "none", null, drawing)]);
    const after = story([
      scene(1, "none", null, drawn("https://b/i1?sig=new")),
      scene(2, "none", null, { image_status: "placeholder", image_url: "https://b/p2?sig=new" }),
    ]);
    const merged = mergeMediaUrls(before, after);
    expect(merged.scenes.map((x) => x.image_url)).toEqual(["https://b/i1?sig=old", "https://b/p2?sig=new"]);
  });

  it("returns the new story when there is no earlier one", () => {
    const first = story([scene(1, "pending")]);
    expect(mergeMediaUrls(null, first)).toBe(first);
  });
});

describe("imageNote", () => {
  it("says when a picture is being drawn or is a placeholder, and nothing otherwise", () => {
    expect(imageNote(scene(1, "none", null, drawing))).toMatch(/Drawing/);
    expect(imageNote(scene(1, "none", null, { image_status: "placeholder", image_url: "p" }))).toMatch(/soft picture/);
    expect(imageNote(scene(1, "none", null, drawn("u")))).toBeNull();
    expect(imageNote(scene(1, "none"))).toBeNull();
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
