"""``capa`` CLI root — Typer dispatcher wiring every sub-app together.

Entry point: ``capa = "capa.cli:main"`` in ``pyproject.toml``. Each
sub-command's body lives in a sibling module so the dispatcher stays
small; the modules export sub-Typer apps (``hardware_app`` etc.) or
plain command callbacks that are registered here.
"""

from __future__ import annotations

import sys

import typer

from capa import __version__ as capa_version
from capa.cli.catalog import catalog_app
from capa.cli.config_cmd import config_app
from capa.cli.devices import devices_app
from capa.cli.hardware import hardware_app
from capa.cli.method_profile import method_app, profile_app
from capa.cli.plugins import plugins_app
from capa.cli.run import finalize, gui, run
from capa.cli.validate import validate
from capa.runtime import RUNTIME_VERSION

app = typer.Typer(
    name="capa",
    help=(
        "Control and DAQ for cone-calorimeter-class lab instruments.\n\n"
        f"capa {capa_version} (runtime {RUNTIME_VERSION})"
    ),
    no_args_is_help=True,
)

app.add_typer(catalog_app, name="catalog")
app.add_typer(plugins_app, name="plugins")
app.add_typer(devices_app, name="devices")
app.add_typer(config_app, name="config")
app.add_typer(hardware_app, name="hardware")
app.add_typer(method_app, name="method")
app.add_typer(profile_app, name="profile")

app.command(name="validate")(validate)
app.command(name="run")(run)
app.command(name="gui")(gui)
app.command(name="finalize")(finalize)


@app.command()
def version() -> None:
    """Print the capa version + runtime revision."""
    typer.echo(f"capa {capa_version} (runtime {RUNTIME_VERSION})")


def main(argv: list[str] | None = None) -> None:
    """Entry point referenced by ``[project.scripts]`` and tests.

    Tests can call ``main([...])`` to exercise the CLI in-process. Typer
    raises :class:`typer.Exit` for non-zero exit codes; we let that
    propagate so :class:`pytest.raises(SystemExit)` can capture the code.
    """
    app(argv)


if __name__ == "__main__":  # pragma: no cover
    main(sys.argv[1:])
