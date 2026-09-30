# No-sudo deployment variant

This branch (`feat/no-sudo-deployment`) is a fallback deployment of the same
observability stack for Mac Minis where the deployment account has **no sudo
or admin rights**. It keeps the architecture, roles, dashboards and latency
checks of the system-level deployment, runs everything as per-user
LaunchAgents under the connecting user's home directory, and drops the SSH
observability feature.

## Main vs no-sudo at a glance

| | System-level deployment (main line) | No-sudo variant (this branch) |
| --- | --- | --- |
| Privilege | `become: sudo`; every run needs `--ask-become-pass` or passwordless sudo | No privilege escalation at all. `become: false` in `ansible.cfg` and on every play |
| Install root | `/opt/observability`, `root:wheel` | `~/tools/observability` (`base_dir`), owned by the deployment user |
| Service definitions | `/Library/LaunchDaemons/*.plist` | `~/Library/LaunchAgents/*.plist` (`launch_agents_dir`) |
| launchd domain | `system` | `user/<uid>` (default) or `gui/<uid>` (`launchd_domain_type`) |
| Services run as | root | the deployment user |
| Starts at boot | Yes, before anyone logs in | **No.** See [LaunchAgent lifecycle](#launchagent-lifecycle) |
| Download staging | `/tmp` | `~/tools/observability/var/downloads` (`download_dir`) |
| SSH observability | Yes: `com.observability.sshstate`, SSH panels on both dashboards | **Removed** |
| Host metrics (CPU, memory, disk, filesystem, network, load) | Yes | Yes, unchanged |
| Latency (`tcp_check` to VictoriaMetrics) | Yes | Yes, unchanged |
| Fleet dashboard + Mac Mini selector | Yes | Yes (SSH panels removed) |
| Mac Mini Detail dashboard | Yes | Yes (SSH row removed) |

Unchanged: the inventory groups (`monitoring_server`, `monitored_nodes`), the
derived `monitoring_server_address`, the OTLP/HTTP → VictoriaMetrics → Grafana
path, the metric names, the checksum-pinned versioned installs, the Grafana
credential handling, optional VictoriaMetrics basic auth, and the
`prerequisites` → install → `flush_handlers` → `verify` order. Ansible still
manages every Mac remotely over its normal SSH connection. Only the *SSH
observability feature* (collecting SSH state and sessions) is gone.

## Deploying

The only requirements on each Mac are Remote Login for the Ansible user and
a home directory with about 1.4 GB free on the monitoring Mac, or about 340 MB
on each monitored Mac. No admin group membership, sudoers entry or become
password is needed.

```bash
ansible all -i inventories/production/hosts.yml -m ping

# Everything: monitoring Mac first, then every monitored Mac
ansible-playbook -i inventories/production/hosts.yml site.yml

# Monitoring Mac only
ansible-playbook -i inventories/production/hosts.yml site.yml --tags server

# All monitored Macs, or one at a time
ansible-playbook -i inventories/production/hosts.yml site.yml --tags agent
ansible-playbook -i inventories/production/hosts.yml site.yml --tags agent --limit mac-mini-02

# Health checks only
ansible-playbook -i inventories/production/hosts.yml site.yml --tags verify
```

Add `--ask-vault-pass` only if you have vaulted credentials. Do not add
`--become` or `--ask-become-pass`: the plays set `become: false`, and a
preflight assert fails the run if it finds itself running as root.

Each play starts with two preflight checks (`observability_common`'s
`tasks/preflight.yml`):

1. **Not root.** The connecting user's uid must be non-zero.
2. **No system-level install of the same service.** If
   `/Library/LaunchDaemons/com.observability.{victoriametrics,grafana,otelcol}.plist`
   exists, the sudo variant is already on that Mac. Running both would
   double-report metrics (agent) or fight over ports 8428/3000 (server).
   Removing a system daemon needs an administrator, so the play stops and says so.

## Paths

All derived from the deployment user's home directory (the `user_dir` fact)
in each role's `defaults/main.yml`, and overridable in `group_vars`:

| Variable | Default |
| --- | --- |
| `base_dir` | `~/tools/observability` |
| `bin_dir`, `etc_dir`, `var_dir`, `log_dir` | `bin/`, `etc/`, `var/`, `var/log/` under `base_dir` |
| `download_dir` | `var/downloads/` under `base_dir` (archives are deleted after extraction) |
| `launch_agents_dir` | `~/Library/LaunchAgents` |
| `launchd_domain_type` | `user` |
| `launchd_domain` | `<launchd_domain_type>/<uid>` |

Keep `base_dir` out of `~/Desktop`, `~/Documents` and `~/Downloads`. macOS
privacy controls (TCC) block background agents from reading those folders.

File modes are unchanged from the system variant: credential-bearing files
(`grafana.ini`, `otel-config.yaml`, the provisioned datasource, the
VictoriaMetrics password file) are `0600`. Directories are `0755`. Extracted
releases have group/other write stripped (`go-w`). `~/Library/LaunchAgents`
is created `0700` only if it does not exist; an existing one is left alone.

## LaunchAgent lifecycle

This is the one place the no-sudo variant is **not** equivalent to the
system deployment. Read it before relying on the stack.

### How the agents are loaded

`launchd_ensure_started.yml` runs `launchctl print <domain>/<label>`, and if
the service is not loaded, `launchctl bootstrap <domain> <plist>`. The
restart handler (`launchd_restart.yml`) runs `bootout` then `bootstrap`
(retrying the bootstrap briefly, since `bootout` returns before teardown
finishes). Every call targets the deployment user's own domain. Nothing
touches `system/`.

The shared plist (`launchd_agent.plist.j2`) sets `RunAtLoad`, `KeepAlive` and
**`LimitLoadToSessionType`**. That last key is load-bearing, as measured on
macOS 27 as an unprivileged user:

| Plist session type | `bootstrap user/<uid>` | `bootstrap gui/<uid>` |
| --- | --- | --- |
| unset (defaults to `Aqua`) | **fails**: `Bootstrap failed: 5: Input/output error` | works |
| `Background` | works | fails (so no second copy is ever loaded there) |

### `launchd_domain_type: user` (default)

The agents live in the `user/<uid>` domain, which a normal user can bootstrap
into over SSH with nobody logged in at the console.

- **Deployment over SSH:** works without a console login.
- **Console login and logout:** do not affect the agents. They are not part
  of the GUI (`Aqua`) session, and the `Background` session type stops launchd
  loading a duplicate into `gui/<uid>` at login.
- **After the Ansible SSH session ends:** the agents are expected to keep
  running, because the user domain is separate from any one login session.
  **Not yet verified on a Mac Mini.** Deploy, disconnect, wait a few minutes,
  and confirm the host is still reporting.
- **After a reboot:** the agents are **not** guaranteed to start by
  themselves. There is no root process to start per-user jobs at boot, and a
  LaunchAgent cannot declare `UserName` the way a LaunchDaemon can. Whether
  launchd re-loads `Background` agents from `~/Library/LaunchAgents` when the
  user's domain is next created (at the user's next SSH or console login) is
  **unverified**. Until it is, treat a reboot as requiring a re-run of the
  playbook:

  ```bash
  ansible-playbook -i inventories/production/hosts.yml site.yml --limit <rebooted-mac>
  ```

  The run is idempotent. It re-bootstraps whatever is not loaded, changes
  nothing else, and restarts nothing that is already running.

### `launchd_domain_type: gui`

Set this in `group_vars` for Macs where the deployment user is **always logged
in at the console**, for example with automatic login configured (enabling
automatic login needs an administrator, once). The plists then carry `Aqua`.

- launchd loads `~/Library/LaunchAgents` into `gui/<uid>` automatically at
  every console login, so with automatic login the agents come back after a
  reboot on their own.
- The agents **stop** when that user logs out of the console.
- Deployment needs the console session to exist, otherwise there is no
  `gui/<uid>` domain to bootstrap into. Whether macOS allows an SSH session to
  bootstrap into the console session's `gui/<uid>` domain without root is
  **unverified**.

Switching between `user` and `gui` on a Mac that is already deployed is
handled: `launchd_ensure_started.yml` boots the label out of the other domain
before bootstrapping, so two copies never run together.

### Background Items

On macOS 13 and later, macOS shows the user a "Background Items Added"
notification the first time these plists appear. They are listed under
*System Settings → General → Login Items & Extensions*, where the user can
switch them off. A switched-off agent will not load. Leave them enabled.

## Other limitations compared to the system deployment

- **Firewall.** If the macOS Application Firewall is on, allowing incoming
  connections to VictoriaMetrics (8428) and Grafana (3000) on the monitoring
  Mac needs either an administrator to add an exception or someone at the
  console to accept the prompt. Outgoing connections from the agents are not
  affected.
- **Only one user per Mac.** The labels (`com.observability.*`) and ports are
  fixed, so deploy the stack as one user per Mac.
- **Cannot clean up a system install.** A no-sudo user cannot remove
  `/opt/observability` or `/Library/LaunchDaemons` from an earlier sudo
  deployment. The preflight check stops rather than running alongside it.
- **Host metrics.** No loss of coverage was found: all the `hostmetrics`
  scrapers used here (cpu, memory, disk, filesystem, network, load) and
  `tcp_check` produced data when run unprivileged. The local test was one
  Apple Silicon Mac on macOS 27, not a Mac Mini fleet.
- **SSH observability is gone.** Its script needed root to see other users'
  sshd process titles and sockets, and it is out of scope for this variant.
  Ansible's own SSH connection is unaffected.

## What was removed (SSH observability)

- `roles/observability_agent/templates/ssh_state.sh.j2` (the snapshot script)
- `roles/observability_agent/tasks/ssh_state.yml` (script, state directory,
  `com.observability.sshstate` daemon)
- The `Restart SSH state collector` handler, the SSH snapshot verify step, and
  every `otel_ssh_*` default
- The `otlp_json_file/ssh_state` receiver and its pipeline entry in
  `otel-config.yaml.j2`
- Fleet dashboard: the **SSH Access** and **Current SSH Sessions** tables and
  their `live_sessions` / `host_links` helpers
- Detail dashboard: the **SSH / Access** row and its six panels
- `StartInterval` support in the shared plist template, which only the SSH
  job used

Metrics named `ssh_*` are no longer produced. `tcp_check` latency is not
part of the SSH feature and is kept.

## Validation status

**Run on one Apple Silicon Mac (macOS 27.0.1), as a normal user with no sudo**,
using `ansible_connection: local` for both groups, spare ports (18428/13000),
and `base_dir`/`launch_agents_dir` redirected to a scratch directory:

- `ansible-playbook site.yml` completed both plays with `failed=0`, and every
  verify task passed.
- All three services ran as the user in `user/501`, from user-owned plists
  with `LimitLoadToSessionType=Background`.
- An immediate second run reported `changed=0` on both hosts, and no process
  restarted (same PIDs).
- Changing `otel_collection_interval` fired the restart handler (`bootout`
  and `bootstrap` in `user/501`), and the collector came back under a new PID.
- Every panel query on both dashboards, run through Grafana's datasource
  proxy, returned data. That includes the `host_name` selector's values and
  `tcpcheck_duration_milliseconds`. No `ssh_*` metrics exist.
- All files were owned by the user, the secret files were `0600`, nothing was
  world-writable, and the download staging directory was empty afterwards.

**Static checks:** `ansible-playbook --syntax-check`, `ansible-inventory
--list`, `ansible-lint` (production profile), the rendered dashboards parsed
as JSON, and `plutil -lint` passed on the rendered plists.

**Not yet verified on physical Mac Minis:**

- Deployment over a real SSH connection (the local run used
  `ansible_connection: local`).
- Agents surviving the end of the Ansible SSH session with nobody logged in
  at the console.
- Behaviour after a reboot (see above), and the `gui` domain option over SSH.
- Firewall behaviour on the monitoring Mac.
- Operation across 20+ Macs.
