# Mac Mini Observability

An Ansible project for collecting host metrics from Apple Silicon Mac Minis and presenting them through VictoriaMetrics and Grafana. It is designed for one monitoring Mac Mini and a fleet of monitored Mac Minis, without Ansible Galaxy dependencies.

## Architecture

![Mac Mini observability architecture](docs/architecture.svg)

Each monitored Mac runs `otelcol-contrib`. Its metrics pipeline is:

`hostmetrics -> resourcedetection -> batch -> otlphttp -> VictoriaMetrics`

The collector sends OTLP/HTTP metrics to `http://<inventory-derived-server>:8428/opentelemetry/v1/metrics`. VictoriaMetrics stores the data and Grafana queries VictoriaMetrics for the supplied host dashboard. There is no Prometheus server or Prometheus-specific Collector component in this design.

## Repository structure

```text
.
|- ansible.cfg
|- site.yml
|- inventories/production/
|  |- hosts.yml
|  `- group_vars/
|     |- all.yml
|     |- monitoring_server.yml
|     `- monitored_nodes.yml
|- roles/
|  |- observability_server/
|  `- observability_agent/
`- docs/architecture.svg
```

`observability_server` installs VictoriaMetrics and Grafana, provisions the datasource and dashboard, and renders their launchd plists. `observability_agent` installs the Apple Silicon `otelcol-contrib` release, renders the OTLP/HTTP pipeline, and renders its launchd plist.

## Inventory and variables

Define exactly one host in `monitoring_server` and all collectors in `monitored_nodes` in [inventories/production/hosts.yml](inventories/production/hosts.yml). Replace only the placeholder `ansible_host` and `ansible_user` values with your environment values. `monitoring_server_address` is derived from the first monitoring-server inventory host, so agent configuration contains no server IP address.

Shared paths and the VictoriaMetrics port are in `group_vars/all.yml`. Server-specific versions, Grafana port, and the Grafana password reference are in `group_vars/monitoring_server.yml`. The collector version and its Darwin ARM64 release URL are in `group_vars/monitored_nodes.yml`.

## Components

### OpenTelemetry Collector

The configured release URL resolves to `otelcol-contrib_<version>_darwin_arm64.tar.gz`, the Apple Silicon macOS distribution. The role extracts the expected `otelcol-contrib` executable to `/opt/observability/bin/otelcol-contrib`, asserts that it exists, and uses that exact path in `com.observability.otelcol.plist`.

The rendered config is `/opt/observability/etc/otel-config.yaml`, which is also the plist config argument. It uses the `hostmetrics` receiver; `resourcedetection` and `batch` processors; and the `otlphttp` exporter. The agent verification task runs `otelcol-contrib validate` against that rendered file. The selected contrib distribution is the required component-bearing distribution for these components.

### VictoriaMetrics

VictoriaMetrics is installed as `/opt/observability/bin/victoria-metrics-prod`. Its plist uses the same executable, stores data in `/opt/observability/var/victoriametrics`, writes launchd output under `/opt/observability/var/log`, and listens on the shared `victoriametrics_port` (default `8428`). This is the port used by the collector's OTLP endpoint.

### Grafana

Grafana is extracted beneath `/opt/observability/grafana`, matching both its plist `WorkingDirectory` and server executable path. Its configuration and provisioning files live under `/opt/observability/etc/grafana`. The provisioned VictoriaMetrics datasource points to the local VictoriaMetrics listener. The dashboard uses collector-emitted host-metric names for CPU, memory, filesystem, and network data.

## Prerequisites

- Ansible installed on the control node.
- SSH connectivity and a sudo-capable Ansible account on each target Mac.
- Apple Silicon macOS targets with outbound access to the configured release URLs during installation.
- Network access from monitored nodes to the monitoring server on port `8428`, and from Grafana users to port `3000` as appropriate.
- A Grafana admin password provided through Ansible Vault; do not commit a real password.

## Deployment

1. Set the inventory host addresses and users.
2. Create an encrypted password variable, for example:

   ```bash
   ansible-vault encrypt_string 'replace-with-a-strong-password' --name 'vault_grafana_admin_password'
   ```

   Store the resulting variable in an encrypted vars file available to the playbook.
3. Check connectivity:

   ```bash
   ansible all -i inventories/production/hosts.yml -m ping
   ```
4. Deploy the monitoring server:

   ```bash
   ansible-playbook -i inventories/production/hosts.yml site.yml --tags server --ask-become-pass --ask-vault-pass
   ```
5. Test one agent first (replace the limit with an inventory alias):

   ```bash
   ansible-playbook -i inventories/production/hosts.yml site.yml --limit mac-mini-02 --tags agent --ask-become-pass --ask-vault-pass
   ```
6. Verify data is present in Grafana at `http://<monitoring-server-address>:3000`, then deploy the remaining agents:

   ```bash
   ansible-playbook -i inventories/production/hosts.yml site.yml --tags agent --ask-become-pass --ask-vault-pass
   ```

To scale to 40 Mac Minis, add their inventory entries under `monitored_nodes` and repeat the agent deployment. The role is intended to be idempotent: archives use binary existence guards, and rendered files notify services only when their content changes.

## macOS and launchd

The project intentionally uses the existing launchd approach: plists are written to `/Library/LaunchDaemons` and managed through Ansible's service interface. No service-management redesign is included. The binary, configuration, storage, port, and log paths have been checked for internal consistency, but launchd bootstrap, restart, ownership, and runtime behavior must be tested on the first real Mac Mini.

## Validation and troubleshooting

Run static checks from the repository root:

```bash
ansible-playbook -i inventories/production/hosts.yml site.yml --syntax-check
ansible-lint
```

The playbook also validates the rendered collector configuration on a target and checks VictoriaMetrics and Grafana health endpoints. Before real Mac access, local validation can confirm YAML, Jinja rendering, variable references, Ansible syntax, and static configuration consistency. It cannot confirm macOS archive behavior, collector component availability at runtime, downloads, launchd operation, firewall policy, or end-to-end ingestion.

If deployment fails, first check SSH/sudo access, the inventory-derived monitoring-server address, port `8428` reachability from an agent, release URL availability, and `/opt/observability/var/log` on the target. If Grafana is healthy but has no data, run the collector validation command shown in the agent verification task and inspect the collector output log.

## Security

Keep credentials in Ansible Vault or an equivalent secret source, restrict SSH keys and sudo permissions to the required operators, and limit network access to the two service ports. Use TLS and authentication appropriate to the network boundary before exposing either service outside a trusted management network.
