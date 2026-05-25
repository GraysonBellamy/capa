# Authorization gates

**Audience:** plugin authors writing procedures, method steps, or manual controls that issue commands to devices.
**Scope:** the [`Authorization`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/authorization.py) helper — the single chokepoint every device write passes through so that `DeviceCommand.issued_by`, `authorization_id`, and `confirmed_by` carry the audit trail described in [safety principle 1](principles.md#invariant-1-every-device-write-is-attributable).

If you find yourself constructing a `DeviceCommand` by hand, you are doing it wrong. The `Authorization` API is the only correct way to mint one.

---

## The three command shapes

Every command lands in the bundle event log with three audit fields. Only two combinations of those fields are valid:

| Provenance | `issued_by` | `authorization_id` | `confirmed_by` | Method to call |
|---|---|---|---|---|
| Procedure / method step | operator id | run-arm id (8-byte hex) | `None` | `Authorization.issue()` |
| Manual operator override | operator id | `None` | operator id | `Authorization.issue_manual()` |

Any other shape — `issued_by` missing, `authorization_id` AND `confirmed_by` both `None`, or `confirmed_by` set without `issued_by` — is rejected. `Authorization.issue_manual()` enforces the `confirmed_by` requirement directly; the rest is enforced by Pydantic on `DeviceCommand` itself.

The bundle's `run_authorization_id` field (recorded by the engine at run-open) matches the `authorization_id` minted by `Authorization.arm()`. Auditing a bundle later, you can correlate every procedure-issued command back to a single run-arm event.

---

## Lifecycle

The `Authorization` instance lives for the duration of one run and one only.

1. **Arm.** The engine constructs `Authorization(operator_id=..., run_id=...)` once when a run is armed. Construction mints a fresh 8-byte hex `authorization_id` via [`secrets.token_hex(8)`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/authorization.py).
2. **Issue.** During the run, the [`MethodExecutor`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/executor.py) and any [`Procedure`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/procedures/base.py) issue commands through `ctx.authorization.issue(...)`. The `issued_by` field defaults to the arming operator id; a procedure that wants to attribute a command to a sub-role (e.g. `"safety_monitor"`) can override.
3. **Disarm.** When the run ends — by any path, including [saturation deadline](../glossary.md#saturation-deadline) and unhandled exception — the [conductor](../glossary.md#conductor)'s `finally` block calls `auth.disarm()`. After that, `issue()` raises `AuthorizationError`. Only `issue_manual()` remains valid, and only with a fresh `confirmed_by`.

`disarm()` is idempotent and the conductor calls it both via `_disarm_unconditional()` on early failures and via the normal drain path on success. A stray procedure task that survives shutdown cannot keep issuing commands.

---

## Issuing from a procedure or custom step

The canonical pattern lives in [`MethodExecutor._command_setpoint`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/executor.py). Reduced to the essentials:

```python
async def _command_setpoint(self, channel: str, value: float) -> None:
    cmd = self.ctx.authorization.issue(
        kind="set_setpoint",
        target=channel,
        payload={"value": value, "channel": channel},
    )
    await self.ctx.dispatcher.dispatch(device_name, cmd)
```

Three things are worth calling out:

- `ctx.authorization` is always present on a [`ProcedureContext`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/procedures/base.py) during a run. You do not construct it yourself.
- `ctx.dispatcher.dispatch(...)` is the concurrency-agnostic dispatch surface — it wires to `AdapterDispatcher` for single-loop engine runs and `ConductorDispatcher` for per-resource-worker runs. Procedures do not need to know which.
- `kind` is a string the adapter understands (`"set_setpoint"`, `"write_parameter"`, etc.). The full set is adapter-specific; see [`docs/devices/`](../devices/overview.md) for each adapter's vocabulary.

If `issue()` raises `AuthorizationError` your procedure is already in a teardown path. Let the exception propagate — the conductor's `finally` block will still run, and the run will end with the appropriate `RunOutcome`.

---

## Manual overrides from the UI

Manual controls in the [Manual Control dock](../user-guide/manual-controls.md) need to issue commands *outside* any procedure — the operator clicks a button to nudge a setpoint or to reset a flow totalizer. These commands cannot use the run-arm authorization (there may be no run armed; even if one is, manual writes are not attributable to the procedure).

The pattern: construct a one-off `Authorization` per dispatch, with `run_id="manual"`, and call `issue_manual()`. From [`manual/cards/base.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/ui/manual/cards/base.py):

```python
auth = Authorization(operator_id=operator, run_id="manual")
cmd = auth.issue_manual(
    kind=kind,
    target=target,
    payload=payload or {},
    issued_by=operator,
    confirmed_by=operator,
)
```

`issued_by` and `confirmed_by` are required and may not be empty. When the dispatch is a destructive verb (factory reset, EEPROM write, valve close, etc.), the parent card's `dispatch()` helper additionally gates this through a `QMessageBox` confirmation *before* constructing the authorization — see [destructive operations](destructive-operations.md).

For an emergency-but-clickable control (the Run tab's Emergency Stop), the [`HoldToConfirmButton`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/ui/hold_to_confirm.py) provides a third layer on top: a 1-second hold requirement before the click signal fires at all.

---

## What denial looks like

`AuthorizationError` is a subclass of `CapaError`, distinct from `AdapterError`, so audit failures are visible as security/policy issues rather than device faults.

Two conditions raise it:

1. **`issue()` after `disarm()`.** Message: `"run authorization is disarmed; manual overrides require confirm_manual()"`. Indicates a procedure task survived shutdown — usually a sign of a `CustomStep` that isn't honouring `external_stop`.
2. **`issue_manual()` without `issued_by` or without `confirmed_by`.** Message: `"manual override requires both issued_by and confirmed_by"`. Indicates a UI code path that bypassed the operator-id gate.

Neither condition crashes the run by itself. The exception propagates to the caller; the conductor records it as part of the normal shutdown sequence and the run seals with a [`crashed` outcome](../glossary.md#bundle-outcome) (unhandled exception) or `aborted` (if it happened during an operator-initiated stop). The bundle's event log will contain the `AuthorizationError` text for post-hoc audit.

---

## What's NOT enforced here

The `Authorization` API enforces *attribution*. It does not enforce:

- **Which operator is allowed to arm a run.** There is no operator allowlist today — anyone with a valid `operator_id` config entry can arm. Operator-id provenance is a config concern, not an `Authorization` concern.
- **Which commands a given operator is allowed to issue.** All commands an adapter advertises are equally available to any armed operator. Role-based access control is not a CAPA feature.
- **Rate-limiting.** A procedure that issues a thousand setpoint changes per second will succeed at issuing them; the adapter or downstream queueing decides what happens next.

Those are intentional omissions. CAPA's safety contract is about *traceability and clean shutdown*, not access control. See [safety principles, invariant 4](principles.md#invariant-4-capa-is-not-a-hard-real-time-control-system).

---

*See also:* [Safety principles](principles.md) · [Destructive operations](destructive-operations.md) · [Manual controls](../user-guide/manual-controls.md)
