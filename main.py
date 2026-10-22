#!/usr/bin/env python3
"""Zelos OPAL-RT Extension - CLI entry point.

Modes:

1. App mode (default): configuration comes from the Zelos App
2. Demo mode: built-in demo model, no RT-LAB
3. CLI trace mode: an RT-LAB project on the command line

Examples:
    uv run main.py                                          # app mode
    uv run main.py demo                                     # demo model
    uv run main.py trace C:/projects/rtdemo1/rtdemo1.llp
    uv run main.py trace --include 'rtdemo1/sm_computation/**'
"""

from __future__ import annotations

import logging
import time

import rich_click as click

from zelos_extension_opal import ACTION_PREFIX as _ACTION_PREFIX
from zelos_extension_opal.cli import app as app_mode

#: Re-exported so a packaging-time action inventory - which reads this entry
#: module - sees the same namespace the live registration uses. See the
#: definition in `zelos_extension_opal/__init__.py`.
ACTION_PREFIX = _ACTION_PREFIX

# UTC ISO 8601 with ms, matching the SDK's Rust tracing lines in the same log stream
logging.Formatter.converter = time.gmtime
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03dZ %(levelname)5s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)


@click.group(invoke_without_command=True)
@click.option("--demo", is_flag=True, help="Run in demo mode with the built-in demo model")
@click.pass_context
def cli(ctx: click.Context, demo: bool) -> None:
    """Zelos OPAL-RT Extension - trace, read and write RT-LAB models.

    Without a subcommand this starts in app mode using the configuration saved
    by the Zelos App. Use --demo or the 'demo' subcommand for the demo model,
    and 'trace' for direct CLI access.
    """
    if ctx.invoked_subcommand is None:
        app_mode.run_app_mode(demo=demo)


@cli.command()
def demo() -> None:
    """Run against the built-in demo model.

    A simulated RT-LAB project (a PI-controlled plant with a setpoint control
    signal, tunable gains and a workspace variable) traced in-process. No RT-LAB
    required.
    """
    app_mode.run_app_mode(demo=True)


@cli.command()
@click.argument("project_file", type=str, required=False, default="")
@click.option(
    "--poll-interval", "-i", type=float, default=0.1, show_default=True, help="Signal poll (s)"
)
@click.option(
    "--parameter-interval",
    type=float,
    default=10.0,
    show_default=True,
    help="Full parameter sweep (s); 0 traces no parameters",
)
@click.option("--include", multiple=True, help="Path glob to trace (repeatable)")
@click.option("--exclude", multiple=True, help="Path glob not traced (repeatable)")
@click.option("--rtlab-path", default="", help="RT-LAB version directory (default: newest)")
@click.option("--prefix", default="OPAL-RT", show_default=True, help="Trace source")
def trace(
    project_file: str,
    poll_interval: float,
    parameter_interval: float,
    include: tuple[str, ...],
    exclude: tuple[str, ...],
    rtlab_path: str,
    prefix: str,
) -> None:
    """Trace an RT-LAB project from the command line.

    PROJECT_FILE is the project's .llp; without it, the one active project
    holding a model.

    \b
    Examples:
        uv run main.py trace C:/projects/rtdemo1/rtdemo1.llp
        uv run main.py trace --include 'rtdemo1/sm_computation/**' -i 0.05
    """
    logger.info("Starting OPAL-RT trace: %s", project_file or "active project")
    app_mode.run_config(
        {
            "project_file": project_file,
            "poll_interval": poll_interval,
            "parameter_interval": parameter_interval,
            "include": list(include),
            "exclude": list(exclude),
            "advanced": {"prefix": prefix, "rtlab_path": rtlab_path},
        }
    )


if __name__ == "__main__":
    cli()
