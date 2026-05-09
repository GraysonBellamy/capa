"""Tests for :mod:`capa.experiment.authorization`."""

from __future__ import annotations

import pytest

from capa.experiment.authorization import Authorization, AuthorizationError


def test_arm_mints_distinct_ids() -> None:
    a = Authorization(operator_id="abr", run_id="r1")
    b = Authorization(operator_id="abr", run_id="r2")
    assert a.authorization_id != b.authorization_id
    assert len(a.authorization_id) == 16  # 8 bytes hex


def test_issue_stamps_command_with_run_arm() -> None:
    auth = Authorization(operator_id="abr", run_id="r1")
    cmd = auth.issue(kind="set_setpoint", target="heater.sp", payload={"value": 400.0})
    assert cmd.issued_by == "abr"
    assert cmd.authorization_id == auth.authorization_id
    assert cmd.confirmed_by is None
    assert cmd.payload == {"value": 400.0}


def test_issue_can_override_issued_by_for_subroles() -> None:
    auth = Authorization(operator_id="abr", run_id="r1")
    cmd = auth.issue(kind="set_setpoint", issued_by="safety_monitor")
    assert cmd.issued_by == "safety_monitor"
    assert cmd.authorization_id == auth.authorization_id


def test_disarm_blocks_subsequent_issue() -> None:
    auth = Authorization(operator_id="abr", run_id="r1")
    auth.disarm()
    with pytest.raises(AuthorizationError, match="disarmed"):
        auth.issue(kind="set_setpoint")


def test_disarm_is_idempotent() -> None:
    auth = Authorization(operator_id="abr", run_id="r1")
    auth.disarm()
    auth.disarm()  # no-op
    assert auth.armed is False


def test_issue_manual_requires_both_ids() -> None:
    auth = Authorization(operator_id="abr", run_id="r1")
    with pytest.raises(AuthorizationError, match="both"):
        auth.issue_manual(kind="set_setpoint", issued_by="", confirmed_by="abr")
    with pytest.raises(AuthorizationError, match="both"):
        auth.issue_manual(kind="set_setpoint", issued_by="abr", confirmed_by="")


def test_issue_manual_works_after_disarm() -> None:
    """Manual overrides survive disarm — they carry their own confirm trail."""
    auth = Authorization(operator_id="abr", run_id="r1")
    auth.disarm()
    cmd = auth.issue_manual(
        kind="set_setpoint",
        issued_by="abr",
        confirmed_by="supervisor",
    )
    assert cmd.authorization_id is None
    assert cmd.issued_by == "abr"
    assert cmd.confirmed_by == "supervisor"
