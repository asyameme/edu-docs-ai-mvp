from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
