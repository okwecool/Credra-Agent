"""Configuration validation tests."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_settings_have_safe_local_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODEL_BASE_URL", raising=False)
    monkeypatch.delenv("MODEL_NAME", raising=False)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    settings = Settings(_env_file=None)

    assert settings.model_base_url == "https://api.openai.com/v1"
    assert settings.checkpoint_db_path == Path("checkpoints/credra_agent.db")
    assert settings.max_retry == 2
    assert settings.research_fail_first is False
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


def test_settings_reject_invalid_thresholds_and_retry_count() -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            max_retry=-1,
            debt_ratio_threshold=2,
            revenue_threshold=-2,
        )
