# Mac Mini Observability - Project Overview

## Purpose

This project uses Ansible to deploy host-metrics observability for Apple Silicon Mac Minis. It supports one central monitoring Mac Mini and a fleet of monitored Mac Minis, with the monitoring-server address derived from inventory rather than embedded in configuration.

## Architecture

![Architecture](architecture.svg)

Each monitored Mac runs the OpenTelemetry Collector Contrib distribution. The metrics flow is:

`hostmetrics -> resourcedetection -> batch -> otlphttp -> VictoriaMetrics -> Grafana`

The collector sends metrics over OTLP/HTTP to:

`http://<monitoring-server-address>:8428/opentelemetry/v1/metrics`

VictoriaMetrics stores the metrics. Grafana uses VictoriaMetrics as its datasource and provides a simple fleet dashboard for CPU, memory, filesystem, and network metrics.

## Main components

| Component | Responsibility |
| --- | --- |
| OpenTelemetry Collector Contrib | Collects macOS host metrics and exports them over OTLP/HTTP. |
| VictoriaMetrics | Receives and stores telemetry metrics on port 8428. |
| Grafana | Visualizes VictoriaMetrics data on port 3000. |
| Ansible | Installs, configures, and verifies the components. |

## Repository roles

| Role | Target group | Responsibility |
| --- | --- | --- |
| `observability_server` | `monitoring_server` | Installs VictoriaMetrics and Grafana; provisions the datasource, dashboard, and launchd plists. |
| `observability_agent` | `monitored_nodes` | Installs and configures the OpenTelemetry Collector and its launchd plist. |

## Deployment summary

1. Add one central host to `monitoring_server` and fleet hosts to `monitored_nodes` in `inventories/production/hosts.yml`.
2. Provide the Grafana administrator password through Ansible Vault.
3. Deploy the server role.
4. Deploy one agent and confirm its metrics in Grafana.
5. Deploy the remaining agents; the inventory can scale to approximately 40 Mac Minis.

## Operational notes

- Software is installed under `/opt/observability`.
- Services use the existing macOS launchd approach and plists are installed in `/Library/LaunchDaemons`.
- The collector validates its rendered configuration during the agent verification tasks.
- VictoriaMetrics and Grafana health endpoints are checked during server verification tasks.
- There is no Prometheus server or Prometheus-specific Collector component in this design.

## Security and validation

Keep credentials in Ansible Vault or another approved secret store. Restrict SSH access, sudo access, and network exposure of ports 8428 and 3000.

Static repository validation covers YAML, Jinja templates, plist XML, dashboard JSON, and SVG syntax. Actual macOS archive execution, launchd behavior, network policy, and end-to-end metric ingestion require validation on the first physical Mac Mini.
