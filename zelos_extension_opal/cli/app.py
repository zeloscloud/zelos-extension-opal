"""App mode: run from the Zelos App's saved config, or demo mode.

Startup problems are one logger.error line plus exit 1; tracebacks are for bugs.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from typing import Any

import zelos_sdk
from zelos_sdk.extensions import ConfigValidationError, load_config

from zelos_extension_opal import ACTION_PREFIX
from zelos_extension_opal import actions as opal_actions
from zelos_extension_opal.client import OpalClient, OpalRunner, install_log_handler

logger = logging.getLogger(__name__)

# A forced exit flushes the trace this long, then leaves; the bound on shutdown is ~3 s.
FORCED_FLUSH_TIMEOUT = 0.3

#: Defaults of the schema's `advanced` object.
ADVANCED_DEFAULTS: dict[str, Any] = {
    "prefix": "OPAL-RT",
    "rtlab_path": "",
    "timeout": 10.0,
    "parameter_chunk": 100,
    "log_level": "INFO",
    "demo": False,
}

#: Defaults of the top-level settings.
DEFAULTS: dict[str, Any] = {
    "project_file": "",
    "poll_interval": 0.1,
    "parameter_interval": 10.0,
    "include": [],
    "exclude": [],
}


def resolve(config: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """(OpalClient arguments, advanced) from an app-shaped config, or exit."""
    advanced = {**ADVANCED_DEFAULTS, **(config.get("advanced") or {})}
    settings = {**DEFAULTS, **{k: v for k, v in config.items() if k != "advanced"}}
    prefix = str(advanced["prefix"]).strip()
    advanced["prefix"] = zelos_sdk.sanitize_name(prefix, kind="source") if prefix else "OPAL-RT"
    return (
        {
            "project_file": str(settings["project_file"]),
            "rtlab_path": str(advanced["rtlab_path"]),
            "poll_interval": float(settings["poll_interval"]),
            "parameter_interval": float(settings["parameter_interval"]),
            "parameter_chunk": int(advanced["parameter_chunk"]),
            "include": list(settings["include"]),
            "exclude": list(settings["exclude"]),
            "timeout": float(advanced["timeout"]),
        },
        advanced,
    )


def build_client(kwargs: dict[str, Any]) -> OpalClient:
    try:
        return OpalClient(**kwargs)
    except ValueError as e:
        logger.error("Invalid configuration: %s", e)
        sys.exit(1)


def apply_log_level(level_name: str) -> None:
    """Apply the configured log level, falling back to INFO on a bad value."""
    level = getattr(logging, str(level_name).upper(), None)
    if not isinstance(level, int):
        logger.warning("Invalid log level '%s', using INFO", level_name)
        level = logging.INFO
    logging.getLogger().setLevel(level)


def serve(client: OpalClient, prefix: str) -> None:
    """Publish the action surface, then trace the project until shutdown.

    Actions register before `init()`: registered later they may never be
    advertised. The source is a plain TraceSource, not init's global one, which
    the SDK holds for the process: rotation must be able to drop it.
    """
    runner = OpalRunner(client)
    opal_actions.set_client(client)
    opal_actions.register_actions(zelos_sdk.actions_registry)
    source = zelos_sdk.TraceSource(prefix)
    handler = install_log_handler(source)
    logger.info("Trace source: %s", prefix)
    zelos_sdk.init(name=ACTION_PREFIX, actions=True)

    client.start(source, handler)
    runner.run()
    code = 1 if runner.failed_at_start else 0
    if runner.wedged:
        # A normal exit would join the wedged worker (atexit) for as long as RT-LAB takes.
        logger.warning("RtlabApi call never returned; forcing exit")
        flushed = _flush_trace(client)
        logger.warning("Forced exit; trace flush %s", "completed" if flushed else "timed out")
        logging.shutdown()
        os._exit(code)
    if code:
        sys.exit(code)


def _flush_trace(client: OpalClient) -> bool:
    """Flush the trace source for at most FORCED_FLUSH_TIMEOUT; True if that finished.

    On a daemon thread: a flush has no timeout of its own.
    """
    source = client._source
    done = threading.Event()

    def flush() -> None:
        if source is not None:
            source.flush()
        done.set()

    threading.Thread(target=flush, daemon=True).start()
    return done.wait(FORCED_FLUSH_TIMEOUT)


def run_config(config: dict[str, Any], demo: bool = False) -> None:
    """Resolve an app-shaped config and serve it; `demo` (the CLI's `demo` / `--demo`,
    or Advanced > Demo) replaces the project with the built-in demo model."""
    kwargs, advanced = resolve(config)
    apply_log_level(advanced["log_level"])
    if demo or advanced["demo"]:
        from zelos_extension_opal.demo import DemoLink, DemoModel

        logger.info("Demo mode: using the built-in demo model")
        model = DemoModel()
        kwargs["link_factory"] = lambda: DemoLink(model)
    serve(build_client(kwargs), advanced["prefix"])


def run_app_mode(demo: bool = False) -> None:
    """Run the extension with configuration from the Zelos App.

    Args:
        demo: If True, run the built-in demo model instead of an RT-LAB project
    """
    try:
        config = load_config()
    except ConfigValidationError as e:
        logger.error("Invalid configuration: %s", "; ".join(m.strip(" •") for m in e.errors))
        sys.exit(1)
    run_config(config, demo)
