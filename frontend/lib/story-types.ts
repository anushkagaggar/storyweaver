// Shapes returned by the backend REST API (/v1/stories). Mirrors backend api/routers/stories.py.

export type StoryStatus = "queued" | "planning" | "checking" | "ready" | "failed";

export type Line = { speaker: string; text: string };

export type Scene = {
  idx: number;
  text: string;
  lines: Line[];
  image_url: string | null;
  audio_url: string | null;
};

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

export const MAX_PROMPT_LENGTH = 500;
export const MIN_PROMPT_LENGTH = 3;
export const SCENE_CHOICES = [5, 6] as const;
export type SceneChoice = (typeof SCENE_CHOICES)[number];
