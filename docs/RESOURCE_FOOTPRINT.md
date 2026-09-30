# Initial Resource Footprint

## Scope

This document estimates the initial on-disk footprint of the current project for review and capacity planning. It covers one monitoring Mac Mini and one worker/monitored Mac Mini only. It does not assume a final fleet size.

The figures below were **measured** by extracting the currently configured Apple Silicon macOS archives on an arm64 Mac: VictoriaMetrics `1.150.0`, Grafana `13.2.0`, and OpenTelemetry Collector Contrib `0.159.0`. Actual size still varies with release packaging, macOS version, and generated logs.

These numbers are substantially larger than earlier estimates in this document, because both Grafana and the collector-contrib distribution have grown considerably. Size the Macs against the measured figures, not the old estimates.

## Initial Installation Footprint

This estimate includes extracted binaries, bundled application files, rendered configuration, launchd plists, and small initial logs. It excludes historical VictoriaMetrics data and material log growth.

| Machine Type | Components Installed | Approx. Initial Footprint | Notes |
| --- | --- | ---: | --- |
| 1 Monitoring Mac | VictoriaMetrics + Grafana + configs/service files | **Approx. 1.4 GB** | Grafana 13.2.0 extracts to ~1.3 GB; VictoriaMetrics adds ~24 MB. Excludes long-term metric data. |
| 1 Worker/Monitored Mac | OpenTelemetry Collector Contrib + config/service files | **Approx. 340 MB** | `otelcol-contrib` 0.159.0 is a single ~334 MB binary. Excludes log growth. |

The collector binary is large because `otelcol-contrib` bundles every upstream
component. The slim `otelcol` core distribution is ~119 MB but does **not**
include the `resourcedetection` processor this project relies on to label
metrics by host, so it cannot be substituted as-is. A custom build (OpenTelemetry
Collector Builder) containing only `hostmetrics`, `resourcedetection`, `batch`,
`basicauth` and `otlphttp` is the way to cut this down if fleet disk becomes a
constraint.

### What is included

- **Monitoring Mac:** `~/tools/observability/bin/victoria-metrics-prod`, the Grafana installation under `~/tools/observability/grafana`, Grafana/VictoriaMetrics configuration, provisioning files, LaunchAgent plists, and initial logs. All of it lives in the deployment user's home, so it counts against that volume and any per-user quota.
- **Worker/Monitored Mac:** `~/tools/observability/bin/otelcol-contrib`, `~/tools/observability/etc/otel-config.yaml`, the collector LaunchAgent plist, and initial logs.

Each release is installed into its own versioned directory (for example `~/tools/observability/grafana-13.2.0`) with a stable symlink pointing at the active one. Upgrading does not remove the previous version, so the installed footprint grows by roughly one full installation for every version deployed until old directories are pruned deliberately. Budget for at least two concurrent versions on any Mac that has been upgraded once.

The install tasks download version-stamped archives to `~/tools/observability/var/downloads` before extraction and delete each one once it has been extracted and verified. They are not included in the table, but budget for the largest archive (Grafana, roughly 200 MB) as transient space during a deployment.

## Runtime / Data Growth

VictoriaMetrics historical metric storage is not included in the initial monitoring-Mac estimate. Its growth depends on:

- Number of worker Macs.
- Metric volume produced by each worker.
- The current collector interval of **15 seconds**.
- Retention period.
- Metric cardinality, including distinct host and filesystem/network attributes.

Logs under `~/tools/observability/var/log` can also grow over time, especially when a service repeatedly fails or cannot reach its destination. The current project does not configure log rotation; monitor this directory during the first real-Mac test.

## Scaling Formula

```text
Monitoring Mac footprint approximately = one monitoring installation + VictoriaMetrics historical data + logs
Worker footprint approximately = one worker installation x N workers + worker logs

At roughly 340 MB per worker, a 40-Mac fleet carries about 13.6 GB of collector
binaries in total, or double that if a version bump is applied without pruning
the superseded install directory.
```

`N` is the eventual number of monitored Mac Minis and is intentionally not calculated here. Size VictoriaMetrics storage only after measuring real metric volume from the initial monitoring-Mac plus one-worker-Mac test.
