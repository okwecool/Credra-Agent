"""Explicit live HTML/PDF fetch smoke; never collected or run by default."""

import os

from app.config import get_settings
from app.search.content import build_content_fetcher


def main() -> None:
    if os.getenv("RUN_LIVE_CONTENT_FETCH_SMOKE") != "1":
        raise SystemExit(
            "Set RUN_LIVE_CONTENT_FETCH_SMOKE=1 to authorize one live URL fetch"
        )
    url = os.getenv("LIVE_CONTENT_FETCH_URL", "").strip()
    if not url:
        raise SystemExit("Set LIVE_CONTENT_FETCH_URL to one public HTML or PDF URL")
    settings = get_settings()
    fetcher = build_content_fetcher(settings)
    if fetcher is None or fetcher.name != "http":
        raise SystemExit("Set CONTENT_FETCH_PROVIDER=http for the live smoke test")
    result = fetcher.fetch(url)
    print(f"provider={fetcher.name}")
    print(f"status={result.status}")
    print(f"final_url={result.final_url}")
    print(f"content_kind={result.content_kind}")
    print(f"byte_size={result.byte_size}")
    print(f"char_count={result.char_count}")
    print(f"locations={len(result.segments)}")
    print(f"document_hash={result.document_hash}")
    print(f"error_code={result.error_code}")
    if result.status != "SUCCESS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
