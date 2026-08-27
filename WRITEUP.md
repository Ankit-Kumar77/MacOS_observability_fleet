# Writeup: macOS compatibility, correctness and hardening

This document explains everything changed in this branch, why, and what was
verified versus what still needs real hardware. It is written for whoever
reviews or deploys this next.

**Headline:** before these changes, the Grafana dashboard would have rendered
six empty panels on a real deployment, and two of them could never have worked
at all on macOS. Every finding below was discovered by running the actual
binaries on Apple Silicon, not by reading code.

---

## 1. Why the dashboard was completely blank

Four independent defects, each sufficient on its own to blank every panel.

### 1.1 Metric and label names never matched

VictoriaMetrics stores OTLP names verbatim, dots included, unless told
otherwise. What it actually stored versus what the dashboard queried:

| Stored by VictoriaMetrics (default) | Queried by dashboard |
| --- | --- |
| `system.cpu.load_average.1m` | — |
| `system.memory.usage` | `system_memory_usage_bytes` |
| `system.filesystem.usage` | `system_filesystem_usage_bytes` |
| `system.network.io` | `system_network_io_bytes_total` |
| label `host.name` | label `host_name` |

All four dashboard metric names returned **0 series**.

**Fix:** VictoriaMetrics now runs with `-opentelemetry.usePrometheusNaming`,
which converts names and labels to exactly the Prometheus-style forms the
dashboard expects. This flag is load-bearing, not cosmetic.

### 1.2 Two panels used invalid PromQL vector matching

The Memory and Disk panels divided a full-label selector by a `sum by (...)`.
The label sets do not match, so the result is empty even though both operands
have data. Measured in isolation:

```
system_memory_usage_bytes{state="used"}           -> 1 series (7.5 GB)
sum by (host_name) (system_memory_usage_bytes)    -> 1 series (8.6 GB)
the division as written in the dashboard          -> 0 series   <-- both exist, result empty
sum by(host_name)(...{state="used"}) / sum by(...) -> 1 series (88.0%)
```

**Fix:** aggregate both sides. Disk behaved identically (`0 series` → `96.3%`).

### 1.3 The `cpu` and `disk` scrapers do not exist on macOS in 0.98.0

The pinned collector logged, every single interval:

```
error  Error scraping metrics  {"error": "not implemented yet", "scraper": "cpu"}
error  Error scraping metrics  {"error": "not implemented yet", "scraper": "disk"}
```

`system.cpu.time` was therefore never produced at all, which made the **CPU
Usage** and **Active Hosts** panels impossible regardless of naming.

**Fix:** collector bumped `0.98.0` → `0.159.0`, where all six scrapers run with
zero errors on darwin/arm64. This is now documented as a hard floor.

### 1.4 "Active Hosts" was the wrong aggregation

`count(system_cpu_time_seconds_total{state="idle"}) by (host_name)` counts
series *per host*. On Linux that is the core count; on macOS it is always 1.
Either way it is not a fleet host count.

**Fix:** `count(count by (host_name) (system_memory_usage_bytes))`.

---

## 2. macOS-specific behaviour that differs from Linux

These are the things that make this stack behave differently on a Mac. None is
visible to a syntax check; each was found by running the real binaries.

### 2.1 There is no per-core `cpu` label

macOS aggregates CPU time across cores. The idiomatic Linux expression —
`100 - avg by (host_name) (rate(idle[5m])) * 100` — assumes each core's idle
rate falls in 0..1. With a single aggregated series it produces:

```
CPU panel as written  ->  -466.32
corrected             ->    28.89
```

**Fix:** normalise against the sum of all CPU states:
`100 * (1 - sum(rate(idle)) / sum(rate(all states)))`.

### 2.2 Memory states are `free` / `inactive` / `used`

There is no `wired` state; wired memory is folded into `used`. `inactive` is
reclaimable and is deliberately not counted as used. Measured total across the
three states matched installed RAM exactly.

### 2.3 APFS volumes share container capacity

Every volume in an APFS container reports the **same** container-wide capacity.
On a test machine the collector scraped 13 mountpoints, of which `/` and
`/System/Volumes/Data` reported identical usage and the rest were synthetic
helper volumes:

```
/                          apfs   494.4 GB
/System/Volumes/Data       apfs   494.4 GB
/System/Volumes/Preboot    apfs   494.4 GB
/System/Volumes/Update     apfs   494.4 GB
/System/Volumes/VM         apfs   494.4 GB
...
```

Summing across mountpoints multiplies the total.

**Fix:** the collector config excludes synthetic volumes and the `autofs` /
`devfs` types, leaving `/` and `/System/Volumes/Data`. Verified: 13 mountpoints
→ 2, filesystem series down to 10. The dashboard charts `/` only.

### 2.4 Grafana 11+ has no `grafana-server` binary

Grafana 13.2.0 ships a single `grafana` binary. The plist pointed at
`bin/grafana-server`, which does not exist — the deploy would have failed on the
binary assertion, and the plist would have referenced a missing path.

**Fix:** the plist runs `grafana server --config=... --homepath=...`.

---

## 3. Deployment correctness

| Problem | Fix |
| --- | --- |
| Handlers flush at the *end* of the play, so `verify.yml` checked services still running their previous configuration — a green run proved nothing. | `meta: flush_handlers` before the verify import in both roles. |
| A version bump was a silent no-op: `unarchive` guarded on `creates:` an unversioned binary path, and `get_url` wrote to a fixed `/tmp` filename with `force: no`, so the new archive was never even downloaded. | Version-stamped `/tmp` archives; each release extracts into its own versioned directory with a stable symlink repointed at it. |
| `unarchive` set no `owner`/`group`, so extracted binaries kept whatever UID the tarball recorded. | `owner: root`, `group: wheel`. |
| Grafana readiness allowed 25s; first boot does sqlite init plus provisioning. | 150s (`grafana_ready_retries: 30`). |
| `groups['monitoring_server'][0]` raises on an empty group; `['ansible_host']` raises if unset. | Explicit `assert` in the agent role with an actionable message. |
| VictoriaMetrics retention silently defaulted to 1 month. | Explicit `victoriametrics_retention: 90d`. |
| `/tmp` archives were never cleaned up. | Removed after extraction. |

### Upgrade model

Each release installs to its own directory with a stable symlink pointing at the
active one:

| Component | Versioned directory | Stable path (symlink) |
| --- | --- | --- |
| VictoriaMetrics | `bin/victoria-metrics-1.150.0/` | `bin/victoria-metrics-prod` |
| Grafana | `grafana-13.2.0/` | `grafana` |
| OTel Collector | `bin/otelcol-contrib-0.159.0/` | `bin/otelcol-contrib` |

Symlinks were chosen over versioned paths in the plists specifically because
`README.md`, `LAUNCHD_TROUBLESHOOTING.md` and `KT_GUIDE.md` hardcode the stable
paths in dozens of diagnostic commands — this keeps every one of them correct
across upgrades. The **symlink flip**, not the extraction, notifies the restart
handler. Superseded versions are left on disk so a rollback is a variable
revert.

---

## 4. Security

| Problem | Fix |
| --- | --- |
| **VictoriaMetrics had no authentication.** Anyone able to reach port 8428 could read, write or delete fleet metrics. | Basic auth on VictoriaMetrics, with matching credentials in the collector's `otlphttp` exporter and the Grafana datasource. |
| `grafana.ini` was mode `0644` with the admin password in plaintext; the template task had no `no_log`, so `--diff` printed it. | `0600` plus `no_log`. Same for the collector config and provisioned datasource. |
| No integrity verification on any download — three binaries installed as root-run daemons. | All three `get_url` calls pin the upstream-published SHA256. |

### Auth design

Verified behaviour, chosen deliberately:

```
/health          no credentials -> 200   (readiness probes keep working)
/api/v1/labels   no credentials -> 401
/api/v1/labels   with credentials -> 200
OTLP write       no credentials -> 401
```

The password reaches VictoriaMetrics as `-httpAuth.password=file://<path>`
rather than an inline argument, so it appears in neither the world-readable
plist nor `ps` output — confirmed by inspecting the running process:

```
-httpAuth.username=observability
-httpAuth.password=file:///opt/observability/etc/victoriametrics-auth-password
```

**This adds a second required vault secret**, `vault_victoriametrics_password`,
alongside `vault_grafana_admin_password`. The collector on every Mac, the
Grafana datasource and VictoriaMetrics itself are rendered from it, so all three
must be deployed from the same vault. This was judged acceptable because nothing
is deployed yet, so there is no existing workflow to break. Set
`victoriametrics_auth_enabled: false` to opt out.

---

## 5. Structure

- **Role defaults.** Both roles now have `defaults/main.yml` and `meta/main.yml`.
  Versions, checksums, paths, ports and tuning live there.
- **`group_vars` is now the cross-role contract only** — `victoriametrics_port`,
  `monitoring_server_address`, and the auth settings. These must agree between
  the monitoring Mac and every agent, so they are defined exactly once.
- **`.ansible-lint` on the `production` profile**, plus a CI workflow running
  syntax check, inventory resolution and lint.

Two lint rules are skipped, with rationale in the config:
`yaml[line-length]` (download URLs), and `var-naming[no-role-prefix]` — enforcing
it would require `observability_agent_base_dir` and
`observability_server_base_dir`, i.e. two names for one shared concept, breaking
the contract `group_vars/all.yml` exists to express.

### The `observability_common` library role

The install flow was written out three times across two roles. It now lives in a
third role that is never applied directly, only pulled in via `include_role` +
`tasks_from`:

| File | Purpose |
| --- | --- |
| `tasks/install_versioned_archive.yml` | download → checksum-verify → versioned extract → stat → assert → clean up |
| `tasks/base_directories.yml` | the `/opt/observability` layout, plus `observability_extra_directories` |
| `templates/launchd_daemon.plist.j2` | one plist for all three daemons |

Two decisions keep this safe:

1. **The shared install file does not create the symlink or notify a handler.**
   Notifying a calling role's handler from inside an included role is the
   fragile part of this pattern. The symlink flip is also genuinely where the
   running version changes, so it belongs beside the handler it triggers. Each
   caller keeps a short symlink + plist + service block.
2. **The shared plist is referenced as
   `{{ role_path }}/../observability_common/templates/launchd_daemon.plist.j2`.**
   A bare `src:` resolves against the *calling* role's `templates/`, which would
   not find it.

`ProgramArguments` are lists in each role's defaults rather than inline XML, so
they are documented and overridable. VictoriaMetrics composes its as
`base_arguments + (auth_arguments if enabled else [])`.

---

## 6. Resource footprint

The previous estimates were wrong, and were wrong before these changes too.
Measured by extracting the archives on arm64:

| Component | Documented estimate | Measured |
| --- | --- | --- |
| Grafana 13.2.0 | part of "250–400 MB" | **1.3 GB** |
| otelcol-contrib 0.159.0 | "90–160 MB" | **334 MB** |
| otelcol-contrib 0.98.0 (old pin) | "90–160 MB" | 234 MB |
| VictoriaMetrics 1.150.0 | — | 24 MB |

A 40-Mac fleet therefore carries roughly **13.6 GB** of collector binaries, or
double that if a version bump is applied without pruning the superseded install
directory.

The collector is large because `otelcol-contrib` bundles every upstream
component. The slim `otelcol` core distribution is 119 MB but **does not include
`resourcedetection`**, which this project relies on to label metrics by host, so
it cannot be substituted as-is — confirmed by running `otelcol validate` against
this project's config. A custom OpenTelemetry Collector Builder image containing
only `hostmetrics`, `resourcedetection`, `batch`, `basicauth` and `otlphttp` is
the way to cut this down if fleet disk becomes a constraint.

---

## 7. What was verified, and how

The full data path was run end to end on Apple Silicon using the real pinned
binaries: `hostmetrics` → collector 0.159.0 → OTLP/HTTP with basic auth →
VictoriaMetrics 1.150.0 → Grafana 13.2.0 with this project's rendered
`grafana.ini`, provisioned datasource and dashboard.

Every panel query was executed **through Grafana's datasource proxy**, which
also proves Grafana's credentials to VictoriaMetrics work:

| Panel | Result |
| --- | --- |
| Active Hosts | 1 |
| CPU Usage (%) | 12.31 |
| Memory Usage (%) | 80.69 |
| Disk Usage (%) | 96.60 |
| Network RX (B/s) | 15,841 |
| Network TX (B/s) | 47,739 |
| Load Average (1m) | 2.12 |

All seven previously returned nothing.

Also verified: all three checksums against upstream publishers; `plutil -lint`
on every rendered plist; the collector config against `otelcol-contrib validate`;
`ansible-playbook --syntax-check`; and `ansible-lint` on the `production`
profile (0 failures, 0 warnings).

### What is NOT verified

- **launchd, entirely.** Service bootstrap, `KeepAlive`, restart behaviour,
  running as root under launchd, and `root:wheel` ownership at runtime.
- **Idempotency across repeated runs** on real hosts.
- **Network and firewall policy** between monitored Macs and the monitoring Mac.
- **The `observability_common` refactor (section 5).** It was performed without
  running anything, at the request of the repository owner. It uses only
  conservative, well-established Ansible constructs, but `--syntax-check` and
  `ansible-lint` have not been run against it. **Run both before deploying.**

```bash
ansible-playbook site.yml --syntax-check
ansible-lint
```

---

## 8. Deliberately not done

- **The `launchctl` refactor.** `LAUNCHD_TROUBLESHOOTING.md` specifies it in
  full but explicitly gates it on the first successful real-hardware test.
  Shipping an untested rewrite of service management across five files, with no
  way to validate it, would trade a known-unknown for an unknown-unknown.
- **Running services as non-root.** The plists set no `UserName`. Doing this
  properly needs a macOS service account created via `dscl`, which interacts
  with launchd in exactly the way the troubleshooting guide says to validate
  first, and could not be tested without modifying a developer machine.
- **Pruning superseded install directories.** Keeping them is what makes
  rollback a variable revert. Worth revisiting once fleet disk is measured.

---

## 9. Operational notes for the first deployment

1. **Two vault secrets are now required.** Deployment fails closed without
   `vault_grafana_admin_password` and `vault_victoriametrics_password`.
2. **The symlink model assumes no prior deployment.** If any Mac already has a
   real `/opt/observability/grafana` *directory* from an earlier run, the
   symlink task will fail — `ansible.builtin.file` will not replace a real
   directory with a symlink. No migration logic was written because the project
   had never been deployed. If that is not true for your fleet, say so and it
   needs a guarded one-time migration.
3. **Version bumps must update the matching `*_checksum`**, or the download
   fails closed. That is intentional.
4. **Install the full `ansible` package, not `ansible-core`.** `ansible.cfg`
   sets `stdout_callback = yaml`, which ships in `community.general`; without it
   every run aborts with `Could not load 'yaml' callback plugin`.
5. Follow the one-monitoring-Mac plus one-agent-Mac sequence in `README.md`
   before scaling out. The launchd layer is still entirely unproven.
