import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from cryptography.exceptions import InvalidTag
from pydantic import SecretStr

from agent_eval.agents.client import ConnectionConfig
from agent_eval.agents.client import test_connection as run_connection_test
from agent_eval.agents.mapping import (
    ResponseMappingError,
    TemplateValidationError,
    extract_answer,
    extract_citations,
    render_request_template,
)
from agent_eval.auth.encryption import TokenCipher, generate_encryption_key
from agent_eval.db.enums import AuthType


class FakeAgentHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        content_length = int(self.headers["Content-Length"])
        request_body = json.loads(self.rfile.read(content_length))
        if self.path == "/query":
            response_body = {
                "answer": f"first: {request_body['query']}",
                "citations": ["https://example.com/first"],
            }
        elif self.path == "/chat" and self.headers.get("Authorization") == "Bearer live-secret":
            response_body = {
                "data": {
                    "text": f"second: {request_body['messages'][0]['content']}",
                    "links": ["https://example.com/second"],
                }
            }
        else:
            self.send_error(401)
            return

        content = json.dumps(response_body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format: str, *args: object) -> None:
        pass


def test_token_cipher_round_trip_and_rejects_invalid_payloads() -> None:
    cipher = TokenCipher.from_secret(SecretStr(generate_encryption_key()))

    encrypted = cipher.encrypt("agent-secret")

    assert encrypted.startswith("v1:")
    assert "agent-secret" not in encrypted
    assert cipher.decrypt(encrypted) == "agent-secret"

    with pytest.raises(ValueError, match="URL-safe Base64"):
        cipher.decrypt("v1:not valid!")
    with pytest.raises(ValueError, match="too short"):
        cipher.decrypt("v1:" + base64.urlsafe_b64encode(b"short").decode("ascii"))

    tampered = encrypted[:-2] + ("A=" if encrypted[-2] != "A" else "B=")
    with pytest.raises(InvalidTag):
        cipher.decrypt(tampered)


def test_request_template_replaces_prompt_as_data() -> None:
    body = render_request_template(
        '{"input":{"query":"prefix {{prompt}}"},"count":1}',
        'quoted "value" and {{other}}',
    )

    assert body == {
        "input": {"query": 'prefix quoted "value" and {{other}}'},
        "count": 1,
    }

    with pytest.raises(TemplateValidationError, match="must contain"):
        render_request_template('{"query":"fixed"}', "prompt")
    with pytest.raises(TemplateValidationError, match="unsupported variable"):
        render_request_template('{"query":"{{prompt}} {{token}}"}', "prompt")
    with pytest.raises(TemplateValidationError, match="JSON key"):
        render_request_template('{"{{prompt}}":"value"}', "prompt")


def test_response_mapping_extracts_answer_and_unique_http_citations() -> None:
    payload = {
        "result": {
            "answer": 42,
            "sources": [
                "https://example.com/a",
                "invalid",
                "https://example.com/a",
                "http://example.com/b",
            ],
        }
    }

    assert extract_answer(payload, "$.result.answer") == "42"
    assert extract_citations(payload, "$.result.sources") == [
        "https://example.com/a",
        "http://example.com/b",
    ]
    with pytest.raises(ResponseMappingError, match="exactly one"):
        extract_answer(payload, "$.missing")


def test_response_mapping_falls_back_to_distinct_inline_http_urls() -> None:
    answer = (
        "Sources: https://example.com/a, (https://example.com/path_(one)). "
        "Duplicate https://example.com/a and invalid https://example.com:bad/x. "
        "Third https://example.org/final?q=1! ignored https://fourth.example/x"
    )

    assert extract_citations({}, None, answer=answer) == [
        "https://example.com/a",
        "https://example.com/path_(one)",
        "https://example.org/final?q=1",
    ]
    assert extract_citations(
        {"sources": ["https://structured.example/source"]},
        "$.sources",
        answer="https://inline.example/ignored",
    ) == ["https://structured.example/source"]
    assert extract_citations({}, "$.missing", answer="ordinary uncited answer") == []


async def test_connection_sends_bearer_request_and_maps_response() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.headers["Authorization"] == "Bearer agent-secret"
        assert json.loads(request.content) == {"query": "测试问题"}
        return httpx.Response(
            200,
            json={
                "data": {
                    "answer": " 测试答案 ",
                    "sources": ["https://example.com/source"],
                    "debug": "received agent-secret",
                }
            },
        )

    result = await run_connection_test(
        ConnectionConfig(
            endpoint_url="https://agent.example/run",
            auth_type=AuthType.BEARER,
            token="agent-secret",
            request_template='{"query":"{{prompt}}"}',
            answer_jsonpath="$.data.answer",
            citations_jsonpath="$.data.sources",
            timeout_seconds=10,
        ),
        "测试问题",
        transport=httpx.MockTransport(handler),
    )

    assert result.success is True
    assert result.status_code == 200
    assert result.answer == "测试答案"
    assert result.citations == ["https://example.com/source"]
    assert "agent-secret" not in json.dumps(result.raw_response)
    assert "[REDACTED]" in json.dumps(result.raw_response)
    assert result.error is None


async def test_connection_rejects_oversized_response() -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * 20))

    result = await run_connection_test(
        ConnectionConfig(
            endpoint_url="https://agent.example/run",
            auth_type=AuthType.NONE,
            token=None,
            request_template='{"query":"{{prompt}}"}',
            answer_jsonpath="$.answer",
            citations_jsonpath=None,
            timeout_seconds=10,
        ),
        "prompt",
        transport=transport,
        max_response_bytes=10,
    )

    assert result.success is False
    assert result.error == "response exceeds 1 MB limit"
    assert result.raw_response is None


async def test_connection_does_not_return_transport_exception_details() -> None:
    secret = "transport-secret-that-must-not-leak"

    async def failed(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(secret, request=request)

    result = await run_connection_test(
        ConnectionConfig(
            endpoint_url="https://agent.example/run",
            auth_type=AuthType.BEARER,
            token="agent-secret",
            request_template='{"query":"{{prompt}}"}',
            answer_jsonpath="$.answer",
            citations_jsonpath=None,
            timeout_seconds=10,
        ),
        "prompt",
        transport=httpx.MockTransport(failed),
    )

    assert result.success is False
    assert result.error == "agent_network_error"
    assert secret not in json.dumps(result.__dict__)


async def test_two_fake_agent_formats_over_real_http() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeAgentHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"

    try:
        first = await run_connection_test(
            ConnectionConfig(
                endpoint_url=f"{base_url}/query",
                auth_type=AuthType.NONE,
                token=None,
                request_template='{"query":"{{prompt}}"}',
                answer_jsonpath="$.answer",
                citations_jsonpath="$.citations",
                timeout_seconds=5,
            ),
            "alpha",
        )
        second = await run_connection_test(
            ConnectionConfig(
                endpoint_url=f"{base_url}/chat",
                auth_type=AuthType.BEARER,
                token="live-secret",
                request_template='{"messages":[{"content":"{{prompt}}"}]}',
                answer_jsonpath="$.data.text",
                citations_jsonpath="$.data.links",
                timeout_seconds=5,
            ),
            "beta",
        )
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)

    assert first.answer == "first: alpha"
    assert first.citations == ["https://example.com/first"]
    assert second.answer == "second: beta"
    assert second.citations == ["https://example.com/second"]
