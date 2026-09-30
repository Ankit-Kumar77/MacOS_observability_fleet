# Mac Mini Observability — Setup Guide

> **Page owner:** Platform / Infrastructure
> **Applies to:** Apple Silicon (arm64) Mac Minis running macOS
> **Repository:** `MacOS_observability_fleet`
> **Variant:** **no-sudo** (branch `feat/no-sudo-deployment`). No sudo/admin rights are needed on any Mac. Everything installs under the deployment user's home and runs as per-user LaunchAgents. There is no SSH observability. See `docs/NO_SUDO_VARIANT.md` for how this differs from the system-level (sudo) deployment.
> **Status:** Full playbook run end to end, unprivileged, on one Apple Silicon Mac. Not yet run over SSH against physical Mac Minis. See *Validation status* at the bottom before treating this as production-ready.

---

## 1. What you are deploying

Ansible installs host-metric observability across a fleet of Mac Minis. One Mac is the **monitoring server**; every other Mac is a **monitored node**.

| Machine | Role applied | Software installed |
| --- | --- | --- |
| Monitoring Mac (exactly one) | `observability_server` | VictoriaMetrics, Grafana, provisioned datasource and dashboards, 2 LaunchAgents |
| Monitored Mac (N of them) | `observability_agent` | OpenTelemetry Collector Contrib, host-metric and latency pipeline, 1 LaunchAgent |

There is deliberately **no Prometheus server and no node_exporter**. Metrics travel over OTLP/HTTP straight into VictoriaMetrics.

### Metric path

```
hostmetrics + tcp_check -> resourcedetection -> batch -> otlphttp
   -> http://<monitoring-mac>:8428/opentelemetry/v1/metrics   (VictoriaMetrics)
   -> Grafana (datasource type "prometheus", uid "victoriametrics", localhost:8428)
```

### Ports

| Port | Host | Purpose |
| --- | --- | --- |
| `8428` | Monitoring Mac | VictoriaMetrics ingest and query. Every monitored Mac must reach this. |
| `3000` | Monitoring Mac | Grafana UI. Operators reach this from a browser. |
| `22` | All Macs | SSH from the Ansible control machine. |

### Pinned versions

| Component | Version | Why it is pinned |
| --- | --- | --- |
| OpenTelemetry Collector Contrib | `0.159.0` | **Floor version on macOS.** At `0.98.0` the `cpu` and `disk` scrapers return `not implemented yet` on darwin and emit nothing. Do not downgrade without re-testing on a Mac. |
| VictoriaMetrics | `1.101.0` | Must run with `-opentelemetry.usePrometheusNaming`, otherwise OTLP names are stored with dots intact and every dashboard query returns zero series. |
| Grafana | `13.2.0` | Grafana 11+ removed `grafana-server`; the LaunchAgent runs `grafana server` instead. |

Every download is SHA256-pinned against the upstream-published checksum. A version bump must also update the matching `*_checksum` variable or the download fails closed.

---

## 2. Before you start

Collect and confirm all of the following. Missing any one of these is the most common cause of a failed first run.

- **A control machine** with Ansible installed and network reach to every Mac. This is usually a laptop or a jump host, not one of the Mac Minis.
- **Apple Silicon Macs** with macOS and Remote Login (SSH) enabled: *System Settings → General → Sharing → Remote Login*.
- **An SSH user on every Mac.** No sudo or admin rights are needed; this user owns everything that is installed and runs every service. SSH keys are strongly preferred over passwords.
- **One Mac chosen as the monitoring server** and at least one other Mac chosen as the first monitored node. Do not use the same Mac for both during first-time setup.
- **Reachable address and SSH username for each Mac.**
- **Network policy** allowing monitored Macs to reach the monitoring Mac on `8428`, and allowing operators to reach Grafana on `3000`.
- **Disk headroom in that user's home directory.** Roughly 1.4 GB on the monitoring Mac and 340 MB per monitored Mac for the initial install, before metric data and logs. Upgrades leave the previous version in place, so budget for two concurrent versions on any Mac upgraded once.

### Install Ansible on the control machine

Install the full `ansible` package rather than bare `ansible-core`; the project standardizes on it and CI does the same.

```bash
pip install ansible ansible-lint
```

`ansible.cfg` in the repository root already sets the inventory path and `roles_path`, and disables privilege escalation (`become = False`), so `-i` is technically optional. Every command on this page passes it explicitly anyway, because being unambiguous about which inventory you are deploying is worth the extra characters.

---

## 3. Setup flow at a glance

```
Step 1  Prepare control machine and clone repo
Step 2  Fill in the inventory
Step 3  Decide on credentials (default is unauthenticated)
Step 4  Static validation  (syntax-check, inventory, lint, ping)
Step 5  Deploy the monitoring Mac        --tags server
Step 6  Verify VictoriaMetrics + Grafana
Step 7  Deploy ONE monitored Mac         --tags agent --limit <host>
Step 8  Verify metrics end to end in Grafana
Step 9  Roll out to the rest of the fleet --tags agent
```

Steps 5 through 8 are the gate. Do not scale out until one monitoring Mac plus one monitored Mac works on physical hardware.

---

## 4. Step-by-step

### Step 1 — Prepare the control machine

```bash
git clone <repository-url>
cd MacOS_observability_fleet
ansible --version
```

All commands below are run from the repository root.

### Step 2 — Fill in the inventory

Edit `inventories/production/hosts.yml`. It ships with `REPLACE_WITH_*` placeholders by design; real addresses and usernames must never be committed.

```yaml
all:
  children:
    monitoring_server:
      hosts:
        monitoring-mac:
          ansible_host: 10.0.0.10
          ansible_user: ops
    monitored_nodes:
      hosts:
        mac-mini-02:
          ansible_host: 10.0.0.11
          ansible_user: ops
        mac-mini-03:
          ansible_host: 10.0.0.12
          ansible_user: ops
```

Two rules the playbook enforces:

1. **`monitoring_server` must contain exactly one host, and that host must have `ansible_host` set.** The agent role asserts this and fails with a clear message otherwise.
2. **Agents derive the export endpoint from that entry.** `monitoring_server_address` is computed as `hostvars[groups['monitoring_server'][0]]['ansible_host']`, so no server address is ever hard-coded in a role or template.

Consequence worth knowing in advance: **moving the monitoring role to a different Mac requires re-running the agent play** on every node, because each collector config has to be re-rendered with the new address.

### Step 3 — Decide on credentials

**No Ansible Vault is required by default.** Out of the box:

- `victoriametrics_auth_enabled: false` in `inventories/production/group_vars/all.yml`
- `grafana_admin_password: admin` in `inventories/production/group_vars/monitoring_server.yml`

That default is intended for local and testing use. It means **anyone who can reach port 8428 on the monitoring Mac can read, write, or delete fleet metrics with no credentials**, and Grafana is reachable with a well-known password. Acceptable on an isolated lab network; not acceptable on a shared one.

To turn on authentication for a real deployment, set all three variables and deploy the server and every agent from the same source, because the collector config, the Grafana datasource, and VictoriaMetrics itself are all rendered from the same credentials.

| Variable | Where to set it | Purpose |
| --- | --- | --- |
| `grafana_admin_password` | `group_vars/monitoring_server.yml` | Grafana admin login. |
| `victoriametrics_auth_enabled` | `group_vars/all.yml` | Set to `true` to require basic auth on VictoriaMetrics. |
| `victoriametrics_auth_username` | `group_vars/all.yml` | Basic-auth username. **Has no default anywhere in the repo** — enabling auth without defining this fails on an undefined variable. |
| `victoriametrics_auth_password` | `group_vars/all.yml` | Basic-auth password. |

To vault a secret:

```bash
ansible-vault encrypt_string 'a-strong-password' --name 'vault_grafana_admin_password'
```

Paste the output into the group_vars file, reference it as `grafana_admin_password: "{{ vault_grafana_admin_password }}"`, and add `--ask-vault-pass` to every playbook command below.

`/health` on VictoriaMetrics stays exempt from authentication on purpose, so readiness and uptime probes keep working. Query and OTLP-write endpoints return `401` when auth is on.

### Step 4 — Static validation and connectivity

Run these before touching a Mac. They are fast and catch most inventory mistakes.

```bash
ansible-playbook -i inventories/production/hosts.yml site.yml --syntax-check
ansible-inventory -i inventories/production/hosts.yml --list
ansible-lint
ansible all -i inventories/production/hosts.yml -m ping
```

`ansible-inventory --list` is the one that confirms group membership and that `monitoring_server_address` resolves to the address you expect.

### Step 5 — Deploy the monitoring Mac

```bash
ansible-playbook -i inventories/production/hosts.yml site.yml \
  --tags server
```

No become password is asked for; nothing in this variant uses sudo.

What this run does, in order:

1. Runs the preflight checks: not running as root, and no sudo-variant LaunchDaemons for VictoriaMetrics or Grafana on this Mac.
2. Creates the `~/tools/observability` layout, owned by the deployment user.
3. Writes the `0600` VictoriaMetrics password file, if auth is enabled.
4. Downloads VictoriaMetrics, verifies its checksum, extracts it into a versioned directory, then repoints the stable `bin/victoria-metrics-prod` symlink.
5. Renders the VictoriaMetrics LaunchAgent into `~/Library/LaunchAgents` and bootstraps it into `user/<uid>`.
6. Does the same for Grafana, then renders `grafana.ini`, the datasource, the dashboard provider, and the fleet and detail dashboards.
7. Flushes handlers, so any pending restart happens **before** verification rather than at the end of the play.
8. Runs the verification tasks in Step 6 automatically.

#### If the Macs need a proxy to reach GitHub

Release archives are downloaded from GitHub and `dl.grafana.com`. On a proxied network, pass `proxy_env`:

```bash
ansible-playbook -i inventories/production/hosts.yml site.yml \
  --tags server \
  -e '{"proxy_env":{"http_proxy":"http://127.0.0.1:9000","https_proxy":"http://127.0.0.1:9000"}}'
```

### Step 6 — Verify the monitoring Mac

The playbook already asserts all of this, but these are the manual equivalents when something fails.

```bash
# On the monitoring Mac
curl -s http://localhost:8428/health

# With auth enabled, this must succeed...
curl -s -u <user>:<password> http://localhost:8428/api/v1/labels
# ...and this must return 401
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8428/api/v1/labels

curl -s http://localhost:3000/api/health
```

Then open `http://<monitoring-mac>:3000` in a browser, sign in as `admin`, and confirm:

- A datasource named **VictoriaMetrics** is present.
- A dashboard with uid `mac-mini-fleet`, titled **Mac Mini Fleet Overview**, is present.
- A dashboard with uid `mac-mini-detail`, titled **Mac Mini Detail**, is present.

The fleet dashboard's panels are Active Hosts, Monitoring Server Unreachable, CPU Usage, Memory Usage, Disk Usage, Network Traffic, Load Average, Latency to Monitoring Server, and a Hosts table. The **Mac Mini** selector at the top filters them; clicking a host's series or its name in the Hosts table opens Mac Mini Detail for that host. Everything will be empty until at least one agent is deployed.

Logs live in `~/tools/observability/var/log/victoriametrics.err.log` and `grafana.err.log`.

### Step 7 — Deploy the first monitored Mac

Deploy exactly one node first. Replace the alias with your own inventory name.

```bash
ansible-playbook -i inventories/production/hosts.yml site.yml \
  --tags agent --limit mac-mini-02
```

What this run does:

1. Asserts the monitoring-server group is usable, the run is not root, and no sudo-variant collector LaunchDaemon exists.
2. Creates the `~/tools/observability` layout.
3. Downloads and checksum-verifies `otelcol-contrib`, extracts it into a versioned directory, then repoints the `bin/otelcol-contrib` symlink.
4. Renders the `0600` collector config with the monitoring Mac's address baked in, renders the LaunchAgent, and bootstraps it into `user/<uid>`.
5. Flushes handlers, then verifies.

### Step 8 — Verify end to end

The agent play checks all three of these itself. Manually:

```bash
# On the monitored Mac, as the deployment user — config is valid and the agent is loaded
~/tools/observability/bin/otelcol-contrib validate \
  --config=$HOME/tools/observability/etc/otel-config.yaml
launchctl print user/$(id -u)/com.observability.otelcol

# From anywhere that can reach the monitoring Mac — which hosts are reporting
curl -s 'http://<monitoring-mac>:8428/api/v1/label/host_name/values'
```

The collector reports the **OS hostname**, which may carry a `.local` suffix and need not match the inventory alias. Confirm the Mac appears in that list rather than expecting an exact string match.

Finally, open the Grafana fleet dashboard and confirm every panel returns data for the new host, and that clicking its name in the Hosts table opens Mac Mini Detail for it. Allow one or two 15-second collection intervals after the collector restarts.

### Step 9 — Roll out to the fleet

Once one monitoring Mac plus one monitored Mac is confirmed working:

1. Add every remaining Mac to `monitored_nodes` in the inventory.
2. Deploy them all:

   ```bash
   ansible-playbook -i inventories/production/hosts.yml site.yml \
     --tags agent
   ```

3. Or roll out in batches during a staged rollout:

   ```bash
   ansible-playbook -i inventories/production/hosts.yml site.yml \
     --tags agent --limit 'mac-mini-04,mac-mini-05'
   ```

4. Confirm every host appears on the fleet dashboard.

The `observability_agent` role is reused unchanged for any fleet size, including roughly 40 Macs.

---

## 5. Command reference

| Goal | Command |
| --- | --- |
| Deploy everything, server first | `ansible-playbook -i inventories/production/hosts.yml site.yml` |
| Monitoring Mac only | `... site.yml --tags server` |
| All monitored Macs | `... site.yml --tags agent` |
| One host only (also: after that Mac rebooted) | `... site.yml --limit mac-mini-02` |
| Re-run health checks only | `... site.yml --tags verify` |
| Connectivity test | `ansible all -i inventories/production/hosts.yml -m ping` |

`--check` mode is not useful here. The install flow branches on `stat` results and shells out to `tar`, so a dry run against a Mac that has nothing installed yet reports failures that are artefacts of check mode rather than real problems.

---

## 6. What lands on disk

Everything is under the deployment user's home, owned by that user. Nothing is written to `/opt`, `/Library` or any other system path.

| Path | Contents |
| --- | --- |
| `~/tools/observability/bin/` | Stable symlinks plus versioned install directories |
| `~/tools/observability/etc/` | `otel-config.yaml` (agents), `grafana/grafana.ini` and provisioning (server) |
| `~/tools/observability/var/` | VictoriaMetrics storage, Grafana data and plugins, `downloads/` (emptied after each install) |
| `~/tools/observability/var/log/` | `victoriametrics.*.log`, `grafana.*.log`, `otelcol.*.log` |
| `~/Library/LaunchAgents/` | `com.observability.victoriametrics.plist`, `com.observability.grafana.plist` (server), `com.observability.otelcol.plist` (agents) |

Files carrying credentials are mode `0600` and marked `no_log`: `grafana.ini`, `otel-config.yaml`, the provisioned datasource, and the VictoriaMetrics password file. The VictoriaMetrics password is passed to launchd as `file://...` rather than inline, so it stays out of the `0644` plist and out of `ps` output.

### Services

| Label | Runs |
| --- | --- |
| `com.observability.victoriametrics` | `bin/victoria-metrics-prod` with storage path, port, 90-day retention, and `-opentelemetry.usePrometheusNaming` |
| `com.observability.grafana` | `grafana server --config=... --homepath=...` |
| `com.observability.otelcol` | `bin/otelcol-contrib --config=~/tools/observability/etc/otel-config.yaml` |

All plists set `RunAtLoad` and `KeepAlive`, so a bootstrapped agent starts immediately and is restarted if it exits. They also set `LimitLoadToSessionType` to `Background`, which is what lets a non-admin user bootstrap them into the `user/<uid>` domain over SSH.

**LaunchAgents do not start at boot.** Unlike the root LaunchDaemons of the sudo deployment, nothing starts per-user jobs before the user logs in. After a Mac reboots, re-run the play for it (`--limit <mac>`); the run is idempotent. See `docs/NO_SUDO_VARIANT.md` for logout, reboot and the `gui` domain option.

Service lifecycle is driven by `launchctl` directly, not by `ansible.builtin.service`, because that module has **no macOS implementation at all** and fails with `get_service_tools not implemented on target platform`.

---

## 7. Day-2 operations

### Upgrading a component

1. Update the version variable and its matching `*_checksum` in the role's `defaults/main.yml`.
2. Re-run the relevant play.

Each release installs into its own versioned directory behind a stable symlink. The **symlink flip**, not the extraction, is what notifies the restart handler and changes the running version. The previous version stays on disk, so a rollback is reverting the version variable and re-running.

Nothing prunes old versions, and these binaries are large: the collector is about 334 MB and Grafana about 1.3 GB. Prune deliberately once a version is confirmed good.

### Restarting a service manually

As the deployment user, no sudo:

```bash
launchctl bootout user/$(id -u)/com.observability.otelcol
launchctl bootstrap user/$(id -u) ~/Library/LaunchAgents/com.observability.otelcol.plist
launchctl print user/$(id -u)/com.observability.otelcol
```

`bootout` followed by `bootstrap` is what the Ansible handler does. `kickstart -k` restarts the process but keeps whatever definition launchd already had loaded, so it will not pick up a changed plist.

### Changing collection frequency

`otel_collection_interval` defaults to `15s`. Override it in `group_vars/monitored_nodes.yml`, then re-run the agent play. Consider raising `grafana_time_interval` on the server to match.

### Retention

`victoriametrics_retention` defaults to `90d`. VictoriaMetrics' own default is one month, which is easy to be surprised by, so it is set explicitly.

---

## 8. Troubleshooting

| Symptom | First checks |
| --- | --- |
| Ansible cannot connect | `ansible_host` and `ansible_user` in the inventory, SSH keys, Remote Login enabled, then `ansible all -m ping`. |
| Play fails on the monitoring-server assert | `monitoring_server` must hold exactly one host with `ansible_host` set. |
| Download fails | Checksum mismatch after a version bump, or no route to GitHub. Set `proxy_env` if a proxy is required. |
| Collector will not start | Run `otelcol-contrib validate`; read `otelcol.err.log`; confirm the symlink resolves. |
| VictoriaMetrics will not start | Read `victoriametrics.err.log`; check port 8428 with `lsof -i :8428`. |
| Grafana will not start | Read `grafana.err.log`; check port 3000; confirm `grafana.ini` paths. |
| Dashboard panels are empty | Confirm `-opentelemetry.usePrometheusNaming` is in the running plist. Without it, metric names keep their dots and every query silently returns nothing. |
| Metrics missing for one host | Check that Mac's `otelcol.err.log`, then its network reach to port 8428 on the monitoring Mac. |
| Service defined but not loaded | `launchctl print user/$(id -u)/<label>`; re-bootstrap as shown above, or re-run the play for that Mac. Expected after a reboot. |
| Play stops at the preflight check | Running as root (drop `--become`), or the sudo variant's `/Library/LaunchDaemons/com.observability.*` plists exist on that Mac; an administrator must remove them. |
| Agent keeps getting switched off | Check *System Settings → General → Login Items & Extensions*; the user can disable background items there. |
| Agents cannot reach the monitoring Mac although VictoriaMetrics is up | The macOS Application Firewall may block incoming connections to the unsigned binary; allowing them needs an administrator or a click at the console. |

### macOS-specific behaviour worth knowing

These were all found by running the real binaries. None is visible to a syntax check.

- **No per-core `cpu` label on macOS.** CPU busy percent must normalise against the sum of all states. The idiomatic Linux form, `avg by (host_name)` of the idle rate, returns roughly `-466%` here.
- **Memory states are `free` / `inactive` / `used`, with no `wired`.** `inactive` is reclaimable and is not counted as used.
- **APFS volumes in one container all report the same container-wide capacity.** `/` and `/System/Volumes/Data` are identical, so summing across mountpoints multiplies the total. The collector config excludes synthetic volumes and the dashboard charts `/` only.
- **`ansible.builtin.unarchive` refuses macOS's built-in tar.** `/usr/bin/tar` is BSD tar; the module hard-requires GNU tar. The install flow shells out to `tar -xzf` directly and strips group/other write afterwards.
- **PromQL: never divide a full-label selector by a `sum by (...)`.** The label sets do not match and the result is empty even when both sides have data. Aggregate both sides.

---

## 9. Validation status

Read this before treating the stack as production-ready.

**Verified, on one Apple Silicon Mac (macOS 27), unprivileged, over `ansible_connection: local`.** Both plays passed with every verify task. A second run reported `changed=0` with no service restarts. A config change fired the restart handler in `user/<uid>`. Every panel on both dashboards returned data through Grafana's datasource proxy. Files were user-owned, and the secret files were `0600`.

**Not yet verified on physical Mac Minis.**

- Deployment over a real SSH connection.
- Agents continuing to run after the Ansible SSH session ends, with nobody logged in at the console.
- Behaviour after a reboot, and the optional `gui` launchd domain.
- Network and firewall policy between monitored Macs and the monitoring Mac.
- Operation across the full fleet.

**Known gaps in the repository itself.**

- `victoriametrics_auth_username` is referenced by five files but defined in none. Enabling authentication without adding it fails on an undefined variable.
- `LAUNCHD_TROUBLESHOOTING.md` is still linked from `README.md`, `CLAUDE.md`, and the docs, but the file was deleted. Those links are dead.

---

## 10. Related pages

| Document | Contents |
| --- | --- |
| `README.md` | Repository entry point and quick start |
| `docs/PROJECT_FLOW.md` | Internal flow with mermaid diagrams |
| `docs/KT_GUIDE.md` | Onboarding walkthrough and FAQ |
| `docs/RESOURCE_FOOTPRINT.md` | Measured disk figures and scaling formula |
| `docs/project-overview.md` | One-page project summary |
| `docs/NO_SUDO_VARIANT.md` | No-sudo vs sudo deployment, LaunchAgent lifecycle and limitations |
