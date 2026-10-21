"""The one RT-LAB link an `OpalClient` drives: RtlabApi to a real project, or the demo model.

RtlabApi binds a project to the thread that opened it: every method here blocks
and is called from the client's one worker thread, never the event loop.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

from zelos_extension_opal.model import ModelState

logger = logging.getLogger(__name__)

RTLAB_ROOT = Path(r"C:\OPAL-RT\RT-LAB")

# OpalApiPy error codes (errno values).
EPERM = 1
ENOENT = 2
EIO = 5
EBADF = 9
EACCES = 13
EINVAL = 22


class RtlabError(Exception):
    """An RtlabApi call failed with `code` (errno-like) and RT-LAB's message."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = " ".join(message.split())


class Link(Protocol):
    """What the client needs from RT-LAB; see `RtlabLink` and `demo.DemoLink`."""

    def open(self) -> str: ...
    def close(self) -> None: ...
    def model_state(self) -> tuple[ModelState, str]: ...
    def info(self) -> dict[str, Any]: ...
    def signals(self) -> Sequence[Sequence[Any]]: ...
    def parameters(self) -> Sequence[Sequence[Any]]: ...
    def variables(self) -> Sequence[Sequence[Any]]: ...
    def read_signals(self, signal_type: int, ids: Sequence[int]) -> Sequence[float]: ...
    def read_signals_by_name(self, names: Sequence[str]) -> Sequence[float]: ...
    def read_parameters(self, names: Sequence[str]) -> Sequence[float]: ...
    def write_signals(self, names: Sequence[str], values: Sequence[float]) -> None: ...
    def write_parameters(self, names: Sequence[str], values: Sequence[float]) -> None: ...
    def write_variable(self, var_id: int, value: float) -> None: ...
    def active_projects(self) -> list[dict[str, Any]]: ...


def _version_key(path: Path) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", path.name))


def rtlab_dirs(rtlab_path: str = "") -> list[Path]:
    """RT-LAB version directories to import RtlabApi from: the configured one, else
    every `C:\\OPAL-RT\\RT-LAB\\v*`, newest first."""
    if rtlab_path:
        return [Path(rtlab_path)]
    return sorted(RTLAB_ROOT.glob("v*"), key=_version_key, reverse=True)


def import_rtlab(rtlab_path: str = "") -> Any:
    """RtlabApi, importable from PYTHONPATH or an RT-LAB install; RuntimeError says how
    to fix it."""
    with contextlib.suppress(ImportError):
        import RtlabApi

        return RtlabApi
    tried = []
    for base in rtlab_dirs(rtlab_path):
        python_dir, bin_dir = base / "common" / "python", base / "common" / "bin"
        if not python_dir.is_dir():
            tried.append(str(base))
            continue
        for d in (str(python_dir), str(bin_dir)):
            if d not in sys.path:
                sys.path.append(d)
        if hasattr(os, "add_dll_directory") and bin_dir.is_dir():
            os.add_dll_directory(str(bin_dir))
        try:
            import RtlabApi

            logger.info("RtlabApi from %s", base)
            return RtlabApi
        except ImportError as e:
            tried.append(f"{base} ({e})")
    where = ", ".join(tried) or f"no RT-LAB under {RTLAB_ROOT}"
    raise RuntimeError(
        f"RtlabApi not importable ({where}); install RT-LAB on this machine, or set "
        "Advanced > RT-LAB Install Path to its version directory"
    )


class RtlabLink:
    """One RtlabApi connection to one project, owned by the thread that opens it."""

    def __init__(self, project_file: str = "", rtlab_path: str = "") -> None:
        self.project_file = project_file
        self.rtlab_path = rtlab_path
        self._api: Any = None

    def _call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        """An RtlabApi call; its exception (OpalApiPy.Exception(code, msg)) as RtlabError."""
        try:
            return getattr(self._api, name)(*args, **kwargs)
        except Exception as e:
            if len(e.args) >= 2 and isinstance(e.args[0], int):
                raise RtlabError(e.args[0], str(e.args[1])) from e
            raise

    def open(self) -> str:
        """Open the configured project, else the one active project with a model."""
        if self._api is None:
            self._api = import_rtlab(self.rtlab_path)
        project = self.project_file
        if not project:
            active = [p for p in self.active_projects() if p["models"]]
            if len(active) != 1:
                found = ", ".join(p["project"] for p in active) or "none"
                raise RtlabError(
                    EINVAL,
                    f"Project File is empty and {len(active)} active RT-LAB projects hold a "
                    f"model ({found}); set Project File",
                )
            project = active[0]["project"]
        # Without returnOnAmbiguity a partial match opens a picker dialog nobody sees.
        self._call("OpenProject", project, returnOnAmbiguity=True)
        return project

    def close(self) -> None:
        if self._api is not None:
            self._call("CloseProject")

    def model_state(self) -> tuple[ModelState, str]:
        state, mode = self._call("GetModelState")
        return ModelState.of(state), str(mode).split("(")[0]

    def info(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, name in (
            ("model", "GetCurrentModel"),
            ("time_info", "GetTimeInfo"),
            ("rtlab_version", "GetRtlabVersion"),
            ("target_platform", "GetTargetPlatform"),
            ("subsystems", "GetSubsystemList"),
        ):
            with contextlib.suppress(RtlabError):
                out[key] = self._call(name)
        model = out.pop("model", None)
        step, factor = out.pop("time_info", (0.0, 0.0))
        subsystems = out.pop("subsystems", ())
        return {
            "model": str(Path(model[0]) / model[1]) if model else "",
            "time_step": float(step),
            "time_factor": float(factor),
            "rtlab_version": str(out.get("rtlab_version", ("",))[0]),
            "target_platform": str(out.get("target_platform", "")).split("(")[0],
            "subsystems": [
                {"path": str(s[0]), "id": int(s[1]), "node": str(s[2])} for s in subsystems
            ],
        }

    def signals(self) -> Sequence[Sequence[Any]]:
        return self._call("GetSignalsDescription")

    def parameters(self) -> Sequence[Sequence[Any]]:
        return self._call("GetParametersDescription")

    def variables(self) -> Sequence[Sequence[Any]]:
        # A model without MATLAB variables raises rather than returning none.
        try:
            return self._call("GetVariablesDescription")
        except RtlabError:
            return ()

    def read_signals(self, signal_type: int, ids: Sequence[int]) -> Sequence[float]:
        return self._call("GetSignalsById", signal_type, tuple(ids))

    def read_signals_by_name(self, names: Sequence[str]) -> Sequence[float]:
        return self._call("GetSignalsByName", tuple(names))

    def read_parameters(self, names: Sequence[str]) -> Sequence[float]:
        return self._call("GetParametersByName", tuple(names))

    def write_signals(self, names: Sequence[str], values: Sequence[float]) -> None:
        """Signal control for every subsystem (logical id 0), held for this write only."""
        self._call("GetSignalControl", 0, 1)
        try:
            self._call("SetSignalsByName", tuple(names), tuple(values))
        finally:
            self._release("GetSignalControl", 0, 0)

    def write_parameters(self, names: Sequence[str], values: Sequence[float]) -> None:
        self._call("GetParameterControl", 1)
        try:
            self._call("SetParametersByName", tuple(names), tuple(values))
        finally:
            self._release("GetParameterControl", 0)

    def _release(self, name: str, *args: Any) -> None:
        """Release control, retried once. A failure never masks the write's own outcome:
        held control blocks the RT-LAB console's writes, so it is an ERROR."""
        for attempt in (1, 2):
            try:
                self._call(name, *args)
                return
            except RtlabError as e:
                if attempt == 2:
                    logger.error(
                        "%s release failed (%s): this client may still hold control; "
                        "restart the extension to release it",
                        name,
                        describe_error(e),
                    )

    def write_variable(self, var_id: int, value: float) -> None:
        self._call("SetVariables", ((var_id, value),))

    def active_projects(self) -> list[dict[str, Any]]:
        """`GetActiveProjects`: projects RT-LAB has open on this machine, with their models."""
        if self._api is None:
            self._api = import_rtlab(self.rtlab_path)
        out = []
        for path, _instance, machine, address, _count, models in self._call("GetActiveProjects"):
            out.append(
                {
                    "project": str(path),
                    "machine": str(machine),
                    "address": str(address),
                    "models": [
                        {"model": str(m[0]), "state": ModelState.of(m[2]).name} for m in models
                    ],
                }
            )
        return out


def describe_error(error: BaseException) -> str:
    """One line for a log or an action result."""
    if isinstance(error, RtlabError):
        return f"{error.message} (RT-LAB error {error.code})"
    return str(error) or error.__class__.__name__


def is_connection_error(error: BaseException) -> bool:
    """The project connection, not one request: reconnect."""
    if isinstance(error, RtlabError):
        return error.code in (EIO, EBADF)
    return isinstance(error, OSError)


def not_loaded(error: BaseException) -> bool:
    """The model is not loaded (any more): wait for the state to say otherwise."""
    return isinstance(error, RtlabError) and error.code in (EINVAL, EACCES)
