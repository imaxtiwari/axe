"""Pydantic Settings for AXE configuration."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """AXE application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = Field(default="development", description="Application environment")
    log_level: str = Field(default="INFO", description="Logging level")

    # Database
    database_url: str = Field(
        default="sqlite+aiosqlite:///./data/axe.db",
        description="Async SQLAlchemy database URL",
    )
    sqlite_wal_mode: bool = Field(default=True, description="Enable SQLite WAL mode")

    # Vector store
    chroma_persist_dir: str = Field(
        default="./data/chroma", description="Directory for ChromaDB persistence"
    )

    # Encryption
    encryption_key: str | None = Field(
        default=None, description="Fernet key for encrypting sensitive data at rest"
    )
    export_encryption_key: str | None = Field(
        default=None, description="Fernet key for encrypted compliance exports"
    )

    # Retention policy
    retention_days: int = Field(
        default=2555, description="Default retention period in days (~7 years)"
    )
    retention_enabled: bool = Field(
        default=True, description="Enable the nightly retention soft-delete job"
    )
    retention_entity_types: list[str] = Field(
        default_factory=lambda: [
            "signal_log",
            "meeting_summary",
            "morning_brief",
            "sparring_session",
            "thesis_version",
            "thesis_test_result",
            "thesis_post_mortem",
            "communication_archive",
        ],
        description="Entity types eligible for retention soft-delete",
    )

    # Azure Foundry LLM
    azure_foundry_endpoint: str | None = Field(default=None)
    azure_foundry_api_key: str | None = Field(default=None)
    azure_foundry_model: str = Field(default="gpt-4o-mini")

    # Observability
    sentry_dsn: str | None = Field(default=None, description="Sentry DSN")

    # Slack
    slack_bot_token: str | None = Field(default=None)
    slack_signing_secret: str | None = Field(default=None)

    # Email delivery
    resend_api_key: str | None = Field(default=None)
    axe_email_domain: str | None = Field(default=None)
    axe_from_email: str | None = Field(default="alerts@axe.test")

    # Alert destinations
    alerts_slack_user_id: str | None = Field(default=None)
    alerts_email: str | None = Field(default=None)

    # Embedding provider (Azure Foundry / OpenAI compatible)
    embedding_api_key: str | None = Field(default=None)
    embedding_api_base: str | None = Field(default=None)
    embedding_model_name: str = Field(default="text-embedding-3-small")
    embedding_dimensions: int = Field(default=1536)
    embedding_similarity_threshold: float = Field(default=0.72)

    # Polygon.io
    polygon_api_key: str | None = Field(default=None)

    # Google OAuth
    google_client_id: str | None = Field(default=None)
    google_client_secret: str | None = Field(default=None)

    @field_validator("chroma_persist_dir", mode="before")
    @classmethod
    def ensure_data_dir(cls, value: str) -> str:
        Path(value).mkdir(parents=True, exist_ok=True)
        return value

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"

    @property
    def is_testing(self) -> bool:
        return self.app_env.lower() == "test"


@lru_cache
def get_settings() -> Settings:
    """Return cached settings instance."""
    return Settings()
