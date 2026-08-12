"""Environment-backed application configuration."""

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Validated configuration with safe local-development defaults."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    model_base_url: str = "https://api.openai.com/v1"
    model_name: str = ""
    model_api_key: SecretStr = SecretStr("")
    checkpoint_db_path: Path = Path("checkpoints/credra_agent.db")
    max_retry: int = Field(default=2, ge=0, le=10)
    research_fail_first: bool = False
    debt_ratio_threshold: float = Field(default=0.15, gt=0, le=1)
    cashflow_threshold: float = Field(default=0.0, ge=-1, le=1)
    revenue_threshold: float = Field(default=0.20, ge=-1, le=10)
    data_dir: Path = Path("data")
    trace_dir: Path = Path("traces")


def get_settings() -> Settings:
    return Settings()
