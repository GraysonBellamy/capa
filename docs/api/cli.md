---
description: capa.cli API — Typer dispatcher and sub-apps behind the capa script entry point, covering headless runs, signal handling, and exit-code conventions.
---

# `capa.cli`

Typer dispatcher and sub-apps. The ``[project.scripts]`` entry point
calls :func:`capa.cli.main`.

**Narrative guides:**

- [CLI overview](../cli/overview.md) — the dispatcher + sub-app map.
- Per-command pages under [`docs/cli/`](../cli/overview.md).
- [Headless runs](../cli/headless-runs.md) — process model, signals,
  exit codes.
- [Exit codes reference](../reference/exit-codes.md).

::: capa.cli
