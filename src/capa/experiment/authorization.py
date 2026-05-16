"""Authorization — tracks operator and run-arm authorization for device writes.

#12: every device write is attributable. ``issued_by`` plus a
run-arm ``authorization_id`` covers procedure/method-driven commands; manual
overrides go through :meth:`Authorization.confirm_manual` which adds a
``confirmed_by`` operator id stamp.

Lifecycle:

* :meth:`Authorization.arm` is called once when a run is armed. It mints a
  fresh ``authorization_id`` (a short ULID-shaped string) tied to the run id
  and the operator who armed.
* :meth:`Authorization.issue` produces a :class:`DeviceCommand` with
  ``issued_by`` = caller, ``authorization_id`` = the armed id; this is the
  path procedures and ``MethodExecutor`` take.
* :meth:`Authorization.issue_manual` produces a command with
  ``authorization_id=None`` and a required ``confirmed_by`` — the UI uses this
  for an operator-initiated override mid-run.

The runtime check that an adapter actually demands one of those stamps lives
on the adapter side (the generic :class:`DeviceCommand` requires ``issued_by``
via Pydantic; the manual-vs-armed semantics are enforced here so a procedure
cannot accidentally send an unauthenticated command).
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Any

from capa.core.errors import CapaError
from capa.devices.adapter import DeviceCommand


class AuthorizationError(CapaError):
    """Raised when a command would be issued without coverage.

    Distinct from :class:`~capa.core.errors.AdapterError` so a missing
    authorization stamp is recognised as a security/audit issue rather than a
    device fault.
    """


def _mint_authorization_id() -> str:
    """Short opaque token. Eight bytes of urandom hex — 64 bits is plenty for
    distinguishing arms within one rig, and it's short enough to fit in a
    log line."""
    return secrets.token_hex(8)


@dataclass(slots=True)
class Authorization:
    """Run-arm authorization handle.

    Constructed once per run. Procedures and :class:`MethodExecutor` issue
    commands through this object so every device write carries the right
    audit fields.
    """

    operator_id: str
    """Operator who armed the run. Stamped as ``issued_by`` on every
    procedure/method command unless the caller passes a different
    ``issued_by`` override."""

    run_id: str
    """Run id this authorization is bound to. Recorded into the manifest's
    audit trail and into every command's metadata."""

    authorization_id: str = field(default_factory=_mint_authorization_id)
    """Stable id minted at construction. Matches the bundle's
    ``run_authorization_id`` field (recorded by the engine at run-open)."""

    armed: bool = True
    """``False`` once :meth:`disarm` is called. After that, :meth:`issue`
    raises — only manual overrides remain valid (and those still require a
    fresh ``confirmed_by``)."""

    def disarm(self) -> None:
        """Revoke the run-arm authorization.

        Called by the engine in its ``finally`` block so a stray procedure
        task that survives shutdown cannot keep issuing commands. Idempotent.
        """
        self.armed = False

    def issue(
        self,
        *,
        kind: str,
        target: str | None = None,
        payload: dict[str, Any] | None = None,
        issued_by: str | None = None,
    ) -> DeviceCommand:
        """Build a :class:`DeviceCommand` covered by the run-arm authorization.

        Raises :class:`AuthorizationError` if the run has been disarmed.
        ``issued_by`` defaults to :attr:`operator_id`; a procedure that wants
        to attribute a command to a sub-role (e.g. ``"safety_monitor"``) can
        override.
        """
        if not self.armed:
            raise AuthorizationError(
                "run authorization is disarmed; manual overrides require confirm_manual()"
            )
        return DeviceCommand(
            kind=kind,
            target=target,
            payload=payload or {},
            issued_by=issued_by or self.operator_id,
            authorization_id=self.authorization_id,
            confirmed_by=None,
        )

    def issue_manual(
        self,
        *,
        kind: str,
        target: str | None = None,
        payload: dict[str, Any] | None = None,
        issued_by: str,
        confirmed_by: str,
    ) -> DeviceCommand:
        """Build a manual-override command.

        Used by the UI when an operator pushes a button outside any method
        step ("nudge setpoint up by 10 K"). ``authorization_id`` is left
        ``None`` so the audit trail clearly shows this was not a planned
        method/procedure command. Both ``issued_by`` (who initiated) and
        ``confirmed_by`` (who confirmed at the dialog) are required.
        """
        if not issued_by or not confirmed_by:
            raise AuthorizationError("manual override requires both issued_by and confirmed_by")
        return DeviceCommand(
            kind=kind,
            target=target,
            payload=payload or {},
            issued_by=issued_by,
            authorization_id=None,
            confirmed_by=confirmed_by,
        )


__all__ = ["Authorization", "AuthorizationError"]
