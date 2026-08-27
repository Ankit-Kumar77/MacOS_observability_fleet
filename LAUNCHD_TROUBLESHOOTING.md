# launchd Troubleshooting Guide

> Current status: real-Mac testing is in progress but launchd service lifecycle has not yet been confirmed working end to end. Test this guide on one monitoring Mac Mini and one agent Mac Mini before any wider rollout.

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

   The plist and service files should be readable by root. This project renders them as `root:wheel`, with mode `0644` for plists and non-sensitive files, and `0600` for anything carrying a secret (`otel-config.yaml`, `grafana.ini`, the provisioned datasource, and the VictoriaMetrics password file).
3. Confirm the executable named in `ProgramArguments` exists and is executable:

   ```bash
   sudo ls -l /opt/observability/bin/otelcol-contrib
   sudo ls -l /opt/observability/bin/victoria-metrics-prod
   sudo ls -l /opt/observability/grafana/bin/grafana
   ```
4. Confirm the configuration path in the plist exists:

   ```bash
   sudo ls -l /opt/observability/etc/otel-config.yaml
   sudo ls -l /opt/observability/etc/grafana/grafana.ini
   sudo ls -l /opt/observability/etc/victoriametrics-auth-password
   ```

   The collector config, `grafana.ini`, the provisioned datasource and the
   VictoriaMetrics password file all carry secrets and are mode `0600`. A
   VictoriaMetrics service that starts but answers `401` on every query usually
   means the password file is missing, unreadable, or does not match what the
   agents and the Grafana datasource were rendered with.

   Note that `/opt/observability/grafana` and the two binaries under
   `/opt/observability/bin` are symlinks into versioned install directories, so
   `ls -l` shows the link target - that is expected.
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

Real-Mac testing confirmed `ansible.builtin.service` has no macOS implementation at all -- it fails immediately with `get_service_tools not implemented on target platform`, before ever touching launchd. The roles now drive `launchctl` directly through two shared task files in `observability_common`, included the same way `install_versioned_archive.yml` is:

- **`launchd_ensure_started.yml`** -- included from the last task in each role's install file (`victoriametrics.yml`, `grafana.yml`, `opentelemetry.yml`). Runs `launchctl print system/<label>`, `changed_when: false, failed_when: false`, and only runs `launchctl bootstrap system <plist>` when that check's `rc != 0`. `RunAtLoad` and `KeepAlive` are always `true` in `launchd_daemon.plist.j2`, so a bootstrapped daemon starts immediately and persists across reboots -- there is no separate "enable" step to express.
- **`launchd_restart.yml`** -- included from each role's restart handler (`Restart VictoriaMetrics`, `Restart Grafana`, `Restart OTel Collector`), notified by the plist template task and every config file the daemon reads. Runs `launchctl bootout system/<label>` (`failed_when: false`, since the label may not be loaded yet) followed by `launchctl bootstrap system <plist>`, so a changed plist definition is actually picked up -- `kickstart -k` alone would restart the process but keep serving whatever definition was already loaded.

Both take `launchd_service_label` and `launchd_service_plist` as vars from the caller, the same parameterization pattern the shared plist template itself uses.

**This has not yet been confirmed working end to end on real hardware** -- it replaces a `ansible.builtin.service` call that failed before reaching launchd at all, but the `launchctl` sequence itself still needs a real run against `bootstrap`, `bootout`, and a live `KeepAlive` restart. Use the checklist and manual recovery commands above if it misbehaves.

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
