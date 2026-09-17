"""Explicit, quota-consuming Tavily smoke test; not collected by pytest."""

import os

from app.config import get_settings
from app.search.providers import build_search_provider
from app.search.queries import build_search_requests


def main() -> None:
    if os.getenv("RUN_LIVE_SEARCH_SMOKE") != "1":
        raise SystemExit("Set RUN_LIVE_SEARCH_SMOKE=1 to authorize a live API call")
    settings = get_settings()
    if settings.research_provider != "tavily":
        raise SystemExit("Set RESEARCH_PROVIDER=tavily for the live smoke test")
    request = build_search_requests("company", "比亚迪股份有限公司")[1]
    response = build_search_provider(settings).search(request)
    print(f"provider={response.provider}")
    print(f"query={request.query}")
    print(f"results={len(response.items)}")
    for item in response.items:
        print(f"url={item.url}")


if __name__ == "__main__":
    main()
