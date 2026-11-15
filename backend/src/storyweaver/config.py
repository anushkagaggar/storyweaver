from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    # Lifetime of the presigned audio URLs in GET /v1/stories/{id}. The UI re-polls for fresh ones.
    audio_url_ttl_seconds: int = Field(default=3600, ge=60, le=7 * 24 * 3600)
    # Delete stories (rows and audio) this many days after creation; 0 keeps them forever (docs/privacy.md).
    story_retention_days: int = Field(default=30, ge=0, le=3650)
    retention_sweep_seconds: float = Field(default=3600.0, ge=10)
    # Run the in-process job worker (disabled in tests, which drive the worker directly).
    worker_enabled: bool = True

    # Comma-separated list of origins allowed by CORS.
    allowed_origins: str = "http://localhost:3000"

    @field_validator(
        "groq_api_key",
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
