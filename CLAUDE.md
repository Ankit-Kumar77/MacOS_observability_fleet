# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An Ansible project (no application code, no unit tests) that deploys host-metric observability to a fleet of Apple Silicon Mac Minis. One Mac runs VictoriaMetrics + Grafana; every other Mac runs an OpenTelemetry Collector.

**Status: the data path is verified, the launchd path is not.** The collector → OTLP/HTTP → VictoriaMetrics → Grafana pipeline has been run end to end with the real binaries on Apple Silicon, and all six dashboard panels return data. Nothing has ever run under launchd on a real Mac Mini. Do not describe service lifecycle, root-under-launchd behaviour, or idempotency as working.

## Commands

Ansible is **not installed** on this machine. Install the full `ansible` package, not bare `ansible-core`: `ansible.cfg` sets `stdout_callback = yaml`, which ships in `community.general`, and without that collection every `ansible-playbook` run aborts with `Could not load 'yaml' callback plugin`. `ansible.cfg` also sets the inventory, `roles_path`, and `become: sudo`, so `-i` is optional but used explicitly throughout the docs.

```bash
ansible all -m ping                      # connectivity

ansible-playbook site.yml --syntax-check # static validation, no hosts touched
ansible-inventory --list                 # confirms group membership + var resolution
ansible-lint                             # must pass; config in .ansible-lint

# Deploy (two vault secrets are required - see "Secrets" below)
ansible-playbook -i inventories/production/hosts.yml site.yml --tags server --ask-become-pass --ask-vault-pass
ansible-playbook -i inventories/production/hosts.yml site.yml --tags agent  --ask-become-pass --ask-vault-pass

# One host, for staged rollout or troubleshooting
ansible-playbook -i inventories/production/hosts.yml site.yml --limit mac-mini-02 --tags agent --ask-become-pass --ask-vault-pass

# Health/verification tasks only
ansible-playbook -i inventories/production/hosts.yml site.yml --tags verify --ask-become-pass --ask-vault-pass
```

### Testing changes without Mac Minis

Most of this project can be verified locally on any Apple Silicon Mac, and it is worth doing — every dashboard defect found so far was invisible to static checks. Download the pinned binaries, run VictoriaMetrics and the collector against each other on spare ports, then query VictoriaMetrics for the metric names and run the dashboard's PromQL directly. Grafana can be started against the rendered `grafana.ini` and provisioning directory to confirm the datasource and dashboard load, and its `/api/datasources/proxy/uid/victoriametrics/api/v1/query` endpoint runs panel queries through the real datasource.

## Architecture

`site.yml` is two plays, one per inventory group, each applying one role:

| Group | Role | Installs |
| --- | --- | --- |
| `monitoring_server` (exactly one host) | `observability_server` | VictoriaMetrics, Grafana, datasource + dashboard provisioning, 2 LaunchDaemons |
| `monitored_nodes` (N hosts) | `observability_agent` | `otelcol-contrib`, host-metric pipeline, 1 LaunchDaemon |

Metric path — deliberately **no Prometheus server and no node_exporter**:

```
hostmetrics -> resourcedetection -> batch -> otlphttp (basic auth)
  -> http://<monitoring_server_address>:8428/opentelemetry/v1/metrics  (VictoriaMetrics)
  -> Grafana (datasource type `prometheus`, uid `victoriametrics`, localhost:8428)
```

Both roles follow the same order: `prerequisites` → install/configure → **`flush_handlers`** → `verify`. The explicit flush matters: handlers otherwise run at the end of the play, so verification would check services still running their previous configuration.

A third role, `observability_common`, holds what both roles share. It has no `tasks/main.yml` and is never applied directly — it is pulled in with `include_role` + `tasks_from`:

| File | Purpose |
| --- | --- |
| `tasks/base_directories.yml` | Creates the `/opt/observability` layout. Callers add role-specific paths via `observability_extra_directories`. |
| `tasks/install_versioned_archive.yml` | Download → checksum-verify → versioned extract → stat → assert → clean up the temp archive. Used once per component. |
| `templates/launchd_daemon.plist.j2` | One plist for all three daemons, parameterised by `launchd_label`, `launchd_program_arguments`, the log paths, and an optional `launchd_working_directory`. |

Two rules keep this safe, and both are load-bearing:

- **`install_versioned_archive.yml` deliberately does not create the symlink or notify a handler.** The symlink flip is the moment the running version changes, so it lives in the calling role next to the handler it notifies. Notifying a caller's handler from inside an included role is the fragile part; this split avoids needing it at all.
- **The shared plist is referenced as `{{ role_path }}/../observability_common/templates/launchd_daemon.plist.j2`.** Ansible resolves a bare `src:` against the *calling* role's `templates/`, which would not find it.

`ProgramArguments` for each daemon are lists in that role's `defaults/main.yml`, so they are documented and overridable rather than buried in XML. VictoriaMetrics builds its list as `victoriametrics_base_arguments + (victoriametrics_auth_arguments if victoriametrics_auth_enabled else [])`.

Everything lands under `/opt/observability`, owned `root:wheel`. LaunchDaemon labels: `com.observability.victoriametrics`, `com.observability.grafana`, `com.observability.otelcol`.

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

## Conventions and gotchas

- **VictoriaMetrics needs `-opentelemetry.usePrometheusNaming`.** Without it, OTLP names are stored verbatim including dots (`system.memory.usage`, label `host.name`) and every dashboard query silently returns zero series. This flag is load-bearing, not cosmetic.
- **PromQL: never divide a full-label selector by a `sum by (...)`.** The label sets do not match and the result is empty even though both operands have data. Aggregate both sides: `sum by (host_name)(x{state="used"}) / sum by (host_name)(x)`. Two panels shipped broken this way.
- **`dashboard.json.j2` is Jinja-rendered**, so Grafana's `{{host_name}}` legend syntax must be escaped as `{{ '{{' }}host_name{{ '}}' }}`. Panels reference the datasource by uid `victoriametrics` and each target needs a `refId`.
- **Releases install into versioned directories behind stable symlinks** (`bin/victoria-metrics-<ver>/`, `grafana-<ver>/`, `bin/otelcol-contrib-<ver>/`), with the stable path repointed by the role. The symlink flip — not the extraction — notifies the restart handler. Archives are version-stamped in `/tmp` and removed after extraction. A `creates:` guard on an unversioned path silently turns a version bump into a no-op. Adding a component means calling `install_versioned_archive` and then writing its symlink + plist + service tasks — do not re-implement the install flow.
- **`{{ grafana_home }}` must not be created as a directory.** It is a symlink, and `ansible.builtin.file` refuses to replace a real directory with one. It is deliberately excluded from the `prerequisites.yml` directory loop; `{{ var_dir }}/grafana` (Grafana's data dir) is a real directory and stays.
- **Old versions are left in place** after an upgrade, enabling rollback by reverting the version variable. Nothing prunes them, and these binaries are large (collector ~334 MB, Grafana ~1.3 GB).
- **Every download is checksum-pinned** against the upstream-published SHA256. A version bump must update the matching `*_checksum`, or the download fails closed.
- **Secrets are `0600` and `no_log`.** `grafana.ini`, `otel-config.yaml`, the provisioned datasource, and the VictoriaMetrics password file all carry credentials. The VictoriaMetrics password is passed to launchd as `file://...` rather than an argument so it stays out of the world-readable plist and out of `ps`.
- **Do not replace `ansible.builtin.service` with `launchctl` yet.** `LAUNCHD_TROUBLESHOOTING.md` contains a fully-specified refactor that is explicitly gated on the first successful real-Mac test.
- **Inventory holds `REPLACE_WITH_*` placeholders by design.** Never commit real addresses, SSH users, or credentials.
- `.ansible-lint` runs the `production` profile. `var-naming[no-role-prefix]` is deliberately skipped — see the comment there.

## Secrets

Two vaulted variables are required; the deployment fails closed without them.

| Variable | Purpose |
| --- | --- |
| `vault_grafana_admin_password` | Grafana admin login. |
| `vault_victoriametrics_password` | VictoriaMetrics basic auth. Without it anyone reaching port 8428 can read, write or delete fleet metrics. |

The collector, the Grafana datasource and VictoriaMetrics itself are all rendered from the same credentials, so all three must be deployed from the same vault. `/health` is deliberately exempt from auth so readiness probes keep working; query and OTLP-write endpoints return `401` unauthenticated.

## Reference docs

`docs/PROJECT_FLOW.md` (internal flow + mermaid diagrams), `docs/KT_GUIDE.md` (onboarding walkthrough and FAQ), `docs/RESOURCE_FOOTPRINT.md` (measured disk figures), `LAUNCHD_TROUBLESHOOTING.md` (launchctl diagnostics + the gated refactor plan). These describe implementation specifics — keep them in sync when changing role behaviour.
