# Security policy

## Reporting a vulnerability

Please email [gbellamy@umd.edu](mailto:gbellamy@umd.edu) or open a
private security advisory on GitHub:
<https://github.com/GraysonBellamy/capa/security/advisories/new>.

**Do not file public issues for security reports.**

You should expect an initial response within a few working days. capa is
pre-alpha and single-maintainer; there is no formal SLA yet.

## Scope

capa is a control and DAQ application for a **controlled-atmosphere
pyrolysis lab instrument** — it drives physical hardware (heaters, mass
flow controllers, balances, NI-DAQ, cameras) over serial, USB, and
network. The threat model is rig-local: bugs that put hardware or
operators at risk are treated as security issues, alongside the usual
software-security categories.

In particular, please report:

- **Hardware-safety bypasses.** Any code path that drives a setpoint,
  opens a purge, or commands a destructive operation **without** the
  required authorization gate or operator confirmation — see
  [`docs/safety/authorization-gates.md`](docs/safety/authorization-gates.md)
  and [`docs/safety/destructive-operations.md`](docs/safety/destructive-operations.md).
- **Shutdown / abort holes.** Conditions where `external_stop`, the
  saturation deadline, or an unhandled error fails to drive devices to a
  safe state — see
  [`docs/safety/shutdown-sequence.md`](docs/safety/shutdown-sequence.md).
- **Bundle-integrity issues.** Anything that lets a sealed bundle be
  silently mutated, lets the manifest disagree with the data, or
  bypasses the seal — bundles are the long-term record of what was
  measured.
- **Credential or PII leakage** in logs, manifests, or bundles.
- **Deserialisation of untrusted input** in config loaders, bundle
  readers, or plugin entry-point discovery.
- **Plugin-trust violations.** Any path where an untrusted plugin can
  execute outside the `ProcedureRegistry`'s sandbox or escalate beyond
  its declared capabilities — see
  [`docs/extending/plugin-system.md`](docs/extending/plugin-system.md).

Out of scope today: bugs that require local filesystem access on the
rig PC (capa assumes the operator owns that machine), and dependency
CVEs in `*lib` sibling libraries (report those upstream).

## Supported versions

capa is pre-alpha. Only the current `main` is supported; there are no
backported fixes to older commits. Pin to a commit if you depend on a
specific revision.
