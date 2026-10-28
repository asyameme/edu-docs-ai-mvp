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
    original_files_path: Path = Path("/data/originals")
    max_upload_bytes: int = Field(default=50 * 1024 * 1024, gt=0)
    parser_url: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
