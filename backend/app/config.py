from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    postgres_host: str = "postgres"
    postgres_db: str = "education"
    postgres_user: str = "education"
    postgres_password: SecretStr
    temporal_db_user: str = "education_temporal"
    temporal_db_password: SecretStr = SecretStr("")
    temporal_address: str = "temporal:7233"
    temporal_namespace: str = "education"
    temporal_task_queue: str = "education-default"
    chroma_url: str = "http://chroma:8000"
    # Только чтение прежнего тома при миграции 0003; рабочие оригиналы лежат в bytea.
    original_files_path: Path = Path("/data/originals")
    max_upload_bytes: int = Field(default=500 * 1024 * 1024, gt=0)
    parser_url: str = ""
    analysis_url: str = ""
    analysis_original_base_url: str = "http://api:8000"
    analysis_max_request_bytes: int = Field(default=20 * 1024 * 1024, gt=0)
    analysis_timeout_seconds: float = Field(default=300, gt=0, le=300)
    analysis_max_response_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    artifact_files_path: Path = Path("/data/artifacts")
    chat_enabled: bool = False
    chat_model: str = ""
    chat_base_url: str = ""
    chat_api_key: SecretStr = SecretStr("")
    chat_credentials_configured: bool = False
    chat_max_turns: int = Field(default=8, ge=1, le=20)
    chat_model_timeout_seconds: float = Field(default=45, gt=0, le=55)
    chat_max_conversation_bytes: int = Field(default=524288, ge=16384, le=1048576)

    @field_validator("chat_credentials_configured", mode="before")
    @classmethod
    def empty_credentials_flag(cls, value):
        return False if value is None or value == "" else value

    @property
    def chat_api_ready(self) -> bool:
        return bool(
            self.chat_enabled
            and self.chat_model.strip()
            and self.chat_base_url.strip()
            and (self.chat_credentials_configured or self.chat_api_key.get_secret_value())
        )

    def validate_chat_worker_config(self) -> bool:
        """Явно включённый чат требует полного конфига перед запуском Worker."""
        if not self.chat_enabled:
            return False
        missing = [
            name
            for name, value in (
                ("CHAT_MODEL", self.chat_model.strip()),
                ("CHAT_BASE_URL", self.chat_base_url.strip()),
                ("CHAT_API_KEY", self.chat_api_key.get_secret_value()),
            )
            if not value
        ]
        if missing:
            raise ValueError("Неполная настройка чата: " + ", ".join(missing))
        return True


@lru_cache
def get_settings() -> Settings:
    return Settings()
