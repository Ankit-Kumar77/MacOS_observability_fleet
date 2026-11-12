# Security policy

## Reporting a vulnerability

Please do **not** open a public issue for a security problem. Use GitHub's private vulnerability reporting (**Security → Report a vulnerability**) on the repository, or contact the maintainers privately. Include the affected file or role, the impact, and how to reproduce it. Expect an acknowledgement within a few working days.

## Supported versions

Only the latest commit of the `LaunchAgent` branch (no-sudo variant) and of `main` (system-level variant) receive fixes.

## Security model

| Area | Design |
| --- | --- |
| Privileges | Nothing runs as root. `become` is disabled in `ansible.cfg` and in both plays, and preflight fails on uid 0 or `ansible_become`. |
| Secrets | Credential-bearing files (`grafana.ini`, `otel-config.yaml`, the provisioned datasource, the VictoriaMetrics password file) are `0600`, and the tasks that write them are `no_log`. The VictoriaMetrics password reaches launchd as `file://…`, never on a command line. API keys for MLX-VLM and OMLX stay in their own `0600` files on the Mac; only the paths are rendered into config. |
| Defaults | **Insecure by default, for testing:** VictoriaMetrics accepts unauthenticated reads, writes and deletes from anyone who can reach its port, and Grafana's admin password is `admin`. Enable `victoriametrics_auth_enabled` and set real passwords (Ansible Vault) before exposing the stack. See the README, "Security". |
| Network | The collector's own telemetry, the LLM agent's OTLP receiver and the telemetry proxy listen on loopback by default. The proxy can be exposed with `mlx_telemetry_host: 0.0.0.0`; it then forwards requests to the inference server for anyone who can reach it, exactly like the server itself. |
| Inference data | No prompts, completions, images, request ids, conversation ids or user ids are logged or exported. The proxy forwards client `Authorization` headers without storing or logging them and keeps no access log. Quality and confidence scoring happen in memory and are discarded. |
| Label hygiene | Model and tool names are sanitised and capped; model paths are reduced to their last component, so home directories never become labels. |
| Supply chain | Every downloaded release is SHA-256 pinned to the upstream-published checksum and fails closed on mismatch. The telemetry agent uses only the Python standard library. |

## Out of scope

- Weak passwords or disabled auth set in your own inventory.
- macOS Local Network Privacy blocking the collector: a platform policy that this project must not work around (see `docs/LAUNCHAGENT_VARIANT.md`).
