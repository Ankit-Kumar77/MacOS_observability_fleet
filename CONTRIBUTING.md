# Contributing

Thanks for your interest in this project. It is an Ansible project — there is no
application code and no unit test suite — so most contributions are role tasks,
templates, variables, or documentation.

Please read [CLAUDE.md](CLAUDE.md) before your first change. It records the
macOS-specific constraints that static checks cannot catch, and most of them were
found the hard way on real hardware.

## Getting set up

Install the full `ansible` package, not bare `ansible-core`. The configuration in
`ansible.cfg` depends on plugins that ship with the full package, and a bare
`ansible-core` install aborts before the first task runs.

```bash
pip install ansible ansible-lint
```

`ansible.cfg` already sets the inventory, `roles_path`, and `become: sudo`, so
`-i` is optional. The documentation passes it explicitly for clarity.

## Before you open a pull request

Run the same three checks that CI runs, from the repository root:

```bash
ansible-playbook site.yml --syntax-check
ansible-inventory -i inventories/production/hosts.yml --list > /dev/null
ansible-lint
```

`ansible-lint` runs the `production` profile and must pass with no findings. Two
rules are skipped deliberately in `.ansible-lint` and both carry a comment
explaining why. Do not add a new skip without an equivalent comment, and do not
silence a finding that points at a real problem.

## Testing on a Mac

Static checks catch very little here. Every dashboard defect found so far was
invisible to them. If you have an Apple Silicon Mac, verify your change against
the real binaries before opening a pull request:

- Download the pinned versions and run VictoriaMetrics and the collector against
  each other on spare ports.
- Query VictoriaMetrics directly for the metric names your change touches.
- For dashboard changes, run the panel PromQL through Grafana's datasource proxy
  rather than eyeballing the JSON.

If you cannot test on a Mac, say so in the pull request. A change that has only
been syntax-checked is still welcome, but it must be labelled as such so it is
not mistaken for verified work.

Service lifecycle is the one area that cannot be validated off real hardware.
launchd bootstrap, `KeepAlive`, restart behaviour, and running as root under
launchd all need physical Macs. Do not describe any of them as working in a
pull request or in documentation unless you have actually run them.

## Conventions worth knowing

These are the ones contributors trip on most often.

- **Both roles are self-contained.** Versions, checksums, paths, ports, and
  tuning live in `roles/<role>/defaults/main.yml`. Only the cross-role contract
  belongs in `inventories/production/group_vars/all.yml`.
- **Adding a component means calling `install_versioned_archive`,** then writing
  its symlink, plist, and service tasks in the calling role. Do not reimplement
  the install flow, and do not move the symlink flip into the shared task file.
- **Every download is checksum-pinned.** A version bump must update the matching
  `*_checksum` in the same commit, or the download fails closed.
- **Never commit real addresses, SSH users, or credentials.** The inventory ships
  `REPLACE_WITH_*` placeholders on purpose. Files that carry secrets are `0600`
  and `no_log`; keep them that way.
- **Keep the reference docs in sync.** Changing role behaviour usually means
  touching `docs/PROJECT_FLOW.md`, `docs/KT_GUIDE.md`, or
  `LAUNCHD_TROUBLESHOOTING.md` alongside the tasks.

## Commits and pull requests

Write commit subjects in the imperative mood and keep them focused on one
change. In the pull request description, state what you changed, why, and how
far you verified it — syntax check only, local run against the real binaries, or
a deployment to physical Macs. That last line matters more than the rest.

## Reporting problems

Open an issue with the macOS version, the Apple Silicon model, the component
versions involved, and the relevant playbook output or `launchctl` diagnostics.
`LAUNCHD_TROUBLESHOOTING.md` lists the commands worth capturing for service
failures.

## License

By contributing, you agree that your contributions are licensed under the
[MIT License](LICENSE) that covers this project.
