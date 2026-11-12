# Alerting, SLOs and runbooks

All rules are Grafana-managed and file-provisioned by the server role. Nothing is created in the Grafana UI.

| File (on the monitoring Mac) | Template | Folder / group | Provisioned |
| --- | --- | --- | --- |
| `etc/grafana/provisioning/alerting/fleet_alerts.yml` | `fleet_alerts.yml.j2` | **Fleet** / Fleet | always (`grafana_fleet_alerts_enabled`, default true) |
| `etc/grafana/provisioning/alerting/llm_alerts.yml` | `llm_alerts.yml.j2` | **LLM Observability** / MLX-LM | when any node has `llm_observability_enabled` (`grafana_llm_alerts_enabled`) |

Common behaviour:

- Every rule is evaluated every minute, as an instant query plus a threshold expression.
- `noDataState: OK`: a disabled signal (latency check, SSH state, self-monitoring, proxy, an unavailable hardware reading) never fires. Missing data has its own rules (*Mac stopped reporting*, *LLM telemetry missing or stale*).
- `execErrState: Error`: a broken query shows as an error in Grafana instead of silently passing.
- Labels: `severity` (`critical` = act now, `warning` = act within the working day) and `component` (`fleet` or `mlx`). Annotations: `summary`, `description`, `runbook_url` (this page) and `dashboard`.
- Turning a rule file off rewrites it as a `deleteRules` list, because removing a provisioning file does not delete the rules Grafana already created.
- Both files are generated as JSON from a data structure and `json.load`-checked before install: Grafana refuses to start when an alerting file does not parse.

## Notifications

No contact point is provisioned, so rules fire into Grafana's default notification policy and its unconfigured default email contact point: **nothing is delivered until you add one** (Alerting → Contact points, then the default policy). Keep webhooks and credentials out of Git (Ansible Vault, or configure them in the UI). The *Alerts Overview* dashboard and the alert list on *Observability Home* show live state either way.

## Service level objectives

Defined for requests measured by the MLX telemetry proxy, per node (`host_name`, `framework`) for alerts and fleet-wide or per node on the *LLM Service Levels* dashboard.

| SLO | Good events | Valid events | Default target |
| --- | --- | --- | --- |
| Availability | requests not ending in `server_error`, `upstream_error` or `timeout` | all requests except `client_error` (4xx) and `client_disconnected` | `llm_slo_availability_target` = **99.5 %** |
| Latency, streaming | requests with TTFT ≤ `llm_slo_ttft_seconds` (**2 s**) | streaming requests with a first token | `llm_slo_latency_target` = **95 %** |
| Latency, non-streaming (dashboard) | successful requests completed within `llm_slo_request_seconds` (**30 s**) | successful non-streaming requests | 95 % |

Window: `llm_slo_window` (**30d**, rolling). Error budget = 1 − target (0.5 % of valid requests for availability). Burn rate = observed error ratio ÷ budget ratio: at 1 the budget is spent exactly over the window.

The latency thresholds must be histogram bucket bounds of the agent (TTFT: 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30 s; request duration: 0.05 … 300 s), because the SLI counts the requests in the `le` bucket.

**Burn-rate alerting** follows the Google SRE workbook's multi-window, multi-burn-rate method: a rule fires only when both a long window (accuracy) and a short window (it is still happening) exceed the burn rate, and only with at least `llm_alert_min_requests` requests in the long window.

| Rule | Long / short window | Burn rate | Budget used before it fires | Severity |
| --- | --- | --- | --- | --- |
| fast burn | 1 h / 5 m | `llm_slo_fast_burn_rate` = 14.4 | 2 % of a 30-day budget | critical |
| slow burn | 6 h / 30 m | `llm_slo_slow_burn_rate` = 6 | 5 % of a 30-day budget | warning |

## Fleet rules

Thresholds are `roles/observability_server/defaults/main.yml` variables (`fleet_alert_*`).

<a id="fleet-host-silent"></a>
### Mac stopped reporting (`fleet-host-silent`, critical)

**Fires when** a Mac sent host metrics in the last hour but none in the last 5 minutes, for `fleet_alert_host_silent_minutes` (5).
**Likely causes:** the collector LaunchAgent stopped or crashed; the Mac slept or **rebooted** (per-user LaunchAgents do not start at boot); macOS **Local Network Privacy** blocks the collector; network trouble.
**First steps:**
1. *Observability Stack Health* → *Last report per Mac*: did every signal stop (collector) or only one?
2. On the Mac: `launchctl print user/$(id -u)/com.observability.user.otelcol | grep -E 'state|last exit'` and `tail ~/tools/observability/var/log/otelcol.err.log`.
3. After a reboot: `ansible-playbook -i inventories/production/hosts.yml site.yml --limit <mac> --tags agent`.
4. `connect: no route to host` in the log: see [Local Network Privacy](LAUNCHAGENT_VARIANT.md#local-network-privacy-why-a-worker-can-get-no-route-to-host).

<a id="fleet-cpu-high"></a>
### CPU busy on a Mac (`fleet-cpu-high`, warning)

**Fires when** CPU busy (all cores, 5-minute rate, idle against all states) stays above `fleet_alert_cpu_percent` (90 %) for 15 minutes.
**First steps:** *Mac Mini Detail* for the trend and load average; on the Mac, `top -o cpu`. On an LLM node, sustained generation legitimately uses CPU; compare with requests in flight.

<a id="fleet-memory-high"></a>
### Memory nearly full on a Mac (`fleet-memory-high`, warning)

**Fires when** used memory (macOS `used` state; reclaimable `inactive` excluded) stays above `fleet_alert_memory_percent` (90 %) for 15 minutes.
**First steps:** *Mac Mini Detail*; on LLM nodes, *LLM Model, Capacity and Confidence* → *Model memory vs node memory* (server footprint, KV cache headroom) and the memory-pressure panels. Large prompt caches (`--prompt-cache-size`, `--prompt-cache-bytes`) grow the footprint.

<a id="fleet-disk-high"></a>
### Root volume filling up (`fleet-disk-high`, warning)

**Fires when** the APFS container holding `/` is above `fleet_alert_disk_percent` (90 %) for 30 minutes.
**First steps:** `du -sh ~/tools/observability/* ~/.cache/huggingface/hub/*`. Old releases are kept for rollback and never pruned (collector ~334 MB, Grafana ~1.3 GB per version): delete versions you no longer need. Model caches are often the largest item.

<a id="fleet-disk-critical"></a>
### Root volume almost full (`fleet-disk-critical`, critical)

**Fires when** more than 97 % of the container is used for 5 minutes. Free space now: macOS, the collector's queue and VictoriaMetrics all fail writes when the disk is full.

<a id="fleet-latency-high"></a>
### Network latency to the monitoring Mac high (`fleet-latency-high`, warning)

**Fires when** the TCP connect time from a Mac to VictoriaMetrics stays above `fleet_alert_latency_ms` (200 ms) for 10 minutes.
**First steps:** *Mac Mini Fleet Overview* latency panel: one Mac (its link or Wi-Fi) or all Macs (the monitoring Mac or the network)?

<a id="fleet-latency-check-failing"></a>
### Monitoring Mac unreachable from a Mac (`fleet-latency-check-failing`, warning)

**Fires when** the `tcp_check` receiver on a Mac cannot connect to VictoriaMetrics for 5 minutes while that Mac's other metrics still arrive (intermittent connectivity, or a changed port or firewall rule). If nothing arrives at all, *Mac stopped reporting* fires instead.

<a id="fleet-ssh-down"></a>
### SSH not answering on a Mac (`fleet-ssh-down`, critical)

**Fires when** Remote Login is enabled (`launchctl print system/com.openssh.sshd` answers) but sshd sent no `SSH-2.0-` banner on loopback for 5 minutes. launchd keeps port 22 open even when sshd is broken, so a listening port is not enough. Ansible cannot reach this Mac until it is fixed.
**First steps:** from another machine, `ssh -v <mac>`; at the console, check Remote Login in System Settings and `log show --last 15m --predicate 'process == "sshd"'`.

<a id="fleet-collector-export-failing"></a>
### Collector dropping metrics (`fleet-collector-export-failing`, warning)

**Fires when** a collector reports at least `fleet_alert_export_failed_points` (1) metric point it could not deliver after retries in 15 minutes (`otelcol_exporter_send_failed_metric_points_total`): data was lost.
**First steps:** the collector log on that Mac; from that Mac, `curl -s http://<monitoring-mac>:<port>/health`; *Observability Stack Health* → VictoriaMetrics panels (is it slow, out of disk, read-only?). With VictoriaMetrics auth on, check the credentials match on both sides.

<a id="fleet-collector-queue-filling"></a>
### Collector export queue filling (`fleet-collector-queue-filling`, warning)

**Fires when** a collector's sending queue stays above 80 % full for 10 minutes. VictoriaMetrics is slow or intermittently unreachable; at 100 % new data is dropped and *Collector dropping metrics* follows.

<a id="fleet-vm-disk-low"></a>
### VictoriaMetrics disk space low (`fleet-vm-disk-low`, critical)

**Fires when** less than 5 GiB is free on the volume holding VictoriaMetrics' data for 15 minutes. VictoriaMetrics goes read-only below its `-storage.minFreeDiskSpaceBytes` limit and rejects every Mac's metrics.
**First steps:** free space on the monitoring Mac, or shorten `victoriametrics_retention` and re-run the server play. *Observability Stack Health* → *Stored data and series churn* shows what is growing.

## LLM rules

Every LLM rule carries `host_name` and `framework` (`mlx-lm`, `mlx-vlm`, `unknown`); summaries read `<host> (<framework>)`. Thresholds are `llm_alert_*` and `llm_slo_*` variables. More context on the metrics: [LLM_OBSERVABILITY.md](LLM_OBSERVABILITY.md#alerts).

<a id="llm-mlx-server-down"></a>
### LLM server unavailable (`llm-mlx-server-down`, critical)

`/health` has not returned 200 for 2 minutes. Check the inference server's LaunchAgent and log on that Mac. An MLX-VLM server started with `MLX_VLM_SERVER_API_KEY` answers 401: set `llm_server_api_key_file`. `framework="unknown"` means nothing answers on `mlx_server_port` and no server process was found.

<a id="llm-mlx-process-missing"></a>
### LLM server process not found (`llm-mlx-process-missing`, warning)

No process matched `mlx_process_pattern` / `mlx_vlm_process_pattern` (or `mlx_launchagent_label`) for 5 minutes. If the server runs from a custom wrapper, adjust the pattern or set `mlx_launchagent_label`.

<a id="llm-mlx-health-check-failing"></a>
### LLM model API or inference probe failing (`llm-mlx-health-check-failing`, warning)

`/v1/models` (`check=models`) or the optional one-token probe (`check=inference`) failed in the last 10 minutes while `/health` may still pass: the model is not loaded or generation is broken. On MLX-VLM the probe needs the served model's id (`llm_model_name`).

<a id="llm-mlx-error-rate"></a>
### LLM request error rate high (`llm-mlx-error-rate`, warning)

More than `llm_alert_error_rate_percent` (5 %) of proxied requests ended in a 5xx, an unreachable server or a timeout over 10 minutes (at least `llm_alert_min_requests` requests). *LLM Traffic, Tools and Outcomes* → *Outcomes* and *Status codes*; the server's log for tracebacks or out-of-memory.

<a id="llm-mlx-ttft-p95"></a>
### LLM time to first token p95 high (`llm-mlx-ttft-p95`, warning)

Streaming TTFT p95 above `llm_alert_ttft_p95_seconds` (5 s) for 10 minutes. Usually long prompts (prefill), requests queueing behind each other (*Requests in flight*), a cold prompt cache, or memory pressure. Compare with the TTFT SLO burn rate.

<a id="llm-mlx-generation-p95"></a>
### LLM generation latency degraded (`llm-mlx-generation-p95`, warning)

p95 time from first token to end of stream above `llm_alert_generation_p95_seconds` (120 s). Long completions or slow decode: check tokens/s, thermal state and *Truncation rate* (runaway answers hitting `max_tokens`).

<a id="llm-mlx-request-latency-p95"></a>
### LLM request latency p95 high (`llm-mlx-request-latency-p95`, warning)

p95 end-to-end duration of successful requests, streaming and non-streaming, above `llm_alert_request_p95_seconds` (60 s) for 10 minutes.

<a id="llm-mlx-no-activity"></a>
### No LLM inference activity where expected (`llm-mlx-no-activity`, warning)

Only for nodes with `llm_activity_expected: true`: no successful request through the proxy for `llm_alert_inactivity_minutes` (60). Clients may be calling the server directly (bypassing the proxy) or failing before they reach it.

<a id="llm-memory-pressure"></a>
### High memory pressure on an LLM node (`llm-memory-pressure`, warning)

Kernel memory pressure at or above `llm_alert_memory_pressure_level` (2 = critical) for 5 minutes. The Mac is compressing and swapping; generation slows sharply. Reduce the model size, context or prompt-cache size, or move work to another node.

<a id="llm-mlx-restarting"></a>
### LLM server process restarting (`llm-mlx-restarting`, warning)

At least `llm_alert_restarts_per_15m` (2) server pid changes in 15 minutes: a crash loop. Read the server's log (Metal errors and out-of-memory are typical).

<a id="llm-telemetry-stale"></a>
### LLM telemetry missing or stale (`llm-telemetry-stale`, warning)

The node reported LLM telemetry in the last hour but not now: the telemetry agent stopped, the Mac rebooted, or the collector cannot export. `launchctl print user/$(id -u)/com.observability.user.mlxtelemetry` and `~/tools/observability/var/log/mlxtelemetry.err.log`.

<a id="llm-thermal-pressure"></a>
### Thermal pressure on an LLM node (`llm-thermal-pressure`, warning)

Thermal state at or above `llm_alert_thermal_state` (2 = serious) for 5 minutes; macOS is throttling. Check airflow and sustained load.

<a id="llm-telemetry-export-failing"></a>
### LLM telemetry export failing (`llm-telemetry-export-failing`, warning)

At least `llm_alert_export_failures_per_15m` (3) failed OTLP exports from the telemetry agent to the local collector in 15 minutes: the collector is down or restarting.

<a id="llm-slo-availability-fast-burn"></a>
### LLM availability SLO burning fast (`llm-slo-availability-fast-burn`, critical)

The availability error ratio is above 14.4x its budget over 1 h and over 5 m: at this rate a 30-day budget is gone in about 2 days. Treat as an outage: *LLM Service Levels* for the trend, then *LLM Traffic, Tools and Outcomes* → *Outcomes* and the server's log.

<a id="llm-slo-availability-slow-burn"></a>
### LLM availability SLO burning slow (`llm-slo-availability-slow-burn`, warning)

Above 6x the budget over 6 h and over 30 m: a persistent low error rate that will exhaust the budget in about 5 days. Look for a recurring error (a client sending something the server rejects with 5xx, periodic restarts).

<a id="llm-slo-ttft-fast-burn"></a>
### LLM time-to-first-token SLO burning fast (`llm-slo-ttft-fast-burn`, critical)

More than 14.4 x 5 % = 72 % of streaming requests over the last hour (and 5 minutes) waited longer than `llm_slo_ttft_seconds` for their first token. Check requests in flight (queueing), prompt sizes, memory pressure and thermal state.

<a id="llm-slo-ttft-slow-burn"></a>
### LLM time-to-first-token SLO burning slow (`llm-slo-ttft-slow-burn`, warning)

More than 30 % of streaming requests over 6 h (and 30 m) were slower than the objective. A gradual change: bigger prompts, a larger model, or more concurrent clients than one node can serve.

## Changing thresholds

Override any `fleet_alert_*`, `llm_alert_*` or `llm_slo_*` variable for the monitoring Mac (`inventories/production/group_vars/monitoring_server.yml`) and re-run the server play. Rule uids never change, so Grafana updates the rules in place and keeps their history.
