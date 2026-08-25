# launchd Troubleshooting Guide

> Current status: this project has not been validated on real macOS hardware. Test this guide on one monitoring Mac Mini and one agent Mac Mini before any wider rollout.

## Purpose

`launchd` is the native macOS service manager. This project uses system-wide LaunchDaemons in `/Library/LaunchDaemons` so services start without a logged-in user and can be kept running after a restart.

The managed service labels are:

| Service | Purpose |
| --- | --- |
| `com.observability.victoriametrics` | Stores OTLP/HTTP metrics on the monitoring Mac. |
| `com.observability.grafana` | Serves dashboards on the monitoring Mac. |
| `com.observability.otelcol` | Collects host metrics and sends them to VictoriaMetrics on each agent Mac. |

## Diagnose a failed service

Run the commands with `sudo`. Substitute the applicable service label and plist path.

```bash
# Validate plist syntax before loading it.
sudo plutil -lint /Library/LaunchDaemons/com.observability.otelcol.plist

# Inspect the service in the system domain.
sudo launchctl print system/com.observability.otelcol

# Inspect recent output and errors.
sudo tail -n 100 /opt/observability/var/log/otelcol.out.log
sudo tail -n 100 /opt/observability/var/log/otelcol.err.log
```

For a service that is not loaded, `launchctl print` may report that it could not find the service. Check the plist, paths, permissions, and logs before attempting to load it again.

### Checklist

1. Validate the plist with `plutil -lint`.
2. Confirm root ownership and safe permissions:

   ```bash
   sudo ls -l /Library/LaunchDaemons/com.observability.otelcol.plist
   sudo ls -ld /opt/observability /opt/observability/bin /opt/observability/etc /opt/observability/var/log
   ```

   The plist and service files should be readable by root; this project renders them as `root:wheel` with mode `0644` for plists and configuration files.
3. Confirm the executable named in `ProgramArguments` exists and is executable:

   ```bash
   sudo ls -l /opt/observability/bin/otelcol-contrib
   sudo ls -l /opt/observability/bin/victoria-metrics-prod
   sudo ls -l /opt/observability/grafana/bin/grafana-server
   ```
4. Confirm the configuration path in the plist exists:

   ```bash
   sudo ls -l /opt/observability/etc/otel-config.yaml
   sudo ls -l /opt/observability/etc/grafana/grafana.ini
   ```
5. Inspect service status with `launchctl print system/<label>` and its stdout/stderr logs.
6. Check for port conflicts on the monitoring Mac:

   ```bash
   sudo lsof -nP -iTCP:8428 -sTCP:LISTEN
   sudo lsof -nP -iTCP:3000 -sTCP:LISTEN
   ```

## Manual launchctl recovery

Use the system domain for these LaunchDaemons. Do not run repeated bootstrap commands for a label that is already loaded.

```bash
# Load a valid plist.
sudo launchctl bootstrap system /Library/LaunchDaemons/com.observability.otelcol.plist

# Stop and unload it.
sudo launchctl bootout system/com.observability.otelcol

# Restart an already-loaded service.
sudo launchctl kickstart -k system/com.observability.otelcol
```

Use the equivalent labels for VictoriaMetrics and Grafana. If `bootstrap` fails, read the service error log and run `plutil -lint`; do not assume the issue is launchd itself. For an updated plist, `bootout` followed by `bootstrap` reloads its definition. `kickstart -k` is appropriate when the definition is unchanged and the process only needs a restart.

## Current Ansible implementation

The current roles render the plists and use `ansible.builtin.service` to enable, start, and restart the labels. This keeps the first implementation small, but generic service handling can behave differently across macOS versions and Ansible environments. It may not express the required `launchctl` system-domain bootstrap lifecycle precisely.

Do not change the current implementation until it has been tested on a real Mac Mini. If service loading or restart behavior is unreliable, apply the following focused refactor.

## FUTURE refactor: idempotent launchctl management

Only after real-Mac testing, replace generic service actions in these existing files:

- `roles/observability_server/tasks/victoriametrics.yml`
- `roles/observability_server/tasks/grafana.yml`
- `roles/observability_server/handlers/main.yml`
- `roles/observability_agent/tasks/opentelemetry.yml`
- `roles/observability_agent/handlers/main.yml`

Keep the existing plist templates, labels, binaries, paths, roles, and architecture. Replace the `ansible.builtin.service` tasks/handlers with `launchctl` commands that first inspect the system-domain label, bootstrap only when absent, and kickstart when a rendered file changes.

Example for a **FUTURE refactor only**:

```yaml
- name: Check whether the OTel launchd service is loaded
  ansible.builtin.command:
    cmd: launchctl print system/com.observability.otelcol
  register: otel_launchd_status
  changed_when: false
  failed_when: false

- name: Bootstrap OTel launchd service when absent
  ansible.builtin.command:
    cmd: launchctl bootstrap system /Library/LaunchDaemons/com.observability.otelcol.plist
  when: otel_launchd_status.rc != 0
  changed_when: true

- name: Restart loaded OTel launchd service
  ansible.builtin.command:
    cmd: launchctl kickstart -k system/com.observability.otelcol
  changed_when: true
```

For a plist whose definition changed, use a controlled reload instead:

```yaml
- name: Unload previous OTel launchd definition
  ansible.builtin.command:
    cmd: launchctl bootout system/com.observability.otelcol
  changed_when: true
  failed_when: false

- name: Load updated OTel launchd definition
  ansible.builtin.command:
    cmd: launchctl bootstrap system /Library/LaunchDaemons/com.observability.otelcol.plist
  changed_when: true
```

Attach the reload sequence to the plist template's notification handler. Use the same label-specific pattern for VictoriaMetrics and Grafana. Keep `changed_when`, `failed_when`, and `when` conditions so repeated playbook runs do not report unnecessary changes or fail merely because a service is not yet loaded.

## Safe first test

1. Use one monitoring Mac Mini and one agent Mac Mini in inventory.
2. Run the server play first; validate both plists, services, logs, and ports `8428` and `3000`.
3. Confirm VictoriaMetrics is healthy and Grafana is reachable.
4. Run the agent play; validate the collector configuration and `com.observability.otelcol` status.
5. Confirm the agent can reach the monitoring Mac on port `8428` and metrics appear in Grafana.
6. Re-run each playbook to assess idempotency, then restart each service once.
7. Only then add the remaining Mac Minis to `monitored_nodes`.

## Common failures

| Symptom | Likely cause | Practical response |
| --- | --- | --- |
| `bootstrap` fails | Invalid plist, bad path, or permissions | Run `plutil -lint`; check `ProgramArguments`, configuration paths, and ownership. |
| Service is loaded but exits immediately | Invalid application configuration or inaccessible path | Read the service error log; run the collector validation command; verify binary and config paths. |
| `kickstart` cannot find the service | Label is not loaded in the system domain | Check with `launchctl print`; bootstrap the plist after validating it. |
| VictoriaMetrics or Grafana cannot bind | Port 8428 or 3000 is already in use | Find the listener with `lsof`; stop or reconfigure the conflicting process deliberately. |
| Agent has no visible metrics | Network path, collector configuration, or server availability | Check collector logs, validate its config, verify port 8428 reachability, and confirm VictoriaMetrics health. |
| Changes do not take effect | The loaded service has an older plist definition | Use `bootout` followed by `bootstrap`, then inspect the service again. |
