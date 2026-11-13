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
    # Run the in-process job worker (disabled in tests, which drive the worker directly).
    worker_enabled: bool = True

    # Comma-separated list of origins allowed by CORS.
    allowed_origins: str = "http://localhost:3000"

    @field_validator("groq_api_key", "langfuse_public_key", "langfuse_secret_key", mode="before")
    @classmethod
    def _blank_key_is_unset(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @property
    def allowed_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.allowed_origins.split(",") if origin.strip()]

    @property
    def tracing_enabled(self) -> bool:
        return self.langfuse_public_key is not None and self.langfuse_secret_key is not None

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
