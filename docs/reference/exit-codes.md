---
description: Exit-code contract for capa CLI verbs — how (run_status, bundle_status, integrity_status) manifest triple maps to 0/1/2/3/5 for shell pipelines and CI drivers.
---

# Exit codes

**Audience:** CI authors, scripters, anyone wiring `capa` into a shell pipeline or batch driver.
**Scope:** the exit code every `capa` subcommand can return, what it means, and how the run-bundle state model maps to a single integer.

The exit code is a *coarse* signal. The bundle's `manifest.json` is the source of truth — every `capa run` invocation records `run_status`, `bundle_status`, and `integrity_status` there, and downstream tooling that cares about the difference between "operator aborted" and "saturation deadline tripped" should read the manifest, not the exit code. The exit code exists so a shell driver can branch on the common cases without parsing JSON.

---

## The triple behind `capa run`

Every headless `capa run` exits according to the triple `(run_status, bundle_status, integrity_status)` recorded in the manifest. The mapping is defined by [`HeadlessResult.exit_code()`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/headless.py):

| Exit code | Triple | What happened |
|---|---|---|
| `0` | `run_status = "completed"`, `bundle_status = "sealed"` | The method ran to completion, finalize succeeded, the integrity hash was written. The bundle is safe to copy and archive. |
| `1` | `run_status = "aborted"` | The operator stopped the run (UI Abort button or `SIGINT`), an external stop event fired, or preflight refused before the run started (procedure not in the trusted registry, adapter materialization failed, resource conflict, pool failed to open). The bundle may or may not exist depending on how far preflight got. |
| `2` | `run_status = "crashed"` | The conductor's task group raised an unhandled exception. Finalize still ran, so a bundle exists on disk with whatever data was captured up to the crash; `bundle_status` may be `sealed` or `verification_failed`. Also returned by `capa run` itself when `ExperimentConfig.load(...)` fails *before* `run_headless` is reached — in that case nothing is written. |
| `3` | `bundle_status = "verification_failed"` (regardless of `run_status`) | The bundle finished finalize but the post-write integrity walk found a `manifest.sha256` mismatch. Data is readable; integrity is not trustworthy. See [Crash recovery → When to salvage versus discard](../troubleshooting/crash-recovery.md#when-to-salvage-versus-discard). |
| `5` | unhandled | Defensive fall-through for triples the mapping doesn't recognise. Should not fire in practice; if you see it, treat it as a bug and attach the manifest to the report. |

`bundle_status = "verification_failed"` short-circuits the mapping — a run that *completed* the method but then failed the integrity check exits `3`, not `0`. The integrity contract is a hard gate.

`crashed_but_sealed` (saturation deadline tripped, conductor still managed a clean finalize) maps to `run_status = "crashed"` → exit `2`. The bundle is sealed and readable, but the run is degraded — see [Saturation and deadlines](../safety/saturation-and-deadlines.md).

---

## Other subcommands

The validate / discover / inspect commands either complete cleanly or fail with a single error code. None of them produce a bundle.

| Command | Exit code(s) |
|---|---|
| `capa validate <config>` | `0` clean, `2` any error (config load, plugin contract, strict-mode handshake failure) |
| `capa config validate <config>` | `0` if no `error`-severity problems, `2` otherwise (warnings and info do not change the exit code) |
| `capa hardware validate <path>` | `0` clean, `2` any error or any `error`-severity problem |
| `capa hardware check <path>` | `0` clean, `2` any error or any `error`-severity problem (live handshake) |
| `capa hardware discover` | `0` clean (notes about per-adapter discovery failures are textual), `2` unknown `--adapter` |
| `capa hardware new <path>` | `0` clean, `2` refusing to overwrite or write failed |
| `capa devices discover` | `0` clean (same convention as `hardware discover`), `2` unknown `--adapter` |
| `capa method validate <path>` | `0` clean, `2` unsupported suffix or any error |
| `capa profile validate <config>` | `0` clean, `2` unknown profile or any error |
| `capa plugins list` | `0` always (rejected plugins are listed in the output, not signalled in the exit code) |
| `capa plugins trust <id>` | `0` clean, `2` missing `--reason`, plugin not found, or lockfile write failed |
| `capa catalog list` | `0` always |
| `capa catalog verify <run_id>` | `0` ok, `2` catalog error, `3` any mismatch |
| `capa catalog verify --all` | `0` every run ok, `3` any mismatch |
| `capa catalog verify` (no arg, no flag) | `64` usage error (`EX_USAGE` from `sysexits.h`) |
| `capa catalog rebuild` | `0` clean |
| `capa finalize <run_id>` | `0` clean, `2` bundle directory or manifest missing/malformed, `3` finalize raised `FinalizeError` |
| `capa gui [<config>]` | `0` clean window close (regardless of what individual runs did inside the GUI), `2` initial config refused at launch |
| `capa version` | `0` always |

`2` is the catch-all "something went wrong before the engine started" code; `3` is reserved for integrity-style failures (catalog mismatch, finalize error, manifest hash mismatch). The same code does *not* mean the same thing across commands — read the row, not the number.

---

## Signals

`SIGINT` (Ctrl-C, on every supported OS) is installed by [`install_sigint_handler`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/signals.py) when `capa run` enters its headless path:

- **First Ctrl-C.** Sets the external-stop event. The conductor drains, finalizes, and writes a sealed bundle. The process exits `1` (`run_status = "aborted"`).
- **Second Ctrl-C.** Reinstalls the OS default `SIGINT` disposition and re-raises. The process dies under the shell's `SIGINT` exit convention — typically `130` on POSIX (`128 + SIGINT`). No bundle finalize runs. The bundle is left in whatever state finalize had reached; recover with `capa finalize` (see [Crash recovery](../troubleshooting/crash-recovery.md)).

`SIGTERM` is not specifically handled. Python's default termination behaviour applies: the process dies under the OS default disposition, no finalize runs, the bundle is left open. If you need an orderly remote stop in a CI environment, send `SIGINT` instead.

The GUI handles its own quit signals through Qt's window-close machinery — closing the window from File→Quit or the title bar's X exits `0` cleanly. Sending `SIGINT` to a running GUI process is not a supported stop path; quit through the window.

---

## Exit-code patterns in a shell driver

The expected pattern for a headless capa caller is to branch on `0` versus everything else, and inspect the manifest for the rest:

```bash
if capa run "$CONFIG" --runs-root "$RUNS_ROOT"; then
    echo "sealed bundle in $RUNS_ROOT/$RUN_ID"
else
    rc=$?
    case "$rc" in
        1) echo "aborted (operator stop or preflight refusal)";;
        2) echo "crashed; finalize attempted (check manifest)";;
        3) echo "integrity check failed; bundle exists but is not trustworthy";;
        *) echo "unexpected exit code $rc";;
    esac
fi
```

For richer branching, parse `manifest.json` directly — `run_status`, `bundle_status`, `integrity_status`, and `manifest.custom["finalize_warnings"]` give the full picture that the exit code summarises.

---

## See also

- [Integrity and sealing](../bundles/integrity-and-sealing.md) — the bundle status state machine that the exit code reads from.
- [Crash recovery](../troubleshooting/crash-recovery.md) — what to do when the exit code is non-zero.
- [Saturation and deadlines](../safety/saturation-and-deadlines.md) — what `crashed_but_sealed` means and when to trust the data.
- [Aborting safely](../user-guide/aborting-safely.md) — operator-side stop guidance.
- [Environment variables](environment-variables.md) — env-var inputs to the commands listed above.
