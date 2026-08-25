# Initial Resource Footprint

## Scope

This document estimates the initial on-disk footprint of the current project for review and capacity planning. It covers one monitoring Mac Mini and one worker/monitored Mac Mini only. It does not assume a final fleet size.

The estimates are intentionally broad. They are based on the currently configured Apple Silicon macOS archives: VictoriaMetrics `1.101.0`, Grafana `10.4.1`, and OpenTelemetry Collector Contrib `0.98.0`. Actual size varies by release packaging, macOS version, and generated logs.

## Initial Installation Footprint

This estimate includes extracted binaries, bundled application files, rendered configuration, launchd plists, and small initial logs. It excludes historical VictoriaMetrics data and material log growth.

| Machine Type | Components Installed | Approx. Initial Footprint | Notes |
| --- | --- | ---: | --- |
| 1 Monitoring Mac | VictoriaMetrics + Grafana + configs/service files | Approx. 250-400 MB | Mostly the Grafana application bundle; excludes long-term metric data. |
| 1 Worker/Monitored Mac | OpenTelemetry Collector Contrib + config/service files | Approx. 90-160 MB | Mostly the `otelcol-contrib` binary; excludes large log growth. |

### What is included

- **Monitoring Mac:** `/opt/observability/bin/victoria-metrics-prod`, the Grafana installation under `/opt/observability/grafana`, Grafana/VictoriaMetrics configuration, provisioning files, launchd plists, and initial logs.
- **Worker/Monitored Mac:** `/opt/observability/bin/otelcol-contrib`, `/opt/observability/etc/otel-config.yaml`, the collector launchd plist, and initial logs.

The current install tasks download archives to `/tmp` before extraction. Those temporary archives are not included in the table because they are not part of the intended installed footprint, but they can temporarily consume additional disk space during deployment if the operating system does not clean them up.

## Runtime / Data Growth

VictoriaMetrics historical metric storage is not included in the initial monitoring-Mac estimate. Its growth depends on:

- Number of worker Macs.
- Metric volume produced by each worker.
- The current collector interval of **15 seconds**.
- Retention period.
- Metric cardinality, including distinct host and filesystem/network attributes.

Logs under `/opt/observability/var/log` can also grow over time, especially when a service repeatedly fails or cannot reach its destination. The current project does not configure log rotation; monitor this directory during the first real-Mac test.

## Scaling Formula

```text
Monitoring Mac footprint approximately = one monitoring installation + VictoriaMetrics historical data + logs
Worker footprint approximately = one worker installation x N workers + worker logs
```

`N` is the eventual number of monitored Mac Minis and is intentionally not calculated here. Size VictoriaMetrics storage only after measuring real metric volume from the initial monitoring-Mac plus one-worker-Mac test.
