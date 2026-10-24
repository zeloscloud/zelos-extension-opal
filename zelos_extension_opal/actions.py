"""OPAL-RT actions, free functions registered under `<ACTION_PREFIX>/<name>`.

Free functions, not client methods: one surface whether or not a client runs.
Failures raise: the protocol reads its verdict from an exception, so
{"success": False} would report a successful run. Every RT-LAB request runs on
the client's worker thread (RtlabApi is bound to the thread that opened the
project); a write still queued at the timeout is cancelled, never sent late.
"""

from __future__ import annotations

import inspect
import logging
import sys
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

import zelos_sdk

from zelos_extension_opal import model as mdl
from zelos_extension_opal.client import MaybeApplied, NotSent
from zelos_extension_opal.link import Link, RtlabError, describe_error

if TYPE_CHECKING:
    from zelos_sdk.actions import ActionsRegistry

    from zelos_extension_opal.client import OpalClient

logger = logging.getLogger(__name__)

_client: OpalClient | None = None


def set_client(client: OpalClient | None) -> None:
    """Bind the client every action in this module operates on."""
    global _client
    _client = client


def _get_client() -> OpalClient:
    if _client is None:
        raise RuntimeError("the extension is not running")
    return _client


def _run(client: OpalClient, fn: Any, *args: Any, what: str, write: bool = False) -> Any:
    """`fn(link, *args)` on the worker; RT-LAB's refusal becomes a ValueError naming it."""
    try:
        return client.submit(fn, *args, write=write)
    except (NotSent, MaybeApplied) as e:
        raise TimeoutError(f"{what}: {e}") from e
    except RtlabError as e:
        raise ValueError(f"{what}: {describe_error(e)}") from e
    except (ValueError, RuntimeError, TimeoutError, OSError):
        raise
    except Exception as e:
        logger.exception("%s failed", what)
        raise RuntimeError(f"{what} failed: {describe_error(e)}") from e


def _pattern_field(func: Any) -> Any:
    return zelos_sdk.action.text(
        "pattern",
        title="Pattern",
        required=False,
        default="",
        description="Path glob (`*` one segment, `**` any depth), e.g. **/Gain*/*; empty lists all",
    )(func)


def _matching(items: dict[str, mdl.Item], pattern: str) -> list[mdl.Item]:
    pattern = pattern.strip()
    return [i for i in items.values() if not pattern or mdl.match(pattern, i.path)]


# ─── Status and model ───────────────────────────────────────────────────────


@zelos_sdk.action(
    "Get Status", "Connection, model state and tracing counters", read_only=True, hidden=True
)
def get_status() -> dict[str, Any]:
    return _get_client().status()


@zelos_sdk.action(
    "Model State",
    "The model's state (RUNNING, PAUSED, LOADABLE...), real-time mode, time step and "
    "subsystems, read now",
    read_only=True,
    hidden=True,
)
def model_state() -> dict[str, Any]:
    client = _get_client()

    def read(link: Link) -> dict[str, Any]:
        state, mode = link.model_state()
        return {"state": state.name, "real_time_mode": mode, **link.info()}

    return {"project": client.project, **_run(client, read, what="Model state")}


@zelos_sdk.action(
    "List Signals",
    "Every signal the model holds (acquisition, dynamic, control) and where it is traced",
    read_only=True,
    hidden=True,
)
@_pattern_field
def list_signals(pattern: str = "") -> dict[str, Any]:
    client = _get_client()
    signals = [client.describe(i) for i in _matching(client.signals, pattern)]
    return {"signals": signals, "count": len(signals)}


@zelos_sdk.action(
    "List Parameters",
    "Every block parameter the model holds and where it is traced",
    read_only=True,
    hidden=True,
)
@_pattern_field
def list_parameters(pattern: str = "") -> dict[str, Any]:
    client = _get_client()
    params = [client.describe(i) for i in _matching(client.parameters, pattern)]
    return {"parameters": params, "count": len(params)}


@zelos_sdk.action(
    "List Variables", "The model's MATLAB workspace variables", read_only=True, hidden=True
)
def list_variables() -> dict[str, Any]:
    client = _get_client()
    variables = [{"name": v.name, "id": v.id} for v in client.variables.values()]
    return {"variables": variables, "count": len(variables)}


# ─── Read / write ───────────────────────────────────────────────────────────


def _write(
    client: OpalClient,
    item: mdl.Item,
    number: float,
    write: Callable[[Link, list[str], list[float]], None],
    read: Callable[[Link, list[str]], Sequence[float]],
) -> dict[str, Any]:
    """Write one item and read back its event's row (the item alone if the row fails).

    Once the write returned, a failing read-back is reported with the write applied:
    reporting the write as failed would invite a second one.
    """
    row = client.siblings(item)

    def run(link: Link) -> tuple[list[mdl.Item], Sequence[float] | None, str]:
        write(link, [item.path], [number])
        error = ""
        for paths in (row, [item]):
            try:
                values = read(link, [i.path for i in paths])
                if len(values) == len(paths):
                    return paths, values, ""
                error = f"{len(values)} values for {len(paths)} paths"
            except RtlabError as e:
                error = describe_error(e)
        return [], None, error

    paths, values, error = _run(client, run, what=f"Write to '{item.path}'", write=True)
    if values is None:
        logger.warning("Wrote %s = %g; read-back failed: %s", item.path, number, error)
        return {"path": item.path, "value": number, "read_back": None, "read_back_error": error}
    client.log_values(paths, values)
    # By path: a re-discovery may have replaced the Item meanwhile.
    read_back = next(float(v) for i, v in zip(paths, values, strict=True) if i.path == item.path)
    logger.info("Wrote %s = %g (read back %g)", item.path, number, read_back)
    return {"path": item.path, "value": number, "read_back": read_back}


@zelos_sdk.action("Read Signal", "Read one signal's value now, by its RT-LAB path", read_only=True)
@zelos_sdk.action.text(
    "path", title="Path", description="e.g. rtdemo1/sm_computation/Integrator2/port1"
)
def read_signal(path: str) -> dict[str, Any]:
    client = _get_client()
    item = client.signal(path)
    value = _run(client, lambda link: link.read_signals_by_name([item.path])[0], what="Read")
    return {"path": item.path, "value": value}


@zelos_sdk.action(
    "Write Signal",
    "Set a control signal (a console input to the model) and read it back. The extension "
    "never writes on its own.",
)
@zelos_sdk.action.text("path", title="Path", description="A writable signal; see list_signals")
@zelos_sdk.action.text("value", title="Value", description="Number (decimal, or hex as 0x...)")
def write_signal(path: str, value: str) -> dict[str, Any]:
    client = _get_client()
    item = client.signal(path)
    if item.readonly:
        raise ValueError(f"'{item.path}' is read-only; only control signals can be written")
    return _write(
        client,
        item,
        mdl.parse_value(value),
        lambda link, paths, values: link.write_signals(paths, values),
        lambda link, paths: link.read_signals_by_name(paths),
    )


@zelos_sdk.action(
    "Read Parameter", "Read one block parameter now, by `<block path>/<name>`", read_only=True
)
@zelos_sdk.action.text("path", title="Path", description="e.g. rtdemo1/sm_computation/Gain Ki/Gain")
def read_parameter(path: str) -> dict[str, Any]:
    client = _get_client()
    item = client.parameter(path)
    value = _run(client, lambda link: link.read_parameters([item.path])[0], what="Read")
    return {"path": item.path, "value": value}


@zelos_sdk.action(
    "Write Parameter",
    "Set a block parameter (taking and releasing RT-LAB's parameter control) and read it back",
)
@zelos_sdk.action.text(
    "path", title="Path", description="`<block path>/<name>`; see list_parameters"
)
@zelos_sdk.action.text("value", title="Value", description="Number (decimal, or hex as 0x...)")
def write_parameter(path: str, value: str) -> dict[str, Any]:
    client = _get_client()
    item = client.parameter(path)
    return _write(
        client,
        item,
        mdl.parse_value(value),
        lambda link, paths, values: link.write_parameters(paths, values),
        lambda link, paths: link.read_parameters(paths),
    )


@zelos_sdk.action("Read Variable", "Read one MATLAB workspace variable now", read_only=True)
@zelos_sdk.action.text("name", title="Name", description="See list_variables")
def read_variable(name: str) -> dict[str, Any]:
    client = _get_client()
    var = client.variable(name)
    rows = _run(client, lambda link: link.variables(), what="Read")
    value = next((float(r[2]) for r in rows if int(r[0]) == var.id), None)
    if value is None:
        raise ValueError(f"Variable '{var.name}' could not be read; see list_variables")
    return {"name": var.name, "value": value}


@zelos_sdk.action("Write Variable", "Set a MATLAB workspace variable and read it back")
@zelos_sdk.action.text("name", title="Name", description="See list_variables")
@zelos_sdk.action.text("value", title="Value", description="Number (decimal, or hex as 0x...)")
def write_variable(name: str, value: str) -> dict[str, Any]:
    client = _get_client()
    var = client.variable(name)
    number = mdl.parse_value(value)

    def write(link: Link) -> float | None:
        link.write_variable(var.id, number)
        return next((float(r[2]) for r in link.variables() if int(r[0]) == var.id), None)

    read_back = _run(client, write, what=f"Write to '{var.name}'", write=True)
    logger.info("Wrote variable %s = %g (read back %s)", var.name, number, read_back)
    return {"name": var.name, "value": number, "read_back": read_back}


# ─── Config-form hook (standalone: runs with the extension stopped) ─────────


@zelos_sdk.action(
    "Auto-configure",
    "Fill in Project File from the RT-LAB projects open on this machine that hold a model. "
    "Review, then save and start.",
    standalone=True,
    read_only=True,
    hidden=True,
)
@zelos_sdk.action.object(
    "config",
    properties={},
    title="Config",
    description="The config form's current (possibly unsaved) data. Empty: the saved config",
    required=False,
)
def auto_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """`config` keys replace the form's: only `project_file`, so the rest survives."""
    from zelos_extension_opal.link import RtlabLink

    advanced = (config or {}).get("advanced") or {}
    try:
        if _client is not None and _client._connected:
            # Running: RtlabApi belongs to the worker thread.
            projects = _client.submit(lambda link: link.active_projects())
        else:
            link = RtlabLink(rtlab_path=str(advanced.get("rtlab_path") or ""))
            projects = link.active_projects()
    except (RtlabError, RuntimeError, TimeoutError) as e:
        return {"status": "error", "message": describe_error(e)}
    with_models = [p for p in projects if p["models"]]
    if not with_models:
        return {
            "status": "error",
            "message": "No RT-LAB project with a model is open on this machine: open one in "
            "RT-LAB, or set Project File to its .llp",
        }
    chosen = with_models[0]
    models = ", ".join(f"{m['model']} ({m['state']})" for m in chosen["models"])
    message = f"Found {chosen['project']}: {models}."
    if len(with_models) > 1:
        others = ", ".join(p["project"] for p in with_models[1:])
        message += f" Also open: {others}; pick one."
    return {"status": "success", "message": message, "config": {"project_file": chosen["project"]}}


# ─── Registration helper ────────────────────────────────────────────────────


def register_actions(registry: ActionsRegistry) -> list[str]:
    """Register every @action function here by bare name; `init(name=ACTION_PREFIX)`
    adds the prefix. Returns the registered names."""
    module = sys.modules[__name__]
    registered: list[str] = []
    for name, obj in inspect.getmembers(module):
        if name.startswith("_"):
            continue
        if inspect.isfunction(obj) and hasattr(obj, "_action"):
            registry.register(obj, name=name)
            registered.append(name)
    logger.info("Registered %d OPAL-RT actions", len(registered))
    return registered
