# Reference

Lookup material. Nothing in this tab tells you *how* to do anything —
go to [Use capa](../user-guide/operator-handbook.md) for that. This is
where you come when you already know what you're trying to do and
need to check a name, a code, a format, or a variable.

<div class="grid cards" markdown>

-   :lucide-book-open:{ .lg .middle } &nbsp; **[Glossary](../glossary.md)**

    ---

    Plain-English definitions of every term capa's docs use as a
    keyword: bundle, channel, procedure, profile, conductor, worker,
    bridge, saturation deadline.

-   :lucide-file-code-2:{ .lg .middle } &nbsp; **[File formats](file-formats.md)**

    ---

    Every on-disk format capa reads or writes — YAML, TOML, Parquet,
    SQLite, JSON, MKV, CSQ — with a pointer to the schema doc for
    each.

-   :lucide-terminal:{ .lg .middle } &nbsp; **[Environment variables](environment-variables.md)**

    ---

    Runtime knobs capa honors via the process environment — debug
    toggles, probe activation, storage roots, hardware overrides.

-   :lucide-x-octagon:{ .lg .middle } &nbsp; **[Exit codes](exit-codes.md)**

    ---

    What each non-zero exit from `capa run`, `capa validate`, and the
    other CLI verbs means, and what to do about it.

-   :lucide-history:{ .lg .middle } &nbsp; **[Changelog](changelog.md)**

    ---

    Per-release notes — runtime protocol revisions, breaking config
    changes, bundle schema migrations.

</div>

## Related lookup pages elsewhere

A few cross-cutting references live in their own tabs because they're
more useful next to their context:

- **CLI verbs** — see [CLI overview](../cli/overview.md) and the per-verb pages under it.
- **Method steps** — see [Method step reference](../procedures/method-steps-reference.md).
- **API surface** — see [API reference](../api/index.md).
- **Manifest schema** — see [Manifest and schema](../bundles/manifest-and-schema.md).
