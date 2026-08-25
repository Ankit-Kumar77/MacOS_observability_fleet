# Mac Mini Observability

Ansible automation for host-metric observability on Apple Silicon Mac Minis. One monitoring Mac Mini runs VictoriaMetrics and Grafana; every monitored Mac Mini runs an OpenTelemetry Collector.

## Architecture

![Mac Mini observability architecture](docs/architecture.svg)

The metric path is:

`hostmetrics -> resourcedetection -> batch -> otlphttp -> VictoriaMetrics -> Grafana`

Each collector sends OTLP/HTTP to `http://<monitoring-server-address>:8428/opentelemetry/v1/metrics`. There is no Prometheus server or Prometheus-specific Collector component in this design.

## Before deployment

You need:

- A control machine with Ansible installed and network access to the target Macs.
- Apple Silicon Macs running macOS, with SSH enabled.
- An Ansible SSH user on every Mac with sudo permission.
- One Mac selected as the monitoring server and at least one separate Mac selected as a monitored node.
- The hostname or reachable address, SSH user, and SSH authentication method for each Mac.
- Network access from monitored Macs to the monitoring Mac on port `8428`; browser access to Grafana on port `3000` as required.

Use SSH keys where possible. Confirm connectivity before deployment:

```bash
ansible all -i inventories/production/hosts.yml -m ping
```

## Prepare the inventory

Edit [inventories/production/hosts.yml](inventories/production/hosts.yml). Define exactly one host in `monitoring_server` and put each collector host in `monitored_nodes`.

```yaml
all:
  children:
    monitoring_server:
      hosts:
        monitoring-mac:
          ansible_host: <monitoring-mac-address>
          ansible_user: <ssh-user>
    monitored_nodes:
      hosts:
        agent-mac-01:
          ansible_host: <agent-mac-address>
          ansible_user: <ssh-user>
```

The agents derive `monitoring_server_address` from the monitoring-server inventory entry. Do not put real production addresses or credentials in source-controlled variable files.

## First-time setup: one monitoring Mac and one agent Mac

Start with one of each. Do not scale out until this flow succeeds on physical Macs.

1. **Prepare the monitoring Mac.** Confirm SSH and sudo access, and ensure ports `8428` and `3000` are available.
2. **Configure inventory.** Add the monitoring Mac and one agent as shown above.
3. **Provide the Grafana password.** Store it with Ansible Vault, for example:

   ```bash
   ansible-vault encrypt_string 'replace-with-a-strong-password' --name 'vault_grafana_admin_password'
   ```

   Make the resulting vaulted variable available to the playbook. The server group references `vault_grafana_admin_password`.
4. **Run the server setup.**

   ```bash
   ansible-playbook -i inventories/production/hosts.yml site.yml --tags server --ask-become-pass --ask-vault-pass
   ```
5. **Verify VictoriaMetrics.** On the monitoring Mac, check `http://localhost:8428/health` and review `/opt/observability/var/log/victoriametrics.err.log` if it does not respond.
6. **Verify Grafana.** Open `http://<monitoring-mac-address>:3000`, sign in with the configured password, and confirm the VictoriaMetrics datasource is present.
7. **Prepare the agent Mac.** Confirm SSH/sudo access and that it can reach the monitoring Mac on port `8428`.
8. **Run the agent setup.** Replace the limit with your agent inventory alias.

   ```bash
   ansible-playbook -i inventories/production/hosts.yml site.yml --limit agent-mac-01 --tags agent --ask-become-pass --ask-vault-pass
   ```
9. **Verify the collector.** On the agent, run:

   ```bash
   sudo /opt/observability/bin/otelcol-contrib validate --config=/opt/observability/etc/otel-config.yaml
   ```

   Then review `/opt/observability/var/log/otelcol.err.log` if needed.
10. **Verify end-to-end metrics.** Confirm VictoriaMetrics remains healthy, then open the provisioned Grafana dashboard and check that the agent host appears.

## Scale to N Mac Minis

After the first monitoring-and-agent test works:

1. Add each additional Mac to `monitored_nodes` in the inventory, using its own placeholder-replaced address and SSH user.
2. Apply the same agent role to all monitored nodes:

   ```bash
   ansible-playbook -i inventories/production/hosts.yml site.yml --tags agent --ask-become-pass --ask-vault-pass
   ```
3. Target one host during troubleshooting or staged rollout:

   ```bash
   ansible-playbook -i inventories/production/hosts.yml site.yml --limit agent-mac-02 --tags agent --ask-become-pass --ask-vault-pass
   ```
4. Check the Grafana fleet dashboard for all hosts.

The `observability_agent` role is reused unchanged for any number of monitored Mac Minis, including a fleet of approximately 40.

## What the project installs

| Role | Target | Installed/configured components |
| --- | --- | --- |
| `observability_server` | `monitoring_server` | VictoriaMetrics, Grafana, datasource/dashboard provisioning, launchd plists |
| `observability_agent` | `monitored_nodes` | `otelcol-contrib`, host-metric pipeline, launchd plist |

Software, configuration, data, and logs are placed under `/opt/observability`. The project uses the existing macOS launchd approach with plists in `/Library/LaunchDaemons`.

## Troubleshooting

| Problem | First checks |
| --- | --- |
| SSH or Ansible cannot connect | Confirm `ansible_host`, `ansible_user`, SSH keys, and `ansible all -m ping`. |
| Collector does not start | Validate `/opt/observability/etc/otel-config.yaml`; inspect `otelcol.err.log`; confirm the collector binary exists. |
| VictoriaMetrics does not start | Check the binary, port `8428`, and `victoriametrics.err.log`. |
| Grafana does not start | Check the binary, port `3000`, configuration path, and `grafana.err.log`. |
| Metrics do not appear | Check the collector log, agent-to-server access on `8428`, VictoriaMetrics health, then Grafana datasource/dashboard. |
| launchd or plist issue | Use [LAUNCHD_TROUBLESHOOTING.md](LAUNCHD_TROUBLESHOOTING.md) for plist validation, service status, logs, and safe recovery commands. |
| Incorrect path or port conflict | Compare the plist `ProgramArguments` with the files under `/opt/observability`; use `lsof` to identify a listener on ports `8428` or `3000`. |

## Security

- Never commit real passwords, private keys, or credentials.
- Use Ansible Vault for the Grafana password and any other secret values.
- Keep inventory addresses environment-specific and avoid hard-coding production infrastructure into roles or templates.
- Restrict SSH, sudo access, and network exposure of ports `8428` and `3000` to the required users and networks.

## Validation limits

Before physical Macs are available, YAML, Jinja templates, plist XML, SVG, dashboard JSON, variable references, and Ansible syntax can be checked locally when the tools are installed.

Physical Apple Silicon Mac Minis are required to validate archive execution, launchd bootstrap/restart behavior, file ownership at runtime, network policy, service health, and end-to-end metric ingestion. This project must not be considered production-tested until the one-monitoring-Mac plus one-agent-Mac test succeeds.

## More documentation

- [Project internal flow](docs/PROJECT_FLOW.md)
- [Confluence-ready project overview](docs/project-overview.md)
- [launchd troubleshooting](LAUNCHD_TROUBLESHOOTING.md)
