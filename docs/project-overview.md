# Mac Mini Observability - Project Overview

## Purpose

> **LaunchAgent / no-sudo branch.** Deploys without sudo; services run as per-user LaunchAgents under the deployment user's home. See [LAUNCHAGENT_VARIANT.md](LAUNCHAGENT_VARIANT.md).

This project uses Ansible to deploy host-metrics observability for Apple Silicon Mac Minis. It supports one central monitoring Mac Mini and a fleet of monitored Mac Minis, with the monitoring-server address derived from inventory rather than embedded in configuration.

## Architecture

![Architecture](architecture.svg)

Each monitored Mac runs the OpenTelemetry Collector Contrib distribution. The metrics flow is:

`hostmetrics + tcp_check + SSH state -> resourcedetection -> batch -> otlphttp -> VictoriaMetrics -> Grafana`

The collector sends metrics over OTLP/HTTP to:

`http://<monitoring-server-address>:8428/opentelemetry/v1/metrics`

VictoriaMetrics stores the metrics. Grafana uses VictoriaMetrics as its datasource and provides a Fleet Overview dashboard and a per-host Mac Mini Detail dashboard (CPU, memory, disk, filesystem, network, load, latency and SSH access).

## Main components

| Component | Responsibility |
| --- | --- |
| OpenTelemetry Collector Contrib | Collects macOS host metrics and exports them over OTLP/HTTP. |
| VictoriaMetrics | Receives and stores telemetry metrics on port 8428 (8429 in side-by-side mode). |
| Grafana | Visualizes VictoriaMetrics data on port 3000 (3001 in side-by-side mode). |
| Ansible | Installs, configures, and verifies the components. |

## Repository roles

| Role | Target group | Responsibility |
| --- | --- | --- |
| `observability_server` | `monitoring_server` | Installs VictoriaMetrics and Grafana; provisions the datasource and dashboards; runs both as LaunchAgents. |
| `observability_agent` | `monitored_nodes` | Installs the OpenTelemetry Collector and the SSH state collector, both as LaunchAgents. |

## Deployment summary

1. Add one central host to `monitoring_server` and fleet hosts to `monitored_nodes` in `inventories/production/hosts.yml`.
2. Optionally provide a real Grafana administrator password (plain or Ansible Vault).
3. Deploy the server role as the normal SSH user: `ansible-playbook -i inventories/production/hosts.yml site.yml --tags server` (no sudo).
4. Deploy one agent and confirm its metrics in Grafana.
5. Deploy the remaining agents; the inventory can scale to approximately 40 Mac Minis.

## Operational notes

- Software is installed under `~/tools/observability`, owned by the deployment user.
- Services are per-user LaunchAgents in `~/Library/LaunchAgents`, loaded into `user/<uid>`. They are not started at boot; re-run the play for a Mac after it reboots.
- The collector validates its rendered configuration during the agent verification tasks.
- VictoriaMetrics and Grafana health endpoints are checked during server verification tasks.
- There is no Prometheus server or Prometheus-specific Collector component in this design.

## Security and validation

Keep credentials in Ansible Vault or another approved secret store. Restrict SSH access and network exposure of the VictoriaMetrics and Grafana ports. No sudo access is required.

Static repository validation covers YAML, Jinja templates, plist XML, dashboard JSON, and SVG syntax. The full playbook has been run unprivileged on one Mac over a local connection. Deployment over SSH, LaunchAgent behaviour across logout and reboot, and network policy still require validation on physical Mac Minis.
