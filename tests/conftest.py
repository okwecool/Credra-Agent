"""Offline-safe defaults for the deterministic test suite."""

import pytest


@pytest.fixture(autouse=True)
def use_mock_research_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never spend external search quota during normal pytest runs."""

    monkeypatch.setenv("RESEARCH_PROVIDER", "mock")
