"""Offline-safe defaults for the deterministic test suite."""

import pytest


@pytest.fixture(autouse=True)
def use_offline_safe_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Never spend external search or model quota during normal pytest runs."""

    monkeypatch.setenv("ANALYSIS_MODE", "deterministic")
    monkeypatch.setenv("INTENT_MODE", "deterministic")
    monkeypatch.setenv("SERVICE_LOG_DIR", str(tmp_path / "service_logs"))
    monkeypatch.delenv("ANALYSIS_LLM_ENABLE_THINKING", raising=False)
    monkeypatch.delenv("ANALYSIS_LLM_RESEARCH_MAX_OUTPUT_TOKENS", raising=False)
    monkeypatch.setenv("MODEL_API_KEY", "")
    monkeypatch.setenv("RESEARCH_PROVIDER", "mock")
    monkeypatch.setenv("TAVILY_API_KEY", "")
    monkeypatch.setenv("CONTENT_FETCH_PROVIDER", "disabled")
    monkeypatch.setenv("FACT_VERIFIER", "rules")
    monkeypatch.delenv("FACT_VERIFIER_ENABLE_THINKING", raising=False)
