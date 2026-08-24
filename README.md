# Mac Mini Observability

![Ansible](https://img.shields.io/badge/ansible-%231A1918.svg?style=for-the-badge&logo=ansible&logoColor=white)
![macOS](https://img.shields.io/badge/mac%20os-000000?style=for-the-badge&logo=macos&logoColor=F0F0F0)
![Grafana](https://img.shields.io/badge/grafana-%23F46800.svg?style=for-the-badge&logo=grafana&logoColor=white)
![OpenTelemetry](https://img.shields.io/badge/OpenTelemetry-000000?style=for-the-badge&logo=opentelemetry&logoColor=white)

An Ansible project for monitoring and observability of approximately 40 Apple Silicon Mac Minis.

## Project Purpose

This project automates the deployment of a monitoring stack consisting of:

- **VictoriaMetrics** — time-series database
- **Grafana** — visualization
- **OpenTelemetry Collector** — host metrics collection and telemetry pipeline

It is designed to be environment-agnostic, idempotent, and specifically tailored for macOS on Apple Silicon.

## Architecture

```text
                         MONITORING MAC
                     ┌────────────────────┐
                     │                    │
                     │  VictoriaMetrics   │
                     │         ▲          │
                     │         │          │
                     │      Metrics       │
                     │         │          │
                     │      Grafana       │
                     │                    │
                     └─────────▲──────────┘
                               │
                            OTLP/HTTP
                               │
          ┌────────────────────┼────────────────────┐
          │                    │                    │
     ┌────▼─────┐         ┌────▼─────┐         ┌────▼─────┐
     │ Mac Mini │         │ Mac Mini │         │ Mac Mini │
     │    02    │         │    03    │         │    N     │
     │          │         │          │         │          │
     │   OTel   │         │   OTel   │         │   OTel   │
     │ Collector│         │ Collector│         │ Collector│
     │          │         │          │         │          │
     │hostmetrics│        │hostmetrics│        │hostmetrics│
     └──────────┘         └──────────┘         └──────────┘


## Repository Structure

```text
mac-mini-observability/
├── ansible.cfg
├── site.yml
├── README.md
├── inventories/
│   └── production/
│       ├── hosts.yml
│       └── group_vars/
│           ├── all.yml
│           ├── monitoring_server.yml
│           └── monitored_nodes.yml
└── roles/
    ├── observability_server/
    │   ├── defaults/main.yml
    │   ├── handlers/main.yml
    │   ├── tasks/main.yml (includes prerequisites, victoriametrics, grafana, verify)
    │   └── templates/ (plist files, Grafana configs, datasources, dashboards)
    └── observability_agent/
        ├── defaults/main.yml
        ├── handlers/main.yml
        ├── tasks/main.yml (includes prerequisites, opentelemetry, verify)
        └── templates/ (plist files, OTel pipeline config)
```

## Roles

The architecture uses two responsibility-based roles:
1. **observability_server**: Installs VictoriaMetrics and Grafana, sets up datasources and default dashboards, and provisions `launchd` services.
2. **observability_agent**: Installs the OpenTelemetry Collector, configures the metrics pipeline, and provisions `launchd` services.

## Inventory

The `inventories/production/hosts.yml` defines two groups:
- `monitoring_server`: The central Mac Mini that stores and visualizes metrics.
- `monitored_nodes`: All other Mac Minis running the observability agents.

## No-Galaxy Approach

This project explicitly avoids using Ansible Galaxy roles or collections. Only built-in modules (`ansible.builtin.*`) are used. This ensures no third-party dependencies exist, increasing security and long-term maintainability for the specific macOS targets.

## No-Hardcoding Approach

All environment-specific values (IPs, credentials, versions, paths) are abstracted into Ansible variables within `group_vars`. No real passwords or production IP addresses are checked into Git. The dynamic `monitoring_server_address` ensures that all agent nodes automatically resolve the server's endpoint based on the inventory configuration.

## OTel Pipeline

**OpenTelemetry Collector** uses its native `hostmetrics` receiver to gather CPU, memory, disk, and network metrics natively on macOS. 
The pipeline configuration:
1. **Receiver**: `hostmetrics` (scrapes macOS internal metrics).
2. **Processors**: `resourcedetection` (adds host metadata), `batch` (optimizes payload size).
3. **Exporter**: `otlphttp` (transmits standard OTLP metrics over HTTP).

## VictoriaMetrics & Grafana

**VictoriaMetrics** natively supports the OpenTelemetry OTLP/HTTP protocol at `/opentelemetry/v1/metrics`. It ingests OTel metrics without requiring a translation proxy or Prometheus server in the middle. This keeps the architecture purely OpenTelemetry-native and highly efficient.
**Grafana** is automatically provisioned with VictoriaMetrics as its default datasource and a standard Mac Mini system dashboard utilizing OTel semantic conventions.

## macOS Considerations

This project avoids Linux-isms (like `apt` or `systemd`). It uses native macOS background service management via `launchd`. `.plist` templates are generated and placed in `/Library/LaunchDaemons/`, enabling services to run safely on boot without requiring an active user session. Tarball distributions are used to avoid package manager collisions (e.g., Homebrew).


## Testing Now — Without Mac Access

You can validate the Ansible logic before deploying it to real hardware:

1. **Syntax Check**:
   ```bash
   ansible-playbook -i inventories/production/hosts.yml site.yml --syntax-check
   ```
2. **Dry Run (Check Mode)**:
   ```bash
   ansible-playbook -i inventories/production/hosts.yml site.yml --check
   ```
   *(Note: This might fail on certain file operations since the target environment doesn't exist locally)*

## Prerequisites (For the Operator)

Before you begin, ensure the machine you are running these commands from (your "Control Node") has the following:
1. **Ansible Installed**: `pip install ansible` or `brew install ansible`.
2. **SSH Access**: You must have SSH access to all Mac Minis. Passwordless SSH (using SSH keys) is highly recommended.
3. **Sudo Privileges**: The Ansible user on the Mac Minis must have `sudo` privileges to install `launchd` services.

## Quick Start / Deployment Guide

If you are setting this up from scratch, follow these exact steps:

**1. Clone the repository**
```bash
git clone <your-repo-url>
cd mac-mini-observability
```

**2. Configure your Inventory**
Open `inventories/production/hosts.yml` and replace the placeholder hostnames/IP addresses and `ansible_user` with your actual Mac Minis' details. Ensure you define exactly one Mac under `monitoring_server` and the rest under `monitored_nodes`.

**3. Configure your Secrets (Important)**
Open `inventories/production/group_vars/monitoring_server.yml`. You MUST provide the Grafana admin password securely, as it does not have an insecure fallback.
Use Ansible Vault to encrypt this secret:
```bash
ansible-vault encrypt_string 'your_secure_password' --name 'vault_grafana_admin_password'
```
Paste the output into the variable file.

**4. Test Connectivity**
Ensure Ansible can talk to all Macs:
```bash
ansible all -i inventories/production/hosts.yml -m ping
```

**5. Deploy the Monitoring Server**
Install VictoriaMetrics and Grafana on the central node. You will be prompted for the sudo password (`--ask-become-pass`):
```bash
ansible-playbook -i inventories/production/hosts.yml site.yml --tags server --ask-become-pass
```

**6. Deploy to ONE Agent (Test Run)**
Test the agent installation on a single Mac Mini first to verify the pipeline works:
```bash
ansible-playbook -i inventories/production/hosts.yml site.yml --limit mac-mini-02 --tags agent --ask-become-pass
```

**7. Verify Metrics in Grafana**
Open `http://<SERVER_IP>:3000` in your browser. Log in using `admin` and the password you set. Verify that metrics from the first agent are appearing in the default dashboard.

**8. Deploy to Remaining Agents**
Once verified, roll out the agent to the remaining fleet:
```bash
ansible-playbook -i inventories/production/hosts.yml site.yml --tags agent --ask-become-pass
```


## Verification

The playbook automatically verifies service health to ensure end-to-end telemetry paths are working.

**Server:** 
- Ensures VictoriaMetrics (`/health`) and Grafana (`/api/health`) HTTP endpoints are responding.
- Verifies that Grafana's API contains the properly provisioned VictoriaMetrics datasource.

**Agent:** 
- Validates the OpenTelemetry Collector configuration using the `otelcol-contrib validate` command.
- Verifies network connectivity from the agent directly to the VictoriaMetrics ingestion port on the monitoring server.

## Grafana Dashboard Setup (Manual)

If you need to create custom dashboards manually:
1. Open Grafana at `http://<SERVER_IP>:3000`.
2. Login with `admin` and the password defined in `group_vars`.
3. Navigate to **Connections -> Data sources** and verify **VictoriaMetrics** is active.
4. Go to **Dashboards -> New dashboard**.
5. Add a visualization, select VictoriaMetrics as the source, and use the metric names provided by the OpenTelemetry Collector. Confirm the available metrics in Grafana's query editor.
6. Save the dashboard.

## Scaling

To scale to 40 machines:
1. Simply add the new hosts under `monitored_nodes` in `inventories/production/hosts.yml`.
2. Re-run the playbook:
   ```bash
   ansible-playbook -i inventories/production/hosts.yml site.yml --tags agent
   ```
The same role will configure the new machines idempotently.

## Security

Do not commit real credentials. The placeholder `grafana_admin_password` should be replaced with an Ansible Vault variable:
```yaml
grafana_admin_password: "{{ vault_grafana_admin_password }}"
```
To run with vault:
```bash
ansible-playbook -i inventories/production/hosts.yml site.yml --ask-vault-pass
```

## Common Failure Scenarios

- **SSH Connectivity**: Ensure `ansible_user` is correct and SSH keys are distributed.
- **Port Conflicts**: Ensure ports 3000 (Grafana) and 8428 (VictoriaMetrics) are not already bound.
- **SIP / Permissions**: `launchd` daemons need to run as root. Ensure you use `--ask-become-pass` or configure passwordless sudo for the ansible user.
