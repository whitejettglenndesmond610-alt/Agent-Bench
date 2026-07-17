import socket

import httpx
import pytest

from agent_eval.citations.fetcher import (
    BlockedUrlError,
    ResolutionError,
    fetch_citation,
    parse_citation_url,
)
from agent_eval.db.enums import CitationFetchStatus


async def public_resolver(host: str, port: int) -> list[str]:
    assert port in {80, 443}
    if host == "private.example":
        return ["10.0.0.8"]
    return ["93.184.216.34"]


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/file",
        "https://user:secret@example.com/",
        "https://example.com:8443/",
        "http://127.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://example.com\\@127.0.0.1/",
    ],
)
async def test_fetcher_blocks_unsafe_urls_before_network_access(url: str) -> None:
    requests = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, headers={"Content-Type": "text/html"}, text="unsafe")

    result = await fetch_citation(
        url,
        resolver=public_resolver,
        transport=httpx.MockTransport(handler),
    )

    assert result.status == CitationFetchStatus.BLOCKED
    assert result.content is None
    assert requests == 0


async def test_fetcher_rejects_hostname_when_any_dns_address_is_private() -> None:
    async def mixed_resolver(_: str, __: int) -> list[str]:
        return ["93.184.216.34", "192.168.1.10"]

    result = await fetch_citation(
        "https://example.com/",
        resolver=mixed_resolver,
        transport=httpx.MockTransport(lambda _: httpx.Response(500)),
    )

    assert result.status == CitationFetchStatus.BLOCKED
    assert result.reason == "target_address_not_public"


@pytest.mark.parametrize("address", ["224.0.0.1", "239.255.255.250", "ff02::1"])
async def test_fetcher_rejects_multicast_addresses(address: str) -> None:
    async def multicast_resolver(_: str, __: int) -> list[str]:
        return [address]

    result = await fetch_citation(
        "https://multicast.example/",
        resolver=multicast_resolver,
        transport=httpx.MockTransport(lambda _: httpx.Response(500)),
    )

    assert result.status == CitationFetchStatus.BLOCKED
    assert result.reason == "target_address_not_public"


async def test_fetcher_revalidates_every_redirect_and_blocks_private_target() -> None:
    requests: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://private.example/secret"})

    result = await fetch_citation(
        "https://public.example/start",
        resolver=public_resolver,
        transport=httpx.MockTransport(handler),
    )

    assert result.status == CitationFetchStatus.BLOCKED
    assert result.reason == "target_address_not_public"
    assert requests == ["https://public.example/start"]


async def test_fetcher_streams_supported_content_with_size_limit() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["User-Agent"] == "AgentEvalCitationFetcher/0.1"
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html; charset=utf-8"},
            content=b"<html>valid source</html>",
        )

    fetched = await fetch_citation(
        "https://example.com/source#fragment",
        resolver=public_resolver,
        transport=httpx.MockTransport(handler),
    )
    oversized = await fetch_citation(
        "https://example.com/large",
        max_response_bytes=5,
        resolver=public_resolver,
        transport=httpx.MockTransport(handler),
    )

    assert fetched.status == CitationFetchStatus.FETCHED
    assert fetched.final_url == "https://example.com/source"
    assert fetched.content == b"<html>valid source</html>"
    assert oversized.status == CitationFetchStatus.UNSUPPORTED
    assert oversized.reason == "response_too_large"
    assert oversized.content is None


async def test_fetcher_classifies_dns_network_and_content_failures() -> None:
    async def failed_resolver(_: str, __: int) -> list[str]:
        raise ResolutionError("dns_resolution_failed")

    dns_failure = await fetch_citation("https://missing.example/", resolver=failed_resolver)
    network_failure = await fetch_citation(
        "https://example.com/",
        resolver=public_resolver,
        transport=httpx.MockTransport(lambda _: (_ for _ in ()).throw(httpx.ConnectError("x"))),
    )
    unsupported = await fetch_citation(
        "https://example.com/file.pdf",
        resolver=public_resolver,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, headers={"Content-Type": "application/pdf"})
        ),
    )

    assert dns_failure.status == CitationFetchStatus.UNREACHABLE
    assert network_failure.status == CitationFetchStatus.UNREACHABLE
    assert network_failure.reason == "network_unreachable"
    assert unsupported.status == CitationFetchStatus.UNSUPPORTED


def test_url_parser_normalizes_standard_ports_and_rejects_malformed_values() -> None:
    assert parse_citation_url("HTTPS://Example.COM:443/path?q=1#x") == (
        "https://example.com/path?q=1",
        "example.com",
        443,
    )
    with pytest.raises(BlockedUrlError, match="invalid_url"):
        parse_citation_url("https://example.com:invalid/")


async def test_default_resolver_wraps_dns_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail_getaddrinfo(*_: object, **__: object) -> list[object]:
        raise socket.gaierror("missing")

    loop = __import__("asyncio").get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", fail_getaddrinfo)
    result = await fetch_citation("https://missing.example/")
    assert result.status == CitationFetchStatus.UNREACHABLE
    assert result.reason == "dns_resolution_failed"
