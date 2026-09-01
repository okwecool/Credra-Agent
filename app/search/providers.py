"""Search provider registry with Mock, Snapshot, and Tavily implementations."""

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, runtime_checkable

import httpx

from app.config import Settings
from app.models.search import SearchItem, SearchRequest, SearchResponse


class SearchProviderError(RuntimeError):
    """Base error for all provider failures crossing the MCP boundary."""


class SearchConfigurationError(SearchProviderError):
    """The selected provider is missing or is not configured."""


class SearchAuthenticationError(SearchProviderError):
    """The provider rejected its credentials."""


class SearchRateLimitError(SearchProviderError):
    """The provider rate limit was reached."""


class SearchQuotaError(SearchProviderError):
    """The provider account has insufficient quota."""


class SearchTimeoutError(SearchProviderError):
    """The provider request timed out."""


@runtime_checkable
class SearchProvider(Protocol):
    name: str

    def search(self, request: SearchRequest) -> SearchResponse: ...


ProviderFactory = Callable[[Settings], SearchProvider]
_PROVIDER_FACTORIES: dict[str, ProviderFactory] = {}


def register_search_provider(
    name: str,
    factory: ProviderFactory,
    *,
    replace: bool = False,
) -> None:
    """Register a provider factory without coupling MCP tools to its class."""

    normalized = name.strip().lower()
    if not normalized:
        raise ValueError("provider name cannot be empty")
    if normalized in _PROVIDER_FACTORIES and not replace:
        raise ValueError(f"search provider already registered: {normalized}")
    _PROVIDER_FACTORIES[normalized] = factory


def build_search_provider(settings: Settings) -> SearchProvider:
    name = settings.research_provider.strip().lower()
    try:
        factory = _PROVIDER_FACTORIES[name]
    except KeyError as exc:
        choices = ", ".join(sorted(_PROVIDER_FACTORIES))
        raise SearchConfigurationError(
            f"unknown research provider '{name}'; available: {choices}"
        ) from exc
    return factory(settings)


class SnapshotStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    @staticmethod
    def key(request: SearchRequest) -> str:
        canonical = request.model_dump_json()
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def path_for(self, request: SearchRequest) -> Path:
        return self.directory / f"{self.key(request)}.json"

    def write(self, response: SearchResponse) -> Path:
        path = self.path_for(response.request)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            response.model_dump_json(indent=2),
            encoding="utf-8",
        )
        return path

    def read(self, request: SearchRequest) -> SearchResponse:
        path = self.path_for(request)
        if not path.is_file():
            raise SearchProviderError(f"search snapshot not found: {path}")
        return SearchResponse.model_validate_json(path.read_text(encoding="utf-8"))


class MockSearchProvider:
    name = "mock"

    def __init__(self, dataset_path: Path) -> None:
        self.dataset_path = dataset_path

    def search(self, request: SearchRequest) -> SearchResponse:
        with self.dataset_path.open(encoding="utf-8") as file:
            dataset = json.load(file)
        collection = "companies" if request.query_type == "company" else "industries"
        facts = dataset[collection].get(request.subject, [])
        items = [
            SearchItem(
                title=fact["statement"],
                content=fact["statement"],
                source_id=fact["source_id"],
                category=fact["category"],
                score=1.0,
            )
            for fact in facts
        ]
        return SearchResponse(provider=self.name, request=request, items=items)


class SnapshotSearchProvider:
    name = "snapshot"

    def __init__(self, store: SnapshotStore) -> None:
        self.store = store

    def search(self, request: SearchRequest) -> SearchResponse:
        response = self.store.read(request)
        return response.model_copy(update={"provider": self.name})


class TavilySearchProvider:
    name = "tavily"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        search_depth: str,
        max_results: int,
        timeout_seconds: float,
        time_range: str | None,
        snapshot_store: SnapshotStore | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key.strip():
            raise SearchConfigurationError(
                "TAVILY_API_KEY is required when RESEARCH_PROVIDER=tavily"
            )
        self._api_key = api_key
        self._endpoint = f"{base_url.rstrip('/')}/search"
        self._search_depth = search_depth
        self._max_results = max_results
        self._time_range = time_range
        self._snapshot_store = snapshot_store
        self._client = client or httpx.Client(timeout=timeout_seconds)

    def search(self, request: SearchRequest) -> SearchResponse:
        payload: dict[str, object] = {
            "query": request.query,
            "topic": "general",
            "search_depth": self._search_depth,
            "max_results": self._max_results,
            "include_answer": False,
            "include_raw_content": False,
        }
        if self._time_range:
            payload["time_range"] = self._time_range
        try:
            response = self._client.post(
                self._endpoint,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=payload,
            )
        except httpx.TimeoutException as exc:
            raise SearchTimeoutError("Tavily search timed out") from exc
        except httpx.RequestError as exc:
            raise SearchProviderError(f"Tavily network error: {exc}") from exc

        if response.status_code == 401:
            raise SearchAuthenticationError("Tavily authentication failed")
        if response.status_code == 429:
            raise SearchRateLimitError("Tavily rate limit reached")
        if response.status_code in (432, 433):
            raise SearchQuotaError("Tavily quota is unavailable or exhausted")
        if response.is_error:
            raise SearchProviderError(
                f"Tavily search failed with HTTP {response.status_code}"
            )

        body = response.json()
        items = [
            SearchItem(
                title=item.get("title") or "Untitled search result",
                url=item.get("url"),
                content=item.get("content") or item.get("title") or "No summary",
                score=float(item.get("score") or 0.0),
                published_at=item.get("published_date") or item.get("published_at"),
                category=request.category,
            )
            for item in body.get("results", [])
        ]
        result = SearchResponse(provider=self.name, request=request, items=items)
        if self._snapshot_store is not None:
            self._snapshot_store.write(result)
        return result


def _mock_factory(settings: Settings) -> SearchProvider:
    del settings
    return MockSearchProvider(
        Path(__file__).resolve().parents[1] / "mcp" / "mock_research.json"
    )


def _snapshot_factory(settings: Settings) -> SearchProvider:
    return SnapshotSearchProvider(SnapshotStore(settings.search_snapshot_dir))


def _tavily_factory(settings: Settings) -> SearchProvider:
    snapshot_store = (
        SnapshotStore(settings.search_snapshot_dir)
        if settings.search_save_snapshots
        else None
    )
    return TavilySearchProvider(
        api_key=settings.tavily_api_key.get_secret_value(),
        base_url=settings.tavily_base_url,
        search_depth=settings.search_depth,
        max_results=settings.search_max_results,
        timeout_seconds=settings.search_timeout_seconds,
        time_range=settings.search_time_range or None,
        snapshot_store=snapshot_store,
    )


register_search_provider("mock", _mock_factory)
register_search_provider("snapshot", _snapshot_factory)
register_search_provider("tavily", _tavily_factory)
