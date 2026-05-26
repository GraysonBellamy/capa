---
description: Where capa release notes live and how bundle_schema_version is versioned independently — GitHub Releases, hatch-vcs tags, and bundle wire-format bumps.
---

# Changelog

**Audience:** anyone tracking what changed between capa versions.
**Scope:** where the human-readable change history lives and how the bundle wire-format is versioned independently.

## Where to find changes

Per-release notes live on the project's [GitHub Releases page](https://github.com/GraysonBellamy/capa/releases). Each release tag carries a summary of what changed since the previous one — bug fixes, new features, deprecations, breaking changes — in a format suitable for someone deciding whether to upgrade.

capa is currently `Development Status :: 2 - Pre-Alpha` and there are no released tags yet. Once the first tag lands, the Releases page becomes the canonical change record. Until then the unfiltered story is `git log` against [`GraysonBellamy/capa`](https://github.com/GraysonBellamy/capa).

The package version is produced dynamically by `hatch-vcs` from the most recent git tag. `capa version` at the CLI prints the installed version alongside the runtime revision string.

## Bundle wire format is versioned separately

The integer field `bundle_schema_version` inside every `manifest.json` is what downstream readers should branch on for compatibility — not the capa package version. The two move on different cadences: small capa releases that only change UI behaviour or fix bugs leave the bundle layout untouched, while a single bundle-layout change (the v1 → v2 in-flight transit format shift, for example) is a hard contract break for anyone reading bundles directly.

See [Bundle versioning](../bundles/bundle-versioning.md) for the bump policy, the migration registry, and the per-bump change log. Tool authors who only care about wire-format compatibility should subscribe to that page rather than this one.

## Promotion plan

When the project leaves pre-alpha and starts cutting tagged releases, this page will be promoted to a Keep-a-Changelog-style table summarising each release's headline changes, with bundle-schema bumps called out explicitly. Until that happens, this page stays a redirect so it does not drift out of sync with the actual release record.

## See also

- [GitHub Releases](https://github.com/GraysonBellamy/capa/releases) — the canonical change history (currently empty pre-alpha).
- [Bundle versioning](../bundles/bundle-versioning.md) — `bundle_schema_version` bump policy.
- [File formats](file-formats.md) — every on-disk format capa reads or writes.
