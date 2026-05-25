# Bundle versioning

**Audience:** tool authors reading bundles across capa versions; contributors who need to bump the schema.
**Scope:** how the bundle schema version is encoded, the bump policy, the migration registry contract, and the forward / backward compatibility guarantees.

The version lives in `manifest.json` as the top-level integer field `bundle_schema_version`. Every bundle written by the current capa carries the value `2`:

```json
{
  "run_id": "2026-05-24T13-02-11Z_abcd",
  "bundle_schema_version": 2,
  "...": "..."
}
```

The constant lives in [`schema.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/schema.py) as `BUNDLE_SCHEMA_VERSION`. `BundleManifest` imports that constant directly as the model default for new manifests; [`current_version()`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/schema.py) is a small helper for callers and tests that want the same value.

---

## Bump policy

- Every change to the bundle layout, the manifest schema, or the in-flight transit format gets a numeric bump. One bump per breaking change.
- There is no semver — bundles are data artifacts, not libraries. A single integer is enough.
- Old bundles remain first-class via the migration registry; a v(N) bundle stays readable by any capa that ships an unbroken chain of `MIGRATIONS[N], MIGRATIONS[N+1], ...` up to the current version.
- A bundle with a version newer than this capa rejects loudly with [`BundleSchemaError`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/schema.py). No silent best-effort read.

---

## The migration registry

The registry is a single module-level dict in [`schema.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/schema.py):

```python
Migration = Callable[[dict[str, Any]], dict[str, Any]]

MIGRATIONS: dict[int, Migration] = {}
# MIGRATIONS[N] upgrades a v(N) manifest dict to v(N+1).
```

Each entry maps a `from_version` to a pure dict-in/dict-out function that produces `from_version + 1`. [`migrate()`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/schema.py) walks the chain: it reads the recorded version from the dict, applies `MIGRATIONS[recorded]`, stamps `bundle_schema_version = recorded + 1`, and loops until the dict is current. If any intermediate step is missing the call raises `BundleSchemaError`.

The call site is [`BundleManifest.read()`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/manifest.py):

```python
@classmethod
def read(cls, path: str | Path) -> BundleManifest:
    with open(path, "rb") as fp:
        data = json.load(fp)
    migrated = migrate(data)
    return cls.model_validate(migrated)
```

Migrations run on the parsed JSON dict *before* Pydantic validation, so a migration is free to rename, drop, or default fields that the current `BundleManifest` model would otherwise reject under its `extra="forbid"` config.

---

## v1 → v2: a worked example of a bump

v1 wrote in-flight transit files as `*.in-flight.parquet`. v2 switched to Arrow IPC streaming with the suffix `*.in-flight.arrows`. Final on-disk artifacts — `scalars.parquet`, `device_records/*.parquet`, `<camera>.frames.parquet` — are unchanged. Only the *in-flight* format moved.

The bump was necessary because in-flight files of a v1 bundle that crashed before sealing cannot be recovered by v2 code without knowing the original transit format. The version field is the unambiguous signal of which on-disk shape to expect for those unsealed files. Final artifacts are untouched, so a v1 bundle that *did* seal would in principle be readable from its sealed parquet alone — but because the manifest itself only carries one version integer, the schema bump covers the whole bundle.

---

## Forward compatibility (newer capa reads older bundle)

Supported via the migration chain. `BundleManifest.read()` applies registered migrations *in memory* before validating, so a v(N) bundle on disk becomes a v(current) Pydantic model in the calling process.

**The on-disk file is not modified by a read.** Reading a v1 bundle a million times leaves all million bytes untouched. If you want to upgrade a bundle's on-disk shape, that's a separate offline tool — not something the read path does implicitly.

---

## Backward compatibility (older capa reads newer bundle)

Explicitly not supported. A v(N+1) bundle read by a capa that only knows v(N) raises `BundleSchemaError` with the message `manifest schema vN+1 is newer than this capa (supports vN); upgrade capa to read it`.

This is by design. A downstream tool that wants tolerance to forward schema changes should either:

1. **Pin the capa version it was tested against** and update on its own cadence, or
2. **Read `bundle_schema_version` directly from the raw JSON** as a feature gate before invoking `BundleManifest.read()` — bail out gracefully on unknown versions instead of crashing on the schema error.

There is no negotiation protocol. The bundle's version is a hard contract.

---

## What about v1 bundles in practice?

`MIGRATIONS` is empty in the current source. There is **no** registered v1 → v2 migration, so a v1 bundle on disk today will reject with `no migration registered from bundle schema v1 to v2`.

That is the intended state. v1 was a pre-release developer-machine format — capa hadn't shipped when v1 existed, so any stray v1 manifest is an artifact of an old working tree, not a real experiment. If you encounter one, the most reliable recovery is to re-run the experiment. The runs are short and the operator cost of replay is lower than the engineering cost of writing and validating a one-shot migration for a format that was never released.

---

## Author checklist: writing a v(N) → v(N+1) migration

1. Add the migration function to [`schema.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/schema.py) and register it as `MIGRATIONS[N] = _migrate_N_to_N_plus_1`.
2. The function takes a parsed manifest dict (post-`json.load`, pre-Pydantic-validate) and returns the upgraded dict. Pure: same input → same output.
3. Bump `BUNDLE_SCHEMA_VERSION` to `N+1`.
4. Add a test that loads a fixture v(N) bundle and confirms `BundleManifest.read()` produces a valid v(N+1) model in memory.
5. Update this page's "v(N-1) → v(N)" section with what changed and why the bump was necessary.
6. Update [`manifest-and-schema.md`](manifest-and-schema.md) with any new fields, removed fields, or renamed fields.

---

## Implementation notes for contributors

A few details about the registry that aren't obvious from the public API:

- **Migrations apply to the parsed manifest dict only.** Re-shaping on-disk files (rewriting a parquet schema, converting an in-flight transit file, repacking a camera container) is a separate offline tool, not a migration function. The registry's job is to make the *manifest* readable; data-file conversion is out of scope.
- **`migrate()` is pure.** Same input dict → same output dict. Side-effects beyond logging are forbidden. This keeps the function safe to call repeatedly and trivial to unit-test.
- **The chain is applied in memory on every read.** Reading a v1 bundle a million times never modifies the file. There is no caching or write-back — the cost is paid per read, which is fine because manifests are small.
- **Backward-compatible-at-the-source bumps want safe defaults.** If a v(N+1) bump only *adds* fields with Pydantic defaults, the migration function can be a near-no-op (just stamp the new version) — the model defaults backfill the missing keys. The Pydantic model is the real contract; the migration just gets the dict past the version check.

---

*See also:* [Manifest and schema](manifest-and-schema.md) · [Integrity and sealing](integrity-and-sealing.md) · [What's in a bundle](what-is-a-bundle.md)
