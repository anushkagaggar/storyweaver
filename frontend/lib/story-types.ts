// Shapes returned by the backend REST API (/v1/stories). Mirrors backend api/routers/stories.py.

export type StoryStatus = "queued" | "planning" | "checking" | "ready" | "failed";

export type Line = { speaker: string; text: string };

export type Scene = {
  idx: number;
  text: string;
  lines: Line[];
  image_url: string | null;
  /** Presigned MP3 link, valid for about an hour. */
  audio_url: string | null;
  /** "pending" while the page's narration is being made; "none" when the story has no narration. */
  audio_status: AudioStatus;
};

export type AudioStatus = "none" | "pending" | "ready" | "failed";

export type Character = { id: string; name: string; species: string; visual: string };

export type Story = {
  id: string;
  status: StoryStatus;
  age_band: string;
  num_scenes: number;
  title: string | null;
  moral: string | null;
  setting: string | null;
  characters: Character[];
  scenes: Scene[];
  /** Set when the idea was adapted before writing, e.g. named characters replaced with original ones. */
  notice: string | null;
  /** "orpheus" or "kokoro" once narration is queued; null for a story without narration. */
  tts_engine: string | null;
  error: { code: string; detail: string | null } | null;
  created_at: string;
  updated_at: string;
};

export type CreateStoryResponse = { story_id: string; job_id: string; status: StoryStatus };

/** RFC 7807 problem body, plus the extra fields our API adds. */
export type Problem = {
  status: number;
  title?: string;
  detail?: string;
  code?: string;
  category?: string | null;
  errors?: { loc: (string | number)[]; msg: string }[];
};

/** Transcription result from POST /api/speech/transcribe. */
export type Transcript = { text: string; engine: string; model: string; duration_seconds: number };

/** Vercel rejects request bodies over 4.5 MB, so recordings stay under this (60 s of speech is about 0.3 MB). */
export const MAX_AUDIO_BYTES = 4_000_000;
/** The backend accepts up to 60 s; the recorder stops a little earlier. */
export const MAX_RECORDING_SECONDS = 55;

export const MAX_PROMPT_LENGTH = 500;
export const MIN_PROMPT_LENGTH = 3;
export const SCENE_CHOICES = [5, 6] as const;
export type SceneChoice = (typeof SCENE_CHOICES)[number];
