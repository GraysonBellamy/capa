# Contributing to capa

Thanks for your interest. capa is pre-alpha, single-maintainer, and
moves fast — read the docs before non-trivial changes.

## Where the real guide lives

The full contributor guide is in the [contributing section of the docs](docs/contributing/). Start here:

- **[Dev setup](docs/contributing/dev-setup.md)** — clone layout, sibling
  device libs, `uv sync`, running capa from source.
- **[Running tests](docs/contributing/running-tests.md)** — markers,
  common invocations, async patterns.
- **[Hardware tests](docs/contributing/hardware-tests.md)** — the
  `CAPA_HARDWARE_TESTS=1` opt-in tier.
- **[Code style](docs/contributing/code-style.md)** — ruff config and
  per-file ignores.
- **[Typing and mypy](docs/contributing/typing-and-mypy.md)** — strict
  baseline, override categories.
- **[Commits and PRs](docs/contributing/commit-and-pr.md)** — subject
  style, when to bundle vs. split, PR-body format.
- **[Release process](docs/contributing/release-process.md)** — what
  happens after merged PRs add up to a release.

## The 30-second version

1. Clone capa and its sibling device libs side-by-side (see [dev setup](docs/contributing/dev-setup.md#sibling-library-layout-the-non-obvious-gotcha)).
2. `uv sync --group dev`
3. Run the four-command dev loop before pushing:
   ```sh
   uv run ruff format
   uv run ruff check
   uv run mypy
   uv run pytest
   ```
4. Open a PR with the [PR template](.github/pull_request_template.md)
   filled in. CI runs the same four gates ([ci.yml](.github/workflows/ci.yml)).

## Reporting bugs

Use the [bug-report issue template](.github/ISSUE_TEMPLATE/bug_report.yml).
For security-sensitive reports, see [SECURITY.md](SECURITY.md).

## Safety-critical paths

Changes to `src/capa/runtime/saturation.py`, the
[authorization gate](docs/safety/authorization-gates.md), the
[shutdown sequence](docs/safety/shutdown-sequence.md), or the bundle
finalize/seal path get direct maintainer review — no auto-merge.

## License

By contributing you agree your contributions are licensed under the
[MIT License](LICENSE).
