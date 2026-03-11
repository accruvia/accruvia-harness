from __future__ import annotations

import json
import logging
import time
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _sanitize_attributes(attributes: dict[str, Any]) -> dict[str, str | int | float | bool]:
    sanitized: dict[str, str | int | float | bool] = {}
    for key, value in attributes.items():
        if value is None:
            continue
        if isinstance(value, (str, int, float, bool)):
            sanitized[key] = value
        else:
            sanitized[key] = str(value)
    return sanitized


def _otlp_signal_endpoints(endpoint: str | None) -> tuple[str | None, str | None]:
    if not endpoint:
        return (None, None)
    trimmed = endpoint.rstrip("/")
    if trimmed.endswith("/v1/traces"):
        return (trimmed, trimmed.removesuffix("/v1/traces") + "/v1/metrics")
    if trimmed.endswith("/v1/metrics"):
        return (trimmed.removesuffix("/v1/metrics") + "/v1/traces", trimmed)
    return (trimmed + "/v1/traces", trimmed + "/v1/metrics")


@dataclass(slots=True)
class TelemetrySink:
    root: Path
    service_name: str = "accruvia-harness"
    otlp_endpoint: str | None = None
    metrics_path: Path = field(init=False)
    spans_path: Path = field(init=False)
    warnings_path: Path = field(init=False)
    _otel: "_OpenTelemetryBridge | None" = field(init=False, default=None)
    otel_status: str = field(init=False, default="disabled")
    otel_warning: str | None = field(init=False, default=None)
    _append_lock: threading.Lock = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.metrics_path = self.root / "metrics.jsonl"
        self.spans_path = self.root / "spans.jsonl"
        self.warnings_path = self.root / "warnings.jsonl"
        self._append_lock = threading.Lock()
        self._otel, self.otel_status, self.otel_warning = _OpenTelemetryBridge.build(
            service_name=self.service_name,
            endpoint=self.otlp_endpoint,
        )
        if self.otel_warning:
            self.warn("otel_setup", self.otel_warning, endpoint=self.otlp_endpoint)

    def metric(self, name: str, value: float, metric_type: str = "counter", **attributes: Any) -> None:
        sanitized = _sanitize_attributes(attributes)
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "type": "metric",
            "name": name,
            "metric_type": metric_type,
            "value": value,
            "attributes": sanitized,
        }
        self._append(self.metrics_path, payload)
        if self._otel is not None:
            try:
                self._otel.metric(name, value, metric_type=metric_type, attributes=sanitized)
            except Exception as exc:
                self.warn("otel_metric_export", str(exc), metric=name, metric_type=metric_type)

    def span(self, name: str, duration_ms: float | None = None, **attributes: Any) -> None:
        sanitized = _sanitize_attributes(attributes)
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "type": "span",
            "name": name,
            "attributes": sanitized,
        }
        if duration_ms is not None:
            payload["duration_ms"] = duration_ms
        self._append(self.spans_path, payload)
        if self._otel is not None:
            try:
                self._otel.span(name, duration_ms=duration_ms, attributes=sanitized)
            except Exception as exc:
                self.warn("otel_span_export", str(exc), span=name)

    def timed(self, name: str, **attributes: Any):
        return _TimedSpan(self, name, attributes)

    def summary(self) -> dict[str, object]:
        metrics = self.load_metrics()
        spans = self.load_spans()
        warnings = self.load_warnings()
        metric_totals: dict[str, float] = {}
        metric_series: dict[str, list[float]] = {}
        cost_totals = {
            "cost_usd": 0.0,
            "prompt_tokens": 0.0,
            "completion_tokens": 0.0,
            "total_tokens": 0.0,
        }
        for item in metrics:
            name = str(item["name"])
            value = float(item["value"])
            metric_totals[name] = metric_totals.get(name, 0.0) + value
            metric_series.setdefault(name, []).append(value)
            if name in {"llm_cost_usd", "llm_prompt_tokens", "llm_completion_tokens", "llm_total_tokens"}:
                key = name.removeprefix("llm_")
                cost_totals[key] = cost_totals.get(key, 0.0) + value
        span_counts: dict[str, int] = {}
        span_durations: dict[str, list[float]] = {}
        for item in spans:
            name = str(item["name"])
            span_counts[name] = span_counts.get(name, 0) + 1
            if "duration_ms" in item:
                span_durations.setdefault(name, []).append(float(item["duration_ms"]))
        span_average_ms = {
            name: (sum(values) / len(values)) for name, values in span_durations.items() if values
        }
        return {
            "service_name": self.service_name,
            "otlp_endpoint": self.otlp_endpoint,
            "otlp_trace_endpoint": self._otel.trace_endpoint if self._otel is not None else None,
            "otlp_metric_endpoint": self._otel.metric_endpoint if self._otel is not None else None,
            "otel_enabled": self._otel is not None,
            "otel_status": self.otel_status,
            "otel_warning": self.otel_warning,
            "metrics_path": str(self.metrics_path),
            "spans_path": str(self.spans_path),
            "warnings_path": str(self.warnings_path),
            "warnings": warnings[-20:],
            "metric_totals": metric_totals,
            "metric_averages": {
                name: (sum(values) / len(values)) for name, values in metric_series.items() if values
            },
            "span_counts": span_counts,
            "span_average_ms": span_average_ms,
            "cost_totals": cost_totals,
            "dashboard": {
                "slowest_operations_ms": sorted(
                    (
                        {"name": name, "avg_ms": avg}
                        for name, avg in span_average_ms.items()
                    ),
                    key=lambda item: item["avg_ms"],
                    reverse=True,
                )[:10],
                "highest_volume_metrics": sorted(
                    (
                        {"name": name, "total": total}
                        for name, total in metric_totals.items()
                    ),
                    key=lambda item: item["total"],
                    reverse=True,
                )[:10],
                "llm_cost_usd": cost_totals["cost_usd"],
                "llm_total_tokens": int(cost_totals["total_tokens"]),
            },
        }

    def _append(self, path: Path, payload: dict[str, Any]) -> None:
        with self._append_lock:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, sort_keys=True) + "\n")

    def warn(self, category: str, message: str, **attributes: Any) -> None:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "type": "warning",
            "category": category,
            "message": message,
            "attributes": _sanitize_attributes(attributes),
        }
        self._append(self.warnings_path, payload)

    def _load(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        records: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                logger.warning("Skipping corrupt telemetry JSONL line in %s: %s", path, exc)
        return records

    def load_metrics(self) -> list[dict[str, Any]]:
        return self._load(self.metrics_path)

    def load_spans(self) -> list[dict[str, Any]]:
        return self._load(self.spans_path)

    def load_warnings(self) -> list[dict[str, Any]]:
        return self._load(self.warnings_path)


class _TimedSpan:
    def __init__(self, sink: TelemetrySink, name: str, attributes: dict[str, Any]) -> None:
        self.sink = sink
        self.name = name
        self.attributes = attributes
        self.started = 0.0

    def __enter__(self) -> "_TimedSpan":
        self.started = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        duration_ms = (time.perf_counter() - self.started) * 1000
        payload = dict(self.attributes)
        if exc_type is not None:
            payload["error"] = exc_type.__name__
        self.sink.span(self.name, duration_ms=duration_ms, **payload)
        self.sink.metric(f"{self.name}_duration_ms", duration_ms, metric_type="histogram", **payload)


class _OpenTelemetryBridge:
    def __init__(self, tracer, meter, trace_endpoint: str, metric_endpoint: str) -> None:
        self.tracer = tracer
        self.meter = meter
        self.trace_endpoint = trace_endpoint
        self.metric_endpoint = metric_endpoint
        self._counters: dict[str, Any] = {}
        self._histograms: dict[str, Any] = {}
        self._counter_types: dict[str, str] = {}

    @classmethod
    def build(
        cls, service_name: str, endpoint: str | None
    ) -> tuple["_OpenTelemetryBridge | None", str, str | None]:
        if not endpoint:
            return None, "disabled", None
        try:
            from opentelemetry import metrics, trace
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.metrics import MeterProvider
            from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
        except Exception as exc:
            return None, "error", f"OpenTelemetry import/setup failed: {exc}"
        trace_endpoint, metric_endpoint = _otlp_signal_endpoints(endpoint)
        if not trace_endpoint or not metric_endpoint:
            return None, "error", "Unable to derive OTLP trace and metric endpoints"
        resource = Resource.create({"service.name": service_name})
        trace_provider = TracerProvider(resource=resource)
        trace_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=trace_endpoint)))
        metric_reader = PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=metric_endpoint))
        meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
        trace.set_tracer_provider(trace_provider)
        metrics.set_meter_provider(meter_provider)
        return (
            cls(
                tracer=trace.get_tracer(service_name),
                meter=metrics.get_meter(service_name),
                trace_endpoint=trace_endpoint,
                metric_endpoint=metric_endpoint,
            ),
            "enabled",
            None,
        )

    def metric(
        self,
        name: str,
        value: float,
        metric_type: str,
        attributes: dict[str, str | int | float | bool],
    ) -> None:
        if metric_type == "histogram":
            instrument = self._histograms.get(name)
            if instrument is None:
                instrument = self.meter.create_histogram(name)
                self._histograms[name] = instrument
            instrument.record(value, attributes=attributes)
            return
        instrument = self._counters.get(name)
        if instrument is None:
            if value < 0:
                instrument = self.meter.create_up_down_counter(name)
                self._counter_types[name] = "up_down_counter"
            else:
                instrument = self.meter.create_counter(name)
                self._counter_types[name] = "counter"
            self._counters[name] = instrument
        if value < 0 and self._counter_types.get(name) == "counter":
            raise ValueError(f"Metric {name} was created as a monotonic counter and cannot accept negative values")
        if value < 0:
            instrument.add(value, attributes=attributes)
            return
        instrument.add(value, attributes=attributes)

    def span(
        self, name: str, duration_ms: float | None, attributes: dict[str, str | int | float | bool]
    ) -> None:
        with self.tracer.start_as_current_span(name) as span:
            for key, value in attributes.items():
                span.set_attribute(key, value)
            if duration_ms is not None:
                span.set_attribute("duration_ms", duration_ms)
