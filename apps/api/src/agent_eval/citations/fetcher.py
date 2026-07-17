import asyncio
import ipaddress
import socket
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Protocol, cast
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpcore
import httpx

from agent_eval.db.enums import CitationFetchStatus

REDIRECT_STATUSES = {301, 302, 303, 307, 308}
SUPPORTED_CONTENT_TYPES = {"text/html", "application/xhtml+xml", "text/plain"}
METADATA_ADDRESSES = {
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("100.100.100.200"),
}

Resolver = Callable[[str, int], Awaitable[list[str]]]


class BlockedUrlError(ValueError):
    pass


class ResolutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class ResolvedTarget:
    url: str
    host: str
    port: int
    addresses: list[str]


@dataclass(frozen=True)
class CitationFetchResult:
    status: CitationFetchStatus
    final_url: str | None
    http_status: int | None
    content: bytes | None
    reason: str


class _ClosableAsyncStream(Protocol):
    def __aiter__(self) -> AsyncIterator[bytes]: ...

    async def aclose(self) -> None: ...


class _CoreResponseStream(httpx.AsyncByteStream):
    def __init__(self, stream: _ClosableAsyncStream) -> None:
        self._stream = stream

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._stream:
            yield chunk

    async def aclose(self) -> None:
        await self._stream.aclose()


class _PinnedNetworkBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, expected_host: str, address: str) -> None:
        self._expected_host = expected_host
        self._address = address
        self._backend = httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        if host.rstrip(".").lower() != self._expected_host:
            raise httpcore.ConnectError("connection host differs from validated host")
        # Keep the hostname for Host/TLS SNI while connecting to the validated IP.
        return await self._backend.connect_tcp(
            self._address,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectError("Unix sockets are not allowed for citation fetching")

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


class _PinnedTransport(httpx.AsyncBaseTransport):
    def __init__(self, host: str, address: str) -> None:
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpcore.default_ssl_context(),
            max_connections=1,
            max_keepalive_connections=0,
            network_backend=_PinnedNetworkBackend(host, address),
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        response = await self._pool.handle_async_request(core_request)
        return httpx.Response(
            status_code=response.status,
            headers=response.headers,
            stream=_CoreResponseStream(cast(_ClosableAsyncStream, response.stream)),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self._pool.aclose()


async def resolve_addresses(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    try:
        results = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        raise ResolutionError("dns_resolution_failed") from error
    addresses = list(dict.fromkeys(result[4][0] for result in results))
    if not addresses:
        raise ResolutionError("dns_no_addresses")
    return addresses


async def resolve_public_target(
    url: str,
    *,
    resolver: Resolver = resolve_addresses,
) -> ResolvedTarget:
    normalized_url, host, port = parse_citation_url(url)
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        addresses = await resolver(host, port)
    else:
        addresses = [str(literal)]
    if not addresses:
        raise ResolutionError("dns_no_addresses")
    for address in addresses:
        try:
            parsed_address = ipaddress.ip_address(address)
        except ValueError as error:
            raise ResolutionError("dns_invalid_address") from error
        if not is_public_address(parsed_address):
            raise BlockedUrlError("target_address_not_public")
    return ResolvedTarget(normalized_url, host, port, list(dict.fromkeys(addresses)))


def parse_citation_url(url: str) -> tuple[str, str, int]:
    if not url or any(ord(character) < 32 for character in url) or "\\" in url:
        raise BlockedUrlError("invalid_url")
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise BlockedUrlError("invalid_url") from error
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise BlockedUrlError("unsupported_scheme")
    if parsed.username is not None or parsed.password is not None:
        raise BlockedUrlError("embedded_credentials")
    if host is None or not parsed.netloc:
        raise BlockedUrlError("missing_host")
    if "%" in host:
        raise BlockedUrlError("invalid_host")
    try:
        ascii_host = host.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError as error:
        raise BlockedUrlError("invalid_host") from error
    expected_port = 80 if scheme == "http" else 443
    if port is not None and port != expected_port:
        raise BlockedUrlError("nonstandard_port")
    authority = f"[{ascii_host}]" if ":" in ascii_host else ascii_host
    path = parsed.path or "/"
    normalized = urlunsplit((scheme, authority, path, parsed.query, ""))
    return normalized, ascii_host, expected_port


def is_public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if address in METADATA_ADDRESSES:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return is_public_address(address.ipv4_mapped)
    return address.is_global and not address.is_multicast and not address.is_unspecified


async def fetch_citation(
    url: str,
    *,
    connect_timeout: float = 5.0,
    read_timeout: float = 15.0,
    max_redirects: int = 3,
    max_response_bytes: int = 2_097_152,
    resolver: Resolver = resolve_addresses,
    transport: httpx.AsyncBaseTransport | None = None,
) -> CitationFetchResult:
    current_url = url
    for redirect_count in range(max_redirects + 1):
        try:
            target = await asyncio.wait_for(
                resolve_public_target(current_url, resolver=resolver),
                timeout=connect_timeout,
            )
        except BlockedUrlError as error:
            return CitationFetchResult(
                CitationFetchStatus.BLOCKED, current_url, None, None, str(error)
            )
        except ResolutionError as error:
            return CitationFetchResult(
                CitationFetchStatus.UNREACHABLE, current_url, None, None, str(error)
            )
        except TimeoutError:
            return CitationFetchResult(
                CitationFetchStatus.UNREACHABLE,
                current_url,
                None,
                None,
                "dns_timeout",
            )

        request_transport = transport or _PinnedTransport(target.host, target.addresses[0])
        timeout = httpx.Timeout(read_timeout, connect=connect_timeout)
        try:
            async with (
                httpx.AsyncClient(
                transport=request_transport,
                follow_redirects=False,
                timeout=timeout,
                trust_env=False,
                ) as client,
                client.stream(
                    "GET",
                    target.url,
                    headers={
                        "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9",
                        "User-Agent": "AgentEvalCitationFetcher/0.1",
                    },
                ) as response,
            ):
                status_code = response.status_code
                if status_code in REDIRECT_STATUSES:
                    location = response.headers.get("Location")
                    if not location:
                        return CitationFetchResult(
                            CitationFetchStatus.UNSUPPORTED,
                            target.url,
                            status_code,
                            None,
                            "redirect_missing_location",
                        )
                    if redirect_count >= max_redirects:
                        return CitationFetchResult(
                            CitationFetchStatus.BLOCKED,
                            target.url,
                            status_code,
                            None,
                            "too_many_redirects",
                        )
                    current_url = urljoin(target.url, location)
                    continue
                if not 200 <= status_code <= 299:
                    return CitationFetchResult(
                        CitationFetchStatus.UNREACHABLE,
                        target.url,
                        status_code,
                        None,
                        "http_error",
                    )
                content_type = response.headers.get("Content-Type", "")
                media_type = content_type.split(";", 1)[0].strip().lower()
                if media_type not in SUPPORTED_CONTENT_TYPES:
                    return CitationFetchResult(
                        CitationFetchStatus.UNSUPPORTED,
                        target.url,
                        status_code,
                        None,
                        "unsupported_content_type",
                    )
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    remaining = max_response_bytes + 1 - len(content)
                    content.extend(chunk[:remaining])
                    if len(chunk) > remaining or len(content) > max_response_bytes:
                        return CitationFetchResult(
                            CitationFetchStatus.UNSUPPORTED,
                            target.url,
                            status_code,
                            None,
                            "response_too_large",
                        )
                return CitationFetchResult(
                    CitationFetchStatus.FETCHED,
                    target.url,
                    status_code,
                    bytes(content),
                    "fetched",
                )
        except (
            httpx.HTTPError,
            httpcore.NetworkError,
            httpcore.ProtocolError,
            httpcore.TimeoutException,
            OSError,
        ):
            return CitationFetchResult(
                CitationFetchStatus.UNREACHABLE,
                target.url,
                None,
                None,
                "network_unreachable",
            )
    raise AssertionError("redirect loop exceeded configured limit")
