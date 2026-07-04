---
description: capa release-cutting — `hatch-vcs` version derivation from git tags, pre-release checklist, annotated tagging, GitHub release notes, docs auto-deploy.
---

# Release process

**Audience:** maintainers cutting a release.
**Scope:** the version-derivation model (`hatch-vcs`), the pre-release checklist, tagging, GitHub release notes, and the docs auto-deploy. CAPA has not yet cut a versioned release — there are no tags on `main` as of this writing. This page documents the intended process; the first real release will inevitably refine it.

CAPA is pre-alpha. The runtime API in `capa.runtime`, `capa.devices`, and `capa.config` is explicitly unstable (see the [project README](https://github.com/GraysonBellamy/capa/blob/main/README.md)). Releases are versioned milestones but carry no backwards-compatibility promise yet — pin to a commit if you are building against capa today, not to a version range.

---

## Versioning — `hatch-vcs`

[pyproject.toml](../../pyproject.toml#L91-L93) declares:

```toml
[tool.hatch.version]
source = "vcs"
fallback-version = "0.0.0"
```

The package version is **derived from the most recent git tag**, not hand-edited. There is no `__version__ = "0.3.0"` line anywhere in `src/capa/`. Build behavior:

| Repo state | Resulting version |
|---|---|
| At an annotated tag `v0.3.0` | `0.3.0` |
| 5 commits past `v0.3.0` | `0.3.1.dev5+g<sha>` |
| No tags at all (current state) | `0.0.0` (the `fallback-version`) |
| Past `v0.3.0`, working tree dirty | `0.3.1.dev5+g<sha>.d<date>` |

This is enforced by hatchling at wheel build time (`uv build` or `hatchling build`). The same logic powers `capa.__version__` at runtime, which the GUI displays in the welcome panel.

**Implication:** you don't bump a version by editing a file. You bump it by creating an annotated tag.

---

## Pre-release checklist

Before tagging, run the full local gate plus a manual GUI smoke:

```powershell
git fetch
git status                                # clean working tree
git log --oneline origin/main..HEAD       # nothing local that hasn't landed
uv sync --group dev --group docs
uv run ruff format --check
uv run ruff check
uv run mypy
uv run pytest
uv run pytest -m smoke                    # explicit re-run; covers the headless e2e
uv run zensical build --clean             # confirm docs build cleanly
uv run capa gui configs/experiments/sim_freerun.yaml   # manual: GUI launches, run completes, bundle seals
```

If hardware is available, run the relevant per-vendor smoke test from [Hardware tests](hardware-tests.md). Don't gate a release on hardware tests for vendors the rig PC doesn't currently have wired up.

If anything fails, **don't tag** — fix it on `main` first.

---

## Tagging

Use annotated tags (`git tag -a`); `hatch-vcs` reads annotated tags by default.

```powershell
git tag -a v0.1.0 -m "v0.1.0"
git push origin v0.1.0
```

Tag naming: `v<major>.<minor>.<patch>`, no suffix. PEP-440 pre-release suffixes (`v0.1.0a1`, `v0.1.0rc1`) work with hatch-vcs if a true pre-release becomes useful, but for pre-alpha the simple `vX.Y.Z` form is enough.

Push the tag *after* the corresponding commit is on `main` — don't tag a local-only commit.

---

## Build the wheel (optional)

Only required if publishing to PyPI, which capa does not currently do. For a local sanity check that the wheel builds at the new version:

```powershell
uv build
```

The output appears under `dist/` and the filename reflects the tag-derived version. PyPI publication is deliberately deferred until the runtime API stabilizes.

---

## GitHub release notes

CAPA does **not** maintain a `CHANGELOG.md` in the repo. GitHub Releases are the changelog. For each release:

1. Visit `https://github.com/GraysonBellamy/capa/releases/new` and select the tag you just pushed.
2. Title: `v0.1.0` (match the tag).
3. Body: a summary derived from the commit log between this tag and the previous one. Useful starting command:
   ```powershell
   git log v0.0.0..v0.1.0 --oneline --no-merges
   ```
   (Substitute the actual previous tag; for the very first release, list highlights instead of a diff.)
4. Structure the body as three short sections — **Highlights**, **Breaking changes**, **Internals** — and call out anything operators or plugin authors would feel.
5. Mark the release as "pre-release" until the project leaves pre-alpha.

A future `docs/reference/changelog.md` may emerge once Phase 12 of [`DOCS_AUTHORING_PLAN.md`](https://github.com/GraysonBellamy/capa/blob/main/DOCS_AUTHORING_PLAN.md) is done; for now that page is intended to redirect to GitHub Releases.

---

## Docs auto-deploy

[.github/workflows/docs.yml](../../.github/workflows/docs.yml) deploys [https://capa.graysonbellamy.dev/](https://capa.graysonbellamy.dev/) on **every push to `main`** — not on tag creation. This means:

- A release tag does not by itself trigger a docs rebuild. The merge commit that the tag points at already triggered one.
- Tagging never blocks a docs deploy or unblocks one.
- Versioned docs (per-tag docs trees) are not currently published. The deployed site is always the head of `main`.

If a release's documentation needs work, land the docs PR before tagging — the tag should describe code that matches the live docs.

---

## After the release

- Announce on whatever channel the project is using (today: the project README is the only public surface).
- Open an issue for any follow-up work the release notes called out so it doesn't get lost.
- If the release surfaced a process gap (a pre-flight check that should have caught something), update *this page* before the gap is forgotten.

---

## Where to go next

- [Commits and PRs](commit-and-pr.md) — what landed during the release window.
- [`DOCS_AUTHORING_PLAN.md`](https://github.com/GraysonBellamy/capa/blob/main/DOCS_AUTHORING_PLAN.md) — the docs roadmap; releases gate when the public-launch milestone in there can be claimed.
