"""Langfuse distributed tracing for the worker, transported over OTLP/HTTP.

Tracing is opt-in: ``init_tracing`` only loads OpenTelemetry and creates a
TracerProvider (BatchSpanProcessor + OTLP HTTP exporter pointed at
``{LANGFUSE_HOST}/api/public/otel/v1/traces``, Basic auth from the
public/secret key pair) when all three LANGFUSE_* settings are present. When
unconfigured, ``trace_span`` yields ``None`` without importing OpenTelemetry,
so the disabled path has no measurable overhead and exports nothing.

Tracing must never break evaluation: exporter failures are logged once and
swallowed, and shutdown errors only produce a warning.

DATA BOUNDARY: spans must never carry question text, prompts, answers,
transcripts, or tool arguments — benchmark items are held-out and agent
replies may contain private data. Only record identifiers (run/item/question/
attempt IDs, task refs), content hashes, token usage, latencies, scores, and
classified error types. Attribute names follow the OpenTelemetry GenAI
conventions (``gen_ai.*``, ``error.type``) where they map naturally; platform
dimensions use the ``agent_eval.*`` prefix.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from agent_eval.core.settings import Settings

if TYPE_CHECKING:
    from agent_eval.agents.inference import AttemptCompletion, FinishAttempt

logger = logging.getLogger(__name__)

SpanAttributes = dict[str, str | int | float | bool | None]

# Own references instead of the OTel global provider: the API process never
# initializes tracing, tests can re-initialize freely, and span context still
# propagates through contextvars (including across asyncio tasks).
_TRACER: Any = None
_PROVIDER: Any = None
_exporter_warning_emitted = False


def tracing_enabled() -> bool:
    return _TRACER is not None


def init_tracing(settings: Settings, *, exporter: Any = None) -> bool:
    """Initialize the tracer provider once; returns whether tracing is active.

    ``exporter`` injects a custom span exporter (e.g. InMemorySpanExporter in
    tests) backed by a SimpleSpanProcessor instead of the batched OTLP one.
    """
    global _TRACER, _PROVIDER
    if _TRACER is not None:
        return True
    if not settings.langfuse_enabled:
        return False
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor

    provider = TracerProvider(
        resource=Resource.create({"service.name": "agent-eval-worker"})
    )
    if exporter is not None:
        provider.add_span_processor(SimpleSpanProcessor(_guarded_exporter(exporter)))
    else:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )

        host = settings.langfuse_host
        public_key = settings.langfuse_public_key
        secret_key = settings.langfuse_secret_key
        assert host is not None and public_key is not None and secret_key is not None
        credentials = base64.b64encode(
            f"{public_key}:{secret_key.get_secret_value()}".encode()
        ).decode()
        otlp_exporter = OTLPSpanExporter(
            endpoint=f"{host.rstrip('/')}/api/public/otel/v1/traces",
            headers={"Authorization": f"Basic {credentials}"},
        )
        provider.add_span_processor(BatchSpanProcessor(_guarded_exporter(otlp_exporter)))
    _PROVIDER = provider
    _TRACER = provider.get_tracer("agent_eval.worker")
    return True


def shutdown_tracing() -> None:
    """Flush pending spans and release the provider; safe to call when disabled."""
    global _TRACER, _PROVIDER
    provider = _PROVIDER
    _TRACER = None
    _PROVIDER = None
    if provider is None:
        return
    try:
        provider.shutdown()
    except Exception as error:
        logger.warning(
            "tracing_shutdown_failed", extra={"error_type": type(error).__name__}
        )


def _reset_for_tests() -> None:
    global _exporter_warning_emitted
    shutdown_tracing()
    _exporter_warning_emitted = False


def _guarded_exporter(exporter: Any) -> Any:
    """Wrap a span exporter so export failures warn once and never propagate."""

    class GuardedExporter:
        def export(self, spans: Any) -> Any:
            global _exporter_warning_emitted
            try:
                return exporter.export(spans)
            except Exception as error:
                if not _exporter_warning_emitted:
                    _exporter_warning_emitted = True
                    logger.warning(
                        "tracing_exporter_failed",
                        extra={"error_type": type(error).__name__},
                    )
                from opentelemetry.sdk.trace.export import SpanExportResult

                return SpanExportResult.FAILURE

        def shutdown(self) -> None:
            try:
                exporter.shutdown()
            except Exception as error:
                logger.warning(
                    "tracing_exporter_shutdown_failed",
                    extra={"error_type": type(error).__name__},
                )

        def force_flush(self, timeout_millis: int = 30000) -> bool:
            try:
                return bool(exporter.force_flush(timeout_millis))
            except Exception:
                return False

    return GuardedExporter()


@contextmanager
def trace_span(name: str, *, attributes: SpanAttributes | None = None) -> Iterator[Any]:
    """Start a child span of the current context; yields None when disabled.

    Exceptions inside the block are recorded on the span (status ERROR) by the
    SDK and re-raised unchanged.
    """
    tracer = _TRACER
    if tracer is None:
        yield None
        return
    with tracer.start_as_current_span(
        name, attributes=_clean_attributes(attributes)
    ) as span:
        yield span


def set_attributes(span: Any, attributes: SpanAttributes) -> None:
    """Set attributes on a span yielded by trace_span; no-op for None."""
    if span is None:
        return
    for key, value in _clean_attributes(attributes).items():
        span.set_attribute(key, value)


def set_current_span_attributes(attributes: SpanAttributes) -> None:
    """Set attributes on whatever span is active in the current context."""
    if _TRACER is None:
        return
    from opentelemetry import trace

    set_attributes(trace.get_current_span(), attributes)


def _clean_attributes(attributes: SpanAttributes | None) -> dict[str, Any]:
    if not attributes:
        return {}
    return {key: value for key, value in attributes.items() if value is not None}


def endpoint_host(url: str) -> str:
    """Redact an agent endpoint to its host for the gen_ai.system attribute."""
    return urlparse(url).netloc or "unknown"


def track_finish_attempt(
    finish: FinishAttempt,
) -> tuple[FinishAttempt, list[AttemptCompletion]]:
    """Wrap a finish-attempt callback, keeping completions for span attributes."""
    completions: list[AttemptCompletion] = []

    async def tracked(completion: AttemptCompletion) -> None:
        completions.append(completion)
        await finish(completion)

    return tracked, completions


def completion_usage_attributes(
    completions: list[AttemptCompletion],
) -> dict[str, int]:
    """Token usage from the most recent completion that reported any."""
    for completion in reversed(completions):
        attributes: dict[str, int] = {}
        if completion.input_tokens is not None:
            attributes["gen_ai.usage.input_tokens"] = completion.input_tokens
        if completion.output_tokens is not None:
            attributes["gen_ai.usage.output_tokens"] = completion.output_tokens
        if completion.total_tokens is not None:
            attributes["gen_ai.usage.total_tokens"] = completion.total_tokens
        if attributes:
            return attributes
    return {}
