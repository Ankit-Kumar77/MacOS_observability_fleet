# Knowledge Transfer Guide: Mac Mini Observability

## 1. Project overview

This project uses Ansible to deploy host-metric observability to Apple Silicon Mac Minis. It solves the practical problem of seeing CPU, memory, filesystem, disk, network, and load information from multiple Macs in one place instead of logging in to each machine individually.

Ansible automates installation, directory creation, configuration rendering, dashboard provisioning, launchd plist creation, service requests, and basic verification. The design has two responsibilities: one monitoring Mac stores and displays metrics, while each worker/monitored Mac collects and sends its own metrics.

## 2. High-level architecture

The monitoring Mac runs VictoriaMetrics and Grafana. Every worker/monitored Mac runs OpenTelemetry Collector Contrib with the `hostmetrics` receiver.

```text
Mac Mini
  -> OpenTelemetry Collector
  -> hostmetrics
  -> resourcedetection
  -> batch
  -> OTLP/HTTP
  -> VictoriaMetrics
  -> Grafana
```

The collector exports to the monitoring Mac's VictoriaMetrics endpoint:

```text
http://<monitoring-server-address>:8428/opentelemetry/v1/metrics
```

See [architecture.svg](architecture.svg) for the visual overview.

## 3. Monitoring Mac versus worker Mac

| Machine | Installed components | Why it exists |
| --- | --- | --- |
| Monitoring Mac | VictoriaMetrics, Grafana, datasource/dashboard configuration, launchd plists | Stores fleet metrics and provides dashboards. |
| Worker/monitored Mac | OpenTelemetry Collector Contrib, collector configuration, launchd plist | Collects local host metrics and exports them to the monitoring Mac. |

The monitoring Mac is selected through the `monitoring_server` inventory group. Workers are members of `monitored_nodes`.

## 4. Ansible project structure

| Location | Purpose |
| --- | --- |
| `site.yml` | Defines the server play for `monitoring_server` and the agent play for `monitored_nodes`. |
| `inventories/production/hosts.yml` | Defines target Macs, their addresses, users, and group membership. |
| `inventories/production/group_vars/` | Holds shared, server-specific, and worker-specific variables. |
| `roles/observability_server/` | Installs and configures VictoriaMetrics and Grafana. |
| `roles/observability_agent/` | Installs and configures OpenTelemetry Collector Contrib. |
| `tasks/` | Ordered deployment and verification work. |
| `handlers/` | Restart requests triggered when relevant rendered files change. |
| `templates/` | Jinja2 templates for application configuration, provisioning, dashboards, and launchd plists. |

Ansible reads inventory and group variables for a target host, runs the matching role from `site.yml`, renders templates with those values, and places the result on the Mac.

## 5. Server role flow

When `observability_server` runs, it performs the following sequence:

1. Creates `/opt/observability` directories for binaries, configuration, data, plugins, and logs.
2. Downloads and extracts `victoria-metrics-prod` to `/opt/observability/bin`.
3. Renders `com.observability.victoriametrics.plist` with the VictoriaMetrics binary, data path, port, and log paths.
4. Downloads and extracts Grafana under `/opt/observability/grafana`.
5. Renders `grafana.ini` and the Grafana launchd plist.
6. Provisions the VictoriaMetrics datasource, dashboard provider, and default Mac Mini dashboard.
7. Requests service start/enable through the current `ansible.builtin.service` tasks.
8. Verifies VictoriaMetrics and Grafana health endpoints and checks the Grafana datasource API.

VictoriaMetrics listens on the shared `victoriametrics_port` variable, currently `8428`. Grafana listens on the server-only `grafana_port`, currently `3000`.

## 6. Agent role flow

When `observability_agent` runs, it:

1. Creates the shared `/opt/observability` directory structure.
2. Downloads the configured Darwin ARM64 `otelcol-contrib` archive.
3. Extracts `/opt/observability/bin/otelcol-contrib` and asserts that the binary exists.
4. Renders `/opt/observability/etc/otel-config.yaml`.
5. Renders `com.observability.otelcol.plist` with the binary, configuration, and log paths.
6. Requests collector start/enable through the current `ansible.builtin.service` task.
7. Checks reachability to the monitoring Mac on port `8428` and validates the rendered collector configuration with `otelcol-contrib validate`.

## 7. OpenTelemetry in simple terms

OpenTelemetry Collector is a configurable telemetry process. It receives, processes, and exports telemetry; here it is used only for host metrics.

| Pipeline part | Meaning in this project |
| --- | --- |
| Receiver | The input component. `hostmetrics` reads metrics from the local macOS host. |
| `hostmetrics` | Collects CPU, memory, disk, filesystem, network, and load metrics every 15 seconds. |
| Processor | Changes or prepares data before export. |
| `resourcedetection` | Adds system metadata, including the operating-system hostname used to identify the source Mac. |
| `batch` | Groups metrics before transmission to reduce individual export operations. |
| `otlphttp` | Sends metrics using the OpenTelemetry Protocol over HTTP. |

The project uses this native collector pipeline instead of a Node Exporter/Prometheus-based collection path. This keeps the collection path focused on the OpenTelemetry configuration that is already deployed.

## 8. VictoriaMetrics

VictoriaMetrics is the monitoring Mac's metric storage service. It receives the metric payloads sent by agents, stores them under `/opt/observability/var/victoriametrics`, and exposes a health endpoint.

The configured OTLP/HTTP ingestion endpoint is:

```text
/opentelemetry/v1/metrics
```

The full endpoint includes the inventory-derived monitoring-server address and port `8428`. Historical metric storage grows with the number of workers, their metric volume, the 15-second collection interval, retention, and metric cardinality.

## 9. Grafana

Grafana is the visualization layer. The server role provisions a VictoriaMetrics datasource and a simple Mac Mini dashboard. Grafana queries VictoriaMetrics and displays CPU, memory, filesystem, and network data by host.

Grafana's configuration is rendered under `/opt/observability/etc/grafana`, its application is under `/opt/observability/grafana`, and its service listens on port `3000` by default.

## 10. End-to-end example

If CPU use increases on Worker Mac 01:

1. `hostmetrics` collects the local CPU metric.
2. The collector applies `resourcedetection`, then `batch`.
3. `otlphttp` sends the metric to the monitoring Mac.
4. VictoriaMetrics receives and persists the metric.
5. Grafana queries VictoriaMetrics when the dashboard refreshes.
6. The CPU panel displays the changed value for that host.

## 11. First-time deployment

Use one monitoring Mac and one worker Mac for the first real-world test.

1. Prepare the monitoring Mac with SSH and sudo access; check ports `8428` and `3000` are available.
2. Add the monitoring Mac and one worker to the inventory.
3. Run the server play: `ansible-playbook -i inventories/production/hosts.yml site.yml --tags server --ask-become-pass --ask-vault-pass`.
4. Verify VictoriaMetrics at `http://localhost:8428/health` on the monitoring Mac.
5. Verify Grafana at `http://<monitoring-mac-address>:3000` and confirm its datasource.
6. Prepare one worker Mac with SSH/sudo access and network reachability to the monitoring Mac on port `8428`.
7. Run the agent play with the worker inventory alias in `--limit`.
8. Validate the collector configuration and check the collector log.
9. Confirm metrics reach VictoriaMetrics.
10. Confirm the worker appears in Grafana.
11. Only then add additional Macs.

## 12. Scaling to N Mac Minis

The final fleet size is not fixed. To scale, add each Mac to `monitored_nodes` in the inventory and run the same agent role:

```bash
ansible-playbook -i inventories/production/hosts.yml site.yml --tags agent --ask-become-pass --ask-vault-pass
```

To stage deployment or troubleshoot one Mac, use its inventory alias:

```bash
ansible-playbook -i inventories/production/hosts.yml site.yml --limit <worker-alias> --tags agent --ask-become-pass --ask-vault-pass
```

No new role is needed for more Macs. Inventory membership determines where `observability_agent` runs.

## 13. launchd

`launchd` is macOS's native service manager. The project renders system LaunchDaemon plists in `/Library/LaunchDaemons` for:

- `com.observability.victoriametrics`
- `com.observability.grafana`
- `com.observability.otelcol`

The corresponding templates are in `roles/observability_server/templates/` and `roles/observability_agent/templates/`. The current roles use `ansible.builtin.service` for service requests. Actual launchd behavior has not been validated on a real Mac Mini; use [LAUNCHD_TROUBLESHOOTING.md](../LAUNCHD_TROUBLESHOOTING.md) for diagnostic commands and the future refactor guidance.

## 14. Variables and dynamic configuration

The configuration flow is:

```text
inventory -> group_vars -> role task -> Jinja2 template -> final configuration on the Mac
```

For example, `monitoring_server_address` is derived from the first host in the `monitoring_server` inventory group. The agent's collector template combines that address with `victoriametrics_port` to form its export endpoint. This is why roles and templates do not contain a real environment address.

## 15. Security

- Use SSH keys or an approved SSH authentication mechanism for Ansible access.
- The Ansible user needs sudo privileges because installation paths and LaunchDaemon plists are system locations.
- Keep the Grafana administrator password in Ansible Vault or another approved secret store.
- Do not commit passwords, private keys, or production credentials to Git.
- Limit access to ports `8428` and `3000` according to the management network policy.

## 16. Automated, manual, and real-Mac work

| Category | Responsibility |
| --- | --- |
| Automated by Ansible | Directories, downloads, archive extraction, templates, datasource/dashboard provisioning, plist creation, service requests, and verification tasks. |
| Manual initial setup | Select Macs, prepare inventory, provide SSH/sudo access, provide Vault secrets, and permit network access. |
| Requires real-Mac validation | Apple Silicon archive execution, launchd loading/restarts, runtime permissions, firewall policy, service health, and end-to-end metric ingestion. |

## 17. Common teammate questions

| Question | Answer |
| --- | --- |
| Why Ansible? | It applies the same installation and configuration steps consistently across Macs. |
| Why two roles? | The monitoring server and workers have different responsibilities and installed software. |
| Why not one role per technology? | The current two-role split maps to deployment responsibility while keeping the project small. |
| Why OpenTelemetry? | The current design uses one collector pipeline for host metrics and OTLP/HTTP export. |
| Why `hostmetrics`? | It collects the required local system metrics on each worker Mac. |
| Why not Node Exporter? | It is not part of this design; the configured collector `hostmetrics` receiver provides the required host metrics. |
| Why not Prometheus? | It is not part of this design; VictoriaMetrics receives OTLP/HTTP metrics directly. |
| Why VictoriaMetrics? | It is the configured metric store and supports the required OTLP/HTTP ingestion endpoint. |
| Why Grafana? | It provides the configured datasource and dashboards for metric visualization. |
| Why one monitoring Mac? | The inventory has one `monitoring_server` target that centralizes storage and visualization. |
| How do workers know where to send metrics? | The address is derived from the `monitoring_server` inventory group and rendered into the collector config. |
| How does the project scale to N Macs? | Add hosts to `monitored_nodes` and reuse the same agent role. |
| What happens if the monitoring Mac is down? | Agents cannot deliver metrics to its endpoint; review collector logs and restore monitoring-Mac availability. |
| Where are metrics stored? | In VictoriaMetrics under `/opt/observability/var/victoriametrics` on the monitoring Mac. |
| What happens if a collector stops? | That Mac stops exporting new metrics until the collector service is restored. |
| What needs real-Mac testing? | launchd behavior, binaries, permissions, networking, health checks, and end-to-end metrics. |

## 18. KT talking points

Use this short flow during the session:

1. “We have one monitoring Mac and any number of worker Macs.”
2. “Workers collect their own host metrics every 15 seconds with OpenTelemetry Collector.”
3. “The collector adds host identity, batches the metrics, and sends them over OTLP/HTTP.”
4. “VictoriaMetrics on the monitoring Mac stores the data, and Grafana visualizes it.”
5. “Inventory determines which Mac is the monitoring server and which Macs are workers; templates turn those values into configuration.”
6. “We first prove the setup with one monitoring Mac and one worker, then add workers by inventory.”
7. “The remaining risk is real macOS runtime validation, especially launchd.”

## 19. Real environment and no-hardcoding handoff

The repository is a template for a real environment. It avoids embedding environment-specific values in roles and templates. A new engineer must provide the following:

- Real Mac Mini hostnames or reachable addresses in `inventories/production/hosts.yml`.
- The correct `ansible_user` for every Mac.
- SSH key/authentication access and sudo permission for that user.
- Exactly one Mac assigned to `monitoring_server`.
- All collector Macs assigned to `monitored_nodes`.
- A reachable monitoring-Mac address; agents derive this from the selected monitoring-server inventory host.
- The Grafana password through `vault_grafana_admin_password` or the approved secret mechanism.
- Any legitimate environment changes to base paths, ports, or versions in group variables. The default paths and ports should only be changed when the environment requires it.

Template inventory:

```text
monitoring_server:
  placeholder-host

monitored_nodes:
  placeholder-node-01
  placeholder-node-02
```

Real-environment shape:

```text
monitoring_server:
  <monitoring-mac-alias>  (ansible_host: <monitoring-mac-address>)

monitored_nodes:
  <worker-mac-01-alias>  (ansible_host: <worker-mac-01-address>)
  <worker-mac-02-alias>  (ansible_host: <worker-mac-02-address>)
```

Use placeholders and a secure inventory/secret process for real values; do not place production credentials in this guide or in the role templates.
