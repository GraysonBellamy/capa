# Example plugins

Minimal, runnable example packages that back the tutorials under
[`docs/extending/`](../../docs/extending/). Each subdirectory is a
self-contained installable Python distribution: install it with
`uv pip install -e examples/plugins/<name>` and the capa engine will
discover it through the normal entry-point mechanism.

| Package | Plugin kind | Backs which tutorial |
|---|---|---|
| [`hello_procedure/`](hello_procedure/) | `capa.procedures` | [Writing a procedure](../../docs/extending/writing-a-procedure.md) |
| [`drying_profile/`](drying_profile/) | `capa.profiles` | [Writing a profile](../../docs/extending/writing-a-profile.md) |

These are not shipped with capa itself — they exist so the docs have a
concrete artifact to point at, and so the tutorials can be exercised in
isolation without inventing a fresh plugin from scratch.
