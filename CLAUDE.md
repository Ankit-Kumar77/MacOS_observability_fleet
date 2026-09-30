# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An Ansible project (no application code, no unit tests) that deploys host-metric observability to a fleet of Apple Silicon Mac Minis. One Mac runs VictoriaMetrics + Grafana; every other Mac runs an OpenTelemetry Collector.

**This branch is the no-sudo variant** (`feat/no-sudo-deployment`). It needs no sudo/admin rights: everything installs under the connecting user's home (`~/tools/observability`) and runs as per-user **LaunchAgents** in `~/Library/LaunchAgents`, loaded into the `user/<uid>` launchd domain. It has **no SSH observability**. The system-level variant (root LaunchDaemons, `/opt/observability`, SSH state) is the main line. `docs/NO_SUDO_VARIANT.md` is the authoritative comparison and lists the LaunchAgent limitations.

**Status:** the full playbook has been run unprivileged on one Apple Silicon Mac (macOS 27) over `ansible_connection: local`. Both plays and all verify tasks passed, a second run was `changed=0` with no restarts, the restart handler reloaded the collector in `user/<uid>`, and every panel on both dashboards returned data. Not yet verified on Mac Minis: deployment over real SSH, agents surviving the end of the SSH session, and behaviour after a reboot (LaunchAgents are not started at boot). Do not describe those as working until they have been tested.

## Commands

Ansible is **not installed** on this machine. Install the full `ansible` package, not bare `ansible-core`: `ansible.cfg` sets `stdout_callback = yaml`, which ships in `community.general`, and without that collection every `ansible-playbook` run aborts with `Could not load 'yaml' callback plugin`. `ansible.cfg` also sets the inventory and `roles_path`, and sets `become = False` (every play in `site.yml` also sets `become: false`), so `-i` is optional but used explicitly throughout the docs.

```bash
ansible all -m ping                      # connectivity

ansible-playbook site.yml --syntax-check # static validation, no hosts touched
ansible-inventory --list                 # confirms group membership + var resolution
ansible-lint                             # must pass; config in .ansible-lint

# Deploy as the regular SSH user - no sudo, no --ask-become-pass
# (no vault secrets required by default - see "Secrets" below)
ansible-playbook -i inventories/production/hosts.yml site.yml --tags server
ansible-playbook -i inventories/production/hosts.yml site.yml --tags agent

# One host, for staged rollout, troubleshooting, or after that Mac rebooted
ansible-playbook -i inventories/production/hosts.yml site.yml --limit mac-mini-02 --tags agent

# Health/verification tasks only
ansible-playbook -i inventories/production/hosts.yml site.yml --tags verify
```

Add `--ask-vault-pass` only if you have re-enabled auth with vaulted credentials (see "Secrets" below).

If a target Mac can only reach GitHub through a proxy, the release-archive downloads (`observability_common`'s `install_versioned_archive.yml`, used for VictoriaMetrics, Grafana and otelcol-contrib) read `proxy_env` (empty by default in `inventories/production/group_vars/all.yml`) and pass it as the module's environment, e.g.:

```bash
ansible-playbook -i inventories/production/hosts.yml site.yml --tags server \
  -e '{"proxy_env":{"http_proxy":"http://127.0.0.1:9000","https_proxy":"http://127.0.0.1:9000"}}'
```

### Testing changes without Mac Minis

Most of this project can be verified locally on any Apple Silicon Mac, and it is worth doing — every dashboard defect found so far was invisible to static checks. Download the pinned binaries, run VictoriaMetrics and the collector against each other on spare ports, then query VictoriaMetrics for the metric names and run the dashboard's PromQL directly. Grafana can be started against the rendered `grafana.ini` and provisioning directory to confirm the datasource and dashboard load, and its `/api/datasources/proxy/uid/victoriametrics/api/v1/query` endpoint runs panel queries through the real datasource.

Because this variant needs no sudo, the whole playbook can also be run against the local Mac: an inventory with one host in each group using `ansible_connection: local`, plus `-e` overrides for `base_dir`, `launch_agents_dir`, `victoriametrics_port` and `grafana_port` (spare ports, scratch directories). Boot the three `com.observability.*` labels out of `user/$(id -u)` afterwards.

## Architecture

`site.yml` is two plays, one per inventory group, each applying one role:

| Group | Role | Installs |
| --- | --- | --- |
| `monitoring_server` (exactly one host) | `observability_server` | VictoriaMetrics, Grafana, datasource + dashboard provisioning, 2 LaunchAgents |
| `monitored_nodes` (N hosts) | `observability_agent` | `otelcol-contrib`, host-metric + latency pipeline, 1 LaunchAgent |

Metric path — deliberately **no Prometheus server and no node_exporter**:

```
hostmetrics + tcp_check -> resourcedetection -> batch -> otlphttp (basic auth)
  -> http://<monitoring_server_address>:8428/opentelemetry/v1/metrics  (VictoriaMetrics)
  -> Grafana (datasource type `prometheus`, uid `victoriametrics`, localhost:8428)
```

Both roles follow the same order: `prerequisites` → install/configure → **`flush_handlers`** → `verify`. The explicit flush matters: handlers otherwise run at the end of the play, so verification would check services still running their previous configuration.

A third role, `observability_common`, holds what both roles share. It has no `tasks/main.yml` and is never applied directly — it is pulled in with `include_role` + `tasks_from`:

| File | Purpose |
| --- | --- |
| `tasks/preflight.yml` | Asserts the play is not running as root and that the sudo variant's `/Library/LaunchDaemons/com.observability.*` plists for this role are absent. Runs first in each role's `prerequisites.yml`. |
| `tasks/base_directories.yml` | Creates the user-owned `~/tools/observability` layout (including `download_dir`), plus `~/Library/LaunchAgents` only if it is missing. Callers add role-specific paths via `observability_extra_directories`. |
| `tasks/install_versioned_archive.yml` | Download (into `download_dir`, not `/tmp`) → checksum-verify → versioned extract → strip group/other write → stat → assert → clean up the archive. Used once per component. |
| `tasks/launchd_ensure_started.yml` | `launchctl print <launchd_domain>/<label>` → if not loaded, boot it out of the *other* per-user domain, then `bootstrap`. Included from each role's install task. |
| `tasks/launchd_restart.yml` | `launchctl bootout` → `bootstrap` (retried: bootout returns before teardown finishes) in `launchd_domain`. Included from each role's restart handler. |
| `templates/launchd_agent.plist.j2` | One LaunchAgent plist for all three services, parameterised by `launchd_label`, `launchd_program_arguments`, the log paths and an optional `launchd_working_directory`. Sets `LimitLoadToSessionType` from `launchd_domain_type` (`Background` for `user`, `Aqua` for `gui`). |

Three rules keep this safe, and all three are load-bearing:

- **`install_versioned_archive.yml` deliberately does not create the symlink or notify a handler.** The symlink flip is the moment the running version changes, so it lives in the calling role next to the handler it notifies. Notifying a caller's handler from inside an included role is the fragile part; this split avoids needing it at all.
- **The shared plist is referenced as `{{ role_path }}/../observability_common/templates/launchd_agent.plist.j2`.** Ansible resolves a bare `src:` against the *calling* role's `templates/`, which would not find it.
- **`launchd_restart.yml` is pulled into handlers with `ansible.builtin.include_tasks: file: "{{ role_path }}/../observability_common/tasks/launchd_restart.yml"`, not `include_role`.** Ansible rejects `include_role`/`import_role` as a handler action outright ("Using 'ansible.builtin.include_role' as a handler is not supported"); only task-level includes work there. `launchd_ensure_started.yml` has no such restriction and is included the normal `include_role` + `tasks_from` way from each role's install task, since those are ordinary tasks, not handlers.

`ProgramArguments` for each daemon are lists in that role's `defaults/main.yml`, so they are documented and overridable rather than buried in XML. VictoriaMetrics builds its list as `victoriametrics_base_arguments + (victoriametrics_auth_arguments if victoriametrics_auth_enabled else [])`.

Everything lands under `base_dir` (default `{{ ansible_facts['user_dir'] }}/tools/observability`), owned by the connecting user; no task sets `owner`/`group`. Plists go to `launch_agents_dir` (`~/Library/LaunchAgents`). LaunchAgent labels: `com.observability.victoriametrics`, `com.observability.grafana`, `com.observability.otelcol`. The path and launchd variables (`base_dir`, `download_dir`, `launch_agents_dir`, `launchd_domain_type`, `launchd_domain`) are duplicated in both roles' `defaults/main.yml`, like `base_dir` always was.

### Variable layout

- `roles/<role>/defaults/main.yml` — versions, checksums, paths, ports, tuning. Each role is self-contained.
- `inventories/production/group_vars/all.yml` — the **cross-role contract** only: `victoriametrics_port`, `monitoring_server_address`, and the VictoriaMetrics auth settings. These must agree between the monitoring Mac and every agent, so they are defined once.
- `group_vars/<group>.yml` — vault secret references and per-environment overrides.

`monitoring_server_address` is derived, not hard-coded:

```yaml
monitoring_server_address: "{{ hostvars[groups['monitoring_server'][0]]['ansible_host'] }}"
```

The agent role asserts this group holds exactly one host with `ansible_host` set. Reassigning the monitoring Mac requires re-running the **agent** play so every collector config is re-rendered.

## macOS-specific constraints

These are the things that make this project different from the same stack on Linux. Each was found by running the real binaries; none is visible to a syntax check.

- **Collector 0.159.0 is the floor.** In `0.98.0` the `hostmetrics` `cpu` and `disk` scrapers return `not implemented yet` on darwin and emit nothing at all. Never downgrade below `0.159.0` without re-testing on a Mac.
- **macOS reports no per-core `cpu` label.** CPU busy% must normalise against the sum of all states — `100 * (1 - sum(rate(idle)) / sum(rate(all)))`. An `avg by (host_name)` of the idle rate, which is the idiomatic Linux form, returns roughly `-466%` here.
- **macOS memory states are `free` / `inactive` / `used`** — no `wired`. `inactive` is reclaimable and is not counted as used.
- **APFS volumes in one container all report the same container-wide capacity.** `/` and `/System/Volumes/Data` are identical; summing across mountpoints multiplies the total. The collector config excludes the synthetic volumes, leaving those two, and the dashboard charts `/` only.
- **Grafana 11+ removed `grafana-server`.** The plist runs `grafana server --config=... --homepath=...`.
- **`ansible.builtin.unarchive` refuses macOS's built-in `tar`.** `/usr/bin/tar` is BSD tar (libarchive); the module hard-requires GNU tar for its idempotency bookkeeping, detects the mismatch, falls through to `unzip`, and fails on every `.tar.gz` release. `install_versioned_archive.yml` shells out to `tar -xzf` directly instead (`# noqa: command-instead-of-module`), keeping the same `creates:` idempotency guard, followed by a `mode: go-w` normalization (run unprivileged, `tar` cannot restore foreign owners, but it can carry over permission bits).

## Conventions and gotchas

- **VictoriaMetrics needs `-opentelemetry.usePrometheusNaming`.** Without it, OTLP names are stored verbatim including dots (`system.memory.usage`, label `host.name`) and every dashboard query silently returns zero series. This flag is load-bearing, not cosmetic.
- **PromQL: never divide a full-label selector by a `sum by (...)`.** The label sets do not match and the result is empty even though both operands have data. Aggregate both sides: `sum by (host_name)(x{state="used"}) / sum by (host_name)(x)`. Two panels shipped broken this way.
- **`dashboard.json.j2` (fleet) and `dashboard_detail.json.j2` (per-host) are Jinja-rendered**, so Grafana's `{{host_name}}` legend syntax must be escaped as `{{ '{{' }}host_name{{ '}}' }}`. Grafana `${...}` variables do not collide and are written as-is. Panels reference the datasource by uid `victoriametrics` and each target needs a `refId`. Both dashboards define a `host_name` variable from `label_values(system_memory_usage_bytes, host_name)`: multi-value with All = `.*` on the fleet dashboard (every query filters `host_name=~"$host_name"`), single-value on the detail dashboard (`host_name="$host_name"`). They link to each other by `grafana_fleet_dashboard_uid` / `grafana_detail_dashboard_uid`; fleet series and Hosts-table links pass `?var-host_name=<host>`.
- **Latency is `tcp_check`, not `hostmetrics`.** `tcp_check/monitoring_server` (Alpha in 0.159.0; component types there are underscored, `tcp_check` not `tcpcheck`) times a TCP connect to `monitoring_server_address:victoriametrics_port` and yields `tcpcheck_duration_milliseconds` (whole ms, one sample per interval, so no percentiles) and `tcpcheck_status_ratio`. Gated by `otel_latency_check_enabled`.
- **Releases install into versioned directories behind stable symlinks** (`bin/victoria-metrics-<ver>/`, `grafana-<ver>/`, `bin/otelcol-contrib-<ver>/`), with the stable path repointed by the role. The symlink flip — not the extraction — notifies the restart handler. Archives are version-stamped in `/tmp` and removed after extraction. A `creates:` guard on an unversioned path silently turns a version bump into a no-op. Adding a component means calling `install_versioned_archive` and then writing its symlink + plist + service tasks — do not re-implement the install flow.
- **`install_versioned_archive.yml` checks the versioned binary path before doing anything else, and skips download/extract/cleanup entirely when it already exists.** Without this, a re-run after any later task in the same play fails (a common case while iterating on a real Mac) always re-downloads — the temp archive is deleted on success, so `get_url`'s own checksum-match skip has nothing to compare against even though the binary is already installed. This has no version-bump footgun since the checked path is the versioned one, not the stable symlink.
- **`{{ grafana_home }}` must not be created as a directory.** It is a symlink, and `ansible.builtin.file` refuses to replace a real directory with one. It is deliberately excluded from the `prerequisites.yml` directory loop; `{{ var_dir }}/grafana` (Grafana's data dir) is a real directory and stays.
- **Old versions are left in place** after an upgrade, enabling rollback by reverting the version variable. Nothing prunes them, and these binaries are large (collector ~334 MB, Grafana ~1.3 GB).
- **Every download is checksum-pinned** against the upstream-published SHA256. A version bump must update the matching `*_checksum`, or the download fails closed.
- **Secrets are `0600` and `no_log`.** `grafana.ini`, `otel-config.yaml`, the provisioned datasource, and (when `victoriametrics_auth_enabled: true`) the VictoriaMetrics password file all carry credentials. The VictoriaMetrics password is passed to launchd as `file://...` rather than an argument so it stays out of the `0644` plist and out of `ps`.
- **Service lifecycle uses `launchctl` directly, not `ansible.builtin.service`.** The module has no macOS implementation at all. `observability_common`'s `launchd_ensure_started.yml` (bootstrap-if-not-loaded, included from each role's install task) and `launchd_restart.yml` (bootout + bootstrap, included from each restart handler) replace it. Every call targets `launchd_domain` (`user/<uid>` by default); nothing may target `system/` in this variant.
- **`LimitLoadToSessionType` in the plist is load-bearing.** Measured on macOS 27 as an unprivileged user: a plist without it (defaulting to `Aqua`) fails `launchctl bootstrap user/<uid>` with `5: Input/output error`. With `Background`, `user/<uid>` works and `gui/<uid>` refuses it, so no duplicate is loaded at console login. Do not remove it.
- **LaunchAgents are not LaunchDaemons.** `RunAtLoad`/`KeepAlive` start the agent immediately and keep it alive, but nothing starts per-user jobs at boot. After a reboot, re-run the play for that Mac. With `launchd_domain_type: gui`, agents live in the console session: they load at console login and stop at logout. See `docs/NO_SUDO_VARIANT.md`.
- **No `become` anywhere.** `ansible.cfg` sets `become = False`, each play sets `become: false`, and `preflight.yml` fails on uid 0. Do not add tasks that need root; there is no way to satisfy them in this environment.
- **Inventory holds `REPLACE_WITH_*` placeholders by design.** Never commit real addresses, SSH users, or credentials.
- `.ansible-lint` runs the `production` profile. `var-naming[no-role-prefix]` is deliberately skipped — see the comment there.

## Secrets

**No vault is required by default.** `victoriametrics_auth_enabled` is `false` in `inventories/production/group_vars/all.yml`, and `grafana_admin_password` in `inventories/production/group_vars/monitoring_server.yml` is a plain, checked-in default (`admin`) — this is intentional for local/testing use, and it means port 8428 on the monitoring Mac is reachable by anyone on the network, unauthenticated, with read/write/delete on fleet metrics.

To re-enable auth for a real deployment:

| Variable | Purpose |
| --- | --- |
| `grafana_admin_password` | Grafana admin login. Set directly, or point at a vaulted var (`ansible-vault encrypt_string 'a-strong-password' --name 'vault_grafana_admin_password'`, then `grafana_admin_password: "{{ vault_grafana_admin_password }}"`). |
| `victoriametrics_auth_enabled` / `victoriametrics_auth_username` / `victoriametrics_auth_password` | Set `victoriametrics_auth_enabled: true` and provide the username/password (plain or vaulted, same pattern as above) to require basic auth on VictoriaMetrics. |

When VictoriaMetrics auth is enabled, the collector, the Grafana datasource and VictoriaMetrics itself must all be rendered from the same credentials, so deploy all three from the same source. `/health` is deliberately exempt from auth so readiness probes keep working; query and OTLP-write endpoints return `401` unauthenticated when auth is on.

## Reference docs

`docs/NO_SUDO_VARIANT.md` (this variant vs the sudo deployment, LaunchAgent lifecycle, limitations, validation status), `docs/PROJECT_FLOW.md` (internal flow + mermaid diagrams), `docs/KT_GUIDE.md` (onboarding walkthrough and FAQ), `docs/RESOURCE_FOOTPRINT.md` (measured disk figures). `LAUNCHD_TROUBLESHOOTING.md` is referenced historically but does not exist in the repository. These describe implementation specifics — keep them in sync when changing role behaviour.
