from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, field_validator
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

    # Comma-separated list of origins allowed by CORS.
    allowed_origins: str = "http://localhost:3000"

    @field_validator("groq_api_key", mode="before")
    @classmethod
    def _blank_key_is_unset(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @property
    def allowed_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.allowed_origins.split(",") if origin.strip()]

    @property
    def planner_model_id(self) -> str:
        provider, _, model = self.llm_planner.partition(":")
        if provider != "groq" or not model:
            raise ValueError(f"LLM_PLANNER must look like 'groq:<model>', got {self.llm_planner!r}")
        return model


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # required fields are read from the environment
