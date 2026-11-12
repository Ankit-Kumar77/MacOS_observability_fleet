# Dashboards

Every dashboard is a Jinja template in `roles/observability_server/templates/`, rendered by the server role and file-provisioned into Grafana. Nothing is edited in the Grafana UI: provisioned dashboards cannot be saved there (`allowUiUpdates: false`), and the next play run would overwrite them anyway.

## Layout

| Folder | Dashboard | uid | When provisioned |
| --- | --- | --- | --- |
| Platform | Observability Home | `observability-home` | always (Grafana home page) |
| Platform | Observability Stack Health | `observability-stack-health` | always |
| Platform | Alerts Overview | `observability-alerts` | always |
| Fleet | Mac Mini Fleet Overview | `mac-mini-fleet` | always |
| Fleet | Mac Mini Detail | `mac-mini-detail` | always |
| LLM | LLM Cluster Overview | `llm-cluster-overview` | any monitored node has `llm_observability_enabled` |
| LLM | LLM Node Detail | `llm-node-detail` | same |
| LLM | LLM Performance, KV Cache and Quality | `llm-performance-quality` | same |
| LLM | LLM Service Levels (SLO) | `llm-slo` | same |
| LLM | LLM Traffic, Tools and Outcomes | `llm-traffic-tools` | same |
| LLM | LLM Model, Capacity and Confidence | `llm-model-capacity` | same |

The catalogue is one list, `observability_server_dashboards` in `roles/observability_server/defaults/main.yml` (template, folder, uid, whether it is an LLM dashboard). Folders come from the provider's `foldersFromFilesStructure`: each dashboard is written to `provisioning/dashboards/<folder>/`. uids are fixed, so `/d/<uid>` links keep working when a dashboard moves between folders. Dashboards provisioned at the top level by earlier releases are removed on upgrade (`observability_server_legacy_dashboard_files`).

## Conventions

- **Navigation.** Every dashboard is tagged `observability` and has a **Dashboards** drop-down listing all of them, plus direct links to its neighbours. Host names in tables link to the matching detail dashboard with the time range kept.
- **Shared crosshair** (`graphTooltip: 1`) on every dashboard.
- **Empty means something.** Panels state why they are empty (`No streaming requests`, `Confidence disabled`, `Collector self-monitoring off`) rather than showing 0. Where 0 is a real value (the proxy is running but saw no requests), queries add `or on() (0 * …)` so it shows as 0, and only then.
- **Quantiles** always come from `histogram_quantile` over `_bucket` series, never from averages.
- **Ratios** aggregate both sides (`sum by (host_name)(x{state="used"}) / sum by (host_name)(x)`); dividing a full-label selector by an aggregate returns nothing.
- **Datasource** is always uid `victoriametrics`; every target has a `refId`.
- **Jinja.** Grafana's `{{label}}` legend syntax collides with Jinja: the LLM and platform dashboards build legends with `m.lg('label')`, the two host dashboards escape it as `{{ '{{' }}label{{ '}}' }}`. All PromQL goes through `tojson`, so quotes are never hand-escaped.
- **Shared macros.** `dashboard_macros.j2` holds the panel builders (`stat`, `ts`, `table`, `bargauge`, `alertlist`, `dashlist`, `text`), the header (`header`, `nav`), variables (`query_var`, `framework_var`) and the shared KV-cache sections.

## Adding or changing a dashboard

1. Write `templates/<name>.json.j2`, starting with `{% import 'dashboard_macros.j2' as m %}` and `{{ m.header(...) }}`.
2. Add it to `observability_server_dashboards` with a folder and uid variable.
3. Check it statically: `ansible-playbook tests/render_templates.yml -e render_dir=/tmp/render && python3 tests/check_dashboards.py /tmp/render` (valid JSON, unique ids, no overlapping panels, every target has a refId).
4. Check it against data: deploy to a test Mac and run `python3 tests/check_panel_queries.py http://<grafana> <host>`. Static checks have never caught a broken query in this project; running them against VictoriaMetrics has.

## Dashboard reference

The tables below list every panel with the description it carries in Grafana (hover the ⓘ next to a panel title to see the same text).

### Observability Home

`/d/observability-home` · folder **Platform** · template `roles/observability_server/templates/home.json.j2`

Start page and Grafana's home dashboard. Fleet and LLM status in one row each, alerts firing now, and a directory of every dashboard grouped by folder. The LLM row appears only when LLM observability is enabled somewhere.

| Section | Panel | What it shows |
| --- | --- | --- |
| Fleet status | Macs reporting | Macs whose collector delivered host metrics in the last 5 minutes. |
| Fleet status | Macs silent (last hour) | Macs that reported in the last hour but not in the last 5 minutes: collector stopped, Mac asleep or rebooted (LaunchAgents do not start at boot), or blocked by Local Network Privacy. |
| Fleet status | Busiest CPU | Highest CPU busy % across the fleet (5-minute rate). |
| Fleet status | Highest memory use | Highest memory used % (inactive, reclaimable memory not counted). |
| Fleet status | Fullest disk | Highest used % of the root volume across the fleet. |
| Fleet status | Worst latency to monitoring Mac | Highest TCP connect time from a Mac to VictoriaMetrics. |
| LLM serving | LLM servers up | Nodes whose MLX-LM / MLX-VLM server answered /health. |
| LLM serving | Requests / min | Proxied inference requests per minute, fleet-wide. |
| LLM serving | Error rate | Share of requests failing server-side over 15 minutes. |
| LLM serving | TTFT p95 | Streaming time to first token, 95th percentile over 15 minutes. |
| LLM serving | Availability (SLO window) | Availability SLI over 30d (target 99.5%). Details on LLM Service Levels. |
| LLM serving | Tokens / min | Completion tokens generated per minute, fleet-wide. |
| Alerts and dashboards | Firing and pending alerts | Grafana-managed alert rules currently firing or pending, all folders. Rules and runbooks: docs/ALERTING.md. |
| Alerts and dashboards | All dashboards | Every dashboard provisioned by this project, grouped by folder. |

### Observability Stack Health

`/d/observability-stack-health` · folder **Platform** · template `roles/observability_server/templates/stack_health.json.j2`

Answers *is the monitoring itself working?* Needs `otel_self_monitoring_enabled` (collector panels) and `victoriametrics_self_scrape_interval` (VictoriaMetrics panels), both on by default. A Mac that cannot export at all cannot report its own failure, so start from **Last report per Mac**.

| Section | Panel | What it shows |
| --- | --- | --- |
| Signal freshness | Last report per Mac | Seconds since the latest sample of each signal, per Mac (instant queries look back 5 minutes, so a silent signal shows as empty). Host metrics and latency come from the collector, SSH state from the sshstate agent, LLM from the MLX telemetry agent. |
| OpenTelemetry Collector (per Mac) | Metric points sent / s | Points each collector delivered to VictoriaMetrics. |
| OpenTelemetry Collector (per Mac) | Metric points failed / s | Points the exporter could not deliver after retries (dropped). Any value here means data loss; see the collector's error log on that Mac. |
| OpenTelemetry Collector (per Mac) | Points accepted vs refused by receivers / s | Points entering each collector's pipeline (host metrics, latency, SSH state, LLM telemetry). |
| OpenTelemetry Collector (per Mac) | Exporter queue fill | Batches waiting to be sent. A queue that fills up means VictoriaMetrics is unreachable or slow; at 100% new data is dropped. |
| OpenTelemetry Collector (per Mac) | Collector CPU | CPU used by each collector process (100 = one core). |
| OpenTelemetry Collector (per Mac) | Collector memory (RSS) | Resident memory of each collector process. |
| VictoriaMetrics (monitoring Mac) | Active series | Series that received samples in the last hour. |
| VictoriaMetrics (monitoring Mac) | Ingestion rate | Samples written per second, all protocols. |
| VictoriaMetrics (monitoring Mac) | Storage used | On-disk size of the stored data (retention: 90d). |
| VictoriaMetrics (monitoring Mac) | Free disk for storage | Free space on the volume holding the VictoriaMetrics data directory. |
| VictoriaMetrics (monitoring Mac) | VictoriaMetrics uptime | Time since VictoriaMetrics started. LaunchAgents are not started at boot: a short uptime after a reboot means the server play was re-run. |
| VictoriaMetrics (monitoring Mac) | Slow inserts | Share of inserts that missed the in-memory index (new series churn or too little RAM). |
| VictoriaMetrics (monitoring Mac) | Rows ingested / s by protocol | opentelemetry = the collectors' OTLP pushes; promscrape = VictoriaMetrics' own metrics. |
| VictoriaMetrics (monitoring Mac) | Queries / s by path | Read traffic (mostly Grafana) and HTTP errors. |
| VictoriaMetrics (monitoring Mac) | VictoriaMetrics memory (Go runtime) | Memory the VictoriaMetrics process obtained from macOS and its live heap. On macOS VictoriaMetrics does not export process RSS or CPU (those come from Linux /proc); see the Mac Mini Detail dashboard for the monitoring Mac's totals. |
| VictoriaMetrics (monitoring Mac) | Stored data and series churn | Disk growth and the rate of brand-new series. Churn (for example SSH sessions or new model names) adds index work. |
| VictoriaMetrics (monitoring Mac) | Series by metric family (top 10) | Live series per metric prefix: where the storage cost comes from. |
| MLX telemetry agents | Telemetry agents | Agent version and enabled features per node, and OTLP exports to the local collector that failed in the last hour. |

### Alerts Overview

`/d/observability-alerts` · folder **Platform** · template `roles/observability_server/templates/alerts_overview.json.j2`

Live state of every Grafana-managed rule provisioned by this project, read from Grafana's alerting state (no metric queries).

| Section | Panel | What it shows |
| --- | --- | --- |
|  | Firing | Rules whose condition is met and whose 'for' period has elapsed. |
|  | Pending | Rules whose condition is met but whose 'for' period has not elapsed yet. |
|  | Fleet rules (all states) | Host-level rules: Macs not reporting, CPU, memory, disk, latency, SSH and collector delivery. |
|  | LLM rules (all states) | MLX-LM / MLX-VLM rules, including the SLO burn-rate rules. Present only when LLM observability is enabled on some node. |

### Mac Mini Fleet Overview

`/d/mac-mini-fleet` · folder **Fleet** · template `roles/observability_server/templates/dashboard.json.j2`

Every Mac at a glance. The `host_name` variable is multi-select with All; click a series or a host to open it in Mac Mini Detail.

| Section | Panel | What it shows |
| --- | --- | --- |
|  | Active Hosts | Distinct Macs reporting host metrics. |
|  | CPU Usage (%) | Busy CPU time as a share of total CPU time. macOS reports no per-core `cpu` label, so this normalises against the sum of all states rather than averaging. Click a series to open that Mac in the detail dashboard. |
|  | Memory Usage (%) | macOS memory states are free / inactive / used; inactive is reclaimable and is not counted as used. |
|  | Monitoring Server Unreachable | Hosts whose latest TCP check to VictoriaMetrics failed. A Mac that cannot reach the server at all stops reporting; compare Active Hosts against the fleet size for that case. |
|  | Disk Usage (%) | Root APFS volume. Sibling volumes in the same container report identical capacity, so only "/" is charted. |
|  | Network Traffic (bytes/sec) | Summed across interfaces; without the sum each host contributes one series per interface. |
|  | Load Average (1m) | Available on macOS from the hostmetrics load scraper. |
|  | Latency to Monitoring Server (ms) | TCP connect time from each Mac to VictoriaMetrics on the monitoring Mac, measured by the collector's tcp_check receiver every collection interval. One sample per interval in whole milliseconds; this is a round-trip time, not a percentile. |
|  | Hosts | Current values per Mac. Click a host name to open it in the Mac Mini Detail dashboard. |
|  | SSH Access | Per Mac: Remote Login is the launchd sshd job being loaded; SSH Service is sshd answering with an SSH protocol banner on loopback (launchd holds port 22 open even when sshd itself is broken, so an open port alone is not proof); Sessions counts authenticated SSH sessions the collector could attribute; TCP Connections counts every established connection on the SSH port. The collector runs as the unprivileged deployment user (LaunchAgent), so a session it cannot attribute -- typically another user's scp/sftp/notty session -- shows up only as Connections > Sessions. Refreshed every otel_ssh_check_interval by the SSH state LaunchAgent. |
|  | Current SSH Sessions | Live authenticated SSH sessions the unprivileged collector could attribute: user, source address, terminal (notty = scp, sftp or a remote command) and login time. Terminal sessions of any user are covered; another user's notty session is listed only when its socket can be tied to its sshd process. Only the fields shown are collected; nothing from inside the session is recorded. |

### Mac Mini Detail

`/d/mac-mini-detail` · folder **Fleet** · template `roles/observability_server/templates/dashboard_detail.json.j2`

One Mac in depth (single-value `host_name`), including live SSH sessions. Sessions are filtered to the host's latest SSH snapshot so ended sessions do not linger.

| Section | Panel | What it shows |
| --- | --- | --- |
| System Metrics | CPU Usage |  |
| System Metrics | Memory Usage |  |
| System Metrics | Disk Usage (/) |  |
| System Metrics | Load Average (1m) |  |
| System Metrics | Latency to Monitoring Server | Latest TCP connect time to VictoriaMetrics, in whole milliseconds. |
| System Metrics | Monitoring Server | Result of the latest TCP check to VictoriaMetrics. |
| System Metrics | CPU Usage (%) | Busy CPU time as a share of total CPU time. macOS reports no per-core `cpu` label, so this normalises against the sum of all states. |
| System Metrics | CPU Time by State (%) | Share of total CPU time spent in each state, stacked to 100%. |
| System Metrics | Memory Usage (%) | macOS memory states are free / inactive / used; inactive is reclaimable and is not counted as used. |
| System Metrics | Memory by State (bytes) |  |
| System Metrics | Disk Usage (%) | Root APFS volume. Sibling volumes in the same container report identical capacity, so only "/" is charted. |
| System Metrics | Disk I/O (bytes/sec) | Summed across physical disks. |
| System Metrics | Network Receive (bytes/sec) | Per interface. Idle interfaces (utun, awdl, ...) sit at zero. |
| System Metrics | Network Transmit (bytes/sec) | Per interface. Idle interfaces (utun, awdl, ...) sit at zero. |
| System Metrics | Network Errors and Drops (packets/sec) | Summed across interfaces. |
| System Metrics | TCP Connections by State |  |
| System Metrics | Load Average | Available on macOS from the hostmetrics load scraper. |
| System Metrics | Latency to Monitoring Server (ms) | TCP connect time from this Mac to VictoriaMetrics on the monitoring Mac, measured by the collector's tcp_check receiver every collection interval. One sample per interval in whole milliseconds; this is a round-trip time, not a percentile. |
| SSH / Access | Remote Login | launchd has the sshd job (com.openssh.sshd by default) loaded, i.e. Remote Login is on. |
| SSH / Access | SSH Service | sshd answered with an SSH protocol banner on loopback. launchd holds port 22 open even when sshd is broken, so this is the health signal, not the open port. |
| SSH / Access | SSH Port | Something is listening on the SSH port (launchd, for the stock Remote Login setup). |
| SSH / Access | Active SSH Sessions | Authenticated SSH sessions the unprivileged collector could attribute right now. Compare with TCP connections on the SSH Sessions graph: more connections than sessions means some sessions (or pre-authentication connections) are not attributed. |
| SSH / Access | Current SSH Sessions | Live authenticated SSH sessions the unprivileged collector could attribute: user, source address, terminal (notty = scp, sftp or a remote command) and login time. Terminal sessions of any user are covered; another user's notty session is listed only when its socket can be tied to its sshd process. Only the fields shown are collected; nothing from inside the session is recorded. |
| SSH / Access | SSH Sessions | Attributed SSH sessions and established TCP connections on the SSH port over time. Connections include sessions the unprivileged collector cannot attribute and connections still authenticating. |

### LLM Cluster Overview

`/d/llm-cluster-overview` · folder **LLM** · template `roles/observability_server/templates/llm_cluster_overview.json.j2`

Fleet view of MLX-LM / MLX-VLM inference. Variables: `host`, `model`, `framework`. Request panels only cover clients that call the telemetry proxy; panels show 0 when the proxy runs and saw nothing, and say so when there is no telemetry at all.

| Section | Panel | What it shows |
| --- | --- | --- |
| Summary (last 5 minutes) | LLM nodes online | Nodes whose inference server (MLX-LM or MLX-VLM) answered /health with 200 at the last check (out of nodes reporting LLM telemetry). |
| Summary (last 5 minutes) | Requests | Inference requests through the telemetry proxy in the last 5 minutes. Clients that call MLX-LM directly are not counted. |
| Summary (last 5 minutes) | Completion tokens | Completion tokens reported in MLX-LM usage blocks in the last 5 minutes. |
| Summary (last 5 minutes) | Avg completion tokens/s | Mean per-request decode rate, (completion_tokens - 1) / generation duration, over streaming requests with usage in the last 5 minutes. |
| Summary (last 5 minutes) | Tool calls | Tool calls MLX-LM returned (message.tool_calls / delta.tool_calls) in the last 5 minutes. |
| Summary (last 5 minutes) | Quality flags | Responses flagged by the optional text heuristics (repetition, hedging, refusal) in the last 5 minutes. Heuristics, not fact checking; off unless llm_quality_metrics_enabled. |
| Summary (last 5 minutes) | TTFT p95 | 95th percentile time to first generated content (streaming requests) from histogram buckets, last 5 minutes. |
| Summary (last 5 minutes) | Server availability | Share of /health checks that answered 200 across the selected nodes, last 5 minutes. |
| Inference performance | Requests per minute | Inference requests per minute through the telemetry proxy, per node. |
| Inference performance | Streaming time to first token | Streaming requests only: request start at the proxy to the first SSE event carrying content, reasoning or a tool call. Not time to HTTP headers. |
| Inference performance | Generation duration | Streaming requests only: first generated content to end of stream ([DONE]). |
| Inference performance | Tokens per minute | Prompt and completion tokens per minute from MLX-LM usage blocks. Requests without usage are counted in 'Usage not reported'. |
| Inference performance | Completion tokens/s per request | Decode rate per streaming request, (completion_tokens - 1) / generation duration, from histogram buckets. |
| Inference performance | Streaming vs non-streaming | Requests per minute by the request's stream flag. |
| Inference performance | Request error rate | Share of requests whose outcome was not success (client_error, server_error, upstream_error, timeout, client_disconnected). |
| Inference performance | Active requests | Requests in flight through the proxy, sampled at each export. |
| Inference performance | Mean sampling temperature | Mean of the temperature value clients set explicitly. Requests that rely on the server default are not included. |
| Inference performance | Request duration (end to end) | Proxy receives the request to the last byte relayed, all outcomes, streaming and non-streaming together (non-streaming alone: Frameworks section). |
| Inference performance | Responses by outcome and status | status_code is MLX-LM's status, or 502/504 when the proxy could not reach it or timed out. |
| Model and context | Models served | model = --model argument of the running server (a path is reduced to its last component), llm_model_name, or the single model /v1/models lists. Max context comes from the model's config.json or llm_model_max_context_tokens. |
| Model and context | Tokens per request | Per-request token counts from MLX-LM usage. Total = prompt + completion = context length used by the request. |
| Model and context | Context utilisation p95 | 95th percentile of (prompt + completion) / maximum context length over the last hour. Needs a known maximum context (config.json or llm_model_max_context_tokens). |
| Model and context | Prompt cache reuse | Share of prompt tokens MLX-LM served from its prompt cache (usage.prompt_tokens_details.cached_tokens), last hour. This is reuse of cached prefixes, not KV-cache size or occupancy. |
| Model and context | Usage not reported | Successful requests without a usage block in the last hour (token counters skip them rather than estimate). |
| Hardware | CPU busy | Existing host metrics (hostmetrics cpu scraper). |
| Hardware | Memory used | Existing host metrics; inactive memory is reclaimable and not counted as used. |
| Hardware | Memory pressure level | kern.memorystatus_vm_pressure_level: 0 normal, 1 warning, 2 critical. |
| Hardware | Server process memory | Physical footprint (includes the Metal buffers holding weights and KV cache) and RSS (excludes most of them) of the MLX-LM / MLX-VLM server process. |
| Hardware | Disk used (/) | Existing host metrics (APFS container capacity). |
| Hardware | GPU utilisation | AGX 'Device Utilization %' from the IORegistry (ioreg, unprivileged; undocumented key that may change between macOS releases). |
| Hardware | GPU / unified memory in use | Unified memory the GPU driver reports in use / allocated (IORegistry PerformanceStatistics). |
| Hardware | Thermal state | NSProcessInfo.thermalState: 0 nominal, 1 fair, 2 serious, 3 critical. |
| Hardware | GPU frequency | Not collected: GPU frequency needs powermetrics (root) or the private IOReport API. |
| Hardware | CPU/GPU/ANE power | Not collected: power readings need powermetrics (root) or the private IOReport API. |
| Hardware | GPU utilisation source | Whether GPU utilisation is collected on all selected nodes (IORegistry). |
| Hardware | Thermal state source | Whether thermal state is collected on all selected nodes (NSProcessInfo). |
| Service health and errors | Nodes | One row per node, with the framework detected there. Error rate covers the last 15 minutes of proxied requests; ages are seconds since the last successful /health check, the last successful inference request, and the last telemetry sample. |
| Service health and errors | Nodes with stale LLM telemetry | Nodes that reported LLM telemetry in the last hour but not now (agent stopped, Mac rebooted, or export failing). |
| Service health and errors | Upstream connection failures | Times per minute the proxy could not connect to, or read from, the inference server. |
| Service health and errors | Recent request failures | Failed requests per interval by outcome. |
| Frameworks and KV cache | MLX-LM nodes | Nodes whose agent detected (or was configured with, llm_framework) MLX-LM. |
| Frameworks and KV cache | MLX-VLM nodes | Nodes whose agent detected (or was configured with, llm_framework) MLX-VLM. |
| Frameworks and KV cache | Framework not detected | Nodes where neither MLX-LM nor MLX-VLM answers on mlx_server_port and no server process was found. Their health panels report the server down; set llm_framework only if a server runs but cannot be identified. |
| Frameworks and KV cache | KV-cache sources enabled | KV-cache sources reporting an enabled cache: MLX-VLM's prefix cache (source=mlx-vlm, needs APC_ENABLED=1 on the server) and OMLX (source=omlx). MLX-LM exposes none. |
| Frameworks and KV cache | Hot KV cache utilisation (max) | Highest in-memory KV used / capacity across selected nodes and sources. |
| Frameworks and KV cache | KV cache hit ratio (1h) | Share of KV-cache lookups served from the cache in the last hour, all sources. |
| Frameworks and KV cache | Requests per minute by framework | Requests through the telemetry proxy by detected framework. |
| Frameworks and KV cache | Streaming TTFT p95 by framework | Streaming requests only: request start to the first SSE event with generated content. |
| Frameworks and KV cache | Non-streaming request latency p95 by framework | Successful non-streaming requests, start to complete response. The proxy sees the response only after generation, so this is whole-request latency, not TTFT. |
| Frameworks and KV cache | KV cache utilisation by node | KV cache used / capacity per node, source (mlx-vlm prefix cache, omlx) and tier (hot = memory, ssd = disk). |
| Frameworks and KV cache | KV cache used by node | KV cache held per node, source and tier, summed over models. |
| Framework comparison | Monitored workers | Nodes reporting LLM telemetry now (filtered by $framework). |
| Framework comparison | Healthy workers | Nodes whose server answered /health with 200 at the last check. |
| Framework comparison | Unhealthy workers | Nodes whose server did not answer /health with 200 (including nodes where no framework is detected). |
| Framework comparison | Telemetry agents up | Telemetry agents currently exporting. Nodes that stopped are listed under Nodes with stale LLM telemetry. |
| Framework comparison | Server restarts (1h) | Observed server pid changes across selected nodes in the last hour. |
| Framework comparison | Workers running both frameworks | Nodes where both an MLX-LM and an MLX-VLM server process run. The agent measures the one on mlx_server_port; its framework is the node's framework. |
| Framework comparison | Server and process up by framework | Nodes per framework whose /health answered 200, and whose server process was found. |
| Framework comparison | Active requests by framework | Requests in flight through the telemetry proxies, per framework. |
| Framework comparison | Server restarts by framework | Server pid changes per interval, per framework. |
| Framework comparison | CPU busy by framework | Host CPU busy, averaged over the nodes of each framework (existing host metrics joined with each node's live framework). |
| Framework comparison | Memory used by framework | Host memory used (inactive memory not counted), averaged over the nodes of each framework. |
| Framework comparison | Server process memory by framework | Physical footprint (weights and KV cache in Metal buffers included) of the server processes, summed per framework. |

### LLM Node Detail

`/d/llm-node-detail` · folder **LLM** · template `roles/observability_server/templates/llm_node_detail.json.j2`

One LLM node (single-value `host`, set by links from the overview).

| Section | Panel | What it shows |
| --- | --- | --- |
| Status | Node | Host name as reported by the collector. |
| Status | Model served | --model argument of the running server (a path is reduced to its last component), llm_model_name, or the single model /v1/models lists. |
| Status | Server /health | Last /health check: 200 = Up. |
| Status | Server process | An mlx_lm.server or mlx_vlm.server process was found (by mlx_launchagent_label, or by mlx_process_pattern / mlx_vlm_process_pattern). |
| Status | Since last health OK | Seconds since /health last answered 200. |
| Status | Requests (5m) | Requests through the telemetry proxy in the last 5 minutes. |
| Status | Telemetry age | Seconds since the newest LLM telemetry sample. |
| Hardware | Load (1m) | Existing host metrics. |
| Hardware | Memory used | Existing host metrics; inactive memory is not counted as used. |
| Hardware | Memory pressure | Kernel memory pressure level (sysctl kern.memorystatus_vm_pressure_level). |
| Hardware | Server footprint | Physical footprint of the server process, including Metal buffers (weights, KV cache). RSS is shown in the time series below. |
| Hardware | Disk used (/) | Existing host metrics. |
| Hardware | GPU utilisation | IORegistry AGX Device Utilization % (unprivileged, undocumented key). |
| Hardware | GPU memory in use | Unified memory the GPU driver reports in use (IORegistry). |
| Hardware | Thermal state | NSProcessInfo.thermalState. |
| Hardware | GPU frequency | Not collected: needs powermetrics (root) or the private IOReport API. |
| Hardware | CPU / GPU / ANE power | Not collected: needs powermetrics (root) or the private IOReport API. |
| Hardware | GPU utilisation source | Whether this node's GPU utilisation is collected (IORegistry). |
| Hardware | Thermal state source | Whether this node's thermal state is collected (NSProcessInfo). |
| Hardware over time | CPU busy and load | Existing host metrics. |
| Hardware over time | System memory | Existing host metrics by state (free / inactive / used). |
| Hardware over time | Memory pressure level | 0 normal, 1 warning, 2 critical. |
| Hardware over time | Server process memory and CPU | Footprint includes Metal buffers; RSS mostly does not. CPU % is ps %cpu (100 = one core). |
| Hardware over time | Disk used (/) | Existing host metrics. |
| Hardware over time | GPU utilisation | IORegistry PerformanceStatistics. |
| Hardware over time | GPU / unified memory | IORegistry PerformanceStatistics. |
| Hardware over time | Thermal state | 0 nominal, 1 fair, 2 serious, 3 critical. |
| LLM performance on this node | Requests per minute by outcome | Requests through the telemetry proxy. |
| LLM performance on this node | Streaming time to first token | Streaming requests: request start to first generated content event. |
| LLM performance on this node | Generation duration | Streaming requests: first generated content to end of stream. |
| LLM performance on this node | Tokens per minute | From MLX-LM usage blocks. |
| LLM performance on this node | Completion tokens/s per request | (completion_tokens - 1) / generation duration, streaming requests with usage. |
| LLM performance on this node | Active requests | Requests in flight through the proxy. |
| LLM performance on this node | Errors by status | Failed requests per interval. |
| LLM performance on this node | Streaming vs non-streaming | By the request's stream flag. |
| LLM performance on this node | Context used per request | Prompt + completion tokens per request, and the model's maximum context when known. KV-cache size and utilisation are not exposed by mlx_lm.server / mlx_vlm.server; with OMLX see the section at the bottom. |
| Troubleshooting | Request failures over time | client_error = 4xx from the server, server_error = 5xx or an error event inside a stream, upstream_error = proxy could not reach/read the server, timeout, client_disconnected. |
| Troubleshooting | Latency spikes | 99th percentiles from histogram buckets; spikes stand out against the p50/p95 panels above. |
| Troubleshooting | Server restarts | Observed pid changes of the server process, and changes of launchd's run count when mlx_launchagent_label is set. |
| Troubleshooting | Health check failures | Failed checks per interval: http = /health, models = /v1/models, inference = optional probe. |
| Troubleshooting | Memory pressure during inference | Pressure level (0-2) against requests in flight. |
| Troubleshooting | Memory, process footprint and latency | Correlate memory growth with request latency on one time axis. |
| Framework and KV cache | Framework | mlx-lm or mlx-vlm; unknown = neither answers on mlx_server_port and no server process was found. |
| Framework and KV cache | Detected from | config = llm_framework in inventory; http = the server answering /health (MLX-VLM Server header, MLX-LM /health shape); process = the server process found; none = not detected. |
| Framework and KV cache | KV cache | Worst state across selected nodes and sources. Enabled = the source reports its KV cache. Disabled on server = MLX-VLM runs without its prefix cache (APC_ENABLED=1 turns it on). Poll failing = MLX-VLM /v1/cache/stats or OMLX /admin/api/stats did not answer, or rejected the API key (mlx_kv_cache_scrape_failures_total{source, reason}); for MLX-LM (source mlx-lm, read from its log with mlx_server_log_path) the log file could not be read. |
| Framework and KV cache | Hot KV cache used | In-memory KV cache held, summed over sources, models and selected nodes. MLX-LM: prompt cache from its log (10 MB resolution, as of the latest request); MLX-VLM: prefix-cache resident bytes; OMLX: hot tier size. |
| Framework and KV cache | Hot KV cache capacity | In-memory KV budget, summed over selected nodes. MLX-VLM: APC resident budget; OMLX: --hot-cache-max-size. Not reported when no budget is set. |
| Framework and KV cache | Hot KV cache utilisation | Hot tier used / capacity, highest across selected nodes and sources. Only computed when a capacity is reported. |
| Framework and KV cache | SSD KV cache used | KV cache kept on disk (MLX-VLM APC disk tier, OMLX paged SSD tier), summed over selected nodes. |
| Framework and KV cache | SSD KV cache utilisation | SSD tier used / its size limit, highest across selected nodes. Not reported for an uncapped disk tier. |
| Framework and KV cache | Non-streaming request latency | Successful non-streaming requests, start to complete response (whole-request latency, not TTFT). The server-reported prompt time comes from MLX-VLM timings; MLX-LM sends none. |
| Framework and KV cache | KV cache used vs capacity | KV cache per source and tier: hot = memory, ssd = disk. Neither source reports a separate 'allocated' figure: the hot tier's size is the memory it holds. |
| Framework and KV cache | KV cache utilisation | Used / capacity per node, source and tier, and (MLX-VLM) prefix-cache blocks in use / block pool size. Computed by the telemetry agent from the source's own figures. |
| Framework and KV cache | KV cache hit ratio and evictions | Share of KV-cache lookups served from the cache, and blocks evicted per minute, per source. The sources' own counters, reset when the server restarts. |
| Server details | Model API | Last /v1/models check (every llm_models_check_interval). |
| Server details | Server uptime | Time since the server process started (ps etime). |
| Server details | Server restarts (1h) | Observed server pid changes in the last hour. |
| Server details | Server CPU | ps %cpu of the server process (100 = one core). |
| Server details | Inference probe | Last one-token probe (llm_inference_probe_enabled; /v1/completions on MLX-LM, chat with the served model on MLX-VLM). |
| Server details | Server processes | MLX-LM and MLX-VLM server processes on this node. Both > 0 means both frameworks genuinely run; the agent measures the one on mlx_server_port. |
| Server details | Request duration percentiles | End-to-end request duration through the proxy, all requests, from histogram buckets. |
| Server details | Health check duration | Duration of the last check: http = /health, models = /v1/models, inference = optional probe. |

### LLM Performance, KV Cache and Quality

`/d/llm-performance-quality` · folder **LLM** · template `roles/observability_server/templates/llm_performance_quality.json.j2`

Latency distributions, context and KV cache, quality heuristics and framework comparison.

| Section | Panel | What it shows |
| --- | --- | --- |
| Performance | Requests per minute | Requests through the telemetry proxy; the streaming series is the subset with stream=true. |
| Performance | Prompt tokens per minute | From MLX-LM usage blocks. |
| Performance | Completion tokens per minute | From MLX-LM usage blocks. |
| Performance | Completion tokens/s per request | (completion_tokens - 1) / generation duration, streaming requests with usage. Quantiles from histogram buckets. |
| Performance | Streaming time to first token | Streaming only: request start to the first SSE event with content, reasoning or a tool call. |
| Performance | Generation duration | Streaming only: first generated content to end of stream. |
| Performance | End-to-end request duration | Proxy receives the request to last byte relayed, streaming and non-streaming together. Non-streaming latency on its own is in the Frameworks section below. |
| Performance | Request error rate | Share of requests with an outcome other than success. |
| Performance | Active requests | Requests in flight through the proxy. |
| Context and KV cache | Context length used per request | Prompt + completion tokens per request (from usage), against the model's maximum context when known. |
| Context and KV cache | Context utilisation | (prompt + completion) / maximum context, per request. Only recorded when the maximum context is known (config.json or llm_model_max_context_tokens). |
| Context and KV cache | Maximum context length | max_position_embeddings from the model's config.json, or llm_model_max_context_tokens. |
| Context and KV cache | Prompt cache reuse (1h) | Share of prompt tokens served from MLX-LM's prompt cache (usage.prompt_tokens_details.cached_tokens). Measured by MLX-LM; it is cache reuse, not KV-cache size. |
| Quality and hallucination-risk heuristics | Responses evaluated (1h) | Responses the heuristics scored in the last hour. |
| Quality and hallucination-risk heuristics | Flagged responses (1h) | Responses with at least one heuristic flag in the last hour. |
| Quality and hallucination-risk heuristics | Heuristic risk p95 (1h) | 95th percentile heuristic risk score (0-1) from histogram buckets. Not a probability of hallucination. |
| Quality and hallucination-risk heuristics | Latest heuristic risk | Score of the most recently evaluated response (highest across selected nodes). |
| Quality and hallucination-risk heuristics | Repetition p95 (1h) | 95th percentile share of repeated word trigrams. |
| Quality and hallucination-risk heuristics | Heuristic flags per minute | Flags raised per minute by heuristic (repetition, hedging, refusal). |
| Quality and hallucination-risk heuristics | Heuristic risk score | Quantiles of the weighted heuristic score from histogram buckets. |
| Frameworks: streaming TTFT and non-streaming latency | Requests per minute by framework | Requests through the telemetry proxy by the framework detected on each node (mlx-lm, mlx-vlm, unknown). |
| Frameworks: streaming TTFT and non-streaming latency | Streaming TTFT p95 by framework | Streaming requests only: request start to the first SSE event with generated content. Non-streaming requests have no TTFT. |
| Frameworks: streaming TTFT and non-streaming latency | Non-streaming request latency | Successful non-streaming requests, request start to the complete response. The proxy only sees a non-streaming response once generation is finished, so this is whole-request latency, not time to first token. |
| Frameworks: streaming TTFT and non-streaming latency | Completion tokens/s p50 by framework | Proxy-measured decode rate of streaming requests with usage, (completion_tokens - 1) / generation duration. |
| Frameworks: streaming TTFT and non-streaming latency | Server-reported generation tokens/s | timings.predicted_per_second as the server reports it per request, the only generation rate available for non-streaming requests. MLX-VLM reports it; MLX-LM sends no timings. Streaming requests carry it only with usage (llm_proxy_stream_usage). |
| Frameworks: streaming TTFT and non-streaming latency | Server-reported prompt processing time | timings.prompt_ms as the server reports it: its own prefill time, derived by the server from its prompt rate. Not measured by the proxy and not the same as TTFT. MLX-VLM only. |
| KV cache (MLX-VLM prefix cache, OMLX) | KV cache | Worst state across selected nodes and sources. Enabled = the source reports its KV cache. Disabled on server = MLX-VLM runs without its prefix cache (APC_ENABLED=1 turns it on). Poll failing = MLX-VLM /v1/cache/stats or OMLX /admin/api/stats did not answer, or rejected the API key (mlx_kv_cache_scrape_failures_total{source, reason}); for MLX-LM (source mlx-lm, read from its log with mlx_server_log_path) the log file could not be read. |
| KV cache (MLX-VLM prefix cache, OMLX) | Hot KV cache used | In-memory KV cache held, summed over sources, models and selected nodes. MLX-LM: prompt cache from its log (10 MB resolution, as of the latest request); MLX-VLM: prefix-cache resident bytes; OMLX: hot tier size. |
| KV cache (MLX-VLM prefix cache, OMLX) | Hot KV cache capacity | In-memory KV budget, summed over selected nodes. MLX-VLM: APC resident budget; OMLX: --hot-cache-max-size. Not reported when no budget is set. |
| KV cache (MLX-VLM prefix cache, OMLX) | Hot KV cache utilisation | Hot tier used / capacity, highest across selected nodes and sources. Only computed when a capacity is reported. |
| KV cache (MLX-VLM prefix cache, OMLX) | SSD KV cache used | KV cache kept on disk (MLX-VLM APC disk tier, OMLX paged SSD tier), summed over selected nodes. |
| KV cache (MLX-VLM prefix cache, OMLX) | SSD KV cache utilisation | SSD tier used / its size limit, highest across selected nodes. Not reported for an uncapped disk tier. |
| KV cache (MLX-VLM prefix cache, OMLX) | KV cache used vs capacity | KV cache per source and tier: hot = memory, ssd = disk. Neither source reports a separate 'allocated' figure: the hot tier's size is the memory it holds. |
| KV cache (MLX-VLM prefix cache, OMLX) | KV cache utilisation | Used / capacity per node, source and tier, and (MLX-VLM) prefix-cache blocks in use / block pool size. Computed by the telemetry agent from the source's own figures. |
| KV cache (MLX-VLM prefix cache, OMLX) | KV cache hit ratio and evictions | Share of KV-cache lookups served from the cache, and blocks evicted per minute, per source. The sources' own counters, reset when the server restarts. |
| KV cache (MLX-VLM prefix cache, OMLX) | Allocated vs used | Unavailable by design: neither MLX-VLM nor OMLX reports an allocated figure separate from what each tier holds and its budget. |
| KV cache (MLX-VLM prefix cache, OMLX) | Hit / miss counters | Whether every KV-cache source on the selected nodes reports lookup hits and misses. |
| Framework comparison | Request duration p95 by framework | End-to-end duration p95 per framework, all requests and successful non-streaming requests. Streaming TTFT by framework is in the Frameworks section above. |
| Framework comparison | Tokens per minute by framework | From the servers' usage blocks. Cached = usage.prompt_tokens_details.cached_tokens (both frameworks report it). |
| Framework comparison | Requests without token usage by framework | Successful requests whose response carried no usage block. Their tokens are not counted or estimated. |
| Framework comparison | Error rate by framework | Share of requests per framework with an outcome other than success. |
| Framework comparison | Generation duration p95 by framework | Streaming requests: first generated content to end of stream. |
| Framework comparison | Heuristic risk p95 by framework | Optional text heuristics (llm_quality_metrics_enabled), computed the same way for both frameworks' text output. Not a hallucination detector. |

### LLM Service Levels (SLO)

`/d/llm-slo` · folder **LLM** · template `roles/observability_server/templates/llm_slo.json.j2`

SLIs, error budgets and burn rates over `llm_slo_window` (30d). Definitions are in [ALERTING.md](ALERTING.md#service-level-objectives); the burn-rate alerts use the same expressions.

| Section | Panel | What it shows |
| --- | --- | --- |
| Objectives over the last 30d | Availability SLI | Share of valid requests that succeeded over 30d. Target 99.5%. |
| Objectives over the last 30d | Availability error budget left | Share of the availability error budget (0.5% of requests) still unspent over 30d. Negative = objective missed. |
| Objectives over the last 30d | TTFT SLI (streaming) | Share of streaming requests whose time to first token was at most 2 s. Target 95%. |
| Objectives over the last 30d | TTFT error budget left | Share of the latency budget for streaming TTFT still unspent. |
| Objectives over the last 30d | Request latency SLI (non-streaming) | Share of successful non-streaming requests that completed within 30 s. Target 95%. |
| Objectives over the last 30d | Request latency budget left | Share of the latency budget for non-streaming requests still unspent. |
| Objectives over the last 30d | Valid requests | Requests counted by the availability SLI over the window. |
| Objectives over the last 30d | Failed requests | 5xx, unreachable server and timeouts over the window. |
| Objectives over the last 30d | Failures the budget allows | Valid requests x (1 - availability target): failures that can occur before the objective is missed. |
| Objectives over the last 30d | Slow first tokens | Streaming requests whose first token took longer than 2 s, over the window. |
| Burn rate (1 = spending the budget exactly over the window) | Availability burn, 1 h | Error ratio over the last hour divided by the error budget ratio. The fast-burn alert fires above 14.4. |
| Burn rate (1 = spending the budget exactly over the window) | Availability burn, 6 h | The slow-burn alert fires above 6. |
| Burn rate (1 = spending the budget exactly over the window) | TTFT burn, 1 h | Share of slow first tokens in the last hour divided by the latency budget. |
| Burn rate (1 = spending the budget exactly over the window) | TTFT burn, 6 h |  |
| Burn rate (1 = spending the budget exactly over the window) | Request latency burn, 1 h |  |
| Burn rate (1 = spending the budget exactly over the window) | Request latency burn, 6 h |  |
| Burn rate (1 = spending the budget exactly over the window) | Availability burn rate | Multi-window burn rate. Short windows react fast, long windows filter noise; the alerts require both to exceed the threshold. |
| Burn rate (1 = spending the budget exactly over the window) | Latency burn rate | Burn rate of the latency objectives: share of slow requests divided by the latency budget. |
| Trends | Error ratio vs budget | Share of valid requests that failed, against the error budget ratio. |
| Trends | TTFT p95 vs objective | Streaming time to first token against the latency objective's threshold. |
| Trends | Non-streaming latency p95 vs objective | End-to-end duration of successful non-streaming requests against the objective's threshold. |
| Per node (rolling 30d) | Service levels per node | Each node's SLIs over the window. A node with no traffic of a kind shows an empty cell for that SLI. |

### LLM Traffic, Tools and Outcomes

`/d/llm-traffic-tools` · folder **LLM** · template `roles/observability_server/templates/llm_traffic_tools.json.j2`

What clients ask for and how responses end. Needs agent 1.3.0+ for response kinds, finish reasons and tool names. The `tool` variable lists every function name seen, so new tools appear without editing the dashboard.

| Section | Panel | What it shows |
| --- | --- | --- |
| Summary (selected time range) | Requests | Proxied inference requests in the selected range, every outcome. |
| Summary (selected time range) | Successful responses | Successful responses classified by kind and finish reason (agent 1.3.0+). |
| Summary (selected time range) | Tool calls | Tool calls returned by the models in the selected range. |
| Summary (selected time range) | Distinct tools called | Function names called at least once in the selected range. |
| Summary (selected time range) | Truncated (finish_reason=length) | Share of responses that stopped because they hit max_tokens or the context limit. A high share means clients' max_tokens is too low or answers are running away. |
| Summary (selected time range) | Reasoning responses | Share of responses that carried reasoning (thinking) text and no tool call. |
| Request mix | Responses / min by kind | chat = plain answer, reasoning = answer with thinking text, tool_call = the model called a tool, completion = legacy /v1/completions. |
| Request mix | Requests / min by mode | Streaming vs non-streaming, chat vs legacy completions, every outcome. |
| Request mix | Requests in flight | Concurrent requests through each proxy. MLX servers process requests largely one at a time, so sustained values above 1 mean queueing. |
| Outcomes | Finish reasons / min | Why responses ended: stop (natural end or stop word), length (max_tokens or context limit), tool_calls, other. none = the server sent no finish_reason. |
| Outcomes | Outcomes / min | success, client_error (4xx), server_error (5xx or an error event in a stream), upstream_error (server unreachable or stream cut), timeout, client_disconnected. |
| Outcomes | Truncation rate | Share of responses cut off by max_tokens or the context limit, per model. |
| Outcomes | Status codes (selected range) | Requests by HTTP status code returned to clients (0 = no response: unreachable server or client hang-up). |
| Outcomes | Responses by kind and finish reason (selected range) | Successful responses broken down by kind, finish reason and streaming mode. |
| Tool calls | Tool calls / min by tool | Function calls per tool name. Use the Tool variable to focus on some tools. |
| Tool calls | Tool-call responses share | Share of responses in which the model called at least one tool, per model. |
| Tool calls | All tools called (selected range) | Every function name called in the range, with its share of all tool calls. 'other' collects names beyond llm_tool_label_max_values; 'unknown' = a call without a function name. |
| Tool calls | Top tools (selected range) | The ten most-called tools in the selected range. |
| Tokens | Tokens / min | Token volume reported in the servers' usage blocks. Cached = prompt tokens served from the prompt (KV) cache. |
| Tokens | Tokens per request | Prompt and completion size per request (bucketed histograms: values are bucket estimates). |
| Tokens | Prompt-cache reuse | Share of prompt tokens the server did not have to process again because they were in its prompt cache. Agentic clients that resend a growing conversation benefit most. |

### LLM Model, Capacity and Confidence

`/d/llm-model-capacity` · folder **LLM** · template `roles/observability_server/templates/llm_model_capacity.json.j2`

What each node serves and whether it fits. Architecture, weights and KV figures need the agent to find the model's `config.json` (`mlx_model_path`, or a Hugging Face repo in the local hub cache). Token confidence needs `llm_confidence_metrics_enabled`.

| Section | Panel | What it shows |
| --- | --- | --- |
| Model architecture (from config.json) | Architecture | architectures[0], model_type and weight quantisation from the served model's config.json. Needs the agent to find config.json (mlx_model_path, or a Hugging Face repo in the local hub cache). |
| Model architecture (from config.json) | Layers | num_hidden_layers |
| Model architecture (from config.json) | Attention heads | num_attention_heads |
| Model architecture (from config.json) | KV heads | num_key_value_heads (grouped-query attention); equals attention heads when the config has none |
| Model architecture (from config.json) | Head dim | head_dim, or hidden_size / attention heads |
| Model architecture (from config.json) | Hidden size | hidden_size |
| Model architecture (from config.json) | FFN size | intermediate_size of the feed-forward blocks |
| Model architecture (from config.json) | Vocabulary | vocab_size |
| Model architecture (from config.json) | Quant bits | Weight quantisation bits (quantization.bits); empty for unquantised models |
| Model architecture (from config.json) | Quant group | Quantisation group size |
| Model architecture (from config.json) | Max context | max_position_embeddings from config.json, or llm_model_max_context_tokens. |
| Model architecture (from config.json) | Weights on disk | Size of the model's *.safetensors files. The weights are loaded into unified memory, so this is the floor of the server's footprint. |
| Model architecture (from config.json) | KV cache per token (est.) | 2 (K and V) x layers x KV heads x head dim x 2 bytes: an fp16 KV cache with full attention. Sliding-window, linear-attention or quantised KV caches need less. |
| Model architecture (from config.json) | KV cache at max context (est.) | KV cache one sequence needs at the model's maximum context: KV per token x max context. |
| Memory fit per node | Model memory vs node memory | Per node: weights, the server's physical footprint (weights + KV cache + buffers), the Mac's memory, and how much more KV cache fits in currently free and inactive (reclaimable) memory, as tokens and as full-context sequences. A planning estimate, not a guarantee: macOS also limits how much unified memory the GPU may wire. |
| Memory fit per node | Server footprint vs weights | The footprint above the weights is mostly KV cache and activation buffers; it grows with context length and cached sequences. |
| Memory fit per node | GPU memory in use | Unified memory the GPU driver reports in use and allocated (IORegistry). |
| Context use | Context tokens per request | Prompt + completion tokens per request against the model's maximum context. |
| Context use | Context utilisation | Context tokens / maximum context, per request. Above ~80% prompts risk truncation and the KV cache is at its largest. |
| KV cache (MLX-LM log, MLX-VLM prefix cache, OMLX) | KV cache | Worst state across selected nodes and sources. Enabled = the source reports its KV cache. Disabled on server = MLX-VLM runs without its prefix cache (APC_ENABLED=1 turns it on). Poll failing = MLX-VLM /v1/cache/stats or OMLX /admin/api/stats did not answer, or rejected the API key (mlx_kv_cache_scrape_failures_total{source, reason}); for MLX-LM (source mlx-lm, read from its log with mlx_server_log_path) the log file could not be read. |
| KV cache (MLX-LM log, MLX-VLM prefix cache, OMLX) | Hot KV cache used | In-memory KV cache held, summed over sources, models and selected nodes. MLX-LM: prompt cache from its log (10 MB resolution, as of the latest request); MLX-VLM: prefix-cache resident bytes; OMLX: hot tier size. |
| KV cache (MLX-LM log, MLX-VLM prefix cache, OMLX) | Hot KV cache capacity | In-memory KV budget, summed over selected nodes. MLX-VLM: APC resident budget; OMLX: --hot-cache-max-size. Not reported when no budget is set. |
| KV cache (MLX-LM log, MLX-VLM prefix cache, OMLX) | Hot KV cache utilisation | Hot tier used / capacity, highest across selected nodes and sources. Only computed when a capacity is reported. |
| KV cache (MLX-LM log, MLX-VLM prefix cache, OMLX) | SSD KV cache used | KV cache kept on disk (MLX-VLM APC disk tier, OMLX paged SSD tier), summed over selected nodes. |
| KV cache (MLX-LM log, MLX-VLM prefix cache, OMLX) | SSD KV cache utilisation | SSD tier used / its size limit, highest across selected nodes. Not reported for an uncapped disk tier. |
| KV cache (MLX-LM log, MLX-VLM prefix cache, OMLX) | KV cache used vs capacity | KV cache per source and tier: hot = memory, ssd = disk. Neither source reports a separate 'allocated' figure: the hot tier's size is the memory it holds. |
| KV cache (MLX-LM log, MLX-VLM prefix cache, OMLX) | KV cache utilisation | Used / capacity per node, source and tier, and (MLX-VLM) prefix-cache blocks in use / block pool size. Computed by the telemetry agent from the source's own figures. |
| KV cache (MLX-LM log, MLX-VLM prefix cache, OMLX) | KV cache hit ratio and evictions | Share of KV-cache lookups served from the cache, and blocks evicted per minute, per source. The sources' own counters, reset when the server restarts. |
| Token confidence (logprobs) | Scored responses | Responses scored from token logprobs in the selected range. |
| Token confidence (logprobs) | Scoring coverage | Scored responses / successful responses. Streaming responses cannot be scored. |
| Token confidence (logprobs) | Mean token probability (median) | Median over responses of the mean probability of the sampled tokens. |
| Token confidence (logprobs) | Perplexity p95 | exp(-mean token logprob) per response; 1 = the model was certain of every token. |
| Token confidence (logprobs) | Low-confidence responses | Scored responses in which at least llm_confidence_low_response_ratio of the tokens had a probability below llm_confidence_low_token_probability. |
| Token confidence (logprobs) | Scored / min | Current scoring rate. |
| Token confidence (logprobs) | Token probability | Distribution over responses of the mean and of the lowest sampled-token probability. A falling p10 means more responses the model was unsure about. |
| Token confidence (logprobs) | Perplexity | Per-response perplexity of the model's own output. |
| Token confidence (logprobs) | Low-confidence responses / min | Low-confidence responses against all scored responses, by how the logprobs were obtained (client = requested by the client, injected = requested by the proxy). |
