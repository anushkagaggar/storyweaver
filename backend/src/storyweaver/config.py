from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Stable Diffusion 1.5 (CreativeML OpenRAIL-M, not gated), pinned to a commit (checked 16 Nov 2026).
# 768x576 keeps the 4:3 page shape at a size SD 1.5 handles well.
SD15_DEFAULTS: dict[str, str | int] = {
    "image_model": "stable-diffusion-v1-5/stable-diffusion-v1-5",
    "image_model_revision": "451f4fe16113bff5a5d2269ed5ad43b0592e9a14",  # pragma: allowlist secret
    "image_width": 768,
    "image_height": 576,
}


class Settings(BaseSettings):
    """All runtime configuration. Values come from environment variables or a .env file."""

    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: Literal["local", "prod"] = "local"
    log_level: str = "INFO"

    storyweaver_api_key: SecretStr
    groq_api_key: SecretStr | None = None
    # Signed-in users (V3, auth/user_token.py): the HS256 key the Vercel server signs X-User-Token with.
    # Unset: only API-key callers (the "operator" owner) can use story routes, and share links are off.
    user_token_secret: SecretStr | None = None
    # How long an owner's share link works, from when it was made (auth/share_links.py). Never renewed.
    share_link_ttl_seconds: int = Field(default=900, ge=60, le=24 * 3600)

    # Format "<provider>:<model id>", e.g. "groq:openai/gpt-oss-20b".
    llm_planner: str = "groq:openai/gpt-oss-20b"
    # Teacher/judge model; also makes the single repair call when a planned story breaks the rules.
    llm_teacher: str = "groq:openai/gpt-oss-120b"
    planner_max_tokens: int = 4000
    planner_temperature: float = 0.7
    planner_reasoning_effort: Literal["low", "medium", "high"] = "low"

    database_url: str | None = None
    redis_url: str | None = None
    # Keep this fraction of each Groq free-tier limit as the usable budget (0.9 leaves 10% headroom).
    quota_safety_margin: float = Field(default=0.9, gt=0, le=1)
    # Safety guards (plan §6.6). Prompt Guard returns P(prompt attack); at or above the threshold is rejected.
    prompt_guard_model: str = "meta-llama/llama-prompt-guard-2-22m"
    safeguard_model: str = "openai/gpt-oss-safeguard-20b"
    prompt_guard_threshold: float = Field(default=0.5, gt=0, lt=1)
    # Langfuse tracing (plan V1): off unless both keys are set. Prompts and story text are sent only when
    # LANGFUSE_CAPTURE_CONTENT=true; by default traces carry metadata only (privacy by design).
    langfuse_public_key: SecretStr | None = None
    langfuse_secret_key: SecretStr | None = None
    langfuse_host: str = "https://cloud.langfuse.com"
    langfuse_capture_content: bool = False
    # Speech-to-text (plan V2, §6.6). "groq": Groq Whisper while quota lasts, then faster-whisper on the
    # Space's CPU. "local": faster-whisper only, so voice never leaves the Space.
    stt_engine: Literal["groq", "local"] = "groq"
    stt_model: str = "whisper-large-v3-turbo"
    stt_local_model: str = "small"
    stt_local_fallback: bool = True
    stt_language: str = "en"
    # Text-to-speech (plan V2). "auto": Orpheus when the whole narration fits today's quota, else Kokoro
    # (CPU). "orpheus" / "kokoro" force one engine. Kokoro's ONNX files (~350 MB for fp32) are downloaded
    # to KOKORO_DIR on first use. fp32 is the default because onnxruntime runs it ~13x faster on CPU than
    # int8 (measured 13 Nov 2026: 2.8x vs 0.2x real time).
    tts_engine: Literal["auto", "orpheus", "kokoro"] = "auto"
    tts_model: str = "canopylabs/orpheus-v1-english"
    tts_max_wait_seconds: float = Field(default=120.0, ge=0)
    kokoro_dir: Path = Path.home() / ".cache" / "storyweaver" / "kokoro"
    kokoro_variant: Literal["int8", "fp16", "fp32"] = "fp32"
    # Object storage for narration audio (plan V2, ADR-0004): any S3-compatible store. SeaweedFS locally,
    # Backblaze B2 in production. Off unless the endpoint and both keys are set; stories then have no audio.
    s3_endpoint_url: str | None = None
    s3_public_endpoint_url: str | None = None  # browser-facing host when it differs (local Docker)
    s3_region: str = "us-east-1"
    s3_bucket: str = "storyweaver-audio"
    s3_access_key_id: SecretStr | None = None
    s3_secret_access_key: SecretStr | None = None
    s3_create_bucket: bool = False
    # Lifetime of the presigned audio and image URLs in GET /v1/stories/{id}. The UI re-polls for fresh ones.
    audio_url_ttl_seconds: int = Field(default=3600, ge=60, le=7 * 24 * 3600)
    # Illustrations (plan V3), stored in the same bucket as the audio. "placeholder" draws a soft wordless
    # picture for every page (no GPU); "sd15" draws real pictures with Stable Diffusion 1.5 on this machine's
    # NVIDIA GPU (imaging/sd15.py; local Docker only, never on the ZeroGPU Space); "none" turns images off.
    image_engine: Literal["none", "placeholder", "sd15"] = "placeholder"
    # Generation settings. Every one is part of the image cache key (imaging/params.py). With
    # IMAGE_ENGINE=sd15, the model, its revision and the size default to SD 1.5's (see _engine_defaults).
    image_model: str = "stabilityai/stable-diffusion-xl-base-1.0"
    image_model_revision: str = "main"
    image_width: int = Field(default=1152, ge=256, le=2048, multiple_of=8)
    image_height: int = Field(default=896, ge=256, le=2048, multiple_of=8)
    image_sampler: str = "dpmpp_2m_karras"
    image_steps: int = Field(default=25, ge=1, le=100)
    image_guidance: float = Field(default=6.0, ge=0, le=20)
    # Character consistency (plan §6.5, IMAGE_ENGINE=sd15): later pages use page 1's picture as an
    # IP-Adapter reference at this strength (0 = only the fixed look; ~1 = copy page 1). Compared on one
    # story (scripts/consistency_probe.py, 16 Nov 2026): 0.5 copied page 1's layout, 0.3-0.4 kept the
    # character on 4 of 5 pages while each page showed its own scene.
    image_reference_scale: float = Field(default=0.4, ge=0, le=1)
    # sd15 keeps the whole model on the GPU (4.5 GB of a 6 GB card, ~6 s a picture). true moves each part
    # there only while it runs: for smaller GPUs, but slower and ~4 GB more RAM (imaging/sd15.py).
    image_cpu_offload: bool = False
    # Our own limits (routing/limits.py). Every user's stories use one Groq organisation and every
    # picture one HF account's ZeroGPU quota, so one user must not use up the day for everyone.
    visitor_stories_per_hour: int = Field(default=5, ge=1)
    visitor_stories_per_day: int = Field(default=15, ge=1)
    gpu_seconds_per_day: int = Field(default=900, ge=0)  # 15 min, below the account's ZeroGPU allowance
    gpu_seconds_per_image: float = Field(default=12.0, gt=0)  # booked before each call, then settled
    # Delete stories (rows and audio) this many days after creation; 0 keeps them forever (docs/privacy.md).
    story_retention_days: int = Field(default=30, ge=0, le=3650)
    retention_sweep_seconds: float = Field(default=3600.0, ge=10)
    # Run the in-process job worker (disabled in tests, which drive the worker directly).
    worker_enabled: bool = True

    # Comma-separated list of origins allowed by CORS.
    allowed_origins: str = "http://localhost:3000"

    @field_validator(
        "groq_api_key",
        "user_token_secret",
        "langfuse_public_key",
        "langfuse_secret_key",
        "s3_endpoint_url",
        "s3_public_endpoint_url",
        "s3_access_key_id",
        "s3_secret_access_key",
        mode="before",
    )
    @classmethod
    def _blank_key_is_unset(cls, value: object) -> object:
        """Blank means unset, and surrounding whitespace is always a paste artefact.

        Pasting a credential into a web console (HF Space Secrets, Vercel) easily carries a trailing newline
        or space. No credential legitimately has one, and an unstripped secret fails in the least obvious
        way possible: S3 signs the request with the wrong key and returns a bare 403.
        """
        if not isinstance(value, str):
            return value
        return value.strip() or None

    @field_validator("s3_region", "s3_bucket", mode="before")
    @classmethod
    def _strip_setting(cls, value: object) -> object:
        """Same paste problem: a trailing space makes the bucket "not found" or the signature invalid."""
        return value.strip() if isinstance(value, str) else value

    @field_validator("s3_endpoint_url", "s3_public_endpoint_url", mode="after")
    @classmethod
    def _endpoint_needs_a_scheme(cls, value: str | None) -> str | None:
        """Add `https://` when the scheme is missing.

        A console shows a B2 endpoint as `s3.us-east-005.backblazeb2.com`, and pasting that verbatim used to
        take the whole Space down: boto3 rejects a scheme-less endpoint with `ValueError: Invalid endpoint`
        while the client is being built, which happens in the app's lifespan (v2.0.1, 14 Nov 2026).

        Anything else is passed through untouched rather than rejected here: a bad endpoint must not make
        the whole Settings object invalid, because that fails at start-up just as hard. `build_blob_store`
        catches what boto3 refuses and disables audio instead.
        """
        if value is None:
            return None
        value = value.strip().rstrip("/")
        return value if "://" in value else f"https://{value}"

    @model_validator(mode="after")
    def _engine_defaults(self) -> "Settings":
        """SD 1.5 settings for IMAGE_ENGINE=sd15, unless IMAGE_MODEL / _REVISION / _WIDTH / _HEIGHT are set.

        The plain defaults are SDXL's (1152x896), which SD 1.5 draws badly (it was trained at 512) and which
        don't fit in 6 GB. The revision is pinned so the image cache key names exact weights.
        """
        if self.image_engine == "sd15":
            for name, value in SD15_DEFAULTS.items():
                if name not in self.model_fields_set:
                    setattr(self, name, value)
        return self

    @property
    def allowed_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.allowed_origins.split(",") if origin.strip()]

    @property
    def tracing_enabled(self) -> bool:
        return self.langfuse_public_key is not None and self.langfuse_secret_key is not None

    @property
    def blob_storage_enabled(self) -> bool:
        return all((self.s3_endpoint_url, self.s3_access_key_id, self.s3_secret_access_key))

    @property
    def planner_model_id(self) -> str:
        return groq_model_id(self.llm_planner, "LLM_PLANNER")

    @property
    def teacher_model_id(self) -> str:
        return groq_model_id(self.llm_teacher, "LLM_TEACHER")


def groq_model_id(spec: str, setting_name: str) -> str:
    provider, _, model = spec.partition(":")
    if provider != "groq" or not model:
        raise ValueError(f"{setting_name} must look like 'groq:<model>', got {spec!r}")
    return model


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # required fields are read from the environment
