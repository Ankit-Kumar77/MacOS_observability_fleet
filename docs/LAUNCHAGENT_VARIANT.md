# LaunchAgent / no-sudo variant

This branch (`feat/launchagent-no-sudo`) is the `main` deployment with a different privilege and service-management model. The monitoring architecture, roles, telemetry pipeline, dashboards and secret handling are the same. What changes is **who runs the services and where they live**:

| | `main` (system-level) | This variant (LaunchAgent / no-sudo) |
| --- | --- | --- |
| Ansible privilege | `become: sudo`, `--ask-become-pass` | `become = False` everywhere; no sudo password, no admin rights |
| Service type | root LaunchDaemons in `/Library/LaunchDaemons` | per-user LaunchAgents in `~/Library/LaunchAgents` |
| launchd domain | `system` | `user/<uid>` (default) or `gui/<uid>` |
| Install root | `/opt/observability`, `root:wheel` | `~/tools/observability`, owned by the deployment user |
| Labels | `com.observability.<service>` | `com.observability.user.<service>` (prefix configurable) |
| Download staging | `/tmp` | `~/tools/observability/var/downloads` |
| Starts at boot | yes | **no** (see [LaunchAgent lifecycle](#launchagent-lifecycle)) |
| SSH state collector | root, uses `lsof` | deployment user, uses `netstat -anv` + `who`; see [SSH observability](#ssh-observability-capability-matrix) |
| Ansible transport | SSH | SSH (unchanged) |

Nothing else was replaced: VictoriaMetrics `1.101.0`, Grafana `13.2.0` and `otelcol-contrib` `0.159.0` keep their pinned versions and checksums, the collector pipeline (`hostmetrics` + `tcp_check` + `otlp_json_file` → `resourcedetection` → `batch` → `otlphttp`) is unchanged, and the Fleet Overview and Mac Mini Detail dashboards are provisioned the same way.

## What runs where

```text
              ANSIBLE CONTROL MACHINE
                       |
             normal Ansible SSH (no become)
                       |
             +---------+---------+
             |                   |
             v                   v
    MONITORING MAC          WORKER MAC MINIS
    user/<uid> domain       user/<uid> domain
             |                   |
  com.observability.user.   com.observability.user.otelcol    (KeepAlive)
    victoriametrics         com.observability.user.sshstate   (every 30 s)
    grafana                      |
             ^                   | OTLP/HTTP :victoriametrics_port
             +-------------------+
```

Layout on each Mac (`base_dir`, default `{{ ansible_facts['user_dir'] }}/tools/observability`):

```text
~/tools/observability/
├── bin/
│   ├── otelcol-contrib -> otelcol-contrib-0.159.0/otelcol-contrib      (workers)
│   ├── observability-ssh-state                                         (workers)
│   └── victoria-metrics-prod -> victoria-metrics-1.101.0/...           (server)
├── grafana -> grafana-13.2.0                                           (server)
├── etc/
│   ├── otel-config.yaml                0600 (workers)
│   └── grafana/grafana.ini, provisioning/{datasources,dashboards}/     (server)
└── var/
    ├── victoriametrics/                VictoriaMetrics storage (server)
    ├── grafana/                        Grafana sqlite + plugins (server)
    ├── ssh-state/                      0700, SSH snapshot (workers)
    ├── downloads/                      release archives, removed after extraction
    └── log/                            <service>.out.log / <service>.err.log

~/Library/LaunchAgents/
├── com.observability.user.victoriametrics.plist   (server)
├── com.observability.user.grafana.plist           (server)
├── com.observability.user.otelcol.plist           (workers)
└── com.observability.user.sshstate.plist          (workers)
```

Every path in the plists is absolute (launchd does not expand `~`). Keep `base_dir` out of `~/Desktop`, `~/Documents` and `~/Downloads`: macOS privacy controls (TCC) block background agents from reading them.

## How sudo was removed

- `ansible.cfg` sets `become = False`; both plays in `site.yml` set `become: false`.
- No task sets `owner`/`group`; files belong to whoever deploys them. `0600` stays on `grafana.ini`, `otel-config.yaml`, the provisioned datasource and the VictoriaMetrics password file; the SSH snapshot directory stays `0700`.
- Release archives are extracted by the user with `tar`, followed by a `go-w` permission normalisation (tar cannot restore foreign owners when run unprivileged, but can carry over permission bits).
- Every `launchctl` call targets `launchd_domain` (`user/<uid>` or `gui/<uid>`). The only `system/` reference is a read-only `launchctl print system/<label>`, used by preflight to detect the old deployment and by the SSH script to see whether Remote Login is on. Both work for any user.
- `roles/observability_common/tasks/preflight.yml` runs first in both roles and fails, before anything is written, if: the play is running as root or with become; the target is not macOS; `base_dir`/`launch_agents_dir` are not absolute or sit inside `/opt/observability`; the per-user launchd domain does not exist; the system-level deployment is present and side-by-side mode is off; side-by-side ports collide; or a port this role needs is already held by another process.

## LaunchAgent lifecycle

A LaunchAgent is not a LaunchDaemon. Read this before relying on the variant for unattended monitoring.

| Event | `launchd_domain_type: user` (default) | `launchd_domain_type: gui` |
| --- | --- | --- |
| Playbook run over SSH | Bootstraps into `user/<uid>`, which exists for any logged-in user, including the SSH session Ansible itself opens. Plists carry `LimitLoadToSessionType: Background`. | Works only while the same user is logged in at the console (or via auto-login); otherwise preflight fails. Plists carry `Aqua`. |
| `RunAtLoad` / `KeepAlive` | Agent starts immediately and is restarted if it exits. | Same. |
| SSH session that ran Ansible ends | **Not verified on Mac Minis.** The agents are owned by launchd, not by the SSH session, but whether macOS tears down `user/<uid>` when the user's last session ends has to be tested on the target macOS version. | Unaffected while the console session lasts. |
| User logs out of the console | Not expected to affect `user/<uid>` agents; not verified. | Agents stop with the GUI session. |
| User logs in at the console | Not verified whether launchd loads Background agents from `~/Library/LaunchAgents` automatically. Re-run the play if they are absent. | launchd loads `Aqua` agents from `~/Library/LaunchAgents` at login. |
| Reboot | **Nothing starts at boot.** Re-run the play for that Mac (`--limit <host>`), or log in. | Agents start when the user logs in at the console; with auto-login enabled that is effectively at boot. |
| Repeated playbook runs | Idempotent: an agent already loaded is left alone; a changed plist, config, binary symlink or dashboard file triggers a bootout + bootstrap of that one agent only. | Same. |

Practical guidance:

- For Mac Minis that are rebooted unattended, use `launchd_domain_type: gui` together with macOS auto-login for the deployment user (setting up auto-login is a one-time admin action outside this playbook), or schedule a re-run of the play after reboots.
- If the per-user domain is missing, preflight fails with an explanation. The playbook never falls back to another domain, another user or sudo.
- Switching `launchd_domain_type` between runs is handled: if a label is not loaded in the target domain, it is booted out of the other per-user domain first so no duplicate keeps running.

## SSH observability capability matrix

The SSH state collector (`templates/ssh_state.sh.j2`) is kept, now running every `otel_ssh_check_interval` seconds as the LaunchAgent `com.observability.user.sshstate`. It emits the same metrics and labels as `main`, plus one new cross-check gauge.

What an unprivileged user can read on macOS was measured on macOS 27 (uid 501, no sudo):

| Source | Unprivileged? | Used for |
| --- | --- | --- |
| `launchctl print system/<label>` | yes (rc 0 for a loaded system job, 113 when not loaded) | Remote Login state |
| `netstat -an` / `netstat -anv` | yes, all sockets, **including the owning `process:pid` of root's sockets** | listening port, established connections, socket → sshd pid → source address |
| `nc` to 127.0.0.1 | yes | sshd protocol banner |
| `ps -axww -o pid,ppid,etime,command` | yes, including root's and other users' process titles | `sshd-session: <user>@<tty>` session processes |
| `who` (`/var/run/utmpx`, world-readable) | yes | terminal sessions with remote host, as a fallback |
| `lsof` on other users' processes | **no** (returns nothing) | not used any more (it was the root-only piece in `main`) |

| Metric / field | Status | Notes |
| --- | --- | --- |
| `ssh_remote_login_enabled` | **Fully preserved** | Same `launchctl print system/com.openssh.sshd` check. |
| `ssh_port_listening` | **Fully preserved** | Same `netstat -an` check. |
| `ssh_service_up` | **Fully preserved** | Same loopback banner check. |
| `ssh_sessions` | **Partially preserved** | Counts sessions the collector can attribute: the `sshd-session` process (or its privileged parent) owns an established socket on the SSH port per `netstat -anv`, or (for terminal sessions) utmpx has a matching record with a remote host. A session that matches neither, typically another user's `notty` (scp/sftp/remote command) session whose socket netstat attributes to a different pid, is not counted. |
| `ssh_connections` *(new)* | **New, complete** | Every established TCP connection on the SSH port, from any user, including connections still authenticating. `ssh_connections > ssh_sessions` means some activity is not attributed. It does not mean nothing is there. |
| `ssh_session_start_time` user, tty, login time, sshd pid | **Partially preserved** | Same labels (`user_name`, `ssh_session_tty`, `ssh_session_id`, value = login time) for every attributed session. |
| `ssh_session_start_time` `source_address` | **Partially preserved** | From the netstat socket when attribution is by socket. From the utmpx host when it falls back to utmpx; that host can be a hostname rather than an IP if sshd's `UseDNS` is on. |
| Terminal contents, commands, keys, environment, sshd log | **Not collected** (same as `main`) | Deliberately never read. |

Dashboards: the SSH panels on both dashboards are kept, and their descriptions now say that sessions are what an unprivileged collector could attribute. The Fleet Overview's **SSH Access** table gains a **TCP Connections** column, and the Mac Mini Detail **SSH Sessions** graph gains a **TCP connections** series.

Validation status for SSH:

- Verified on this Mac without sudo: the rendered script's syntax; the unprivileged sources above; Remote Login detection (off here); and session attribution. For attribution, a process titled `sshd-session: <user>@notty` holding an established socket on a test port was counted with the correct user, tty, pid and source address. It reached VictoriaMetrics and both **Current SSH Sessions** tables through the full LaunchAgent pipeline. A process titled `sshd-session: mallory@notty` with no socket and no utmpx record was correctly not counted.
- **Not verified:** a real `com.openssh.sshd` with Remote Login on and live sessions from several users. Turning Remote Login on needs an administrator, so this was not done here. Whether `netstat -anv` attributes a real sshd socket to the `sshd-session` child or its `[priv]` parent (the script accepts either), and the column layout of `netstat -anv` on older macOS releases (the script reads the `pid` column from the header in that case), must be checked on the Mac Minis.

Set `otel_ssh_session_details_enabled: false` to drop the per-session series and keep only the gauges, or `otel_ssh_monitoring_enabled: false` to disable SSH state entirely.

## Side-by-side testing with the system-level deployment

The old system-level stack may still be running on some Macs (for example MM7 as the monitoring Mac and MM4 as a worker), and you may not have the rights to remove it. This variant never stops, unloads, deletes or modifies anything belonging to it: no `/Library/LaunchDaemons` writes, no `system/` bootout, no `/opt/observability` writes, no access to its VictoriaMetrics data.

| | System-level (`main`) | LaunchAgent stack, side-by-side mode |
| --- | --- | --- |
| VictoriaMetrics | `:8428`, `/opt/observability/var/victoriametrics` | `:8429`, `~/tools/observability/var/victoriametrics` |
| Grafana | `:3000` | `:3001` |
| Labels | `com.observability.*` in `system` | `com.observability.user.*` in `user/<uid>` |
| Collector target | old VictoriaMetrics `:8428` | new VictoriaMetrics `:8429` |

Enable it fleet-wide, so the monitoring Mac and every collector agree on the port:

```yaml
# inventories/production/group_vars/all.yml
observability_side_by_side: true
# optional, these are the defaults:
observability_side_by_side_victoriametrics_port: 8429
observability_side_by_side_grafana_port: 3001
```

or per run:

```bash
ansible-playbook -i inventories/production/hosts.yml site.yml -e observability_side_by_side=true
```

With it on, `victoriametrics_port` becomes 8429, so VictoriaMetrics listens there and every collector's `otlphttp` exporter and `tcp_check` target it. `grafana_port` becomes 3001, and the Grafana datasource points at `http://localhost:8429`.

What preflight enforces:

- **Mode off, old stack found** (its plists in `/Library/LaunchDaemons` or its labels loaded in `system`): fail, naming what was found. Two copies on one Mac would fight over 8428/3000 or double-report metrics.
- **Mode on:** the new VictoriaMetrics and Grafana ports must differ from the old ones (`observability_legacy_victoriametrics_port` / `_grafana_port`), the new labels must differ from the old labels, and `base_dir` must not be inside `/opt/observability`. So the two can never share a port or a storage directory.
- **Either mode:** if a port is already taken before our agent is loaded, fail and name the port. The agent would otherwise crash-loop under `KeepAlive`.

Disable side-by-side mode (`observability_side_by_side: false`, the default) for a normal deployment on 8428/3000 once the old stack is gone. Re-run **both** plays when you toggle it, because collector configs must be re-rendered with the new port. VictoriaMetrics data does not move between the two stacks.

## Operating the LaunchAgents

All commands run as the deployment user, without sudo. `uid=$(id -u)`; use `gui/$uid` instead of `user/$uid` if you set `launchd_domain_type: gui`.

```bash
# Status (state = running, pid, last exit code)
launchctl print user/$(id -u)/com.observability.user.otelcol
launchctl list | grep com.observability.user

# Restart a running agent in place
launchctl kickstart -k user/$(id -u)/com.observability.user.otelcol

# Stop (unload) / start (load) an agent
launchctl bootout   user/$(id -u)/com.observability.user.otelcol
launchctl bootstrap user/$(id -u) ~/Library/LaunchAgents/com.observability.user.otelcol.plist

# Logs
tail -f ~/tools/observability/var/log/otelcol.err.log

# Validate a plist and the collector config
plutil -lint ~/Library/LaunchAgents/com.observability.user.*.plist
~/tools/observability/bin/otelcol-contrib validate --config=$HOME/tools/observability/etc/otel-config.yaml
```

Re-running the play (optionally with `--limit <host>`) is always a safe way to get an agent back to its intended state.

To remove this stack from a Mac (user-owned only, so no sudo): boot out each `com.observability.user.*` label from `user/$(id -u)` (or `gui/`), delete the four plists from `~/Library/LaunchAgents`, then delete `~/tools/observability`.

## Diagnosing failures

| Symptom | Check |
| --- | --- |
| Preflight: "LaunchAgent / no-sudo variant" | You connected as root or passed `--become`. Connect as the normal user. |
| Preflight: domain not available | `launchctl print user/$(id -u)` (or `gui/`) on that Mac, as that user. For `gui`, the user must be logged in at the console. |
| Preflight: found the system-level deployment | Enable `observability_side_by_side`, or have an admin remove the old stack. |
| Preflight: port already in use | `lsof -nP -iTCP:<port> -sTCP:LISTEN`. Usually the old stack; use side-by-side mode or other ports. |
| `Bootstrap failed: 5: Input/output error` | Usually a plist without `LimitLoadToSessionType` being loaded into `user/<uid>`, or a bootstrap racing a bootout. The role sets the key and retries; check `plutil -lint` and the label is not loaded twice. |
| Agent loaded but `state = waiting` / restarting | Read `~/tools/observability/var/log/<service>.err.log`; confirm the `ProgramArguments` paths exist. |
| No metrics from a worker | Collector log, `curl http://<monitoring-mac>:<victoriametrics_port>/health` from the worker, and whether side-by-side mode is set identically for the server and the workers. |
| Worker vanished after a reboot | Expected for `user` domain: re-run `--limit <host> --tags agent`, or use `gui` + auto-login. |
| SSH sessions lower than TCP connections | Expected for sessions the unprivileged collector cannot attribute; see the capability matrix above. |

## Validation status

See the "Validation" section of [README.md](../README.md#validation) for exactly what was run on this branch and what still needs physical Mac Minis.
