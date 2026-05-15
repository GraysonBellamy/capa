"""Tests for the typer CLI in :mod:`capa.cli`.

Drives the CLI in-process via :class:`typer.testing.CliRunner` so failures
surface cleanly without spawning a subprocess.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from capa.cli import app
from capa.storage.catalog import RunCatalog

_FREE_RUN_TOML = """
procedure = {{ id = "capa.builtin.free_run", config = {{ duration_s = {duration} }} }}
calibration_set = {{ name = "default" }}
operator = {{ id = "abr", display_name = "A. R." }}
sample = {{ id = "{sample_id}" }}

hardware = "hardware.toml"
"""

_HARDWARE_TOML = """
name = "tiny"
[[devices]]
name = "heater"
adapter = "capa.devices.sim.watlow_sim"
"""


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def example_config(tmp_path: Path) -> Path:
    (tmp_path / "experiment.toml").write_text(
        _FREE_RUN_TOML.format(duration=0.05, sample_id="CLI-1"),
        encoding="utf-8",
    )
    (tmp_path / "hardware.toml").write_text(_HARDWARE_TOML, encoding="utf-8")
    return tmp_path / "experiment.toml"


class TestValidate:
    def test_clean_config_returns_zero(self, runner: CliRunner, example_config: Path) -> None:
        result = runner.invoke(app, ["validate", str(example_config)])
        assert result.exit_code == 0, result.stdout
        assert "OK:" in result.stdout

    def test_strict_loads_adapter_module(self, runner: CliRunner, example_config: Path) -> None:
        result = runner.invoke(app, ["validate", "--strict", str(example_config)])
        assert result.exit_code == 0, result.stdout
        assert "WatlowSim" in result.stdout

    def test_missing_file_returns_nonzero(self, runner: CliRunner, tmp_path: Path) -> None:
        result = runner.invoke(app, ["validate", str(tmp_path / "absent.yaml")])
        assert result.exit_code != 0

    def test_strict_invokes_watlow_handshake(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``validate --strict`` calls ``watlow.handshake`` for the real adapter,
        which goes through ``watlowlib.open_device``. We monkeypatch
        ``watlowlib.open_device`` to return an in-process stub so the test
        exercises the handshake without a serial port."""
        import watlowlib

        from tests._watlow_stub import StubWatlowController

        stub = StubWatlowController(signals={("process_value", 1): 100.0})

        async def fake_open_device(*args: object, **kwargs: object) -> object:
            del args, kwargs
            return stub

        monkeypatch.setattr(watlowlib, "open_device", fake_open_device)

        (tmp_path / "hardware.toml").write_text(
            """
name = "real"
[[devices]]
name = "heater"
adapter = "capa.devices.watlow"
[devices.params]
port = "fake://stub"
""",
            encoding="utf-8",
        )
        (tmp_path / "experiment.toml").write_text(
            _FREE_RUN_TOML.format(duration=0.05, sample_id="CLI-WL-1"),
            encoding="utf-8",
        )
        result = runner.invoke(app, ["validate", "--strict", str(tmp_path / "experiment.toml")])
        assert result.exit_code == 0, result.stdout
        # The handshake summary is what we want to see, not the import-only line
        assert "PM3C1AJ-AAAAAAA" in result.stdout
        assert "no handshake hook" not in result.stdout


class TestRun:
    def test_headless_seals_bundle(
        self, runner: CliRunner, example_config: Path, tmp_path: Path
    ) -> None:
        runs = tmp_path / "runs"
        result = runner.invoke(
            app,
            ["run", "--headless", "--runs-root", str(runs), str(example_config)],
        )
        assert result.exit_code == 0, result.stdout + (result.stderr or "")
        assert "bundle_status:    sealed" in result.stdout
        assert "integrity_status: ok" in result.stdout
        bundles = [p for p in runs.iterdir() if p.is_dir() and p.name != "runs.sqlite"]
        bundles = [p for p in bundles if (p / "manifest.json").exists()]
        assert len(bundles) == 1


class TestPluginsLockAutoDiscovery:
    """Hardware-day §5.4 anomaly: production mode without ``--plugins-lock``
    silently fell back to dev-mode behavior. The CLI must auto-discover
    ``./plugins.lock`` (or ``$XDG_CONFIG_HOME/capa/plugins.lock``) and
    fail loudly when neither exists.
    """

    def test_production_mode_fails_loudly_without_any_lock(
        self,
        runner: CliRunner,
        example_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Ensure a clean slate: no cwd lock, no XDG lock, no $HOME lock.
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-xdg"))
        monkeypatch.setenv("HOME", str(tmp_path / "no-home"))
        monkeypatch.setenv("CAPA_PLUGIN_MODE", "production")

        result = runner.invoke(
            app,
            [
                "run",
                "--headless",
                "--runs-root",
                str(tmp_path / "runs"),
                str(example_config),
            ],
        )
        assert result.exit_code == 2, result.stdout + (result.stderr or "")
        combined = result.stdout + (result.stderr or "")
        assert "production plugin mode requires a plugins.lock" in combined

    def test_production_mode_auto_discovers_cwd_lock(
        self,
        runner: CliRunner,
        example_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Trust the builtin recipe runner so the auto-discovered lock has
        # *something* in it; the run itself uses free_run, so the recipe
        # entry is irrelevant for the run path but lock-load itself must
        # succeed.
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("CAPA_PLUGIN_MODE", "production")

        # Trust *the* free_run plugin so the run is gated against a real
        # lock entry. Use the plugins trust subcommand to populate it
        # against the real installed metadata.
        trust = runner.invoke(
            app,
            [
                "plugins",
                "trust",
                "capa.builtin.free_run",
                "--reason",
                "auto-discovery test",
            ],
        )
        assert trust.exit_code == 0, trust.stdout

        result = runner.invoke(
            app,
            [
                "run",
                "--headless",
                "--runs-root",
                str(tmp_path / "runs"),
                str(example_config),
            ],
        )
        assert result.exit_code == 0, result.stdout + (result.stderr or "")
        # The auto-discovery line must surface the resolved path so
        # operators see exactly which lock was honored.
        assert "plugins.lock (auto-discovered):" in result.stdout
        assert "plugins.lock" in result.stdout
        assert "bundle_status:    sealed" in result.stdout

    def test_dev_mode_quiet_when_no_lock(
        self,
        runner: CliRunner,
        example_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Default mode is dev — no lock required, no auto-discovery banner.
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("CAPA_PLUGIN_MODE", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-xdg"))
        monkeypatch.setenv("HOME", str(tmp_path / "no-home"))

        result = runner.invoke(
            app,
            [
                "run",
                "--headless",
                "--runs-root",
                str(tmp_path / "runs"),
                str(example_config),
            ],
        )
        assert result.exit_code == 0, result.stdout + (result.stderr or "")
        assert "auto-discovered" not in result.stdout


class TestCatalog:
    def test_list_after_run(self, runner: CliRunner, example_config: Path, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runner.invoke(
            app,
            ["run", "--headless", "--runs-root", str(runs), str(example_config)],
        )
        result = runner.invoke(app, ["catalog", "list", "--runs-root", str(runs)])
        assert result.exit_code == 0, result.stdout
        assert "completed" in result.stdout
        assert "sealed" in result.stdout
        assert "ok" in result.stdout

    def test_list_json(self, runner: CliRunner, example_config: Path, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runner.invoke(
            app,
            ["run", "--headless", "--runs-root", str(runs), str(example_config)],
        )
        result = runner.invoke(app, ["catalog", "list", "--runs-root", str(runs), "--json"])
        assert result.exit_code == 0
        import json

        rows = json.loads(result.stdout)
        assert isinstance(rows, list) and len(rows) == 1
        assert rows[0]["run_status"] == "completed"

    def test_verify_all_after_run(
        self, runner: CliRunner, example_config: Path, tmp_path: Path
    ) -> None:
        runs = tmp_path / "runs"
        runner.invoke(
            app,
            ["run", "--headless", "--runs-root", str(runs), str(example_config)],
        )
        result = runner.invoke(app, ["catalog", "verify", "--runs-root", str(runs), "--all"])
        assert result.exit_code == 0
        assert ": ok" in result.stdout

    def test_rebuild_after_drop(
        self, runner: CliRunner, example_config: Path, tmp_path: Path
    ) -> None:
        runs = tmp_path / "runs"
        runner.invoke(
            app,
            ["run", "--headless", "--runs-root", str(runs), str(example_config)],
        )
        (runs / "runs.sqlite").unlink()
        result = runner.invoke(app, ["catalog", "rebuild", "--runs-root", str(runs)])
        assert result.exit_code == 0
        assert "1 run(s) indexed" in result.stdout
        with RunCatalog(runs) as cat:
            assert len(cat.list()) == 1


class TestFinalize:
    def test_idempotent_on_already_sealed(
        self, runner: CliRunner, example_config: Path, tmp_path: Path
    ) -> None:
        runs = tmp_path / "runs"
        runner.invoke(
            app,
            ["run", "--headless", "--runs-root", str(runs), str(example_config)],
        )
        bundle = next(p for p in runs.iterdir() if p.is_dir() and (p / "manifest.json").exists())
        result = runner.invoke(
            app,
            ["finalize", bundle.name, "--runs-root", str(runs)],
        )
        assert result.exit_code == 0, result.stdout
        assert "finalized:" in result.stdout


class TestVersion:
    def test_prints_version(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["version"])
        assert result.exit_code == 0
        assert "runtime" in result.stdout


@pytest.fixture
def _stub_discover(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force every non-camera adapter's discovery hook to return ``[]``.

    Lets the CLI test exercise the "no devices found" code path
    independently of whatever hardware happens to be attached to the
    machine running the suite (a real Watlow at COM6 will otherwise
    make the suite flaky on the lab bench).
    """
    import importlib

    from capa.devices.discovery import discoverable_descriptors

    async def empty_discover(**_kwargs: object) -> list[dict[str, object]]:
        return []

    for descriptor in discoverable_descriptors(include_cameras=False):
        try:
            module = importlib.import_module(descriptor.id)
        except ImportError:
            continue
        if hasattr(module, "discover"):
            monkeypatch.setattr(module, "discover", empty_discover)


class TestDevicesDiscover:
    def test_no_hardware_returns_zero(self, runner: CliRunner, _stub_discover: None) -> None:
        # Every probe stubbed to return empty; the command still exits
        # cleanly with a "no devices" message rather than a non-zero exit.
        result = runner.invoke(app, ["devices", "discover"])
        assert result.exit_code == 0, result.stdout
        assert "no devices" in result.stdout.lower()

    def test_json_output_is_valid(self, runner: CliRunner, _stub_discover: None) -> None:
        import json as _json

        result = runner.invoke(app, ["devices", "discover", "--json"])
        assert result.exit_code == 0
        payload = _json.loads(result.stdout)
        assert "devices" in payload
        assert isinstance(payload["devices"], list)
        assert "notes" in payload

    def test_unknown_adapter_returns_nonzero(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["devices", "discover", "--adapter", "made_up"])
        assert result.exit_code != 0
        assert (
            "unknown adapter" in result.stderr.lower() or "unknown adapter" in result.stdout.lower()
        )

    def test_specific_adapter_filters(self, runner: CliRunner, _stub_discover: None) -> None:
        # Asking for "alicat" specifically should not surface notes for
        # nidaq / sartorius / watlow.
        result = runner.invoke(app, ["devices", "discover", "--adapter", "alicat"])
        assert result.exit_code == 0
        assert "nidaq" not in result.stdout.lower()
        assert "watlow" not in result.stdout.lower()
