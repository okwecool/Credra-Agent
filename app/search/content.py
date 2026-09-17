"""Controlled retrieval and extraction of untrusted HTML/PDF evidence."""

from __future__ import annotations

import hashlib
import ipaddress
import re
import socket
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from typing import Protocol, runtime_checkable
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from pypdf import PdfReader

from app.config import Settings
from app.models.content import (
    ContentFetchStatus,
    ContentSegment,
    FetchedContentReference,
    FetchedDocument,
    FetchErrorCode,
)
from app.models.search import ResearchEvidence
from credra_agent.observability.instrumentation import source_call

_ALLOWED_CONTENT_TYPES = {
    "text/html": "HTML",
    "application/xhtml+xml": "HTML",
    "application/pdf": "PDF",
}
_REDIRECT_CODES = {301, 302, 303, 307, 308}
_BLOCK_TAGS = {
    "address",
    "article",
    "blockquote",
    "br",
    "dd",
    "div",
    "dl",
    "dt",
    "figcaption",
    "footer",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "li",
    "main",
    "p",
    "section",
    "td",
    "th",
    "tr",
}
_IGNORED_TAGS = {"script", "style", "noscript", "template", "svg"}
_PUBLISHED_META_KEYS = {
    "article:published_time",
    "date",
    "datepublished",
    "pubdate",
    "publishdate",
}
_PROMPT_INJECTION_PATTERNS = {
    "IGNORE_PREVIOUS_INSTRUCTIONS": re.compile(
        r"ignore\s+(all\s+)?previous\s+instructions|忽略.{0,8}(之前|以上).{0,8}(指令|要求)",
        re.IGNORECASE,
    ),
    "SYSTEM_PROMPT_REFERENCE": re.compile(
        r"system\s+prompt|developer\s+message|系统提示词|开发者消息",
        re.IGNORECASE,
    ),
    "TOOL_CALL_INSTRUCTION": re.compile(
        r"call\s+(the\s+)?tool|execute\s+(this\s+)?command|调用.{0,8}工具|执行.{0,8}命令",
        re.IGNORECASE,
    ),
}
_ACCESS_WALL_PATTERNS = (
    "登录后查看",
    "请先登录",
    "sign in to continue",
    "log in to continue",
    "subscribe to continue",
)


class ContentFetchError(RuntimeError):
    """One safe, categorized content retrieval failure."""

    def __init__(self, code: FetchErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@runtime_checkable
class ContentFetcher(Protocol):
    name: str

    def fetch(self, url: str) -> FetchedDocument: ...


HostResolver = Callable[[str], Iterable[str]]


def _default_resolver(hostname: str) -> Iterable[str]:
    try:
        return {
            item[4][0]
            for item in socket.getaddrinfo(
                hostname,
                None,
                type=socket.SOCK_STREAM,
            )
        }
    except OSError as exc:
        raise ContentFetchError(
            "DNS_ERROR", "URL hostname could not be resolved"
        ) from exc


def normalize_public_url(url: str, resolver: HostResolver = _default_resolver) -> str:
    """Normalize and reject URLs that could address local or non-public services."""

    try:
        parsed = urlsplit(url.strip())
    except ValueError as exc:
        raise ContentFetchError("INVALID_URL", "URL cannot be parsed") from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ContentFetchError("INVALID_URL", "only HTTP and HTTPS URLs are allowed")
    if not parsed.hostname or parsed.username or parsed.password:
        raise ContentFetchError("INVALID_URL", "URL must contain a public hostname")
    try:
        parsed_port = parsed.port
    except ValueError as exc:
        raise ContentFetchError("INVALID_URL", "URL port is invalid") from exc
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".local"):
        raise ContentFetchError("PRIVATE_ADDRESS", "local hostnames are not allowed")
    try:
        literal_address = ipaddress.ip_address(hostname)
    except ValueError:
        addresses = list(resolver(hostname))
        if not addresses:
            raise ContentFetchError(
                "DNS_ERROR", "URL hostname resolved to no addresses"
            )
    else:
        addresses = [str(literal_address)]
    for address in addresses:
        try:
            parsed_address = ipaddress.ip_address(address)
        except ValueError as exc:
            raise ContentFetchError(
                "DNS_ERROR", "resolver returned an invalid address"
            ) from exc
        if not parsed_address.is_global:
            raise ContentFetchError(
                "PRIVATE_ADDRESS", "URL hostname resolves to a non-public address"
            )
    port = f":{parsed_port}" if parsed_port else ""
    host_for_netloc = f"[{hostname}]" if ":" in hostname else hostname
    netloc = f"{host_for_netloc}{port}"
    return urlunsplit(
        (parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, "")
    )


def _normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


class _HTMLExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.published_at: str | None = None
        self._ignored_depth = 0
        self._in_title = False
        self._buffer: list[str] = []
        self.paragraphs: list[str] = []

    def _flush(self) -> None:
        paragraph = _normalize_space(" ".join(self._buffer))
        self._buffer.clear()
        if paragraph and (not self.paragraphs or self.paragraphs[-1] != paragraph):
            self.paragraphs.append(paragraph)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in _IGNORED_TAGS:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if tag == "title":
            self._in_title = True
        if tag == "meta":
            attributes = {key.lower(): value for key, value in attrs if value}
            key = (attributes.get("property") or attributes.get("name") or "").lower()
            if key in _PUBLISHED_META_KEYS and not self.published_at:
                self.published_at = attributes.get("content")
        if tag in _BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _IGNORED_TAGS:
            self._ignored_depth = max(0, self._ignored_depth - 1)
            return
        if self._ignored_depth:
            return
        if tag == "title":
            self._in_title = False
        if tag in _BLOCK_TAGS:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        text = _normalize_space(data)
        if not text:
            return
        if self._in_title:
            self.title_parts.append(text)
        else:
            self._buffer.append(text)

    def close(self) -> None:
        super().close()
        self._flush()


def _document_hash(segments: list[ContentSegment]) -> str:
    text = "\n".join(segment.text for segment in segments)
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def _detect_prompt_injection(text: str) -> list[str]:
    return [
        name
        for name, pattern in _PROMPT_INJECTION_PATTERNS.items()
        if pattern.search(text)
    ]


def _decode_html(payload: bytes, content_type: str) -> str:
    match = re.search(r"charset=([^;\s]+)", content_type, re.IGNORECASE)
    encoding = match.group(1).strip("\"'") if match else "utf-8"
    try:
        return payload.decode(encoding, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _extract_html(
    payload: bytes,
    content_type: str,
    *,
    max_text_chars: int,
) -> tuple[str | None, str | None, list[ContentSegment], list[str]]:
    parser = _HTMLExtractor()
    try:
        parser.feed(_decode_html(payload, content_type))
        parser.close()
    except Exception as exc:
        raise ContentFetchError(
            "PARSE_ERROR", "HTML content could not be parsed"
        ) from exc
    segments = [
        ContentSegment(location=f"paragraph:{index}", text=paragraph)
        for index, paragraph in enumerate(parser.paragraphs, start=1)
    ]
    char_count = sum(len(segment.text) for segment in segments)
    if not segments:
        raise ContentFetchError(
            "EMPTY_CONTENT", "HTML page contains no extractable text"
        )
    if char_count > max_text_chars:
        raise ContentFetchError(
            "RESPONSE_TOO_LARGE", "extracted HTML text exceeds limit"
        )
    combined = "\n".join(segment.text for segment in segments).casefold()
    if char_count < 1000 and any(
        pattern in combined for pattern in _ACCESS_WALL_PATTERNS
    ):
        raise ContentFetchError("ACCESS_BLOCKED", "HTML page appears to require login")
    title = _normalize_space(" ".join(parser.title_parts)) or None
    return title, parser.published_at, segments, _detect_prompt_injection(combined)


def _extract_pdf(
    payload: bytes,
    *,
    max_pages: int,
    max_text_chars: int,
) -> tuple[str | None, str | None, list[ContentSegment], list[str]]:
    try:
        reader = PdfReader(BytesIO(payload), strict=False)
        if reader.is_encrypted and reader.decrypt("") == 0:
            raise ContentFetchError("ACCESS_BLOCKED", "PDF is encrypted")
        if len(reader.pages) > max_pages:
            raise ContentFetchError(
                "RESPONSE_TOO_LARGE", "PDF page count exceeds limit"
            )
        segments: list[ContentSegment] = []
        for index, page in enumerate(reader.pages, start=1):
            text = _normalize_space(page.extract_text() or "")
            if text:
                segments.append(ContentSegment(location=f"page:{index}", text=text))
    except ContentFetchError:
        raise
    except Exception as exc:
        raise ContentFetchError(
            "PARSE_ERROR", "PDF content could not be parsed"
        ) from exc
    char_count = sum(len(segment.text) for segment in segments)
    if not segments:
        raise ContentFetchError("EMPTY_CONTENT", "PDF contains no extractable text")
    if char_count > max_text_chars:
        raise ContentFetchError(
            "RESPONSE_TOO_LARGE", "extracted PDF text exceeds limit"
        )
    metadata = reader.metadata or {}
    title = _normalize_space(str(metadata.get("/Title") or "")) or None
    combined = "\n".join(segment.text for segment in segments)
    return title, None, segments, _detect_prompt_injection(combined)


class ContentSnapshotStore:
    """Filesystem cache keyed by the normalized requested URL."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    @staticmethod
    def key(url: str) -> str:
        return hashlib.sha256(url.encode("utf-8")).hexdigest()

    @classmethod
    def snapshot_id(cls, url: str) -> str:
        return f"content:{cls.key(url)}"

    def path_for(self, url: str) -> Path:
        return self.directory / f"{self.key(url)}.json"

    def write(self, document: FetchedDocument) -> str:
        path = self.path_for(document.requested_url)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(document.model_dump_json(indent=2), encoding="utf-8")
        return self.snapshot_id(document.requested_url)

    def read(self, url: str) -> FetchedDocument:
        path = self.path_for(url)
        if not path.is_file():
            raise ContentFetchError("SNAPSHOT_MISSING", "content snapshot is missing")
        return FetchedDocument.model_validate_json(path.read_text(encoding="utf-8"))


class HTTPContentFetcher:
    name = "http"

    def __init__(
        self,
        *,
        timeout_seconds: float,
        max_bytes: int,
        max_redirects: int,
        max_pdf_pages: int,
        max_text_chars: int,
        snapshot_store: ContentSnapshotStore,
        client: httpx.Client | None = None,
        resolver: HostResolver = _default_resolver,
        restrict_redirect_host: bool = False,
    ) -> None:
        self._max_bytes = max_bytes
        self._max_redirects = max_redirects
        self._max_pdf_pages = max_pdf_pages
        self._max_text_chars = max_text_chars
        self._snapshot_store = snapshot_store
        self._client = client or httpx.Client(timeout=timeout_seconds)
        self._resolver = resolver
        self._restrict_redirect_host = restrict_redirect_host

    def _failure(
        self,
        url: str,
        error: ContentFetchError,
        *,
        final_url: str | None = None,
    ) -> FetchedDocument:
        return FetchedDocument(
            requested_url=url,
            final_url=final_url,
            status="FAILED",
            error_code=error.code,
            error_message=str(error),
        )

    @source_call
    def fetch(self, url: str) -> FetchedDocument:
        current_url: str | None = None
        try:
            current_url = normalize_public_url(url, self._resolver)
            for redirect_count in range(self._max_redirects + 1):
                with self._client.stream(
                    "GET",
                    current_url,
                    follow_redirects=False,
                    headers={
                        "Accept": "text/html,application/xhtml+xml,application/pdf",
                        "User-Agent": "Credra-Agent/1.0 content-fetcher",
                    },
                ) as response:
                    if response.status_code in _REDIRECT_CODES:
                        location = response.headers.get("location")
                        if not location:
                            raise ContentFetchError(
                                "HTTP_ERROR", "redirect response has no location"
                            )
                        if redirect_count >= self._max_redirects:
                            raise ContentFetchError(
                                "TOO_MANY_REDIRECTS", "redirect limit exceeded"
                            )
                        redirect_url = normalize_public_url(
                            urljoin(current_url, location), self._resolver
                        )
                        if (
                            self._restrict_redirect_host
                            and urlsplit(redirect_url).hostname
                            != urlsplit(current_url).hostname
                        ):
                            raise ContentFetchError(
                                "INVALID_URL",
                                "agent source scope forbids cross-host redirects",
                            )
                        if (
                            urlsplit(current_url).scheme == "https"
                            and urlsplit(redirect_url).scheme == "http"
                        ):
                            raise ContentFetchError(
                                "INVALID_URL",
                                "HTTPS redirects cannot downgrade to HTTP",
                            )
                        current_url = redirect_url
                        continue
                    if response.status_code in {401, 403}:
                        raise ContentFetchError(
                            "ACCESS_BLOCKED",
                            f"content access returned HTTP {response.status_code}",
                        )
                    if response.is_error:
                        raise ContentFetchError(
                            "HTTP_ERROR",
                            f"content request returned HTTP {response.status_code}",
                        )
                    content_type_header = response.headers.get("content-type", "")
                    content_type = content_type_header.split(";", maxsplit=1)[0].lower()
                    content_kind = _ALLOWED_CONTENT_TYPES.get(content_type)
                    if content_kind is None:
                        raise ContentFetchError(
                            "UNSUPPORTED_CONTENT_TYPE",
                            f"unsupported content type: {content_type or 'missing'}",
                        )
                    declared_length = response.headers.get("content-length")
                    if declared_length:
                        try:
                            declared_size = int(declared_length)
                        except ValueError as exc:
                            raise ContentFetchError(
                                "HTTP_ERROR", "response content length is invalid"
                            ) from exc
                        if declared_size < 0:
                            raise ContentFetchError(
                                "HTTP_ERROR", "response content length is invalid"
                            )
                        if declared_size > self._max_bytes:
                            raise ContentFetchError(
                                "RESPONSE_TOO_LARGE",
                                "declared response size exceeds limit",
                            )
                    chunks: list[bytes] = []
                    byte_size = 0
                    for chunk in response.iter_bytes():
                        byte_size += len(chunk)
                        if byte_size > self._max_bytes:
                            raise ContentFetchError(
                                "RESPONSE_TOO_LARGE", "response body exceeds limit"
                            )
                        chunks.append(chunk)
                payload = b"".join(chunks)
                if content_kind == "HTML":
                    title, published_at, segments, signals = _extract_html(
                        payload,
                        content_type_header,
                        max_text_chars=self._max_text_chars,
                    )
                else:
                    title, published_at, segments, signals = _extract_pdf(
                        payload,
                        max_pages=self._max_pdf_pages,
                        max_text_chars=self._max_text_chars,
                    )
                document = FetchedDocument(
                    requested_url=url,
                    final_url=current_url,
                    status="SUCCESS",
                    content_kind=content_kind,
                    content_type=content_type,
                    title=title,
                    published_at=published_at,
                    byte_size=byte_size,
                    char_count=sum(len(segment.text) for segment in segments),
                    document_hash=_document_hash(segments),
                    segments=segments,
                    prompt_injection_signals=signals,
                )
                self._snapshot_store.write(document)
                return document
            raise AssertionError("redirect loop exited unexpectedly")
        except ContentFetchError as exc:
            return self._failure(url, exc, final_url=current_url)
        except httpx.TimeoutException:
            return self._failure(
                url,
                ContentFetchError("TIMEOUT", "content request timed out"),
                final_url=current_url,
            )
        except httpx.RequestError:
            return self._failure(
                url,
                ContentFetchError("NETWORK_ERROR", "content request failed"),
                final_url=current_url,
            )
        except (OSError, ValueError):
            return self._failure(
                url,
                ContentFetchError("INTERNAL_ERROR", "unexpected content fetch failure"),
                final_url=current_url,
            )


class SnapshotContentFetcher:
    name = "snapshot"

    def __init__(self, store: ContentSnapshotStore) -> None:
        self._store = store

    @source_call
    def fetch(self, url: str) -> FetchedDocument:
        try:
            return self._store.read(url)
        except ContentFetchError as exc:
            return FetchedDocument(
                requested_url=url,
                status="FAILED",
                error_code=exc.code,
                error_message=str(exc),
            )


def build_content_fetcher(
    settings: Settings, *, restrict_redirect_host: bool = False
) -> ContentFetcher | None:
    provider = settings.content_fetch_provider.lower()
    if provider == "auto":
        provider = {
            "tavily": "http",
            "snapshot": "snapshot",
            "mock": "disabled",
        }.get(settings.research_provider.lower(), "disabled")
    store = ContentSnapshotStore(settings.search_content_snapshot_dir)
    if provider == "disabled":
        return None
    if provider == "snapshot":
        return SnapshotContentFetcher(store)
    if provider == "http":
        return HTTPContentFetcher(
            timeout_seconds=settings.search_fetch_timeout_seconds,
            max_bytes=settings.search_fetch_max_bytes,
            max_redirects=settings.search_fetch_max_redirects,
            max_pdf_pages=settings.search_fetch_max_pdf_pages,
            max_text_chars=settings.search_fetch_max_text_chars,
            snapshot_store=store,
            restrict_redirect_host=restrict_redirect_host,
        )
    raise ValueError(f"unknown content fetch provider: {provider}")


def _reference(
    document: FetchedDocument,
    snapshot_store: ContentSnapshotStore | None,
) -> FetchedContentReference:
    snapshot_id = (
        snapshot_store.snapshot_id(document.requested_url)
        if snapshot_store is not None and document.status == "SUCCESS"
        else None
    )
    return FetchedContentReference(
        status=document.status,
        requested_url=document.requested_url,
        final_url=document.final_url,
        content_kind=document.content_kind,
        content_type=document.content_type,
        title=document.title,
        published_at=document.published_at,
        fetched_at=document.fetched_at,
        byte_size=document.byte_size,
        char_count=document.char_count,
        document_hash=document.document_hash,
        locations=[segment.location for segment in document.segments],
        snapshot_id=snapshot_id,
        untrusted_input=document.untrusted_input,
        prompt_injection_signals=document.prompt_injection_signals,
        error_code=document.error_code,
        error_message=document.error_message,
    )


def fetch_candidate_content(
    evidence: list[ResearchEvidence],
    *,
    fetcher: ContentFetcher | None,
    snapshot_store: ContentSnapshotStore,
    max_candidates: int,
    max_concurrency: int,
) -> tuple[list[ResearchEvidence], ContentFetchStatus]:
    """Attach small fetch references while keeping full text in ignored snapshots."""

    candidates = [
        item
        for item in evidence
        if item.evidence_stage == "CANDIDATE" and item.source_url
    ]
    if not candidates:
        return evidence, "NOT_NEEDED"
    if fetcher is None:
        return evidence, "DISABLED"

    urls: list[str] = []
    for item in candidates:
        if item.source_url not in urls:
            urls.append(item.source_url)
    selected = urls[:max_candidates]
    skipped = set(urls[max_candidates:])
    with ThreadPoolExecutor(max_workers=max_concurrency) as executor:
        futures = [
            executor.submit(copy_context().run, fetcher.fetch, url) for url in selected
        ]
        documents = dict(
            zip(selected, (future.result() for future in futures), strict=True)
        )
    for document in documents.values():
        if document.status == "SUCCESS":
            snapshot_store.write(document)

    updated: list[ResearchEvidence] = []
    for item in evidence:
        if item.evidence_stage != "CANDIDATE" or not item.source_url:
            updated.append(item)
        elif item.source_url in skipped:
            skipped_document = FetchedDocument(
                requested_url=item.source_url,
                status="SKIPPED",
                error_code="BUDGET_EXCEEDED",
                error_message="content fetch candidate budget exceeded",
            )
            updated.append(
                item.model_copy(
                    update={"fetched_content": _reference(skipped_document, None)}
                )
            )
        else:
            document = documents[item.source_url]
            updated.append(
                item.model_copy(
                    update={"fetched_content": _reference(document, snapshot_store)}
                )
            )

    statuses = [
        item.fetched_content.status
        for item in updated
        if item.fetched_content is not None
    ]
    successes = statuses.count("SUCCESS")
    if successes == len(statuses):
        status: ContentFetchStatus = "COMPLETE"
    elif successes:
        status = "PARTIAL"
    else:
        status = "FAILED"
    return updated, status
