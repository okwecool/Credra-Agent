"""Configuration validation tests."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_settings_have_safe_local_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANALYSIS_MODE", raising=False)
    monkeypatch.delenv("ANALYSIS_LLM_ENABLE_THINKING", raising=False)
    monkeypatch.delenv("MODEL_BASE_URL", raising=False)
    monkeypatch.delenv("MODEL_NAME", raising=False)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.delenv("RESEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("CONTENT_FETCH_PROVIDER", raising=False)
    monkeypatch.delenv("FACT_VERIFIER", raising=False)
    settings = Settings(_env_file=None)

    assert settings.model_base_url == "https://api.openai.com/v1"
    assert settings.analysis_mode == "deterministic"
    assert settings.analysis_llm_enable_thinking is None
    assert settings.analysis_llm_max_retry == 1
    assert settings.analysis_llm_max_input_chars == 30_000
    assert settings.analysis_llm_max_output_tokens == 1_200
    assert settings.checkpoint_db_path == Path("checkpoints/credra_agent.db")
    assert settings.max_retry == 2
    assert settings.research_fail_first is False
    assert settings.content_fetch_provider == "auto"
    assert settings.search_fetch_max_bytes == 5_000_000
    assert settings.search_fetch_max_concurrency == 3
    assert settings.fact_verifier == "rules"
    assert settings.fact_verifier_min_confidence == 0.75
    assert settings.fact_verifier_max_candidates == 10
    assert settings.verification_snapshot_dir == Path("data/verification_snapshots")
    assert settings.data_dir.name == "data"
    assert settings.trace_dir.name == "traces"


def test_model_connection_settings_load_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MODEL_BASE_URL", "https://llm.example.test/v1")
    monkeypatch.setenv("MODEL_NAME", "example-model")
    monkeypatch.setenv("MODEL_API_KEY", "test-key")

    settings = Settings(_env_file=None)

    assert settings.model_base_url == "https://llm.example.test/v1"
    assert settings.model_name == "example-model"
    assert settings.model_api_key.get_secret_value() == "test-key"


def test_analysis_thinking_mode_can_be_disabled_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANALYSIS_LLM_ENABLE_THINKING", "false")

    settings = Settings(_env_file=None)

    assert settings.analysis_llm_enable_thinking is False


def test_settings_reject_invalid_thresholds_and_retry_count() -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            max_retry=-1,
            debt_ratio_threshold=2,
            revenue_threshold=-2,
            search_min_relevance_score=1.1,
            search_fetch_max_concurrency=0,
            fact_verifier_min_confidence=1.1,
            fact_verifier_max_candidates=0,
            analysis_llm_max_output_tokens=10,
        )
