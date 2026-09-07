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
        protected_namespaces=("settings_",),
    )

    model_base_url: str = "https://api.openai.com/v1"
    model_name: str = ""
    model_api_key: SecretStr = SecretStr("")
    analysis_mode: str = Field(
        default="deterministic", pattern=r"^(deterministic|llm)$"
    )
    analysis_model: str = ""
    analysis_llm_enable_thinking: bool | None = None
    analysis_llm_thinking_ttft_seconds: float = Field(default=30.0, gt=0, le=120)
    analysis_llm_thinking_budget_tokens: int = Field(default=800, ge=100, le=8_000)
    analysis_llm_process_summary_max_chars: int = Field(default=400, ge=80, le=2_000)
    analysis_llm_timeout_seconds: float = Field(default=60.0, gt=0, le=300)
    analysis_llm_max_retry: int = Field(default=1, ge=0, le=5)
    analysis_llm_max_input_chars: int = Field(default=30_000, ge=1_000, le=200_000)
    analysis_llm_max_output_tokens: int = Field(default=1_200, ge=100, le=8_000)
    analysis_llm_research_max_output_tokens: int = Field(
        default=2_400, ge=100, le=8_000
    )
    checkpoint_db_path: Path = Path("checkpoints/credra_agent.db")
    max_retry: int = Field(default=2, ge=0, le=10)
    research_fail_first: bool = False
    research_provider: str = Field(default="tavily", pattern=r"^[a-z][a-z0-9_-]*$")
    tavily_api_key: SecretStr = SecretStr("")
    tavily_base_url: str = "https://api.tavily.com"
    search_depth: str = Field(default="basic", pattern=r"^(basic|advanced)$")
    search_max_results: int = Field(default=5, ge=1, le=20)
    search_min_relevance_score: float = Field(default=0.5, ge=0.0, le=1.0)
    search_timeout_seconds: float = Field(default=15.0, gt=0, le=120)
    search_time_range: str = Field(default="year", pattern=r"^(|day|week|month|year)$")
    search_snapshot_dir: Path = Path("data/search_snapshots")
    search_save_snapshots: bool = True
    content_fetch_provider: str = Field(
        default="auto", pattern=r"^(auto|http|snapshot|disabled)$"
    )
    search_content_snapshot_dir: Path = Path("data/content_snapshots")
    search_fetch_timeout_seconds: float = Field(default=15.0, gt=0, le=120)
    search_fetch_max_bytes: int = Field(default=5_000_000, ge=1_024, le=50_000_000)
    search_fetch_max_text_chars: int = Field(default=2_000_000, ge=1_000, le=10_000_000)
    search_fetch_max_redirects: int = Field(default=3, ge=0, le=10)
    search_fetch_max_concurrency: int = Field(default=3, ge=1, le=10)
    search_fetch_max_candidates: int = Field(default=10, ge=1, le=50)
    search_fetch_max_pdf_pages: int = Field(default=200, ge=1, le=2_000)
    fact_verifier: str = Field(default="rules", pattern=r"^(rules|llm|disabled)$")
    fact_verifier_model: str = ""
    fact_verifier_enable_thinking: bool | None = None
    fact_verifier_min_confidence: float = Field(default=0.75, ge=0.0, le=1.0)
    fact_verifier_max_candidates: int = Field(default=10, ge=1, le=50)
    fact_verifier_max_input_chars: int = Field(default=30_000, ge=1_000, le=200_000)
    fact_verifier_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    fact_verifier_max_retry: int = Field(default=1, ge=0, le=5)
    verification_snapshot_dir: Path = Path("data/verification_snapshots")
    debt_ratio_threshold: float = Field(default=0.15, gt=0, le=1)
    cashflow_threshold: float = Field(default=0.0, ge=-1, le=1)
    revenue_threshold: float = Field(default=0.20, ge=-1, le=10)
    data_dir: Path = Path("data")
    trace_dir: Path = Path("traces")


def get_settings() -> Settings:
    return Settings()
