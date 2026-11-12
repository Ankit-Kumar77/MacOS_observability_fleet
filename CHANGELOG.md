# Changelog

All notable changes to the LaunchAgent / no-sudo variant. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions of the MLX telemetry agent (`roles/observability_llm/files/mlx_telemetry.py`, `VERSION`) are noted where they change.

## [Unreleased]: observability suite v2 (MLX telemetry agent 1.3.0)

Ports the inference observability ideas of [hellodk/mlx-oc](https://github.com/hellodk/mlx-oc) (except its Opik tracing) into this project's push-based, no-sudo architecture, and adds SRE, self-monitoring and fleet alerting.

### Added

- **Response outcomes:** `mlx_responses_total{kind, finish_reason}`. Kinds are chat, reasoning, tool_call and completion; finish reasons expose truncated answers (`length`).
- **Tool calls per function:** `mlx_tool_calls_by_tool_total{tool}`, with sanitised names capped by `llm_tool_label_max_values`.
- **Token confidence** (opt-in, `llm_confidence_metrics_enabled`): mean and weakest token probability, perplexity and low-confidence responses from the sampled tokens' logprobs. With `llm_confidence_inject_logprobs`, the proxy asks MLX-LM for logprobs on non-streaming requests and strips them, so clients receive byte-identical replies.
- **Model architecture and sizing:** `mlx_model_config{attr}`, `mlx_model_architecture`, `mlx_model_kv_cache_per_token_bytes` and `mlx_model_weights_size_bytes`, all from the model's `config.json`.
- **MLX-LM prompt (KV) cache** from its log (`mlx_server_log_path`), as `mlx_kv_cache_*{source="mlx-lm"}`, next to the MLX-VLM and OMLX sources.
- **Collector self-monitoring** (`otel_self_monitoring_enabled`): the collector scrapes its own loopback telemetry, giving `otelcol_*` points sent and failed, queue fill, CPU and memory per Mac.
- **VictoriaMetrics self-monitoring** (`victoriametrics_self_scrape_interval`, `-selfScrapeInterval`): ingestion, storage, free disk and churn.
- **Dashboards (6 new, 11 in total, in three folders):** Observability Home (Grafana's home page), Observability Stack Health, Alerts Overview, LLM Service Levels (SLO), LLM Traffic, Tools and Outcomes, and LLM Model, Capacity and Confidence.
- **Fleet alert rules (11):** Mac stopped reporting, CPU, memory and disk (warning and critical), latency, monitoring Mac unreachable, SSH not answering, collector dropping metrics, export queue filling, VictoriaMetrics disk low.
- **SLO burn-rate alerts (4):** availability and TTFT, fast (14.4x, 1 h and 5 m) and slow (6x, 6 h and 30 m). SLO settings are under `llm_slo_*`.
- `runbook_url` on every rule, pointing at [docs/ALERTING.md](docs/ALERTING.md).
- **Tests:** `tests/test_mlx_telemetry.py` (18 unit and contract tests, standard library), `tests/render_templates.yml` and `tests/check_dashboards.py` (render and lint every dashboard and rule file), and `tests/check_panel_queries.py` (every panel query against a live Grafana). CI runs the first three.
- **Docs:** `docs/DASHBOARDS.md`, `docs/ALERTING.md`, `SECURITY.md` and this changelog. The README has been rewritten.

### Changed

- Dashboards are provisioned into Grafana folders (**Platform**, **Fleet**, **LLM**) from one catalogue, `observability_server_dashboards`. uids are unchanged, so existing links keep working. Files provisioned flat by earlier releases are removed on upgrade.
- Every dashboard is tagged `observability`, has a **Dashboards** menu and a shared crosshair; the two host dashboards gained descriptions.
- `llm_dashboard_macros.j2` was renamed `dashboard_macros.j2` and gained `header`, `nav`, `query_var`, `bargauge`, `alertlist` and `dashlist`.
- Tool calls in streams are counted once per call: fragments without a function name or id are no longer counted.
- Server verification now checks every provisioned dashboard and that the fleet rules are loaded.
- Grafana usage reporting and update checks are turned off.

### Notes

- Collector configs change on every Mac (new `prometheus/self` receiver), so the agent play restarts each collector once.
- Token confidence is off by default. Injection adds `"logprobs": true` to non-streaming MLX-LM requests; measure its cost on your model before enabling it fleet-wide.

## 2026-10-30: MLX-VLM parity (agent 1.2.0)

- MLX-VLM support with framework detection, a `framework` label on every series, the MLX-VLM prefix-cache and OMLX KV-cache sources, framework-aware alerts (13 rules), and framework comparison panels. See [docs/LLM_OBSERVABILITY.md](docs/LLM_OBSERVABILITY.md#validation).

## 2026-10-22: MLX-LM observability (agent 1.0.0)

- Optional `observability_llm` role: telemetry proxy, health checks, unprivileged hardware readings, quality heuristics, three LLM dashboards and 10 alert rules.

## 2026-10-14: LaunchAgent fixes

- The collector's internal telemetry moved to `otel_internal_metrics_port` (18888), clear of the system-level collector's 8888.
- Agent verification diagnoses macOS Local Network Privacy blocks.
- Confluence guide for the LaunchAgent deployment.

## 2026-10-13: LaunchAgent / no-sudo variant

- Per-user LaunchAgents under `~/tools/observability`, no sudo, preflight guards, side-by-side mode, and SSH observability without root.

## 2026-09-23: Fleet dashboards, latency and SSH

- Fleet overview and per-host detail dashboards, `tcp_check` latency and SSH access state.
