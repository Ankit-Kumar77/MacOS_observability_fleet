#!/usr/bin/env python3
# Managed by Ansible (roles/observability_llm). Local edits are overwritten.
"""MLX-LM / MLX-VLM telemetry agent.

One long-running process, run as a per-user LaunchAgent, with up to five parts:

  proxy     An OpenAI-compatible reverse proxy in front of mlx_lm.server or
            mlx_vlm.server (both speak the same API). Every request is
            forwarded unchanged, images included (streams stay streamed); the
            proxy only observes timing, HTTP status and the "usage" (and, from
            MLX-VLM, "timings") blocks the server returns. Clients must be
            pointed at the proxy to be measured.
  health    Periodic checks of the server process, /health, /v1/models and an
            optional one-token inference probe, plus framework detection
            (inventory override, else the server answering on the port, else
            the process found).
  hardware  Readings macOS exposes to an unprivileged user: GPU utilisation and
            GPU memory (IORegistry), memory pressure level (sysctl) and thermal
            state (NSProcessInfo). Readings that need root (power, GPU
            frequency) are reported as unavailable, never estimated.
  quality   Optional text heuristics (repetition, refusal, hedging) computed in
            memory from completed responses. Heuristics, not fact checking.
  omlx      Optional KV-cache figures from an OMLX server's admin stats.

Metrics are exported as OTLP/HTTP JSON (cumulative temporality) to the local
OpenTelemetry Collector, which adds host.name and forwards them to
VictoriaMetrics with the rest of the host's metrics.

Privacy: prompts, completions and images are never logged, exported or
written to disk. Metric labels are limited to bounded values (model,
framework, endpoint, stream, outcome, status code, check name, cache tier).

Python 3.9+ standard library only, so it runs on macOS's /usr/bin/python3.
"""

import argparse
import ctypes
import ctypes.util
import http.client
import json
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.2.0"
LOG = logging.getLogger("mlx-telemetry")

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

FRAMEWORKS = ("mlx-lm", "mlx-vlm")
# Default server patterns. They only count when the process's executable is a
# Python interpreter or the server's own entry point (see _is_server_command),
# so editors, log tails and greps mentioning the module name do not match.
DEFAULT_LM_PATTERN = r"mlx_lm(\.server\b|\s+server\b|/server\.py\b)"
DEFAULT_VLM_PATTERN = r"mlx_vlm(\.server\b|\s+server\b|/server(/__main__)?\.py\b)"

DEFAULTS = {
    "upstream_host": "127.0.0.1",
    "upstream_port": 8080,
    "proxy_enabled": True,
    "proxy_host": "127.0.0.1",
    "proxy_port": 18080,
    "proxy_stream_usage": True,
    "proxy_connect_timeout_seconds": 5,
    "proxy_read_timeout_seconds": 600,
    "proxy_max_request_bytes": 33554432,
    "otlp_endpoint": "http://127.0.0.1:14318/v1/metrics",
    "export_interval_seconds": 15,
    "health_check_interval_seconds": 15,
    "models_check_interval_seconds": 60,
    "inference_probe_enabled": False,
    "inference_probe_interval_seconds": 300,
    "inference_probe_prompt": "ping",
    "hardware_metrics_enabled": True,
    "hardware_interval_seconds": 15,
    "quality_metrics_enabled": False,
    "quality_max_chars": 65536,
    "quality_repetition_threshold": 0.3,
    "quality_hedging_threshold": 2.0,
    "quality_risk_weights": {"repetition": 0.5, "hedging": 0.3, "refusal": 0.2},
    "model_name": "",
    "model_path": "",
    "model_max_context_tokens": 0,
    "model_label_max_values": 10,
    "hf_cache_dir": "",
    "mlx_launchagent_label": "",
    "mlx_process_pattern": DEFAULT_LM_PATTERN,
    "mlx_vlm_process_pattern": DEFAULT_VLM_PATTERN,
    "framework": "auto",
    "upstream_api_key_file": "",
    "omlx_enabled": False,
    "omlx_url": "http://127.0.0.1:8000",
    "omlx_api_key_file": "",
    "omlx_poll_interval_seconds": 30,
    "omlx_timeout_seconds": 5,
    "vlm_cache_stats_enabled": True,
    "activity_expected": False,
    "buckets": {
        "request_duration": [0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300],
        "time_to_first_token": [0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30],
        "generation_duration": [0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300],
        "tokens_per_second": [1, 5, 10, 20, 30, 50, 75, 100, 150, 200, 300],
        "tokens": [16, 64, 256, 512, 1024, 2048, 4096, 8192, 16384, 32768, 65536, 131072],
        "ratio": [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0],
        "temperature": [0, 0.1, 0.3, 0.5, 0.7, 0.9, 1.0, 1.2, 1.5, 2.0],
    },
}

_TYPES = {
    "upstream_host": str, "upstream_port": int, "proxy_enabled": bool,
    "proxy_host": str, "proxy_port": int, "proxy_stream_usage": bool,
    "proxy_connect_timeout_seconds": (int, float),
    "proxy_read_timeout_seconds": (int, float),
    "proxy_max_request_bytes": int, "otlp_endpoint": str,
    "export_interval_seconds": (int, float),
    "health_check_interval_seconds": (int, float),
    "models_check_interval_seconds": (int, float),
    "inference_probe_enabled": bool,
    "inference_probe_interval_seconds": (int, float),
    "inference_probe_prompt": str, "hardware_metrics_enabled": bool,
    "hardware_interval_seconds": (int, float),
    "quality_metrics_enabled": bool, "quality_max_chars": int,
    "quality_repetition_threshold": (int, float),
    "quality_hedging_threshold": (int, float),
    "quality_risk_weights": dict, "model_name": str, "model_path": str,
    "model_max_context_tokens": int, "model_label_max_values": int,
    "hf_cache_dir": str, "mlx_launchagent_label": str,
    "mlx_process_pattern": str, "mlx_vlm_process_pattern": str,
    "framework": str, "upstream_api_key_file": str, "omlx_enabled": bool,
    "omlx_url": str, "omlx_api_key_file": str,
    "omlx_poll_interval_seconds": (int, float), "omlx_timeout_seconds": (int, float),
    "vlm_cache_stats_enabled": bool,
    "activity_expected": bool, "buckets": dict,
}


def load_config(path):
    """Read and validate the JSON config; raise ValueError with every problem."""
    with open(path, encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict):
        raise ValueError("config must be a JSON object")
    problems = []
    unknown = sorted(set(raw) - set(DEFAULTS))
    if unknown:
        problems.append("unknown keys: " + ", ".join(unknown))
    cfg = json.loads(json.dumps(DEFAULTS))
    for key, value in raw.items():
        if key not in DEFAULTS:
            continue
        expected = _TYPES[key]
        # bool is a subclass of int; never accept it where a number is meant.
        if isinstance(value, bool) and expected is not bool:
            problems.append("%s must be %s, not a boolean" % (key, expected))
        elif not isinstance(value, expected):
            problems.append("%s has the wrong type (%s)" % (key, type(value).__name__))
        elif key == "buckets":
            cfg[key].update(value)
        else:
            cfg[key] = value
    for key in ("upstream_port", "proxy_port"):
        if not 0 < int(cfg[key]) < 65536:
            problems.append("%s must be a TCP port" % key)
    for key in ("export_interval_seconds", "health_check_interval_seconds",
                "models_check_interval_seconds", "inference_probe_interval_seconds",
                "hardware_interval_seconds", "proxy_read_timeout_seconds",
                "proxy_connect_timeout_seconds", "omlx_poll_interval_seconds",
                "omlx_timeout_seconds"):
        if cfg[key] <= 0:
            problems.append("%s must be positive" % key)
    if cfg["framework"] not in ("auto",) + FRAMEWORKS:
        problems.append("framework must be one of auto, %s" % ", ".join(FRAMEWORKS))
    if not cfg["omlx_url"].startswith(("http://", "https://")):
        problems.append("omlx_url must be an http:// or https:// URL")
    if cfg["proxy_enabled"] and (cfg["proxy_host"], cfg["proxy_port"]) == (cfg["upstream_host"], cfg["upstream_port"]):
        problems.append("the proxy cannot listen on the MLX-LM server's own address")
    if not cfg["otlp_endpoint"].startswith("http://"):
        problems.append("otlp_endpoint must be an http:// URL (loopback collector receiver)")
    for name, bounds in cfg["buckets"].items():
        if not (isinstance(bounds, list) and bounds and all(isinstance(b, (int, float)) and not isinstance(b, bool) for b in bounds)
                and bounds == sorted(set(bounds))):
            problems.append("buckets.%s must be a strictly increasing list of numbers" % name)
    for key in ("mlx_process_pattern", "mlx_vlm_process_pattern"):
        try:
            re.compile(cfg[key])
        except re.error as exc:
            problems.append("%s is not a valid regex: %s" % (key, exc))
    if problems:
        raise ValueError("; ".join(problems))
    return cfg


# --------------------------------------------------------------------------
# Metrics registry and OTLP/JSON encoding
# --------------------------------------------------------------------------

def _now_ns():
    return time.time_ns()


def _attrs(labels):
    return [{"key": k, "value": {"stringValue": str(v)}} for k, v in sorted(labels)]


class Counter:
    kind = "sum"

    def __init__(self, name, unit, description):
        self.name, self.unit, self.description = name, unit, description
        self._values = {}
        self._lock = threading.Lock()

    def add(self, amount=1, **labels):
        if amount < 0:
            return
        key = tuple(sorted(labels.items()))
        with self._lock:
            self._values[key] = self._values.get(key, 0) + amount

    def touch(self, **labels):
        """Export an explicit zero so 'no events yet' is distinguishable from 'no telemetry'."""
        self.add(0, **labels)

    def points(self, start_ns, now_ns):
        with self._lock:
            items = list(self._values.items())
        return [{"attributes": _attrs(k), "startTimeUnixNano": str(start_ns),
                 "timeUnixNano": str(now_ns), "asDouble": float(v)} for k, v in items]


class ObservedCounter(Counter):
    """A cumulative counter kept by another process (OMLX, MLX-VLM) and copied here.
    When the value drops, that process restarted, so the series gets a new
    start time and VictoriaMetrics treats it as a counter reset."""

    def __init__(self, name, unit, description):
        Counter.__init__(self, name, unit, description)
        self._starts = {}

    def observe_total(self, value, start_ns, **labels):
        if value is None or value < 0:
            return
        key = tuple(sorted(labels.items()))
        with self._lock:
            if key not in self._values or value < self._values[key]:
                self._starts[key] = start_ns
            self._values[key] = value

    def retain(self, source, models):
        """Drop this source's series whose model is not in models (unloaded
        models; an empty set drops them all). Other sources are untouched."""
        with self._lock:
            for key in [k for k in self._values
                        if dict(k).get("source") == source and dict(k).get("model") not in models]:
                del self._values[key]
                self._starts.pop(key, None)

    def points(self, start_ns, now_ns):
        with self._lock:
            items = [(k, v, self._starts.get(k, start_ns)) for k, v in self._values.items()]
        return [{"attributes": _attrs(k), "startTimeUnixNano": str(s),
                 "timeUnixNano": str(now_ns), "asDouble": float(v)} for k, v, s in items]


class Gauge:
    kind = "gauge"

    def __init__(self, name, unit, description):
        self.name, self.unit, self.description = name, unit, description
        self._values = {}
        self._lock = threading.Lock()

    def set(self, value, **labels):
        key = tuple(sorted(labels.items()))
        with self._lock:
            if value is None:
                self._values.pop(key, None)
            else:
                self._values[key] = float(value)

    def replace(self, series, source=None):
        """Replace every series at once: [(value, {labels})]. Drops ones not
        listed. With source, only that source's series are replaced, so two
        KV-cache sources can share one gauge."""
        new = {tuple(sorted(labels.items())): float(value) for value, labels in series}
        with self._lock:
            if source is not None:
                kept = {k: v for k, v in self._values.items() if dict(k).get("source") != source}
                kept.update(new)
                new = kept
            self._values = new

    def relabel(self, label, value):
        """Set `label` to value on every series (the node's framework changed)."""
        with self._lock:
            self._values = {tuple(sorted(dict(k, **{label: value}).items())): v
                            for k, v in ((dict(k), v) for k, v in self._values.items())}

    def points(self, start_ns, now_ns):
        with self._lock:
            items = list(self._values.items())
        return [{"attributes": _attrs(k), "timeUnixNano": str(now_ns), "asDouble": v} for k, v in items]


class Histogram:
    kind = "histogram"

    def __init__(self, name, unit, description, bounds):
        self.name, self.unit, self.description = name, unit, description
        self.bounds = list(bounds)
        self._values = {}
        self._lock = threading.Lock()

    def observe(self, value, **labels):
        if value is None or value != value or value < 0:
            return
        key = tuple(sorted(labels.items()))
        index = len(self.bounds)
        for i, bound in enumerate(self.bounds):
            if value <= bound:
                index = i
                break
        with self._lock:
            entry = self._values.get(key)
            if entry is None:
                entry = self._values[key] = [0, 0.0, [0] * (len(self.bounds) + 1)]
            entry[0] += 1
            entry[1] += value
            entry[2][index] += 1

    def points(self, start_ns, now_ns):
        with self._lock:
            items = [(k, (c, s, list(b))) for k, (c, s, b) in self._values.items()]
        return [{"attributes": _attrs(k), "startTimeUnixNano": str(start_ns),
                 "timeUnixNano": str(now_ns), "count": str(c), "sum": s,
                 "bucketCounts": [str(x) for x in b], "explicitBounds": self.bounds}
                for k, (c, s, b) in items]


class Registry:
    def __init__(self):
        self._metrics = []
        self.start_ns = _now_ns()

    def _add(self, metric):
        self._metrics.append(metric)
        return metric

    def counter(self, name, unit, description):
        return self._add(Counter(name, unit, description))

    def gauge(self, name, unit, description):
        return self._add(Gauge(name, unit, description))

    def histogram(self, name, unit, description, bounds):
        return self._add(Histogram(name, unit, description, bounds))

    def to_otlp(self):
        now = _now_ns()
        metrics = []
        for metric in self._metrics:
            points = metric.points(self.start_ns, now)
            if not points:
                continue
            entry = {"name": metric.name, "unit": metric.unit, "description": metric.description}
            if metric.kind == "sum":
                entry["sum"] = {"aggregationTemporality": 2, "isMonotonic": True, "dataPoints": points}
            elif metric.kind == "histogram":
                entry["histogram"] = {"aggregationTemporality": 2, "dataPoints": points}
            else:
                entry["gauge"] = {"dataPoints": points}
            metrics.append(entry)
        return {"resourceMetrics": [{
            "resource": {"attributes": _attrs([("service.name", "mlx-telemetry"), ("service.version", VERSION)])},
            "scopeMetrics": [{"scope": {"name": "mlx-telemetry", "version": VERSION}, "metrics": metrics}],
        }]}


class Metrics:
    """Every metric this agent can emit. Names and units are chosen so that
    VictoriaMetrics' -opentelemetry.usePrometheusNaming produces the documented
    Prometheus-style names (unit "s" -> _seconds, "By" -> _bytes, "%" ->
    _percent, monotonic sum -> _total, "{...}" annotations dropped)."""

    def __init__(self, cfg):
        b = cfg["buckets"]
        r = self.registry = Registry()
        # Agent self-reporting.
        self.telemetry_up = r.gauge("mlx.telemetry.up", "", "1 while the MLX telemetry agent is running and exporting")
        self.telemetry_info = r.gauge("mlx.telemetry.info", "", "Static agent information (value always 1)")
        self.export_failures = r.counter("mlx.telemetry.export.failures", "{export}", "Failed OTLP exports to the local collector")
        self.activity_expected = r.gauge("mlx.activity.expected", "", "1 if inference traffic is expected on this node (llm_activity_expected)")
        # Requests through the proxy.
        self.requests = r.counter("mlx.requests", "{request}", "Inference requests handled by the proxy")
        self.active = r.gauge("mlx.requests.active", "{request}", "Inference requests currently in flight through the proxy")
        self.usage_missing = r.counter("mlx.requests.usage_missing", "{request}", "Successful requests whose token usage the server did not report")
        self.passthrough = r.counter("mlx.proxy.passthrough_requests", "{request}", "Non-inference requests forwarded unmeasured (e.g. /v1/models)")
        self.upstream_failures = r.counter("mlx.upstream.connection_failures", "{failure}", "Proxy could not reach or read from MLX-LM")
        self.request_duration = r.histogram("mlx.request.duration", "s", "End-to-end request duration seen by the proxy", b["request_duration"])
        self.ttft = r.histogram("mlx.time_to_first_token", "s", "Streaming only: request start to first SSE event carrying generated content", b["time_to_first_token"])
        self.generation_duration = r.histogram("mlx.generation.duration", "s", "Streaming only: first generated content to end of stream", b["generation_duration"])
        self.tokens_per_second = r.histogram("mlx.completion.tokens_per_second", "", "Streaming only: (completion_tokens - 1) / generation duration", b["tokens_per_second"])
        self.prompt_tokens = r.counter("mlx.tokens.prompt", "{token}", "Prompt tokens reported by MLX-LM usage")
        self.completion_tokens = r.counter("mlx.tokens.completion", "{token}", "Completion tokens reported by MLX-LM usage")
        self.cached_tokens = r.counter("mlx.tokens.prompt_cached", "{token}", "Prompt tokens served from MLX-LM's prompt cache (usage.prompt_tokens_details.cached_tokens)")
        self.request_prompt_tokens = r.histogram("mlx.request.prompt_tokens", "{token}", "Prompt tokens per request", b["tokens"])
        self.request_completion_tokens = r.histogram("mlx.request.completion_tokens", "{token}", "Completion tokens per request", b["tokens"])
        self.request_context_tokens = r.histogram("mlx.request.context_tokens", "{token}", "Prompt plus completion tokens per request (context length used)", b["tokens"])
        self.context_utilization = r.histogram("mlx.request.context_utilization", "", "Context tokens / model maximum context length, per request", b["ratio"])
        self.temperature = r.histogram("mlx.request.temperature", "", "Sampling temperature explicitly set by the client", b["temperature"])
        self.tool_calls = r.counter("mlx.tool_calls", "{call}", "Tool calls returned by MLX-LM")
        self.last_success = r.gauge("mlx.request.last_success_timestamp", "s", "Unix time of the last successful inference request")
        # Server and process health.
        self.server_up = r.gauge("mlx.server.up", "", "1 if MLX-LM /health answered 200 at the last check")
        self.models_api_up = r.gauge("mlx.models_api.up", "", "1 if MLX-LM /v1/models answered with a model list at the last check")
        self.probe_up = r.gauge("mlx.inference_probe.success", "", "1 if the last one-token inference probe succeeded")
        self.probe_duration = r.gauge("mlx.inference_probe.duration", "s", "Duration of the last inference probe")
        self.check_last_success = r.gauge("mlx.health.last_success_timestamp", "s", "Unix time of the last successful health check, per check")
        self.check_failures = r.counter("mlx.health.check_failures", "{check}", "Failed health checks, per check")
        self.check_duration = r.gauge("mlx.health.check.duration", "s", "Duration of the last health check, per check")
        self.process_up = r.gauge("mlx.process.up", "", "1 if an MLX-LM server process was found")
        self.process_rss = r.gauge("mlx.process.memory.rss", "By", "Resident set size of the MLX-LM server process (excludes most Metal/GPU buffers)")
        self.process_footprint = r.gauge("mlx.process.memory.footprint", "By", "Physical footprint of the MLX-LM process incl. Metal buffers (top MEM)")
        self.process_cpu = r.gauge("mlx.process.cpu.utilization", "%", "CPU of the MLX-LM process as reported by ps (100 = one core)")
        self.process_restarts = r.counter("mlx.process.restarts", "{restart}", "Observed changes of the server pid")
        self.process_uptime = r.gauge("mlx.process.uptime", "s", "Elapsed time since the server process started (ps etime)")
        self.framework_processes = r.gauge("mlx.framework.processes", "{process}", "Server processes found per framework (both frameworks always reported)")
        self.launchd_runs = r.gauge("mlx.process.launchd_runs", "{run}", "launchd 'runs' count for the MLX-LM LaunchAgent (mlx_launchagent_label)")
        self.model_info = r.gauge("mlx.model.info", "", "Model served, value always 1")
        self.model_max_context = r.gauge("mlx.model.context_length.max", "{token}", "Maximum context length from the model config or llm_model_max_context_tokens")
        self.framework_info = r.gauge("mlx.framework.info", "", "Inference framework on this node (mlx-lm, mlx-vlm or unknown) and how it was detected, value always 1")
        # Timings the server itself reports in its response (MLX-VLM "timings"); MLX-LM sends none.
        self.server_tps = r.histogram("mlx.server_reported.tokens_per_second", "", "Generation rate the server reports per request (timings.predicted_per_second)", b["tokens_per_second"])
        self.server_prompt_duration = r.histogram("mlx.server_reported.prompt_duration", "s", "Prompt processing time the server reports per request (timings.prompt_ms)", b["time_to_first_token"])
        # KV cache, labelled by source: "mlx-vlm" (MLX-VLM's prefix cache,
        # GET /v1/cache/stats) or "omlx" (optional, GET <omlx_url>/admin/api/stats).
        self.kv_up = r.gauge("mlx.kv_cache.source.up", "", "1 if the last poll of this KV-cache source succeeded")
        self.kv_enabled = r.gauge("mlx.kv_cache.enabled", "", "1 if the source reports its KV cache as enabled")
        self.kv_failures = r.counter("mlx.kv_cache.scrape.failures", "{poll}", "Failed KV-cache stats polls, per source and reason")
        self.kv_available = r.gauge("mlx.kv_cache.metric.available", "", "1 if the source reports this KV-cache figure, 0 if it does not")
        self.kv_used = r.gauge("mlx.kv_cache.used", "By", "KV cache held per source, tier (hot = memory, ssd = disk) and model")
        self.kv_capacity = r.gauge("mlx.kv_cache.capacity", "By", "KV cache budget per source and tier")
        self.kv_utilization = r.gauge("mlx.kv_cache.utilization", "%", "KV cache used / capacity per source and tier, only when a capacity is reported")
        self.kv_entries = r.gauge("mlx.kv_cache.entries", "{entry}", "KV cache blocks held per source, tier and model")
        self.kv_entries_capacity = r.gauge("mlx.kv_cache.entries.capacity", "{entry}", "Block pool size per source and tier, where the source reports one")
        self.kv_lookups = ObservedCounter("mlx.kv_cache.lookups", "{lookup}", "KV cache lookups by result (hit/miss), the source's own counters since it started")
        self.kv_hot_hits = ObservedCounter("mlx.kv_cache.hot_hits", "{lookup}", "OMLX: lookups served from the in-memory hot tier (a subset of hits)")
        self.kv_evictions = ObservedCounter("mlx.kv_cache.evictions", "{block}", "KV cache blocks evicted per source and tier")
        for metric in (self.kv_lookups, self.kv_hot_hits, self.kv_evictions):
            r._add(metric)
        # Hardware (unprivileged sources only).
        self.hw_available = r.gauge("macos.hardware.metric.available", "", "1 if this hardware reading is collected on this node, 0 if unavailable")
        self.gpu_util = r.gauge("macos.gpu.utilization", "%", "GPU device utilisation (IORegistry PerformanceStatistics)")
        self.gpu_renderer_util = r.gauge("macos.gpu.renderer.utilization", "%", "GPU renderer utilisation (IORegistry PerformanceStatistics)")
        self.gpu_mem_in_use = r.gauge("macos.gpu.memory.in_use", "By", "Unified memory in use by the GPU driver (IORegistry PerformanceStatistics)")
        self.gpu_mem_alloc = r.gauge("macos.gpu.memory.allocated", "By", "Unified memory allocated by the GPU driver (IORegistry PerformanceStatistics)")
        self.memory_pressure = r.gauge("macos.memory.pressure_level", "", "Kernel memory pressure: 0 normal, 1 warning, 2 critical")
        self.thermal_state = r.gauge("macos.thermal.state", "", "NSProcessInfo thermal state: 0 nominal, 1 fair, 2 serious, 3 critical")
        # Optional quality heuristics.
        self.q_evaluations = r.counter("mlx.quality.evaluations", "{response}", "Responses scored by the quality heuristics")
        self.q_flags = r.counter("mlx.quality.flags", "{flag}", "Heuristic flags raised, per flag")
        self.q_flagged = r.counter("mlx.quality.flagged_responses", "{response}", "Responses with at least one heuristic flag")
        self.q_repetition = r.histogram("mlx.quality.repetition", "", "Share of repeated word trigrams per response", b["ratio"])
        self.q_risk = r.histogram("mlx.quality.heuristic_risk_score", "", "Weighted heuristic risk score 0-1 (not a hallucination detector)", b["ratio"])
        self.q_last_risk = r.gauge("mlx.quality.last_heuristic_risk_score", "", "Heuristic risk score of the latest evaluated response")


# --------------------------------------------------------------------------
# Shared state
# --------------------------------------------------------------------------

def sanitize_model(value):
    """Bounded, non-sensitive model label: a path becomes its last component
    (so home directories and user names never become labels)."""
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if not value:
        return ""
    if value.startswith(("/", "~", ".")):
        value = os.path.basename(value.rstrip("/")) or "local-model"
    value = re.sub(r"[^A-Za-z0-9._:/@+-]", "_", value)
    return value[:128]


class State:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.served_model = sanitize_model(cfg["model_name"])
        self.served_model_source = "config" if self.served_model else ""
        self.known_models = set()
        self.seen_labels = set()
        # Set by Health on every tick; read by the proxy for the framework label.
        self.framework = cfg["framework"] if cfg["framework"] in FRAMEWORKS else "unknown"
        self.framework_source = "config" if cfg["framework"] in FRAMEWORKS else "none"

    def model_label(self, requested):
        cfg = self.cfg
        if cfg["model_name"]:
            return sanitize_model(cfg["model_name"])
        with self.lock:
            served = self.served_model or "unknown"
            req = sanitize_model(requested) if requested not in (None, "", "default_model") else ""
            label = served
            if req and req != served:
                label = req if req in self.known_models else "other"
            if label not in self.seen_labels:
                if len(self.seen_labels) >= cfg["model_label_max_values"]:
                    return "other"
                self.seen_labels.add(label)
            return label


# --------------------------------------------------------------------------
# Quality heuristics (optional)
# --------------------------------------------------------------------------

_WORD = re.compile(r"[a-z0-9']+")
_REFUSAL = re.compile(
    r"\b(i can(?:no|')t (?:help|assist|provide|comply)|i(?: am|'m) (?:unable|not able) to (?:help|assist|provide)"
    r"|i won't be able to|as an ai(?: language model)?|i must decline|i(?: am|'m) sorry,? but i can)",
    re.IGNORECASE)
_HEDGES = re.compile(
    r"\b(i think|i believe|i(?: am|'m) not (?:sure|certain)|possibly|probably|perhaps|might be|may be|could be"
    r"|it seems|it appears|likely|unclear|not entirely sure)\b",
    re.IGNORECASE)


def score_quality(text, cfg):
    """Return (repetition, hedging_per_100_words, refusal, flags, risk) or None
    for responses too short to score. Documented in docs/LLM_OBSERVABILITY.md."""
    words = _WORD.findall(text.lower())
    if len(words) < 20:
        return None
    trigrams = list(zip(words, words[1:], words[2:]))
    repetition = 1.0 - len(set(trigrams)) / float(len(trigrams))
    hedging = 100.0 * len(_HEDGES.findall(text)) / len(words)
    refusal = bool(_REFUSAL.search(text))
    rep_t, hedge_t = cfg["quality_repetition_threshold"], cfg["quality_hedging_threshold"]
    flags = []
    if repetition >= rep_t:
        flags.append("repetition")
    if hedging >= hedge_t:
        flags.append("hedging")
    if refusal:
        flags.append("refusal")
    w = cfg["quality_risk_weights"]
    risk = (float(w.get("repetition", 0)) * min(1.0, repetition / rep_t if rep_t else 0)
            + float(w.get("hedging", 0)) * min(1.0, hedging / hedge_t if hedge_t else 0)
            + float(w.get("refusal", 0)) * (1.0 if refusal else 0.0))
    return repetition, hedging, refusal, flags, max(0.0, min(1.0, risk))


# --------------------------------------------------------------------------
# Proxy
# --------------------------------------------------------------------------

_HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
               "trailer", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
               "accept-encoding", "proxy-connection"}
# /chat/completions without /v1 is MLX-VLM's primary route; MLX-LM serves it too.
_INSTRUMENTED = {"/v1/chat/completions": "chat", "/chat/completions": "chat",
                 "/v1/completions": "completion"}
STATUS_PATH = "/-/mlx-telemetry/status"


class _ClientGone(Exception):
    pass


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "mlx-telemetry/" + VERSION
    # Set on the class by run_proxy().
    cfg = None
    metrics = None
    state = None

    def log_message(self, fmt, *args):  # no access log: paths can carry query data
        return

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def do_OPTIONS(self):
        self._handle()

    def do_HEAD(self):
        self._handle()

    def do_PUT(self):
        self._handle()

    def do_DELETE(self):
        self._handle()

    # -- helpers -----------------------------------------------------------

    def _send_json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _write(self, data):
        try:
            self.wfile.write(data)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, socket.timeout, OSError):
            raise _ClientGone()

    def _upstream_headers(self):
        headers = {}
        for key, value in self.headers.items():
            if key.lower() not in _HOP_BY_HOP:
                headers[key] = value
        return headers

    def _open_upstream(self, body, headers):
        cfg = self.cfg
        conn = http.client.HTTPConnection(cfg["upstream_host"], cfg["upstream_port"],
                                          timeout=cfg["proxy_connect_timeout_seconds"])
        conn.connect()
        conn.sock.settimeout(cfg["proxy_read_timeout_seconds"])
        if body is not None:
            headers["Content-Length"] = str(len(body))
        conn.request(self.command, self.path, body=body, headers=headers)
        return conn, conn.getresponse()

    def _relay_headers(self, resp, close):
        self.send_response(resp.status, resp.reason)
        for key, value in resp.getheaders():
            if key.lower() not in _HOP_BY_HOP:
                self.send_header(key, value)
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True

    def _read_body(self):
        length = self.headers.get("Content-Length")
        if length is None:
            return None if self.command in ("GET", "HEAD", "OPTIONS", "DELETE") else b""
        try:
            length = int(length)
        except ValueError:
            return False
        if length < 0 or length > self.cfg["proxy_max_request_bytes"]:
            return False
        return self.rfile.read(length) if length else b""

    # -- dispatch ----------------------------------------------------------

    def _handle(self):
        path = self.path.split("?", 1)[0]
        if path == STATUS_PATH and self.command == "GET":
            cfg = self.cfg
            self._send_json(200, {"agent": "mlx-telemetry", "version": VERSION,
                                  "upstream": "%s:%s" % (cfg["upstream_host"], cfg["upstream_port"]),
                                  "served_model": self.state.served_model or None,
                                  "framework": self.state.framework,
                                  "framework_source": self.state.framework_source})
            return
        body = self._read_body()
        if body is False:
            self._send_json(413, {"error": "request body too large or invalid Content-Length"})
            return
        endpoint = _INSTRUMENTED.get(path) if self.command == "POST" else None
        if endpoint:
            self._instrumented(endpoint, body or b"")
        else:
            self._passthrough(body)

    def _passthrough(self, body):
        m = self.metrics
        m.passthrough.add(1)
        try:
            conn, resp = self._open_upstream(body, self._upstream_headers())
        except socket.timeout:
            m.upstream_failures.add(1, reason="timeout", framework=self.state.framework)
            self._send_json(504, {"error": "MLX-LM server timed out"})
            return
        except OSError:
            m.upstream_failures.add(1, reason="connect", framework=self.state.framework)
            self._send_json(502, {"error": "MLX-LM server unavailable"})
            return
        try:
            length = resp.getheader("Content-Length")
            self._relay_headers(resp, close=length is None)
            self.end_headers()
            while True:
                chunk = resp.read1(65536) if hasattr(resp, "read1") else resp.read(65536)
                if not chunk:
                    break
                self._write(chunk)
        except _ClientGone:
            self.close_connection = True
        except (socket.timeout, OSError):
            m.upstream_failures.add(1, reason="read", framework=self.state.framework)
            self.close_connection = True
        finally:
            conn.close()

    def _instrumented(self, endpoint, body):
        cfg, m, state = self.cfg, self.metrics, self.state
        t0 = time.monotonic()
        try:
            req = json.loads(body.decode("utf-8")) if body else None
        except (UnicodeDecodeError, ValueError):
            req = None
        if not isinstance(req, dict):
            req = None
        stream = bool(req and req.get("stream") is True)
        model = state.model_label(req.get("model") if req else None)
        injected = False
        if stream and cfg["proxy_stream_usage"] and "stream_options" not in req:
            req["stream_options"] = {"include_usage": True}
            body = json.dumps(req).encode()
            injected = True
        framework = state.framework
        temperature = req.get("temperature") if req else None
        if isinstance(temperature, (int, float)) and not isinstance(temperature, bool):
            m.temperature.observe(float(temperature), model=model, framework=framework)

        result = {"status": 0, "outcome": "success", "usage": None, "timings": None, "t_first": None,
                  "t_end": None, "tool_calls": 0, "text": [] if cfg["quality_metrics_enabled"] else None,
                  "text_len": 0}
        labels = {"model": model, "endpoint": endpoint, "stream": "true" if stream else "false",
                  "framework": framework}
        self._set_active(m, state, 1)
        conn = None
        try:
            try:
                conn, resp = self._open_upstream(body, self._upstream_headers())
            except socket.timeout:
                m.upstream_failures.add(1, reason="timeout", framework=self.state.framework)
                result.update(status=504, outcome="timeout")
                self._send_json(504, {"error": "MLX-LM server timed out"})
                return
            except OSError:
                m.upstream_failures.add(1, reason="connect", framework=self.state.framework)
                result.update(status=502, outcome="upstream_error")
                self._send_json(502, {"error": "MLX-LM server unavailable"})
                return
            result["status"] = resp.status
            ctype = (resp.getheader("Content-Type") or "").lower()
            if resp.status == 200 and "text/event-stream" in ctype:
                self._relay_stream(resp, result, injected)
            else:
                self._relay_whole(resp, result, stream)
        except _ClientGone:
            result["outcome"] = "client_disconnected"
            self.close_connection = True
        finally:
            if conn is not None:
                conn.close()
            self._set_active(m, state, -1)
            self._record(t0, labels, result)

    _active = 0
    _active_lock = threading.Lock()

    @classmethod
    def _active_delta(cls, delta):
        with cls._active_lock:
            cls._active += delta
            return cls._active

    @classmethod
    def _set_active(cls, metrics, state, delta):
        # One series, labelled with the node's current framework.
        metrics.active.replace([(cls._active_delta(delta), {"framework": state.framework})])

    def _relay_whole(self, resp, result, stream):
        try:
            data = resp.read()
        except socket.timeout:
            self.metrics.upstream_failures.add(1, reason="timeout", framework=self.state.framework)
            result.update(status=504, outcome="timeout")
            self._send_json(504, {"error": "MLX-LM server timed out"})
            return
        except OSError:
            self.metrics.upstream_failures.add(1, reason="read", framework=self.state.framework)
            result.update(status=502, outcome="upstream_error")
            self._send_json(502, {"error": "MLX-LM server connection failed"})
            return
        self._relay_headers(resp, close=False)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self._write(data)
        result["t_end"] = time.monotonic()
        if resp.status >= 500:
            result["outcome"] = "server_error"
        elif resp.status >= 400:
            result["outcome"] = "client_error"
        if resp.status != 200:
            return
        try:
            obj = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return
        if isinstance(obj, dict):
            result["usage"] = obj.get("usage") if isinstance(obj.get("usage"), dict) else None
            self._collect_timings(result, obj.get("timings"))
            for choice in obj.get("choices") or []:
                if not isinstance(choice, dict):
                    continue
                message = choice.get("message") or {}
                if isinstance(message, dict):
                    result["tool_calls"] += len(message.get("tool_calls") or [])
                    self._collect_text(result, message.get("content"))
                self._collect_text(result, choice.get("text"))

    def _relay_stream(self, resp, result, injected):
        self._relay_headers(resp, close=True)
        self.end_headers()
        skip_blank = False
        try:
            while True:
                line = resp.readline(1048576)
                if not line:
                    break
                out = line
                if line.startswith(b"data:"):
                    payload = line[5:].strip()
                    if payload == b"[DONE]":
                        result["t_end"] = time.monotonic()
                    else:
                        drop = self._observe_chunk(payload, result, injected)
                        if drop:
                            out, skip_blank = None, True
                elif skip_blank and not line.strip():
                    out, skip_blank = None, False
                if out is not None:
                    self._write(out)
        except socket.timeout:
            self.metrics.upstream_failures.add(1, reason="timeout", framework=self.state.framework)
            result["outcome"] = "timeout"
        except _ClientGone:
            raise
        except OSError:
            self.metrics.upstream_failures.add(1, reason="read", framework=self.state.framework)
            result["outcome"] = "upstream_error"
        if result["t_end"] is None:
            result["t_end"] = time.monotonic()
            if result["outcome"] == "success":
                # Stream ended without [DONE]: MLX-LM closed it early.
                result["outcome"] = "upstream_error"

    def _observe_chunk(self, payload, result, injected):
        """Inspect one SSE event. Returns True if it is the usage-only event the
        proxy itself requested and must therefore hide from the client."""
        try:
            obj = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return False
        if not isinstance(obj, dict):
            return False
        choices = obj.get("choices") or []
        if obj.get("error") and not choices and result["outcome"] == "success":
            # MLX-VLM reports a failure inside an already-started 200 stream.
            result["outcome"] = "server_error"
        usage = obj.get("usage")
        if isinstance(usage, dict):
            result["usage"] = usage
            self._collect_timings(result, obj.get("timings"))
            if injected and not choices:
                return True
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
            content = delta.get("content")
            reasoning = delta.get("reasoning") or delta.get("reasoning_content")
            tools = delta.get("tool_calls") or []
            text = choice.get("text")
            if tools:
                result["tool_calls"] += len(tools)
            if (content or reasoning or tools or text) and result["t_first"] is None:
                result["t_first"] = time.monotonic()
            self._collect_text(result, content)
            self._collect_text(result, text)
        return False

    @staticmethod
    def _collect_timings(result, timings):
        """Keep the final per-request timings block (it has prompt_ms; the
        running timings on content events only carry a partial rate)."""
        if isinstance(timings, dict) and "prompt_ms" in timings:
            result["timings"] = timings

    def _collect_text(self, result, text):
        if result["text"] is None or not isinstance(text, str) or not text:
            return
        room = self.cfg["quality_max_chars"] - result["text_len"]
        if room > 0:
            result["text"].append(text[:room])
            result["text_len"] += min(room, len(text))

    # -- recording -----------------------------------------------------------

    def _record(self, t0, labels, result):
        cfg, m, state = self.cfg, self.metrics, self.state
        end = result["t_end"] or time.monotonic()
        status = result["status"]
        outcome = result["outcome"]
        m.requests.add(1, outcome=outcome, status_code=str(status or 0), **labels)
        m.request_duration.observe(end - t0, outcome=outcome, **labels)
        model, endpoint, stream = labels["model"], labels["endpoint"], labels["stream"] == "true"
        if outcome != "success":
            return
        # Every request-derived series carries the framework, so the
        # dashboards' $framework filter applies to all of them.
        mf = {"model": model, "framework": labels["framework"]}
        m.last_success.set(time.time(), **mf)
        if result["tool_calls"]:
            m.tool_calls.add(result["tool_calls"], **mf)
        usage = result["usage"] or {}
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
        have_usage = _is_count(prompt) and _is_count(completion)
        if stream and result["t_first"] is not None:
            m.ttft.observe(result["t_first"] - t0, endpoint=endpoint, **mf)
            generation = end - result["t_first"]
            m.generation_duration.observe(generation, endpoint=endpoint, **mf)
            if have_usage and completion >= 2 and generation > 0:
                m.tokens_per_second.observe((completion - 1) / generation, endpoint=endpoint, **mf)
        timings = result["timings"] or {}
        server_tps, prompt_ms = timings.get("predicted_per_second"), timings.get("prompt_ms")
        # MLX-VLM reports 0 when it has no measurement; that is not a rate.
        if _is_number(server_tps) and server_tps > 0:
            m.server_tps.observe(float(server_tps), stream=labels["stream"], **mf)
        if _is_number(prompt_ms) and prompt_ms > 0:
            m.server_prompt_duration.observe(prompt_ms / 1000.0, stream=labels["stream"], **mf)
        if not have_usage:
            m.usage_missing.add(1, **labels)
        else:
            m.prompt_tokens.add(prompt, **labels)
            m.completion_tokens.add(completion, **labels)
            details = usage.get("prompt_tokens_details")
            cached = details.get("cached_tokens") if isinstance(details, dict) else None
            if _is_count(cached):
                m.cached_tokens.add(cached, **labels)
            m.request_prompt_tokens.observe(prompt, **mf)
            m.request_completion_tokens.observe(completion, **mf)
            m.request_context_tokens.observe(prompt + completion, **mf)
            max_ctx = state_max_context(state)
            if max_ctx:
                m.context_utilization.observe(min(1.5, (prompt + completion) / float(max_ctx)), **mf)
        if result["text"]:
            scored = score_quality("".join(result["text"]), cfg)
            if scored is not None:
                repetition, _hedging, _refusal, flags, risk = scored
                m.q_evaluations.add(1, **mf)
                m.q_repetition.observe(repetition, **mf)
                m.q_risk.observe(risk, **mf)
                m.q_last_risk.set(risk, **mf)
                for flag in flags:
                    m.q_flags.add(1, flag=flag, **mf)
                if flags:
                    m.q_flagged.add(1, **mf)


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value


def _is_count(value):
    """A token count as servers report it: a non-negative int, never a bool."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def state_max_context(state):
    return getattr(state, "max_context", 0)


def run_proxy(cfg, metrics, state):
    ProxyHandler.cfg, ProxyHandler.metrics, ProxyHandler.state = cfg, metrics, state
    server = ThreadingHTTPServer((cfg["proxy_host"], cfg["proxy_port"]), ProxyHandler)
    server.daemon_threads = True
    LOG.info("proxy listening on %s:%s -> inference server %s:%s", cfg["proxy_host"], cfg["proxy_port"],
             cfg["upstream_host"], cfg["upstream_port"])
    return server


# --------------------------------------------------------------------------
# Health checks and process discovery
# --------------------------------------------------------------------------

def _run(argv, timeout=5):
    try:
        out = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             timeout=timeout, check=False)
        return out.returncode, out.stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return -1, ""


def _http_get(url, timeout, token=None, with_headers=False):
    """GET a URL; an HTTP error status is returned, not raised. The token is
    sent as a Bearer header and never logged."""
    headers = {"Authorization": "Bearer " + token} if token else {}
    req = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - URL from config
            status, data, resp_headers = resp.status, resp.read(4194304), resp.headers
    except urllib.error.HTTPError as exc:
        status, data, resp_headers = exc.code, b"", exc.headers
    return (status, data, resp_headers) if with_headers else (status, data)


def _read_secret(path):
    """First line of a 0600 key file, or None. Read on every use so a rotated
    key needs no restart; the value never leaves this process except as the
    Authorization header to the configured server."""
    if not path:
        return None
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as handle:
            return handle.readline().strip() or None
    except OSError:
        return None


def framework_from_response(headers, body):
    """Identify the server answering on the upstream port from its /health
    response: MLX-VLM (FastAPI) stamps every response with a Server:
    mlx_vlm/<version> header; MLX-LM (http.server) answers /health with exactly
    {"status": "ok"|"unavailable"} and a BaseHTTP Server header. Anything else
    (including OMLX, which wraps both) is not claimed."""
    server = (headers.get("Server", "") if headers is not None else "") or ""
    if server.lower().startswith("mlx_vlm/"):
        return "mlx-vlm"
    try:
        obj = json.loads(body.decode("utf-8")) if body else None
    except (UnicodeDecodeError, ValueError):
        obj = None
    if isinstance(obj, dict) and "loaded_model" in obj and "continuous_batching_enabled" in obj:
        return "mlx-vlm"
    if (server.startswith("BaseHTTP/") and isinstance(obj, dict) and set(obj) == {"status"}
            and obj["status"] in ("ok", "unavailable")):
        return "mlx-lm"
    return None


# Executables a real server process runs as: a Python interpreter (python3,
# python3.12, macOS's Python.app "Python") or the console-script entry points.
_SERVER_EXECUTABLES = re.compile(r"^(python[0-9.]*|Python|mlx_lm\.server|mlx_vlm\.server|mlx_lm|mlx_vlm)$")


def _is_server_command(command, pattern, strict):
    if not pattern.search(command):
        return False
    if not strict:
        return True
    tokens = command.split()
    if not tokens or not _SERVER_EXECUTABLES.match(os.path.basename(tokens[0])):
        return False
    # The server must be what runs (entry point, script or -m module, within
    # the first tokens), not an argument passed to some other program.
    return bool(pattern.search(" ".join(tokens[:4])))


def _etime_seconds(value):
    """ps etime ([[dd-]hh:]mm:ss) to seconds, or None."""
    match = re.match(r"^(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+)$", value or "")
    if not match:
        return None
    days, hours, minutes, seconds = (int(g or 0) for g in match.groups())
    return ((days * 24 + hours) * 60 + minutes) * 60 + seconds


def _port_from_argv(command):
    match = re.search(r"--port(?:=|\s+)(\d+)", command)
    return int(match.group(1)) if match else None


_SIZE = {"B": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}


def _footprint(pid):
    """Physical footprint (top's MEM column), which unlike RSS counts the Metal
    buffers MLX keeps model weights and KV cache in. None if top cannot read it."""
    rc, out = _run(["top", "-l", "1", "-pid", str(pid), "-stats", "pid,mem"], timeout=10)
    for line in out.splitlines()[::-1]:
        fields = line.split()
        if len(fields) == 2 and fields[0] == str(pid):
            match = re.match(r"^([\d.]+)([BKMGT])[+-]?$", fields[1])
            if match:
                return int(float(match.group(1)) * _SIZE[match.group(2)])
    return None


def _model_from_argv(command):
    match = re.search(r"--model(?:=|\s+)(\"[^\"]+\"|'[^']+'|\S+)", command)
    return match.group(1).strip("\"'") if match else ""


def _find_config_json(model, cfg):
    candidates = []
    if cfg["model_path"]:
        candidates.append(os.path.join(os.path.expanduser(cfg["model_path"]), "config.json"))
    if model and model.startswith(("/", "~")):
        candidates.append(os.path.join(os.path.expanduser(model), "config.json"))
    elif model and "/" in model:
        hub = cfg["hf_cache_dir"] or os.environ.get("HF_HUB_CACHE") or os.path.join(
            os.environ.get("HF_HOME", os.path.expanduser("~/.cache/huggingface")), "hub")
        snapshots = os.path.join(os.path.expanduser(hub), "models--" + model.replace("/", "--"), "snapshots")
        try:
            for name in sorted(os.listdir(snapshots)):
                candidates.append(os.path.join(snapshots, name, "config.json"))
        except OSError:
            pass
    for path in candidates:
        try:
            with open(path, encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            continue
    return None


def _max_context_from(config):
    if not isinstance(config, dict):
        return 0
    for scope in (config, config.get("text_config") or {}):
        if not isinstance(scope, dict):
            continue
        for key in ("max_position_embeddings", "max_sequence_length", "seq_length", "n_positions"):
            value = scope.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                return value
    return 0


class Health:
    def __init__(self, cfg, metrics, state):
        self.cfg, self.m, self.state = cfg, metrics, state
        self.uid = os.getuid()
        # (framework, pattern, strict): a customised pattern is trusted as is.
        self.patterns = [
            ("mlx-lm", re.compile(cfg["mlx_process_pattern"]), cfg["mlx_process_pattern"] == DEFAULT_LM_PATTERN),
            ("mlx-vlm", re.compile(cfg["mlx_vlm_process_pattern"]), cfg["mlx_vlm_process_pattern"] == DEFAULT_VLM_PATTERN),
        ]
        self.http_framework = None
        self.process_framework = None
        self.omlx = OMLXCollector(cfg, metrics) if cfg["omlx_enabled"] else None
        self.next_omlx = 0.0
        self.vlm_cache = VLMCacheCollector(cfg, metrics, self._token) if cfg["vlm_cache_stats_enabled"] else None
        self.next_vlm_cache = 0.0
        self.process_counts = {}
        # Set after the first tick: until then the framework is not known, and
        # exporting would leave short-lived framework="unknown" series.
        self.ready = threading.Event()
        self.last_pid = None
        self.raw_model = ""
        self.next_models = 0.0
        self.next_probe = 0.0
        self.next_hardware = 0.0
        self.base = "http://%s:%s" % (cfg["upstream_host"], cfg["upstream_port"])
        self.thermal = _ThermalState()
        state.max_context = 0
        m = metrics
        m.export_failures.touch()
        self.info = {"version": VERSION,
                     "proxy": "enabled" if cfg["proxy_enabled"] else "disabled",
                     "hardware": "enabled" if cfg["hardware_metrics_enabled"] else "disabled",
                     "quality": "enabled" if cfg["quality_metrics_enabled"] else "disabled",
                     "omlx": "enabled" if cfg["omlx_enabled"] else "disabled"}
        self._publish_framework()
        m.activity_expected.set(1 if cfg["activity_expected"] else 0)

    # Node-level series that carry the node's framework. On a change they are
    # relabelled in place, so no series is ever exported without the label and
    # the old framework's series stop at once.
    def _framework_gauges(self):
        m = self.m
        return (m.server_up, m.models_api_up, m.probe_up, m.probe_duration, m.check_last_success,
                m.check_duration, m.process_up, m.process_rss, m.process_footprint, m.process_cpu,
                m.process_uptime, m.launchd_runs, m.active)

    @property
    def fw(self):
        return {"framework": self.state.framework}

    def _publish_framework(self):
        m, fw = self.m, self.fw
        m.telemetry_info.replace([(1, dict(self.info, **fw))])
        # Zeros, so "no failures yet" is distinguishable from "no telemetry".
        # Not for "unknown" (the state before the first detection), which would
        # otherwise leave flat zero series for a framework the node never had.
        if fw["framework"] != "unknown":
            for check in ("http", "models", "inference"):
                m.check_failures.touch(check=check, **fw)
            m.process_restarts.touch(**fw)
            m.upstream_failures.touch(reason="connect", **fw)
        for gauge in self._framework_gauges():
            gauge.relabel("framework", fw["framework"])

    def run_forever(self, stop):
        while not stop.is_set():
            started = time.monotonic()
            try:
                self.tick()
            except Exception:  # never let one bad check stop the agent
                LOG.exception("health tick failed")
            stop.wait(max(1.0, self.cfg["health_check_interval_seconds"] - (time.monotonic() - started)))

    def tick(self):
        now = time.monotonic()
        self.check_process()
        self.check_http()
        # Before the probe and the KV poll, which depend on the framework.
        self.update_framework()
        if now >= self.next_models:
            self.next_models = now + self.cfg["models_check_interval_seconds"]
            self.check_models()
        if self.cfg["inference_probe_enabled"] and now >= self.next_probe:
            self.next_probe = now + self.cfg["inference_probe_interval_seconds"]
            self.probe()
        if self.cfg["hardware_metrics_enabled"] and now >= self.next_hardware:
            self.next_hardware = now + self.cfg["hardware_interval_seconds"]
            self.hardware()
        if self.omlx is not None and now >= self.next_omlx:
            self.next_omlx = now + self.cfg["omlx_poll_interval_seconds"]
            self.omlx.poll()
        if self.vlm_cache is not None and now >= self.next_vlm_cache:
            self.next_vlm_cache = now + self.cfg["models_check_interval_seconds"]
            self.vlm_cache.poll(self.state.framework == "mlx-vlm", self.base, self.state.served_model or "unknown")
        self.update_model()
        self.m.telemetry_up.set(1)
        self.ready.set()

    def update_framework(self):
        """Inventory override, else what answers on the upstream port, else
        which server process was found. 'unknown' is reported, never guessed."""
        if self.cfg["framework"] in FRAMEWORKS:
            framework, source = self.cfg["framework"], "config"
        elif self.http_framework:
            framework, source = self.http_framework, "http"
        elif self.process_framework:
            framework, source = self.process_framework, "process"
        else:
            framework, source = "unknown", "none"
        with self.state.lock:
            changed = framework != self.state.framework
            self.state.framework, self.state.framework_source = framework, source
        if changed:
            self._publish_framework()
        self.m.framework_info.replace([(1, {"framework": framework, "source": source})])
        self.m.framework_processes.replace(
            [(self.process_counts.get(name, 0), {"framework": name}) for name in FRAMEWORKS])

    def _token(self):
        return _read_secret(self.cfg["upstream_api_key_file"])

    # -- process -------------------------------------------------------------

    def _launchd(self):
        label = self.cfg["mlx_launchagent_label"]
        if not label:
            return None
        for domain in ("gui/%d" % self.uid, "user/%d" % self.uid):
            rc, out = _run(["launchctl", "print", "%s/%s" % (domain, label)])
            if rc == 0:
                info = {}
                for key in ("pid", "runs", "last exit code"):
                    match = re.search(r"^\s*%s = (-?\d+)" % re.escape(key), out, re.MULTILINE)
                    if match:
                        info[key] = int(match.group(1))
                info["running"] = bool(re.search(r"^\s*state = running", out, re.MULTILINE))
                return info
        return {}

    def check_process(self):
        m, fw = self.m, self.fw
        pid, command = None, ""
        info = self._launchd()
        if info is not None:
            if "runs" in info:
                m.launchd_runs.set(info["runs"], **fw)
            if info.get("running") and info.get("pid"):
                pid = info["pid"]
        rc, out = _run(["ps", "-axww", "-o", "pid=,command="])
        pid, command, self.process_framework = self.select_process(out, pid)
        if pid is None:
            m.process_up.set(0, **fw)
            for gauge in (m.process_rss, m.process_cpu, m.process_footprint, m.process_uptime):
                gauge.set(None, **fw)
            return
        m.process_up.set(1, **fw)
        if self.last_pid is not None and pid != self.last_pid:
            m.process_restarts.add(1, **fw)
        self.last_pid = pid
        if command:
            self.raw_model = _model_from_argv(command) or self.raw_model
        rc, out = _run(["ps", "-o", "rss=,%cpu=,etime=", "-p", str(pid)])
        fields = out.split()
        if rc == 0 and len(fields) >= 3:
            try:
                m.process_rss.set(int(fields[0]) * 1024, **fw)
                m.process_cpu.set(float(fields[1]), **fw)
            except ValueError:
                pass
            m.process_uptime.set(_etime_seconds(fields[2]), **fw)
        m.process_footprint.set(_footprint(pid), **fw)

    def classify(self, command, strict=True):
        for framework, pattern, is_default in self.patterns:
            if _is_server_command(command, pattern, strict and is_default):
                return framework
        return None

    def select_process(self, ps_output, launchd_pid=None):
        """Return (pid, command, framework) of the inference server, or
        (None, "", None). With a launchd pid, that process is the server and
        is only classified. Otherwise only MLX-LM / MLX-VLM server processes
        serving the upstream port count (--port, or no --port when the upstream
        is the servers' default 8080), preferring the framework that answers on
        the port. A server started without --port on another port needs
        mlx_launchagent_label or llm_framework."""
        me = os.getpid()
        candidates, launchd_match = [], None
        for line in ps_output.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) != 2 or not parts[0].isdigit():
                continue
            this_pid, this_cmd = int(parts[0]), parts[1]
            if this_pid == me or "mlx_telemetry" in this_cmd or "mlx-telemetry" in this_cmd:
                continue
            if launchd_pid is not None and this_pid == launchd_pid:
                launchd_match = (this_pid, this_cmd, self.classify(this_cmd, strict=False))
            framework = self.classify(this_cmd)
            if framework:
                candidates.append((this_pid, this_cmd, framework))
        # Reported per framework, so a Mac genuinely running both shows both.
        self.process_counts = {}
        for _pid, _cmd, framework in candidates:
            self.process_counts[framework] = self.process_counts.get(framework, 0) + 1
        if launchd_pid is not None:
            return launchd_match or (launchd_pid, "", None)
        port = self.cfg["upstream_port"]

        def serves_port(candidate):
            argv_port = _port_from_argv(candidate[1])
            return argv_port == port or (argv_port is None and port == 8080)
        # A server on another port is not this node's server (it is still
        # counted above): without this, a stopped server would be "replaced"
        # by any other MLX server on the Mac.
        mine = [c for c in candidates if serves_port(c)]
        if not mine:
            return None, "", None
        return min(mine, key=lambda c: 0 if c[2] == self.http_framework else 1)

    # -- endpoints -----------------------------------------------------------

    def _check(self, name, fn):
        m = self.m
        started = time.monotonic()
        ok = False
        try:
            ok = bool(fn())
        except Exception:
            ok = False
        m.check_duration.set(time.monotonic() - started, check=name, **self.fw)
        if ok:
            m.check_last_success.set(time.time(), check=name, **self.fw)
        else:
            m.check_failures.add(1, check=name, **self.fw)
        return ok

    def check_http(self):
        def health():
            # Cleared first: nothing answering means nothing is identified.
            self.http_framework = None
            status, data, headers = _http_get(self.base + "/health", 5, self._token(), with_headers=True)
            # Identifies the server even when it answers 401 (MLX-VLM with an
            # API key) or 503 (MLX-LM still loading).
            self.http_framework = framework_from_response(headers, data)
            return status == 200
        self.m.server_up.set(1 if self._check("http", health) else 0, **self.fw)

    def check_models(self):
        def models():
            status, data = _http_get(self.base + "/v1/models", 10, self._token())
            if status != 200:
                return False
            obj = json.loads(data.decode("utf-8"))
            ids = [sanitize_model(item.get("id")) for item in obj.get("data", []) if isinstance(item, dict)]
            with self.state.lock:
                self.state.known_models = set(i for i in ids if i)
            return True
        self.m.models_api_up.set(1 if self._check("models", models) else 0, **self.fw)

    def probe(self):
        cfg = self.cfg
        started = time.monotonic()

        def inference():
            headers = {"Content-Type": "application/json"}
            token = self._token()
            if token:
                headers["Authorization"] = "Bearer " + token
            if self.state.framework == "mlx-vlm":
                # MLX-VLM has no /v1/completions and rejects a request without
                # "model" (422), so name the model it serves: its --model
                # argument, else the only model /v1/models lists.
                path, payload = "/v1/chat/completions", {"messages": [{"role": "user", "content": cfg["inference_probe_prompt"]}]}
                with self.state.lock:
                    known = sorted(self.state.known_models)
                model = self.raw_model or (known[0] if len(known) == 1 else "")
                if model:
                    payload["model"] = model
            else:
                path, payload = "/v1/completions", {"prompt": cfg["inference_probe_prompt"]}
            payload.update(max_tokens=1, stream=False)
            req = urllib.request.Request(self.base + path, data=json.dumps(payload).encode(), method="POST",
                                         headers=headers)
            with urllib.request.urlopen(req, timeout=cfg["proxy_read_timeout_seconds"]) as resp:  # noqa: S310
                return resp.status == 200
        ok = self._check("inference", inference)
        self.m.probe_up.set(1 if ok else 0, **self.fw)
        self.m.probe_duration.set(time.monotonic() - started, **self.fw)

    def update_model(self):
        cfg, m, state = self.cfg, self.m, self.state
        if cfg["model_name"]:
            model, source = sanitize_model(cfg["model_name"]), "config"
        elif self.raw_model:
            model, source = sanitize_model(self.raw_model), "process"
        else:
            with state.lock:
                known = sorted(state.known_models)
            model, source = (known[0], "models_api") if len(known) == 1 else ("", "")
        with state.lock:
            state.served_model, state.served_model_source = model, source
        m.model_info.replace([(1, {"model": model, "source": source})] if model else [])
        max_ctx = cfg["model_max_context_tokens"]
        if not max_ctx and (self.raw_model or cfg["model_path"]):
            max_ctx = _max_context_from(_find_config_json(self.raw_model, cfg))
        state.max_context = max_ctx
        m.model_max_context.replace([(max_ctx, {"model": model or "unknown"})] if max_ctx else [])

    # -- hardware ------------------------------------------------------------

    def hardware(self):
        m = self.m
        rc, out = _run(["ioreg", "-r", "-d", "1", "-w", "0", "-c", "IOAccelerator"])
        stats = {}
        match = re.search(r'"PerformanceStatistics" = \{([^}]*)\}', out) if rc == 0 else None
        if match:
            for key, value in re.findall(r'"([^"]+)"=(\d+)', match.group(1)):
                stats[key] = int(value)
        util = stats.get("Device Utilization %")
        m.gpu_util.set(util)
        m.gpu_renderer_util.set(stats.get("Renderer Utilization %"))
        m.gpu_mem_in_use.set(stats.get("In use system memory"))
        m.gpu_mem_alloc.set(stats.get("Alloc system memory"))
        rc, out = _run(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"])
        pressure = {1: 0, 2: 1, 4: 2}.get(int(out.strip())) if rc == 0 and out.strip().isdigit() else None
        m.memory_pressure.set(pressure)
        thermal = self.thermal.read()
        m.thermal_state.set(thermal)
        available = {
            "gpu_utilization": util is not None,
            "gpu_memory": "In use system memory" in stats,
            "memory_pressure": pressure is not None,
            "thermal_state": thermal is not None,
            # Need powermetrics (root) or the private IOReport API.
            "gpu_frequency": False,
            "cpu_power": False,
            "gpu_power": False,
            "ane_power": False,
        }
        m.hw_available.replace([(1 if ok else 0, {"metric": name}) for name, ok in available.items()])


class _ThermalState:
    """NSProcessInfo.thermalState through the Objective-C runtime (public API)."""

    def __init__(self):
        self._fn = None
        try:
            objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
            ctypes.cdll.LoadLibrary("/System/Library/Frameworks/Foundation.framework/Foundation")
            objc.objc_getClass.restype = ctypes.c_void_p
            objc.objc_getClass.argtypes = [ctypes.c_char_p]
            objc.sel_registerName.restype = ctypes.c_void_p
            objc.sel_registerName.argtypes = [ctypes.c_char_p]
            send_obj = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(("objc_msgSend", objc))
            send_long = ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p)(("objc_msgSend", objc))
            info = send_obj(objc.objc_getClass(b"NSProcessInfo"), objc.sel_registerName(b"processInfo"))
            selector = objc.sel_registerName(b"thermalState")
            if info:
                self._fn = lambda: int(send_long(info, selector))
        except (OSError, AttributeError, TypeError):
            self._fn = None

    def read(self):
        if self._fn is None:
            return None
        try:
            value = self._fn()
        except Exception:
            return None
        return value if 0 <= value <= 3 else None


# --------------------------------------------------------------------------
# KV cache: MLX-VLM's prefix cache and (optional) OMLX
# --------------------------------------------------------------------------

# Figures a source may report. "allocated" has no counterpart separate from
# "used" in either source (each reports what its tiers hold and their
# budget), so it is always reported unavailable. Dashboards read
# mlx_kv_cache_metric_available to say "Unavailable" instead of showing 0.
_KV_FIGURES = ("hot_used", "hot_capacity", "ssd_used", "ssd_capacity", "lookups", "evictions")


def _num(source, key):
    value = source.get(key) if isinstance(source, dict) else None
    return value if _is_number(value) and value >= 0 else None


class KVCollector:
    """Shared publishing for one KV-cache source (label source=<name>)."""
    source = ""

    def __init__(self, cfg, metrics):
        self.cfg, self.m = cfg, metrics
        self.models = set()

    def start_reporting(self):
        """Series appear only once the source applies to this node."""
        self.m.kv_failures.touch(source=self.source, reason="connect")
        self.m.kv_up.replace([(0, {"source": self.source})], source=self.source)

    def _model(self, value):
        model = sanitize_model(value) or "unknown"
        if model not in self.models:
            if len(self.models) >= self.cfg["model_label_max_values"]:
                return "other"
            self.models.add(model)
        return model

    def _get(self, url, timeout, token):
        """Returns the decoded JSON object, or the failure reason."""
        try:
            status, data = _http_get(url, timeout, token)
        except (OSError, ValueError):
            return "connect"
        if status in (401, 403):
            return "auth"
        if status != 200:
            return "http"
        try:
            obj = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return "parse"
        return obj if isinstance(obj, dict) else "parse"

    def fail(self, reason):
        self.m.kv_failures.add(1, source=self.source, reason=reason)
        self.clear(up=0)

    def clear(self, up):
        """Stale KV figures would read as current, so drop them all."""
        m, src = self.m, self.source
        for gauge in (m.kv_used, m.kv_capacity, m.kv_utilization, m.kv_entries, m.kv_entries_capacity, m.kv_enabled):
            gauge.replace([], source=src)
        for counter in (m.kv_lookups, m.kv_hot_hits, m.kv_evictions):
            counter.retain(src, ())
        m.kv_available.replace([(0, {"source": src, "metric": name}) for name in _KV_FIGURES + ("allocated",)], source=src)
        m.kv_up.replace([(up, {"source": src})], source=src)

    def publish(self, used, capacities, entries, entries_capacity, seen):
        """used/entries: [(value, {"tier", "model"})]; capacities and
        entries_capacity: {tier: value or None}. Utilisation per tier = the
        sum over models / that tier's capacity, only when a capacity (> 0) is
        reported."""
        m, src = self.m, self.source
        tag = lambda series: [(v, dict(labels, source=src)) for v, labels in series]  # noqa: E731
        m.kv_used.replace(tag(used), source=src)
        m.kv_entries.replace(tag(entries), source=src)
        m.kv_entries_capacity.replace([(v, {"source": src, "tier": t}) for t, v in entries_capacity.items() if v], source=src)
        capacity, utilization = [], []
        for tier, cap in capacities.items():
            seen[tier + "_capacity"] = bool(cap)
            if not cap:
                continue
            capacity.append((cap, {"source": src, "tier": tier}))
            tier_used = [value for value, labels in used if labels["tier"] == tier]
            if tier_used:
                utilization.append((min(100.0, 100.0 * sum(tier_used) / cap), {"source": src, "tier": tier}))
        m.kv_capacity.replace(capacity, source=src)
        m.kv_utilization.replace(utilization, source=src)
        m.kv_available.replace([(1 if seen.get(name) else 0, {"source": src, "metric": name}) for name in _KV_FIGURES]
                               + [(0, {"source": src, "metric": "allocated"})], source=src)
        m.kv_enabled.replace([(1, {"source": src})], source=src)
        m.kv_up.replace([(1, {"source": src})], source=src)


class OMLXCollector(KVCollector):
    """Polls OMLX's documented admin endpoint GET /admin/api/stats (Bearer =
    OMLX's main API key; no key needed when OMLX runs loopback-only with
    authentication disabled) and copies its KV-cache figures.

    Only the runtime_cache block and uptime are read. The response also
    carries OMLX's own API key and paths; those fields are never read,
    exported or logged. Nothing is sent to OMLX except this GET."""
    source = "omlx"

    def __init__(self, cfg, metrics):
        KVCollector.__init__(self, cfg, metrics)
        self.url = cfg["omlx_url"].rstrip("/") + "/admin/api/stats"
        self.start_reporting()

    def poll(self):
        obj = self._get(self.url, self.cfg["omlx_timeout_seconds"], _read_secret(self.cfg["omlx_api_key_file"]))
        if isinstance(obj, str):
            return self.fail(obj)
        cache = obj.get("runtime_cache")
        if not isinstance(cache, dict):
            return self.fail("parse")
        self.apply(cache, obj.get("uptime_seconds"))

    def apply(self, cache, uptime=None):
        m, src = self.m, self.source
        start_ns = int((time.time() - uptime) * 1e9) if _is_number(uptime) and uptime >= 0 else m.registry.start_ns
        used, entries, present = [], [], set()
        seen = {"hot_used": False, "ssd_used": False, "lookups": False, "evictions": False}
        for row in [r for r in cache.get("models") or [] if isinstance(r, dict)]:
            model = self._model(row.get("id"))
            present.add(model)
            for tier, size_key, count_key in (("hot", "hot_cache_size_bytes", "hot_cache_entries"),
                                              ("ssd", "total_size_bytes", "num_files")):
                size, count = _num(row, size_key), _num(row, count_key)
                if size is not None:
                    used.append((size, {"tier": tier, "model": model}))
                    seen[tier + "_used"] = True
                if count is not None:
                    entries.append((count, {"tier": tier, "model": model}))
            hits, misses = _num(row, "hits"), _num(row, "misses")
            if hits is not None and misses is not None:
                m.kv_lookups.observe_total(hits, start_ns, source=src, model=model, result="hit")
                m.kv_lookups.observe_total(misses, start_ns, source=src, model=model, result="miss")
                seen["lookups"] = True
            if _num(row, "hot_cache_hits") is not None:
                m.kv_hot_hits.observe_total(_num(row, "hot_cache_hits"), start_ns, source=src, model=model)
            for tier, key in (("ssd", "evictions"), ("hot", "hot_cache_evictions")):
                if _num(row, key) is not None:
                    m.kv_evictions.observe_total(_num(row, key), start_ns, source=src, model=model, tier=tier)
                    seen["evictions"] = True
        for counter in (m.kv_lookups, m.kv_hot_hits, m.kv_evictions):
            counter.retain(src, present)
        # Process-wide budgets: OMLX keeps the largest per-model cap. 0 means
        # no budget (hot tier off, SSD cache disabled).
        self.publish(used, {"hot": _num(cache, "hot_cache_max_bytes"), "ssd": _num(cache, "disk_max_bytes")},
                     entries, {}, seen)


class VLMCacheCollector(KVCollector):
    """MLX-VLM's Automatic Prefix Cache, GET <server>/v1/cache/stats (the same
    server and API key as the health checks). Polled only while the node's
    framework is mlx-vlm. With APC off (MLX-VLM's default; APC_ENABLED=1
    turns it on) the server answers {"enabled": false}: reported as
    mlx_kv_cache_enabled 0 with every figure unavailable, never as zeros.

    Mapping (mlx-vlm 0.7.11 stats_snapshot): hot used = resident_bytes,
    capacity = memory_max_bytes (its resident budget); entries = pool_used of
    num_blocks; lookups = lookups_hit / lookups_miss; hot evictions = block
    pool evictions; ssd used / capacity / evictions = disk_bytes /
    disk_max_bytes (0 = uncapped) / disk_evictions. MLX-VLM serves one model,
    so the model label is the node's served model."""
    source = "mlx-vlm"

    def __init__(self, cfg, metrics, token):
        KVCollector.__init__(self, cfg, metrics)
        self.token = token
        self.start_ns = metrics.registry.start_ns
        self.active = False

    def poll(self, is_vlm, base, model="unknown"):
        if not is_vlm:
            if self.active:
                # The node stopped running MLX-VLM: drop its figures entirely.
                self.clear(up=0)
                self.m.kv_up.replace([], source=self.source)
                self.active = False
            return
        if not self.active:
            self.start_reporting()
            self.active = True
        obj = self._get(base + "/v1/cache/stats", 10, self.token())
        if isinstance(obj, str):
            return self.fail(obj)
        if obj.get("enabled") is not True:
            self.clear(up=1)
            self.m.kv_enabled.replace([(0, {"source": self.source})], source=self.source)
            return
        self.apply(obj, model)

    def apply(self, snap, model):
        m, src = self.m, self.source
        model = self._model(model)
        seen = {}
        used, entries = [], []
        for tier, key in (("hot", "resident_bytes"), ("ssd", "disk_bytes")):
            if _num(snap, key) is not None:
                used.append((_num(snap, key), {"tier": tier, "model": model}))
                seen[tier + "_used"] = True
        if _num(snap, "pool_used") is not None:
            entries.append((_num(snap, "pool_used"), {"tier": "hot", "model": model}))
        hits, misses = _num(snap, "lookups_hit"), _num(snap, "lookups_miss")
        if hits is not None and misses is not None:
            m.kv_lookups.observe_total(hits, self.start_ns, source=src, model=model, result="hit")
            m.kv_lookups.observe_total(misses, self.start_ns, source=src, model=model, result="miss")
            seen["lookups"] = True
        for tier, key in (("hot", "evictions"), ("ssd", "disk_evictions")):
            if _num(snap, key) is not None:
                m.kv_evictions.observe_total(_num(snap, key), self.start_ns, source=src, model=model, tier=tier)
                seen["evictions"] = True
        for counter in (m.kv_lookups, m.kv_evictions):
            counter.retain(src, {model})
        self.publish(used, {"hot": _num(snap, "memory_max_bytes"), "ssd": _num(snap, "disk_max_bytes")},
                     entries, {"hot": _num(snap, "num_blocks")}, seen)


# --------------------------------------------------------------------------
# Exporter
# --------------------------------------------------------------------------

class Exporter:
    def __init__(self, cfg, metrics, ready=None):
        self.cfg, self.m = cfg, metrics
        self.ready = ready
        self._last_error_log = None

    def export_once(self):
        payload = json.dumps(self.m.registry.to_otlp()).encode()
        req = urllib.request.Request(self.cfg["otlp_endpoint"], data=payload, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 - loopback collector
                resp.read()
                return 200 <= resp.status < 300
        except Exception as exc:
            self.m.export_failures.add(1)
            if self._last_error_log is None or time.monotonic() - self._last_error_log > 300:
                self._last_error_log = time.monotonic()
                LOG.warning("OTLP export to %s failed: %s", self.cfg["otlp_endpoint"], exc.__class__.__name__)
            return False

    def run_forever(self, stop):
        if self.ready is not None:
            # Bounded, so a stuck first tick never stops the export.
            self.ready.wait(max(30.0, 2 * self.cfg["health_check_interval_seconds"]))
        while not stop.wait(self.cfg["export_interval_seconds"]):
            self.export_once()
        self.export_once()


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description="MLX-LM / MLX-VLM telemetry agent")
    parser.add_argument("--config", help="JSON config file")
    parser.add_argument("--check-config", metavar="FILE", help="validate a config file and exit")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.check_config:
        try:
            load_config(args.check_config)
        except (OSError, ValueError) as exc:
            print("invalid mlx-telemetry config: %s" % exc, file=sys.stderr)
            return 1
        print("mlx-telemetry config OK")
        return 0
    if not args.config:
        parser.error("--config is required")
    try:
        cfg = load_config(args.config)
    except (OSError, ValueError) as exc:
        LOG.error("invalid config %s: %s", args.config, exc)
        return 1

    metrics = Metrics(cfg)
    state = State(cfg)
    stop = threading.Event()
    health = Health(cfg, metrics, state)
    exporter = Exporter(cfg, metrics, health.ready)
    server = None
    if cfg["proxy_enabled"]:
        try:
            server = run_proxy(cfg, metrics, state)
        except OSError as exc:
            LOG.error("cannot listen on %s:%s: %s", cfg["proxy_host"], cfg["proxy_port"], exc)
            return 1
        metrics.active.replace([(0, {"framework": state.framework})])

    def shutdown(signum, _frame):
        LOG.info("received signal %s, shutting down", signum)
        stop.set()
        if server is not None:
            threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    threading.Thread(target=health.run_forever, args=(stop,), name="health", daemon=True).start()
    export_thread = threading.Thread(target=exporter.run_forever, args=(stop,), name="export", daemon=True)
    export_thread.start()
    LOG.info("mlx-telemetry %s started (framework=%s, proxy=%s, hardware=%s, quality=%s, omlx=%s, export=%s)",
             VERSION, cfg["framework"], cfg["proxy_enabled"], cfg["hardware_metrics_enabled"],
             cfg["quality_metrics_enabled"], cfg["omlx_enabled"], cfg["otlp_endpoint"])
    if server is not None:
        server.serve_forever(poll_interval=0.5)
        server.server_close()
    else:
        stop.wait()
    metrics.telemetry_up.set(0)
    export_thread.join(timeout=15)
    return 0


if __name__ == "__main__":
    sys.exit(main())
