# LLM (MLX-LM) observability

Optional monitoring for Mac Minis that serve models with `mlx_lm.server`. It extends the existing stack rather than adding a new one:

- metrics go through each Mac's existing OpenTelemetry Collector to the existing VictoriaMetrics;
- the dashboards are provisioned into the existing Grafana;
- nothing needs sudo, and there is no Prometheus server or node_exporter.

**Status:** validated on one Apple Silicon Mac (macOS 27.0.1), as an unprivileged user over `ansible_connection: local`. That run used a real `mlx_lm.server` 0.32.2 serving `mlx-community/SmolLM2-135M-Instruct-8bit`; see [Validation](#validation). It has not been tested on the Mac Minis, with production models, or with real client applications.

## Architecture

```
client app ──HTTP──▶ MLX telemetry proxy (127.0.0.1:18080) ──▶ mlx_lm.server (127.0.0.1:8080)
                         │  timing, status, usage (no content kept)
                         │  + health checks + unprivileged hardware readings
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

The role never starts, stops or reconfigures `mlx_lm.server` or its own LaunchAgent. If you set `mlx_launchagent_label`, that label is only read with `launchctl print`.

## How applications must connect

MLX-LM serves its OpenAI-compatible API on `mlx_server_port` (8080 by default), and that stays unchanged. Requests are measured **only if the client sends them to the proxy**:

```
before:  base_url = http://127.0.0.1:8080/v1
after:   base_url = http://127.0.0.1:18080/v1      # mlx_telemetry_host:mlx_telemetry_port
```

Nothing is redirected automatically. Clients that keep calling MLX-LM directly still work, but they appear only in the health, process and hardware panels, never in the request metrics. To measure clients on other machines, set `mlx_telemetry_host: 0.0.0.0` and open that port. Local Network Privacy does not restrict incoming connections.

What the proxy does with requests:
- It forwards every method and path. Only `POST /v1/chat/completions` and `POST /v1/completions` are measured.
- Streams are relayed line by line as they arrive, never buffered.
- MLX-LM's status code and headers are passed through. The proxy answers 502 itself when MLX-LM is unreachable and 504 when it times out.
- A client hang-up closes the upstream connection, which stops generation.
- Client `Authorization` headers are forwarded but never stored or logged.
- `GET /-/mlx-telemetry/status` is answered by the proxy itself and is used by the play's verification.

**Token usage on streams.** MLX-LM includes usage in a stream only when the request sets `stream_options.include_usage`. With `llm_proxy_stream_usage: true` (the default), the proxy handles a streaming request that has no `stream_options` like this:
1. It adds `{"include_usage": true}` to the request.
2. It reads the usage event MLX-LM then sends.
3. It removes that event before it reaches the client.

The client therefore receives exactly the bytes MLX-LM would have sent without the option. In testing, the event streams with and without the proxy were identical apart from per-request ids and timestamps. Requests that set `stream_options` themselves are passed through unchanged. With the option set to false, streaming requests without usage are counted in `mlx_requests_usage_missing_total`, and no token counts are guessed.

## Enable on a node

1. In the inventory, set the flag for each Mac that runs `mlx_lm.server`, or for a child group of `monitored_nodes` via `group_vars/<group>.yml`. Do not change the fleet default in `group_vars/all.yml`.
   ```yaml
   mac-mini-02:
     ansible_host: REPLACE_WITH_NODE2_IP
     ansible_user: REPLACE_WITH_SSH_USER
     llm_observability_enabled: true
     mlx_server_port: 8080                    # what mlx_lm.server --port uses on this Mac
     mlx_launchagent_label: com.example.mlx   # optional: label of the existing MLX LaunchAgent
   ```
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
| `mlx_process_pattern` | `mlx_lm(\.server\b\|\s+server\b\|/server\.py\b)` | Used to find the server process when no label is set. |
| `mlx_model_path` / `llm_hf_cache_dir` | `""` | Where to find the model's `config.json`, for its maximum context length. A Hugging Face repo is looked up in `$HF_HUB_CACHE`, `$HF_HOME/hub` or `~/.cache/huggingface/hub`. |
| `llm_model_name` | `""` | Fixes the `model` label. When empty, it comes from the server's `--model` argument; a path is reduced to its last component, so home directories never become labels. |
| `llm_model_max_context_tokens` | `0` | Sets the maximum context explicitly. `0` means read it from `config.json`. |
| `llm_model_label_max_values` | `10` | Cardinality cap: model names beyond this many are labelled `other`. |
| `llm_metrics_enabled` | `true` | Runs the proxy (request telemetry). |
| `llm_proxy_stream_usage` | `true` | Adds and removes `include_usage` on streams, as described above. |
| `llm_proxy_read_timeout` / `llm_proxy_connect_timeout` / `llm_proxy_max_request_bytes` | `600` / `5` / 32 MiB | Proxy limits. |
| `llm_health_check_interval` / `llm_models_check_interval` | `15` / `60` | Seconds between checks. |
| `llm_inference_probe_enabled` / `llm_inference_probe_interval` | `false` / `300` | Optional one-token `/v1/completions` probe, sent directly to MLX-LM rather than through the proxy. |
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
- **`{model, endpoint, stream}`:** `endpoint` is `chat` or `completion`; `stream` is `true` or `false`.
- **`outcome`:** one of `success`, `client_error` (4xx), `server_error` (5xx), `upstream_error` (MLX-LM unreachable, the read failed, or the stream ended without `[DONE]`), `timeout`, or `client_disconnected`.

### Requests (proxy)

| Metric | Type | Labels | Definition |
| --- | --- | --- | --- |
| `mlx_requests_total` | counter | model, endpoint, stream, outcome, status_code | Each measured request, counted once when it finishes. |
| `mlx_requests_active` | gauge | – | Requests in flight. |
| `mlx_request_duration_seconds` | histogram | model, endpoint, stream, outcome | End to end: from the proxy reading the request to the last byte it relays. This is the only latency recorded for non-streaming requests. |
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
| `mlx_upstream_connection_failures_total` | counter | reason (`connect`, `read`, `timeout`) | Times the proxy could not reach MLX-LM or read from it. |
| `mlx_proxy_passthrough_requests_total` | counter | – | Unmeasured requests that were forwarded anyway, such as `/v1/models`. |

Request counters are created at the first request. Before any request, dashboards show `0` while the proxy is running (detected via `mlx_telemetry_info{proxy="enabled"}`), or "No proxy telemetry" when there is no telemetry at all. Missing telemetry is never displayed as zero activity.

### Health, process and model

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
| `mlx_process_launchd_runs` | launchd's `runs` count, only when `mlx_launchagent_label` is set. |
| `mlx_model_info{model, source}` | Always 1. `source` is `config`, `process` or `models_api`. |
| `mlx_model_context_length_max{model}` | `max_position_embeddings` (or a similar key) from `config.json`, or `llm_model_max_context_tokens`. |
| `mlx_telemetry_up`, `mlx_telemetry_info{version, proxy, hardware, quality}`, `mlx_telemetry_export_failures_total`, `mlx_activity_expected` | The agent's own status. |

### Hardware (unprivileged sources only)

| Metric | Source |
| --- | --- |
| `macos_gpu_utilization_percent`, `macos_gpu_renderer_utilization_percent` | `ioreg -c IOAccelerator`, PerformanceStatistics → `Device Utilization %` and `Renderer Utilization %`. These keys are undocumented and may change between macOS releases. |
| `macos_gpu_memory_in_use_bytes`, `macos_gpu_memory_allocated_bytes` | The same IORegistry entry: `In use system memory` and `Alloc system memory`. |
| `macos_memory_pressure_level` | `sysctl kern.memorystatus_vm_pressure_level`, mapped to 0 normal, 1 warning, 2 critical. |
| `macos_thermal_state` | `NSProcessInfo.thermalState`, a public API called through the Objective-C runtime: 0 nominal, 1 fair, 2 serious, 3 critical. |
| `macos_hardware_metric_available{metric}` | 1 if the reading is collected on this node, 0 if not. Dashboards use it to show "Unavailable" rather than an empty or zero panel. |

CPU, memory, disk, network and load come from the existing `hostmetrics` pipeline and are reused, not collected again.

### Quality heuristics (optional, `llm_quality_metrics_enabled`)

Each successful response of at least 20 words is scored in memory, from at most `quality_max_chars` (64 KiB) of text, and then discarded. **These are text heuristics. They are not a hallucination detector or a fact check.**

| Signal | Definition | Flag when |
| --- | --- | --- |
| repetition | 1 − (unique word trigrams ÷ all trigrams) | ≥ `llm_quality_repetition_threshold` (0.3) |
| hedging | Hedge phrases ("I think", "probably", "might be", "not sure", …) per 100 words | ≥ `llm_quality_hedging_threshold` (2.0) |
| refusal | A refusal phrase ("I can't help", "as an AI", …) is present | present |
| heuristic risk score | 0.5·min(1, repetition/threshold) + 0.3·min(1, hedging/threshold) + 0.2·refusal (weights: `llm_quality_risk_weights`) | – |

The resulting metrics are `mlx_quality_evaluations_total`, `mlx_quality_flags_total{flag}`, `mlx_quality_flagged_responses_total`, `mlx_quality_repetition` (histogram), `mlx_quality_heuristic_risk_score` (histogram) and `mlx_quality_last_heuristic_risk_score`, all labelled by `model`.

**Groundedness is not evaluated**, because there is no reference source or evaluator. To add one, compute it inside `ProxyHandler._record` in `files/mlx_telemetry.py`, where the response text is available in memory, and export it as a new `mlx.quality.*` metric.

### Data hygiene

- No prompts, completions, request ids, conversation ids or user ids appear in metrics, labels or logs.
- The agent writes no access log.
- Labels are limited to bounded values. Model names are sanitised and capped.

## Unavailable metrics and why

| Metric | Why not |
| --- | --- |
| KV-cache size, utilisation, cached-sequence count, evictions | `mlx_lm.server` 0.32.2 exposes none of these over HTTP, and this project does not patch MLX-LM. The `--prompt-cache-size`/`--prompt-cache-bytes` flags set limits, but their current usage is not reported. A utilisation figure would need both, so none is estimated, and certainly not from host memory. The prompt-cache reuse figure (`mlx_tokens_prompt_cached_total`) is the cache signal MLX-LM does report. Extension point: an `mlx.kv_cache.*` gauge in the agent, once MLX-LM exposes it. |
| GPU frequency; CPU, GPU and ANE power | These come from `powermetrics`, which requires root, or from the private IOReport API. This variant runs without sudo and uses only supported, unprivileged sources, so `macos_hardware_metric_available` reports them as 0. |
| TTFT and generation duration for non-streaming requests | A non-streaming response gives no indication of when its first token was generated, so only end-to-end duration is recorded. |
| Token counts for streaming requests without usage | Recorded in `mlx_requests_usage_missing_total`. Counts are never derived from response length. |
| Groundedness / factual correctness | No evaluator is configured. |

## Dashboards

All three dashboards use the existing datasource (uid `victoriametrics`). They have `$host` and `$model` variables (`$host` is single-value on the node dashboard), link to each other, and leave the two host dashboards unchanged.

| Dashboard (uid) | Sections |
| --- | --- |
| **LLM Cluster Overview** (`llm-cluster-overview`) | **Summary:** nodes online, requests, completion tokens, average tokens/s, tool calls, quality flags, TTFT p95 and availability (all over 5 minutes). **Inference performance:** requests/min, TTFT and generation p50/p95, tokens/min, tokens/s p50/p95, streaming vs non-streaming, error rate, active requests, mean temperature, end-to-end p50/p95, outcome/status breakdown. **Model and context:** models served, tokens per request, context utilisation, prompt-cache reuse, usage not reported, KV cache (not instrumented). **Hardware:** CPU, memory, memory pressure, MLX-LM memory, disk, GPU utilisation, GPU memory, thermal, and GPU frequency/power marked unavailable. **Service health:** a per-node table (health, process, 15-minute error rate, time since last health OK and last inference, restarts, telemetry age; each row links to the node dashboard), stale nodes, upstream failures, recent failures. |
| **LLM Node Detail** (`llm-node-detail`) | **Status:** node, model, health, process, time since last health OK, requests, telemetry age. **Hardware gauges:** load, memory, pressure, footprint, disk, GPU utilisation and memory, thermal, plus availability tiles. **Hardware time series.** **LLM performance.** **Troubleshooting:** failures, latency spikes (p99), restarts, health-check failures, memory pressure against active requests, and memory, footprint and latency on one axis. |
| **LLM Performance, KV Cache and Quality** (`llm-performance-quality`) | **Performance.** **Context and KV cache:** context used against the maximum, utilisation, maximum context, prompt-cache reuse, and KV cache marked not instrumented. **Quality heuristics:** method, evaluations, flags, risk p95 and latest, repetition p95, groundedness marked not evaluated, flags/min, risk over time. |

Quantiles always come from `histogram_quantile` over `_bucket` series; there are no percentiles derived from averages. Empty panels state why, for example "No streaming requests", "Heuristics disabled" or "Max context unknown".

## Alerts

Grafana provisions these from `etc/grafana/provisioning/alerting/llm_alerts.yml` into the folder **LLM Observability**. All use `noDataState: OK`, so an absent optional metric never fires an alert. Missing telemetry has its own rule.

| Rule | Fires when |
| --- | --- |
| MLX-LM server unavailable (critical) | `/health` is failing for 2 minutes. |
| MLX-LM model API or inference probe failing | A `/v1/models` or probe failure in the last 10 minutes. |
| MLX-LM request error rate high | 5xx, upstream errors or timeouts exceed `llm_alert_error_rate_percent` over 10 minutes, with at least `llm_alert_min_requests` requests. |
| MLX-LM time to first token p95 high | p95 TTFT exceeds `llm_alert_ttft_p95_seconds` for 10 minutes. |
| MLX-LM generation latency degraded | p95 generation duration exceeds `llm_alert_generation_p95_seconds` for 10 minutes. |
| No MLX-LM inference activity where expected | `llm_activity_expected` is set and there was no successful request for `llm_alert_inactivity_minutes`. |
| High memory pressure on an LLM node | Pressure is at or above `llm_alert_memory_pressure_level` for 5 minutes. |
| MLX-LM process restarting | At least `llm_alert_restarts_per_15m` pid changes in 15 minutes. |
| LLM telemetry missing or stale | The node reported within the last hour but not now. |
| Thermal pressure on an LLM node | Thermal state is at or above `llm_alert_thermal_state`, only where it is collected. |

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

**Not yet tested:**
- Mac Minis, deployment over SSH, and larger models under real load.
- Real client applications.
- The `mlx_launchagent_label` path against a real MLX LaunchAgent.
- Behaviour across reboots.
- Several LLM nodes at once.
- Alert notification delivery.
