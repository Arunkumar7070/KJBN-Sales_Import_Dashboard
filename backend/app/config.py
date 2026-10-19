from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    app_env: str = "development"
    max_upload_size_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    max_files_per_upload: int = Field(default=10, gt=0)
    upload_rate_limit_per_minute: int = Field(default=10, gt=0)
    # Honour the X-Test-Clock header (lets a caller move "now"; never in production).
    allow_test_clock: bool = False

    @field_validator("database_url")
    @classmethod
    def use_psycopg3_driver(cls, url: str) -> str:
        # Neon hands out plain "postgresql://" URLs, which SQLAlchemy maps to
        # psycopg2. This project installs psycopg 3, so name that driver explicitly.
        for prefix in ("postgresql://", "postgres://"):
            if url.startswith(prefix):
                return "postgresql+psycopg://" + url.removeprefix(prefix)
        return url


@lru_cache
def get_settings() -> Settings:
    return Settings()
