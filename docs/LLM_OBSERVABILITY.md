# LLM (MLX-LM / MLX-VLM) observability

Optional monitoring for Mac Minis that serve models with `mlx_lm.server` or `mlx_vlm.server` (both speak the same OpenAI-compatible API), with optional KV-cache readings from OMLX. It extends the existing stack rather than adding a new one:

- metrics go through each Mac's existing OpenTelemetry Collector to the existing VictoriaMetrics;
- the dashboards are provisioned into the existing Grafana;
- nothing needs sudo, and there is no Prometheus server or node_exporter.

**Status:** validated on one Apple Silicon Mac (macOS 27.0.1), as an unprivileged user over `ansible_connection: local`. That run used a real `mlx_lm.server` 0.32.2 serving `mlx-community/SmolLM2-135M-Instruct-8bit`. MLX-VLM support was then validated the same way against real `mlx_lm.server` 0.32.2 (Qwen3-4B-Instruct-2507-4bit) and `mlx_vlm.server` 0.7.11 (SmolVLM-256M-Instruct); the OMLX adapter has only been tested against a stub of OMLX's documented stats endpoint, not a real OMLX. See [Validation](#validation). It has not been tested on the Mac Minis, with production models, or with real client applications.

## Architecture

```
client app ──HTTP──▶ MLX telemetry proxy (127.0.0.1:18080) ──▶ mlx_lm.server or mlx_vlm.server (127.0.0.1:8080)
                         │  timing, status, usage, MLX-VLM timings (no content kept)
                         │  + health checks + framework detection + unprivileged hardware readings
                         │  + optional: GET <omlx_url>/admin/api/stats (OMLX KV cache)
                         ▼  OTLP/HTTP JSON, loopback
                  existing otelcol-contrib (receiver otlp/llm on 127.0.0.1:14318)
                         │  resourcedetection (host_name), batch
                         ▼  existing otlphttp exporter
                  VictoriaMetrics ──▶ Grafana: 3 LLM dashboards + 10 alert rules
```

| Piece | Where | What it is |
| --- | --- | --- |
| `roles/observability_llm` | monitored nodes, second role of the agents play in `site.yml` | Installs and manages the telemetry agent. It does nothing unless `llm_observability_enabled` is true for the host. |
| MLX telemetry agent | `~/tools/observability/bin/mlx-telemetry`, LaunchAgent `com.observability.user.mlxtelemetry` | One Python 3.9+ process using only the standard library, with four parts: the proxy, health checks, hardware readings and optional quality heuristics. Its config is `etc/mlx-telemetry.json` (0600). Logs go to `var/log/mlxtelemetry.err.log`. |
| `otlp/llm` receiver | the existing collector config (`roles/observability_agent`) | Present only when `llm_observability_enabled` is true, and bound to `127.0.0.1`. When the flag is false, the collector config renders byte-for-byte as before. |
| Dashboards and alerts | `roles/observability_server` | Provisioned as soon as any monitored node has the feature enabled. |

The role never starts, stops or reconfigures the inference server (`mlx_lm.server`, `mlx_vlm.server` or OMLX) or its LaunchAgent. If you set `mlx_launchagent_label`, that label is only read with `launchctl print`.

## How applications must connect

MLX-LM serves its OpenAI-compatible API on `mlx_server_port` (8080 by default), and that stays unchanged. Requests are measured **only if the client sends them to the proxy**:

```
before:  base_url = http://127.0.0.1:8080/v1
after:   base_url = http://127.0.0.1:18080/v1      # mlx_telemetry_host:mlx_telemetry_port
```

Nothing is redirected automatically. Clients that keep calling MLX-LM directly still work, but they appear only in the health, process and hardware panels, never in the request metrics. To measure clients on other machines, set `mlx_telemetry_host: 0.0.0.0` and open that port. Local Network Privacy does not restrict incoming connections.

What the proxy does with requests:
- It forwards every method and path. Only `POST /v1/chat/completions`, `POST /chat/completions` (MLX-VLM's primary route; MLX-LM serves it too) and `POST /v1/completions` are measured.
- Request bodies, including image content (`image_url` parts, base64 data URLs), are forwarded byte for byte. The only change ever made is the `stream_options` addition described below.
- Streams are relayed line by line as they arrive, never buffered.
- MLX-LM's status code and headers are passed through. The proxy answers 502 itself when MLX-LM is unreachable and 504 when it times out.
- A client hang-up closes the upstream connection, which stops generation.
- Client `Authorization` headers are forwarded but never stored or logged.
- `GET /-/mlx-telemetry/status` is answered by the proxy itself and is used by the play's verification.

**Token usage on streams.** MLX-LM includes usage in a stream only when the request sets `stream_options.include_usage`. With `llm_proxy_stream_usage: true` (the default), the proxy handles a streaming request that has no `stream_options` like this:
1. It adds `{"include_usage": true}` to the request.
2. It reads the usage event MLX-LM then sends.
3. It removes that event before it reaches the client.

The client therefore receives exactly the bytes MLX-LM would have sent without the option. In testing, the event streams with and without the proxy were identical apart from per-request ids and timestamps. Requests that set `stream_options` themselves are passed through unchanged. One visible side effect: when the server rejects such a request (for example MLX-VLM's 422 for a missing `model`), its error body echoes the request, including the added `stream_options`. With the option set to false, streaming requests without usage are counted in `mlx_requests_usage_missing_total`, and no token counts are guessed.

## MLX-LM and MLX-VLM

One agent and one proxy serve both; there is no separate VLM agent. Each Mac reports the framework it actually runs as `mlx_framework_info{framework, source}` and as a `framework` label (`mlx-lm`, `mlx-vlm` or `unknown`) on every request-derived series.

**Detection,** re-evaluated on every health check (`llm_health_check_interval`), in this order:

| `source` | How |
| --- | --- |
| `config` | `llm_framework` is set to `mlx-lm` or `mlx-vlm` for the host. Use it only when detection is ambiguous, for example a server behind another proxy or started from a custom wrapper. |
| `http` | What answers `GET /health` on `mlx_server_port`: MLX-VLM stamps every response, including a 401, with `Server: mlx_vlm/<version>`; MLX-LM answers with exactly `{"status": "ok"}` (or `"unavailable"`) and Python's `BaseHTTP` Server header. |
| `process` | A server process: `mlx_process_pattern` (MLX-LM) or `mlx_vlm_process_pattern` (MLX-VLM). With the default patterns the process must be a Python interpreter or the server's own entry point, so editors, `tail`, `grep` or the shell that launched the server (whose command line also contains `mlx_lm.server`) never count. Among several servers, the one whose `--port` is `mlx_server_port` wins. |
| `none` | Nothing answers and no server process exists: `framework="unknown"`, `mlx_server_up 0`, `mlx_process_up 0`. A Mac with neither framework is never reported healthy. |

OMLX wraps MLX-LM and MLX-VLM itself and is not claimed by `http` detection; if clients reach OMLX through this proxy, set `llm_framework`, and `mlx_process_pattern` to match its process, for that host.

**Differences the agent handles** (measured with mlx-lm 0.32.2 and mlx-vlm 0.7.11):

| | MLX-LM | MLX-VLM |
| --- | --- | --- |
| Chat route | `/v1/chat/completions`, `/chat/completions` | `/chat/completions`, `/v1/chat/completions` |
| `/v1/completions` | yes | no (404); the inference probe uses chat instead |
| `model` field | optional | required (422 without it); the probe sends the served model |
| `/health`, `/v1/models` with an API key | no key support | 401 when started with `MLX_VLM_SERVER_API_KEY`: put the key in a 0600 file and set `llm_server_api_key_file`. Without it, the server is still identified as MLX-VLM but reported down. Client requests keep their own `Authorization` header. |
| `usage` | `prompt_tokens`, `completion_tokens`, `prompt_tokens_details.cached_tokens` | the same fields; every stream chunk also carries `"usage": null` |
| `timings` | not sent | `prompt_ms`, `predicted_per_second`, …, exported as `mlx_server_reported_*` |
| Errors during a stream | stream ends early (`upstream_error`) | a `data: {"error": ...}` event inside the 200 stream (`server_error`) |

### MLX-LM / MLX-VLM telemetry parity

| Area | MLX-LM | MLX-VLM | Notes |
| --- | --- | --- | --- |
| Process up, restarts, uptime, CPU, RSS, footprint | yes | yes | `mlx_process_*{framework}`; both-frameworks count in `mlx_framework_processes` |
| Server up (`/health`), check duration, last success, failures | yes | yes | MLX-VLM with an API key needs `llm_server_api_key_file` |
| Model API (`/v1/models`) | yes | yes | |
| Inference probe (optional) | `/v1/completions` | chat with the served model | MLX-VLM has no `/v1/completions` and requires `model` |
| Model name, max context | yes | yes | |
| Model readiness / loading | via `/health` (503 while not ready) | via `/health` | no separate readiness metric; neither server reports load progress |
| Host CPU, memory, disk, network, load | yes | yes | existing hostmetrics pipeline, joined with `framework` in comparison panels |
| Hardware (GPU, memory pressure, thermal) | yes | yes | node-level, framework-independent |
| Agent health, export failures | yes | yes | `mlx_telemetry_info{framework}` |
| Requests, rate, active, outcomes, status codes, upstream failures | yes | yes | |
| Request duration histograms (p50/p90/p95/p99) | yes | yes | |
| Streaming TTFT, generation duration, tokens/s | yes | yes | |
| Non-streaming request latency | yes | yes | `mlx_request_duration_seconds{stream="false"}`, never called TTFT |
| Prompt, completion, cached tokens | yes | yes | both report `prompt_tokens_details.cached_tokens` |
| Missing usage | yes | yes | `mlx_requests_usage_missing_total` |
| Server-reported timings (prompt time, generation rate) | **not exposed** | yes | `mlx_server_reported_*` |
| Tool calls, temperature, context utilisation | yes | yes | |
| Quality heuristics (optional) | yes | yes | computed from text output, same method |
| KV cache | **not exposed** | yes, when the server runs with `APC_ENABLED=1` | `mlx_kv_cache_*{source="mlx-vlm"}`; OMLX adds `source="omlx"` |
| Image content | n/a | forwarded byte for byte, never logged | |

## Enable on a node

1. In the inventory, set the flag for each Mac that runs `mlx_lm.server`, or for a child group of `monitored_nodes` via `group_vars/<group>.yml`. Do not change the fleet default in `group_vars/all.yml`.
   ```yaml
   mac-mini-02:
     ansible_host: REPLACE_WITH_NODE2_IP
     ansible_user: REPLACE_WITH_SSH_USER
     llm_observability_enabled: true
     mlx_server_port: 8080                    # what mlx_lm.server --port uses on this Mac
     mlx_launchagent_label: com.example.mlx   # optional: label of the existing MLX LaunchAgent
     # llm_framework: mlx-vlm                 # optional: only if auto-detection is ambiguous
   ```
   Existing MLX-LM hosts need no new variables: the defaults detect the framework.
2. Deploy as the normal user. Run the agents play first, so the collector gains the `otlp/llm` receiver and the telemetry agent is installed, then the server play for the dashboards and alerts:
   ```bash
   ansible-playbook -i inventories/production/hosts.yml site.yml --tags agent --limit mac-mini-02
   ansible-playbook -i inventories/production/hosts.yml site.yml --tags server
   # whole fleet, side-by-side ports as currently deployed:
   ansible-playbook -i inventories/production/hosts.yml site.yml -e observability_side_by_side=true
   ```
   `--tags llm` runs only this role. It refuses to continue if the collector config has no `otlp/llm` receiver yet.
3. Point the applications at the proxy (see [How applications must connect](#how-applications-must-connect)).

Re-runs are idempotent: in testing, a second run reported `changed=0`. A changed agent file, config or plist restarts only the telemetry agent; a changed collector config restarts only the collector. A new worker needs nothing beyond its inventory entry.

Like the other agents, the telemetry agent is **not started at boot**. After a reboot, re-run the agents play for that Mac (see "LaunchAgent lifecycle" in [LAUNCHAGENT_VARIANT.md](LAUNCHAGENT_VARIANT.md)). The stale-telemetry alert flags a Mac where this happened.

## Variables

**Worker** (`roles/observability_llm/defaults/main.yml`; override per host or group):

| Variable | Default | Meaning |
| --- | --- | --- |
| `llm_observability_enabled` | `false` (set in `group_vars/all.yml`) | Enables the feature. Changing true to false removes the telemetry LaunchAgent and the collector receiver. |
| `mlx_server_host` / `mlx_server_port` | `127.0.0.1` / `8080` | Where `mlx_lm.server` listens. |
| `mlx_telemetry_host` / `mlx_telemetry_port` | `127.0.0.1` / `18080` | Where the proxy listens, for clients to use. |
| `mlx_launchagent_label` | `""` | The existing MLX LaunchAgent's label. Optional; used to read process state and launchd's run count. |
| `mlx_process_pattern` | `mlx_lm(\.server\b\|\s+server\b\|/server\.py\b)` | Used to find an MLX-LM server process when no label is set. |
| `mlx_vlm_process_pattern` | `mlx_vlm(\.server\b\|\s+server\b\|/server(/__main__)?\.py\b)` | The same for MLX-VLM. A customised pattern is used as is, without the interpreter check. |
| `llm_framework` | `auto` | `auto`, `mlx-lm` or `mlx-vlm`. See [MLX-LM and MLX-VLM](#mlx-lm-and-mlx-vlm). |
| `llm_server_api_key_file` | `""` | 0600 file whose first line is the server's API key, sent on the agent's own `/health`, `/v1/models` and probe requests (MLX-VLM with `MLX_VLM_SERVER_API_KEY`). Only the path is rendered into the agent's config. |
| `llm_vlm_cache_stats_enabled` | `true` | Poll MLX-VLM's prefix-cache stats on MLX-VLM nodes (no effect on MLX-LM nodes). |
| `omlx_monitoring_enabled` | `false` | Poll OMLX for KV-cache figures. See [KV cache (OMLX)](#kv-cache-mlx-vlm-prefix-cache-omlx). |
| `omlx_url` / `omlx_api_key_file` | `http://127.0.0.1:8000` / `""` | OMLX's address, and a 0600 file holding OMLX's main API key (not needed when OMLX runs loopback-only with authentication disabled). |
| `omlx_poll_interval` / `omlx_timeout` | `30` / `5` | Seconds between polls; request timeout. |
| `mlx_model_path` / `llm_hf_cache_dir` | `""` | Where to find the model's `config.json`, for its maximum context length. A Hugging Face repo is looked up in `$HF_HUB_CACHE`, `$HF_HOME/hub` or `~/.cache/huggingface/hub`. |
| `llm_model_name` | `""` | Fixes the `model` label. When empty, it comes from the server's `--model` argument; a path is reduced to its last component, so home directories never become labels. |
| `llm_model_max_context_tokens` | `0` | Sets the maximum context explicitly. `0` means read it from `config.json`. |
| `llm_model_label_max_values` | `10` | Cardinality cap: model names beyond this many are labelled `other`. |
| `llm_metrics_enabled` | `true` | Runs the proxy (request telemetry). |
| `llm_proxy_stream_usage` | `true` | Adds and removes `include_usage` on streams, as described above. |
| `llm_proxy_read_timeout` / `llm_proxy_connect_timeout` / `llm_proxy_max_request_bytes` | `600` / `5` / 32 MiB | Proxy limits. |
| `llm_health_check_interval` / `llm_models_check_interval` | `15` / `60` | Seconds between checks. |
| `llm_inference_probe_enabled` / `llm_inference_probe_interval` | `false` / `300` | Optional one-token probe sent directly to the server rather than through the proxy: `/v1/completions` on MLX-LM, `/v1/chat/completions` with the served model on MLX-VLM. |
| `llm_hardware_metrics_enabled` / `llm_hardware_interval` | `true` / `15` | Unprivileged hardware readings. |
| `llm_quality_metrics_enabled` | `false` | Text heuristics, plus their thresholds and weights. |
| `llm_activity_expected` | `false` | Enables the "no inference activity" alert for this node. |
| `llm_telemetry_python` | Ansible's own interpreter on the host | Any Python 3.9 or later works, including `/usr/bin/python3`. |
| `llm_require_mlx_reachable` | `true` | Fails the play if MLX-LM `/health` does not answer at deploy time. |
| `llm_otel_endpoint` / `llm_export_interval` | the loopback receiver / `15` | Where and how often the agent exports. |

**Fleet** (`group_vars/all.yml`): `llm_otlp_receiver_port: 14318`. It must not collide with `otel_internal_metrics_port` (18888) or with MLX-LM.

**Monitoring Mac** (`roles/observability_server/defaults/main.yml`):
- `grafana_llm_dashboards_enabled` and `grafana_llm_alerts_enabled` are derived from the inventory; set them explicitly to override.
- The dashboard UIDs are `llm-cluster-overview`, `llm-node-detail` and `llm-performance-quality`.
- The alert thresholds are `llm_alert_ttft_p95_seconds` (5), `llm_alert_generation_p95_seconds` (120), `llm_alert_error_rate_percent` (5), `llm_alert_min_requests` (5), `llm_alert_inactivity_minutes` (60), `llm_alert_memory_pressure_level` (2), `llm_alert_restarts_per_15m` (2), `llm_alert_stale_minutes` (10) and `llm_alert_thermal_state` (2).

## Metrics

Names are as stored in VictoriaMetrics (`-opentelemetry.usePrometheusNaming`). Every series also carries `host_name` (from the collector's `resourcedetection`), `service_name="mlx-telemetry"` and `service_version`.

Label sets used below:
- **`{model, framework, endpoint, stream}`:** `framework` is `mlx-lm`, `mlx-vlm` or `unknown` (the node's detected framework when the request arrived); `endpoint` is `chat` or `completion`; `stream` is `true` or `false`. Every request-derived metric below carries `framework`, alongside the labels listed. Series recorded by agent 1.0.0 have no `framework` label; dashboards still include them under *All*.
- **`outcome`:** one of `success`, `client_error` (4xx), `server_error` (5xx, or an error event inside a 200 stream), `upstream_error` (MLX-LM unreachable, the read failed, or the stream ended without `[DONE]`), `timeout`, or `client_disconnected`.

### Requests (proxy)

| Metric | Type | Labels | Definition |
| --- | --- | --- | --- |
| `mlx_requests_total` | counter | model, endpoint, stream, outcome, status_code | Each measured request, counted once when it finishes. |
| `mlx_requests_active` | gauge | framework | Requests in flight. |
| `mlx_request_duration_seconds` | histogram | model, endpoint, stream, outcome | End to end: from the proxy reading the request to the last byte it relays. With `stream="false", outcome="success"` it is the **non-streaming request latency** the dashboards show separately: the proxy only sees a non-streaming response once generation is complete, so it is whole-request latency, never reported as TTFT. |
| `mlx_time_to_first_token_seconds` | histogram | model, endpoint | Streaming only: from request start to the first SSE event carrying `delta.content`, `delta.reasoning`, `delta.tool_calls` or `text`. Response headers and `: keepalive` comments do not count. |
| `mlx_generation_duration_seconds` | histogram | model, endpoint | Streaming only: from that first content event to `[DONE]`. |
| `mlx_completion_tokens_per_second` | histogram | model, endpoint | Streaming only, and only when usage is reported and there are at least 2 completion tokens: `(completion_tokens − 1) / generation duration`, i.e. tokens produced after the first one. |
| `mlx_tokens_prompt_total` / `mlx_tokens_completion_total` | counter | model, endpoint, stream | The `usage.prompt_tokens` / `usage.completion_tokens` values MLX-LM reports. |
| `mlx_tokens_prompt_cached_total` | counter | model, endpoint, stream | `usage.prompt_tokens_details.cached_tokens`: prompt tokens MLX-LM served from its prompt cache. |
| `mlx_request_prompt_tokens`, `_completion_tokens`, `_context_tokens` | histogram | model | Tokens per request. Context tokens = prompt + completion. |
| `mlx_request_context_utilization` | histogram | model | Context tokens ÷ maximum context. Recorded only when the maximum is known. |
| `mlx_requests_usage_missing_total` | counter | model, endpoint, stream | Successful requests without usage. Their tokens are not estimated. |
| `mlx_request_temperature` | histogram | model | `temperature`, only when the client set it explicitly. |
| `mlx_tool_calls_total` | counter | model | Tool calls returned by MLX-LM. |
| `mlx_request_last_success_timestamp_seconds` | gauge | model | Unix time of the last successful request. |
| `mlx_upstream_connection_failures_total` | counter | reason (`connect`, `read`, `timeout`), framework | Times the proxy could not reach MLX-LM or read from it. |
| `mlx_proxy_passthrough_requests_total` | counter | – | Unmeasured requests that were forwarded anyway, such as `/v1/models`. |
| `mlx_server_reported_tokens_per_second` | histogram | model, stream | The generation rate the server itself reports per request (`timings.predicted_per_second`). The only generation rate for non-streaming requests. MLX-VLM only; MLX-LM sends no timings, so this stays empty there. Streaming requests carry it only with usage. |
| `mlx_server_reported_prompt_duration_seconds` | histogram | model, stream | The server's own prompt-processing time (`timings.prompt_ms`, which MLX-VLM derives from its prompt rate). Not measured by the proxy, and not TTFT. MLX-VLM only. |

Request counters are created at the first request. Before any request, dashboards show `0` while the proxy is running (detected via `mlx_telemetry_info{proxy="enabled"}`), or "No proxy telemetry" when there is no telemetry at all. Missing telemetry is never displayed as zero activity.

### Health, process and model

Every series in this table except `mlx_model_*` carries the node's `framework` label (relabelled in place when the framework changes, so no stale series of the old framework remain).

| Metric | Definition |
| --- | --- |
| `mlx_server_up` | 1 if `GET /health` returned 200 at the last check. |
| `mlx_models_api_up` | 1 if `GET /v1/models` returned a model list. |
| `mlx_inference_probe_success`, `mlx_inference_probe_duration_seconds` | Result of the optional one-token probe. |
| `mlx_health_last_success_timestamp_seconds{check}`, `mlx_health_check_failures_total{check}`, `mlx_health_check_duration_seconds{check}` | Per check; `check` is `http`, `models` or `inference`. |
| `mlx_process_up` | 1 if the server process was found (via the label, or the pattern match). |
| `mlx_process_memory_rss_bytes` | `ps` RSS. This excludes most Metal/GPU buffers: 57 MB against a 316 MB footprint in testing. |
| `mlx_process_memory_footprint_bytes` | Physical footprint, the MEM column of `top`. This includes the Metal buffers holding the weights and KV cache. |
| `mlx_process_cpu_utilization_percent` | `ps %cpu` (100 = one core). |
| `mlx_process_restarts_total` | Observed changes of the server's pid. |
| `mlx_process_uptime_seconds` | Time since the server process started (`ps etime`). |
| `mlx_framework_processes{framework}` | MLX-LM and MLX-VLM server processes found, always reported for both frameworks: a worker genuinely running both shows 1 for each. The agent measures the one on `mlx_server_port`. |
| `mlx_process_launchd_runs` | launchd's `runs` count, only when `mlx_launchagent_label` is set. |
| `mlx_model_info{model, source}` | Always 1. `source` is `config`, `process` or `models_api`. |
| `mlx_framework_info{framework, source}` | Always 1. See [MLX-LM and MLX-VLM](#mlx-lm-and-mlx-vlm). |
| `mlx_model_context_length_max{model}` | `max_position_embeddings` (or a similar key) from `config.json`, or `llm_model_max_context_tokens`. |
| `mlx_telemetry_up`, `mlx_telemetry_info{version, proxy, hardware, quality, omlx, framework}`, `mlx_telemetry_export_failures_total`, `mlx_activity_expected` | The agent's own status. |

### Hardware (unprivileged sources only)

| Metric | Source |
| --- | --- |
| `macos_gpu_utilization_percent`, `macos_gpu_renderer_utilization_percent` | `ioreg -c IOAccelerator`, PerformanceStatistics → `Device Utilization %` and `Renderer Utilization %`. These keys are undocumented and may change between macOS releases. |
| `macos_gpu_memory_in_use_bytes`, `macos_gpu_memory_allocated_bytes` | The same IORegistry entry: `In use system memory` and `Alloc system memory`. |
| `macos_memory_pressure_level` | `sysctl kern.memorystatus_vm_pressure_level`, mapped to 0 normal, 1 warning, 2 critical. |
| `macos_thermal_state` | `NSProcessInfo.thermalState`, a public API called through the Objective-C runtime: 0 nominal, 1 fair, 2 serious, 3 critical. |
| `macos_hardware_metric_available{metric}` | 1 if the reading is collected on this node, 0 if not. Dashboards use it to show "Unavailable" rather than an empty or zero panel. |

CPU, memory, disk, network and load come from the existing `hostmetrics` pipeline and are reused, not collected again.

### KV cache (MLX-VLM prefix cache, OMLX)

One metric family, `mlx_kv_cache_*`, labelled by **`source`**: the same panels show both sources, and the dashboards' KV sections work for either. **MLX-LM exposes no KV-cache statistics** over HTTP (0.32.2), so MLX-LM nodes report none.

| `source` | Where it comes from | When |
| --- | --- | --- |
| `mlx-vlm` | MLX-VLM's Automatic Prefix Cache (APC): `GET /v1/cache/stats` on the same server, with `llm_server_api_key_file` if set | On nodes whose framework is `mlx-vlm`, every `llm_models_check_interval`, unless `llm_vlm_cache_stats_enabled: false`. APC is **off by default** in MLX-VLM (the server's `APC_ENABLED=1` turns it on); with it off, the node reports `mlx_kv_cache_enabled{source="mlx-vlm"} 0` and no figures, never zeros. |
| `omlx` | [OMLX](https://github.com/jundot/omlx)'s documented admin endpoint `GET <omlx_url>/admin/api/stats` (Bearer = OMLX's main API key from `omlx_api_key_file`) | Only with `omlx_monitoring_enabled: true`. OMLX is optional and never installed or managed here. Only `runtime_cache` and `uptime_seconds` are read; the same response also carries OMLX's own API key and local paths, which are never read, exported or logged. |

What each source exposes, and what is exported (mlx-vlm 0.7.11, OMLX 0.7.3rc1 / `main`, October 2026):

| Figure | MLX-VLM APC | OMLX | Metric |
| --- | --- | --- | --- |
| Used, in memory | `resident_bytes` | `hot_cache_size_bytes`, per model | `mlx_kv_cache_used_bytes{source, tier="hot", model}` |
| Used, on disk | `disk_bytes` (APC disk tier) | `total_size_bytes`, per model | `mlx_kv_cache_used_bytes{source, tier="ssd", model}` |
| Capacity | `memory_max_bytes` (APC resident budget), `disk_max_bytes` (0 = uncapped, not reported) | `hot_cache_max_bytes`, `disk_max_bytes` (process-wide) | `mlx_kv_cache_capacity_bytes{source, tier}` |
| Utilisation % | derived: used ÷ capacity, only when a capacity is reported | same | `mlx_kv_cache_utilization_percent{source, tier}` |
| Blocks | `pool_used` of `num_blocks` | hot entries, SSD files | `mlx_kv_cache_entries{source, tier, model}`, `mlx_kv_cache_entries_capacity{source, tier}` (MLX-VLM only) |
| Hit / miss | `lookups_hit`, `lookups_miss` | `hits`, `misses` (+ `hot_cache_hits`) | `mlx_kv_cache_lookups_total{source, model, result}`, `mlx_kv_cache_hot_hits_total` (OMLX) |
| Evictions | `evictions` (block pool, hot), `disk_evictions` (ssd) | `hot_cache_evictions`, `evictions` (ssd) | `mlx_kv_cache_evictions_total{source, model, tier}` |
| Allocated, as distinct from used | **no** | **no** | `mlx_kv_cache_metric_available{source, metric="allocated"} 0` |
| KV memory of in-flight requests | **no** | **no** | – |

`mlx_kv_cache_source_up{source}` is 1 while polls succeed; failures count in `mlx_kv_cache_scrape_failures_total{source, reason}` (`connect`, `auth`, `http`, `parse`), and that source's figures are dropped until the next good poll, so panels say "Not reported" rather than showing stale or zero values. `mlx_kv_cache_metric_available{source, metric}` says which figures a source reported. Counters are the sources' own (OMLX's carry OMLX's start time); a server restart is a normal counter reset. A node that stops running MLX-VLM drops its `mlx-vlm` series entirely. Model names are sanitised and capped by `llm_model_label_max_values`.

**Renamed from agent 1.1.0:** the `omlx_up`, `omlx_scrape_failures_total` and `omlx_kv_cache_*` series of the previous commit are now `mlx_kv_cache_source_up`, `mlx_kv_cache_scrape_failures_total` and `mlx_kv_cache_*` with `source="omlx"`. The dashboards use the new names.

To enable OMLX on a Mac that runs it:

```yaml
omlx_monitoring_enabled: true
omlx_url: http://127.0.0.1:8000                     # OMLX's default port
omlx_api_key_file: /Users/<user>/.config/omlx.key  # 0600, first line = OMLX's main API key
```

### Quality heuristics (optional, `llm_quality_metrics_enabled`)

Each successful response of at least 20 words is scored in memory, from at most `quality_max_chars` (64 KiB) of text, and then discarded. **These are text heuristics. They are not a hallucination detector or a fact check.**

| Signal | Definition | Flag when |
| --- | --- | --- |
| repetition | 1 − (unique word trigrams ÷ all trigrams) | ≥ `llm_quality_repetition_threshold` (0.3) |
| hedging | Hedge phrases ("I think", "probably", "might be", "not sure", …) per 100 words | ≥ `llm_quality_hedging_threshold` (2.0) |
| refusal | A refusal phrase ("I can't help", "as an AI", …) is present | present |
| heuristic risk score | 0.5·min(1, repetition/threshold) + 0.3·min(1, hedging/threshold) + 0.2·refusal (weights: `llm_quality_risk_weights`) | – |

The resulting metrics are `mlx_quality_evaluations_total`, `mlx_quality_flags_total{flag}`, `mlx_quality_flagged_responses_total`, `mlx_quality_repetition` (histogram), `mlx_quality_heuristic_risk_score` (histogram) and `mlx_quality_last_heuristic_risk_score`, all labelled by `model` and `framework`.

**Groundedness is not evaluated**, because there is no reference source or evaluator. To add one, compute it inside `ProxyHandler._record` in `files/mlx_telemetry.py`, where the response text is available in memory, and export it as a new `mlx.quality.*` metric.

### Data hygiene

- No prompts, completions, request ids, conversation ids or user ids appear in metrics, labels or logs.
- The agent writes no access log.
- Labels are limited to bounded values. Model names are sanitised and capped.

## Unavailable metrics and why

| Metric | Why not |
| --- | --- |
| KV-cache size, utilisation, cached-sequence count, evictions **without OMLX** | `mlx_lm.server` 0.32.2 exposes none of these over HTTP, and this project does not patch MLX-LM or MLX-VLM (MLX-LM exposes none; MLX-VLM's prefix cache and OMLX do, see [KV cache (OMLX)](#kv-cache-mlx-vlm-prefix-cache-omlx)). With OMLX, see that section; allocated-vs-used and in-flight KV memory stay unavailable there too. The `--prompt-cache-size`/`--prompt-cache-bytes` flags set limits, but their current usage is not reported. A utilisation figure would need both, so none is estimated, and certainly not from host memory. The prompt-cache reuse figure (`mlx_tokens_prompt_cached_total`) is the cache signal MLX-LM does report. Extension point: an `mlx.kv_cache.*` gauge in the agent, once MLX-LM exposes it. |
| GPU frequency; CPU, GPU and ANE power | These come from `powermetrics`, which requires root, or from the private IOReport API. This variant runs without sudo and uses only supported, unprivileged sources, so `macos_hardware_metric_available` reports them as 0. |
| TTFT and generation duration for non-streaming requests | A non-streaming response arrives only after generation is complete and says nothing about when its first token was generated, so the proxy records whole-request latency (`mlx_request_duration_seconds{stream="false"}`) instead. MLX-VLM's own `timings` (server-reported prompt time and generation rate) are exported separately as `mlx_server_reported_*` and are not called TTFT. |
| Tokens/s for non-streaming MLX-LM requests | Neither a first-token time nor server timings exist, so none is computed. |
| Token counts for streaming requests without usage | Recorded in `mlx_requests_usage_missing_total`. Counts are never derived from response length. |
| Groundedness / factual correctness | No evaluator is configured. |

## Dashboards

All three dashboards use the existing datasource (uid `victoriametrics`). They have `$host`, `$model` and `$framework` (mlx-lm / mlx-vlm / unknown) variables (`$host` is single-value on the node dashboard), link to each other, and leave the two host dashboards unchanged. There are no separate MLX-VLM dashboards: both frameworks share every panel. Panels that show a node's current framework use only `mlx_framework_info` samples newer than 90 s, so a node that switched framework is not counted under both for VictoriaMetrics' 5-minute lookback (keep `llm_export_interval` well below that).

| Dashboard (uid) | Sections |
| --- | --- |
| **LLM Cluster Overview** (`llm-cluster-overview`) | **Summary:** nodes online, requests, completion tokens, average tokens/s, tool calls, quality flags, TTFT p95 and availability (all over 5 minutes). **Inference performance:** requests/min, TTFT and generation p50/p95, tokens/min, tokens/s p50/p95, streaming vs non-streaming, error rate, active requests, mean temperature, end-to-end p50/p95, outcome/status breakdown. **Model and context:** models served, tokens per request, context utilisation, prompt-cache reuse, usage not reported, KV cache (not instrumented). **Hardware:** CPU, memory, memory pressure, MLX-LM memory, disk, GPU utilisation, GPU memory, thermal, and GPU frequency/power marked unavailable. **Service health:** a per-node table (health, process, 15-minute error rate, time since last health OK and last inference, restarts, telemetry age; each row links to the node dashboard), stale nodes, upstream failures, recent failures. **Frameworks and KV cache (OMLX):** MLX-LM / MLX-VLM / undetected node counts, OMLX nodes reporting, hot KV utilisation, KV block hit ratio, requests by framework, streaming TTFT and non-streaming latency by framework, KV utilisation and KV used per node. The Nodes table has a Framework column. **Framework comparison:** monitored / healthy / unhealthy workers, telemetry agents up, restarts, workers running both frameworks; server and process up, active requests and restarts by framework; host CPU and memory and server process memory by framework. |
| **LLM Node Detail** (`llm-node-detail`) | **Status:** node, model, health, process, time since last health OK, requests, telemetry age. **Hardware gauges:** load, memory, pressure, footprint, disk, GPU utilisation and memory, thermal, plus availability tiles. **Hardware time series.** **LLM performance.** **Troubleshooting:** failures, latency spikes (p99), restarts, health-check failures, memory pressure against active requests, and memory, footprint and latency on one axis. **Framework and KV cache (OMLX):** framework and how it was detected, OMLX status, hot/SSD KV used, capacity and utilisation, non-streaming latency with MLX-VLM's server-reported prompt time, KV used vs capacity, utilisation (and MLX-VLM block pool), hit ratio and evictions. **Server details:** model API, server uptime, restarts, server CPU, inference probe, MLX-LM / MLX-VLM process counts, request duration p50/p90/p95/p99, health-check duration. |
| **LLM Performance, KV Cache and Quality** (`llm-performance-quality`) | **Performance.** **Context and KV cache:** context used against the maximum, utilisation, maximum context, prompt-cache reuse, and KV cache marked not instrumented. **Quality heuristics:** method, evaluations, flags, risk p95 and latest, repetition p95, groundedness marked not evaluated, flags/min, risk over time. **Frameworks:** requests, streaming TTFT and decode rate by framework; non-streaming request latency; MLX-VLM's server-reported generation rate and prompt time. **KV cache (OMLX):** status, hot/SSD used, capacity and utilisation, used vs capacity, utilisation, hit ratio and evictions, entries, and which figures the sources do not report (allocated vs used). **Framework comparison:** request duration p95, tokens (prompt, completion, cached), requests without usage, error rate, generation duration p95 and heuristic risk p95, each by framework. Streaming TTFT, end-to-end and non-streaming latency panels show p50/p90/p95/p99. |

Quantiles always come from `histogram_quantile` over `_bucket` series; there are no percentiles derived from averages. Empty panels state why, for example "No streaming requests", "Heuristics disabled" or "Max context unknown".

## Alerts

Grafana provisions these from `etc/grafana/provisioning/alerting/llm_alerts.yml` into the folder **LLM Observability**. All use `noDataState: OK`, so an absent optional metric never fires an alert. Missing telemetry has its own rule.

Every rule carries `host_name` and `framework`, so one rule covers MLX-LM and MLX-VLM nodes and the notification names both (summaries read `<host> (<framework>)`). Rules on per-node series without a framework label (memory pressure, thermal state, activity, export failures) take it from the node's live `mlx_framework_info`; nodes whose agent predates framework detection still alert, without the label. Rule uids are unchanged; titles are framework-neutral and the `component` label is `mlx`.

| Rule (uid) | Fires when |
| --- | --- |
| LLM server unavailable, critical (`llm-mlx-server-down`) | `/health` is failing for 2 minutes, including nodes where no framework is detected. |
| LLM server process not found (`llm-mlx-process-missing`, new) | No MLX-LM / MLX-VLM server process for 5 minutes. |
| LLM model API or inference probe failing (`llm-mlx-health-check-failing`) | A `/v1/models` or probe failure in the last 10 minutes. |
| LLM request error rate high (`llm-mlx-error-rate`) | 5xx, upstream errors or timeouts exceed `llm_alert_error_rate_percent` over 10 minutes, with at least `llm_alert_min_requests` requests. |
| LLM time to first token p95 high (`llm-mlx-ttft-p95`) | Streaming p95 TTFT exceeds `llm_alert_ttft_p95_seconds` for 10 minutes. |
| LLM generation latency degraded (`llm-mlx-generation-p95`) | p95 generation duration exceeds `llm_alert_generation_p95_seconds` for 10 minutes. |
| LLM request latency p95 high (`llm-mlx-request-latency-p95`, new) | p95 end-to-end duration of successful requests (streaming and non-streaming) exceeds `llm_alert_request_p95_seconds` (60) for 10 minutes. |
| No LLM inference activity where expected (`llm-mlx-no-activity`) | `llm_activity_expected` is set and there was no successful request for `llm_alert_inactivity_minutes`. |
| High memory pressure on an LLM node (`llm-memory-pressure`) | Pressure is at or above `llm_alert_memory_pressure_level` for 5 minutes. |
| LLM server process restarting (`llm-mlx-restarting`) | At least `llm_alert_restarts_per_15m` pid changes in 15 minutes. |
| LLM telemetry missing or stale (`llm-telemetry-stale`) | The node reported within the last hour but not now. |
| Thermal pressure on an LLM node (`llm-thermal-pressure`) | Thermal state is at or above `llm_alert_thermal_state`, only where it is collected. |
| LLM telemetry export failing (`llm-telemetry-export-failing`, new) | At least `llm_alert_export_failures_per_15m` (3) failed OTLP exports to the local collector in 15 minutes. |

**Notifications:** this repository has no notification integration, so these rules use Grafana's default notification policy and its default (unconfigured) email contact point. To get notified, add a contact point in Grafana, or provision one under `provisioning/alerting/`, and keep its webhook or credentials out of Git, for example in Ansible Vault.

**Grafana start-up:** Grafana refuses to start at all if an alerting provisioning file fails to parse. The rules file is therefore generated as JSON from a data structure and checked with `json.load` before installation, and the rule content itself was validated against Grafana 13.2.0.

## Verify

```bash
# On the worker
launchctl print user/$(id -u)/com.observability.user.mlxtelemetry | grep -E 'state|pid'
curl -s http://127.0.0.1:18080/-/mlx-telemetry/status
curl -s http://127.0.0.1:18080/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"hi"}],"max_tokens":8,"stream":true}'
tail -f ~/tools/observability/var/log/mlxtelemetry.err.log

# On the monitoring Mac (VictoriaMetrics ignores the newest ~30 s of samples)
curl -s 'http://127.0.0.1:8429/api/v1/query' --data-urlencode 'query=max by (host_name) (mlx_telemetry_up)'
curl -s 'http://127.0.0.1:8429/api/v1/query' --data-urlencode 'query=sum by (host_name, outcome) (mlx_requests_total)'
curl -s 'http://127.0.0.1:8429/api/v1/query' --data-urlencode \
  'query=histogram_quantile(0.95, sum by (le) (rate(mlx_time_to_first_token_seconds_bucket[10m])))'
```

### Verify both frameworks

```bash
# Both framework labels present (live samples only):
curl -s 'http://<monitoring-mac>:8429/api/v1/query' --data-urlencode \
  'query=count by (framework) (mlx_telemetry_info and (time() - timestamp(mlx_telemetry_info)) < 90)'
# Per node: framework, how it was detected, health:
curl -s 'http://<monitoring-mac>:8429/api/v1/query' --data-urlencode 'query=max by (host_name, framework, source) (mlx_framework_info)'
curl -s 'http://<monitoring-mac>:8429/api/v1/query' --data-urlencode 'query=max by (host_name, framework) (mlx_server_up)'
# Requests, streaming TTFT, non-streaming latency per framework:
curl -s 'http://<monitoring-mac>:8429/api/v1/query' --data-urlencode 'query=sum by (framework, stream, outcome) (increase(mlx_requests_total[1h]))'
curl -s 'http://<monitoring-mac>:8429/api/v1/query' --data-urlencode \
  'query=histogram_quantile(0.95, sum by (le, framework) (rate(mlx_time_to_first_token_seconds_bucket[10m])))'
curl -s 'http://<monitoring-mac>:8429/api/v1/query' --data-urlencode \
  'query=histogram_quantile(0.95, sum by (le, framework) (rate(mlx_request_duration_seconds_bucket{stream="false"}[10m])))'
# KV cache per source:
curl -s 'http://<monitoring-mac>:8429/api/v1/query' --data-urlencode 'query=max by (host_name, source, tier) (mlx_kv_cache_utilization_percent)'
```

Use port 8428 instead of 8429 when not running side by side.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Play fails at "Assert the MLX-LM server is reachable" | Start `mlx_lm.server`, fix `mlx_server_host`/`mlx_server_port`, or set `llm_require_mlx_reachable: false`. |
| Play fails at "Assert the collector has the loopback OTLP receiver" | Run the agent role first (`--tags agent`) with `llm_observability_enabled: true`. |
| Proxy returns 502 | MLX-LM is down or on another port; see `mlx_server_up` and `mlx_upstream_connection_failures_total`. |
| Health panels have data but request panels say "No proxy telemetry" | Clients are calling MLX-LM directly; point them at the proxy port. |
| `mlx_telemetry_up` is missing | Check `launchctl print` for the agent, its error log, and whether the Mac rebooted (re-run the play). Also check the collector: the same Local Network Privacy issue as host metrics applies to its export ([LAUNCHAGENT_VARIANT.md](LAUNCHAGENT_VARIANT.md#local-network-privacy-why-a-worker-can-get-no-route-to-host)). |
| Gaps in every series, host metrics included | The Mac slept. Mac Minis serving models should have sleep disabled; that is an administrator setting. |
| Model shows `unknown` or `other` | Set `llm_model_name`, or check that the server is started with `--model`. |
| Framework shows `unknown` | Nothing answers `/health` on `mlx_server_port` and no server process was found: start the server or fix the port. If a server runs but cannot be identified (custom wrapper, another proxy in between), set `llm_framework` for the host. |
| MLX-VLM shows as down although it serves requests | It runs with `MLX_VLM_SERVER_API_KEY`, so `/health` answers 401: set `llm_server_api_key_file`. |
| Inference probe failing on MLX-VLM | The probe needs the served model's id: check `mlx_model_info`, or set `llm_model_name`. |
| KV cache tile says "No KV-cache source", "Disabled on server" or "Poll failing" | MLX-LM nodes have no KV source. On MLX-VLM, "Disabled on server" means the server runs without `APC_ENABLED=1`. For OMLX set `omlx_monitoring_enabled`. "Poll failing": check `mlx_kv_cache_scrape_failures_total{source, reason}` (`auth` = missing or rotated key, `connect` = server not running or wrong URL). |
| Max context "Unknown" | Set `llm_model_max_context_tokens`, `mlx_model_path` or `llm_hf_cache_dir`. |

## Disable or roll back

Set `llm_observability_enabled: false` for the host (or remove the setting), then re-run the agents and server plays. On the worker, this:
- unloads `com.observability.user.mlxtelemetry` and deletes its plist;
- re-renders the collector config without `otlp/llm`, identical to the config before this feature, and restarts the collector.

On the monitoring Mac, once no node has the feature enabled, it removes the three LLM dashboards and rewrites the alert file as a `deleteRules` list, so Grafana deletes the rules.

Host metrics, the existing dashboards and MLX-LM itself are not touched. The agent's program and config files stay under `~/tools/observability` and can be deleted by hand. To roll back the repository change, revert the commit and re-run both plays.

## Validation

Everything below ran on one Apple Silicon Mac (macOS 27.0.1, uid 501, no sudo), using the pinned binaries and the real `mlx_lm.server` 0.32.2 with `mlx-community/SmolLM2-135M-Instruct-8bit`:

- **Static checks:**
  - `ansible-playbook site.yml --syntax-check` and `ansible-lint` (production profile, 0 findings) pass.
  - The agent compiles under `/usr/bin/python3` 3.9.6 and is clean under pyflakes.
  - All three dashboards render to valid JSON with unique panel ids.
  - The rendered collector config passes `otelcol-contrib validate` (0.159.0).
  - With the feature off, the collector config renders identically to the previous branch.
- **Proxy:**
  - Streaming output through the proxy matched direct MLX-LM output (same seed, ids and timestamps removed).
  - Streams were delivered incrementally (202 events spread over 1.2 s).
  - The injected usage event was hidden from clients, while a client's own `include_usage` was kept.
  - A 400 passed through; MLX-LM down gave 502.
  - A client hang-up closed the upstream connection within 0.2 s and was recorded as `client_disconnected`.
  - An invalid config was rejected before install.
- **Full playbook:**
  - Both plays ran over `ansible_connection: local` with the feature on: `failed=0`, and a re-run reported `changed=0`.
  - The agent ran as a LaunchAgent in `user/501` under `/usr/bin/python3`.
  - Grafana loaded all five dashboards and the 10 alert rules, all with health `ok`.
  - Under traffic, 191 of 198 panel queries across the five dashboards returned data through Grafana's datasource proxy. The 7 empty ones are expected: no stale nodes, no `mlx_launchagent_label`, no heuristic flags, and no SSH sessions (Remote Login is off).
- **Rollback:** turning the feature off removed the agent, receiver, dashboards and rules, and the next run reported `changed=0`.

### MLX-VLM, TTFT and OMLX changes (agent 1.1.0)

Run on one Apple Silicon Mac (macOS 27, uid 501, no sudo). The full playbook ran over `ansible_connection: local` with scratch paths and ports; the real servers were `mlx_lm.server` 0.32.2 (`mlx-community/Qwen3-4B-Instruct-2507-4bit`) and `mlx_vlm.server` 0.7.11 (`mlx-community/SmolVLM-256M-Instruct-bf16`), started one after the other on the same port.

- **Static checks:** `ansible-playbook site.yml --syntax-check` and `ansible-lint` (production profile) pass. The agent compiles under `/usr/bin/python3` 3.9.6 and is pyflakes-clean. The three dashboards render to valid JSON with unique panel ids and no overlapping panels; every pre-existing panel keeps its id, position and query (plus the `$framework` filter). The alert file renders byte-identical to agent 1.0.0's.
- **Unit and contract tests:** 33 tests against fake MLX-LM, MLX-VLM and OMLX servers built from their source. They cover config validation, HTTP and process detection (including editors, `tail`, `grep` and wrapper shells that mention the module), streaming TTFT skipping empty and role-only events, non-streaming requests recording no TTFT, missing usage never fabricated, byte-identical forwarding of image requests and Authorization headers, MLX-VLM error events, server-reported timings, bounded label keys, OTLP serialisation, and the OMLX adapter (figures, utilisation, counter resets, 401 handling, no key or path leaking into the export).
- **Full playbook:** `failed=0` on both plays; a re-run reported `changed=0`.
- **MLX-LM (real):** detected as `mlx-lm` from `http`. Streaming, non-streaming, chat and completions requests were measured; the injected usage event was hidden from clients.
- **No server (real):** with the server stopped, the agent reported `framework="unknown"`, `source="none"`, and the proxy answered 502.
- **MLX-VLM (real):** detected as `mlx-vlm` with no inventory change. Image requests, streaming and not, gave the same output through the proxy as directly (temperature 0), with the same SSE event count. Streams were relayed incrementally (151 events over 0.7 s). Usage carried the same fields as MLX-LM, and server timings were recorded. With `MLX_VLM_SERVER_API_KEY` set, the server was still identified from its 401 and reported down until `llm_server_api_key_file` was deployed, after which `mlx_server_up` returned to 1. Clients' own keys passed through. Testing found that the probe needed a `model` field; that was fixed and the probe then passed.
- **Pipeline:** every new metric reached VictoriaMetrics under the names documented above, and host metrics were unaffected. 248 panel queries across the five dashboards ran through Grafana's datasource proxy with 0 errors; 226 returned data. The 22 empty ones are expected: quality heuristics off, no request errors, no stale or undetected nodes, no MLX-LM node at that moment, no restarts, no SSH sessions.
- **OMLX:** only against a **stub** serving the shape of OMLX's `/admin/api/stats` (taken from OMLX's source), through the full pipeline to Grafana. **Not tested against a real OMLX instance.**

### MLX-VLM parity round (agent 1.2.0)

Same Mac and constraints, with two workers at once: the Ansible-deployed node (`Mac`) running real `mlx_lm.server` 0.32.2 (SmolLM2-135M-Instruct-8bit) through the normal LaunchAgent and collector, and a **simulated** second worker (`mm-vlm-sim`) running real `mlx_vlm.server` 0.7.11 (SmolVLM-256M, `APC_ENABLED=1`) on another port. The simulated worker is the unmodified agent file started by a test-only wrapper that sets `host.name`, exporting through its own `otelcol-contrib` 0.159.0 (the shared collector would overwrite `host.name`); it has no host metrics.

- **Static checks:** syntax check, `ansible-lint` (production profile) pass; the agent compiles under `/usr/bin/python3` 3.9.6 and is pyflakes-clean; dashboards render to valid JSON with unique ids and no overlaps; every 1.0.0 panel keeps its id, position and query (apart from the `$framework` filter).
- **Unit tests:** 43, including framework relabelling on a switch, both-frameworks process counts, a server on another port not being taken as this node's, the first tick probing with the detected framework, and the MLX-VLM prefix-cache collector (enabled, disabled, failing, non-VLM node, coexisting with OMLX).
- **Playbook:** server play `failed=0` (Grafana provisioned 71 / 70 / 53 panels and 13 alert rules, all rule health `ok`); worker play `failed=0`, a re-run `changed=0`.
- **VictoriaMetrics:** `count by (framework) (mlx_telemetry_info)` returned `mlx-lm` and `mlx-vlm` from live samples. Health, process, request, TTFT, non-streaming, token, cached-token, active-request and server-reported metrics were present per framework, and MLX-VLM's real prefix cache reported resident bytes, budget, block pool, hits and misses.
- **Dashboards:** every panel query of the three LLM dashboards (83 / 68 / 87 targets) ran against VictoriaMetrics with `$framework` = All, `mlx-lm` and `mlx-vlm`, and the node dashboard for each node: 0 errors. Empty results were only for things that did not occur (errors, restarts, temperature, quality heuristics off) or do not exist (host metrics of the simulated node, MLX-LM server timings).
- **Alerts:** all 13 expressions evaluated and carried `framework`.
- **Framework lifecycle:** stopping MLX-VLM gave `framework="unknown"`, `mlx_server_up 0`; restarting it gave `mlx-vlm` again.
- **Bugs found and fixed while testing:** (1) the first export could run before the first detection, leaving short-lived `framework="unknown"` series, and the first probe used the wrong route; (2) with a stopped server, another MLX server on the same Mac (other port) was taken as this node's; (3) a join without aggregation failed (422) while series of the previous agent version were still in the lookback window.

**Not yet tested:**
- Two physical Mac Minis (the second worker was simulated on one Mac, see above), deployment over SSH.
- A real OMLX server (KV-cache figures, authentication, counter resets).
- Mac Minis, deployment over SSH, and larger models under real load.
- Real client applications.
- The `mlx_launchagent_label` path against a real MLX LaunchAgent.
- Behaviour across reboots.
- Several LLM nodes at once.
- Alert notification delivery.
