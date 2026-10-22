"""A simulated RT-LAB project for demo mode and tests: no RT-LAB install needed.

`DemoModel` is a PI-controlled first-order plant with a setpoint the console
sends (a control signal), tunable gains (parameters) and a workspace variable,
stepped at its 1 ms time step whenever it is read. `DemoLink` answers like
RtlabApi, its errors included: signals of an unloaded model raise error 22.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Sequence
from typing import Any

from zelos_extension_opal.link import EINVAL, ENOENT, EPERM, RtlabError
from zelos_extension_opal.model import LOADED, ModelState, SignalType

DEMO_PROJECT = "zelos_demo.llp"
MODEL = "zelos_demo"
_SM = f"{MODEL}/sm_plant"
_SC = f"{MODEL}/sc_console"
TIME_STEP = 0.001

#: (type, path, label) per signal; ids count per type, from 1.
SIGNALS: list[tuple[SignalType, str, str]] = [
    (SignalType.DYNAMIC, f"{_SM}/Sine Wave/port1", "disturbance"),
    (SignalType.DYNAMIC, f"{_SM}/Sum/port1", "error"),
    (SignalType.DYNAMIC, f"{_SM}/Controller/port1", "command"),
    (SignalType.DYNAMIC, f"{_SM}/Plant/port1", "plant response"),
    (SignalType.DYNAMIC, f"{_SM}/Clock/port1", "time"),
    (SignalType.CONTROL, f"{_SC}/port1", "setpoint"),
    (SignalType.ACQUISITION, f"{_SM}/port1", "setpoint"),
    (SignalType.ACQUISITION, f"{_SM}/port2", "plant response"),
]

PARAMETERS: dict[str, float] = {
    f"{_SM}/Controller/Kp": 2.0,
    f"{_SM}/Controller/Ki": 1.0,
    f"{_SM}/Plant/TimeConstant": 0.5,
    f"{_SM}/Sine Wave/Amplitude": 0.2,
    f"{_SM}/Sine Wave/Frequency": 0.5,
}

VARIABLES: dict[str, float] = {"setpoint_limit": 100.0}


class DemoModel:
    """The simulated model; thread-safe (the link runs on a worker, tests poke it)."""

    def __init__(self, state: ModelState = ModelState.RUNNING) -> None:
        self.lock = threading.Lock()
        self.state = state
        self.signals = list(SIGNALS)
        self.parameters = dict(PARAMETERS)
        self.variables = dict(VARIABLES)
        self.setpoint = 1.0
        self.sim_time = 0.0
        self._y = 0.0
        self._integral = 0.0
        self._last = time.monotonic()
        # Fault injection for tests.
        self.open_error: RtlabError | None = None
        self.fail_next: RtlabError | None = None
        self.stall = 0.0  # seconds every call blocks
        self.fail_read_back: RtlabError | None = None  # GetSignalsByName raises it while set
        self.control_holders = 0  # signal/parameter control taken and not released
        self.writes: list[tuple[str, float]] = []

    # ─── Simulation ─────────────────────────────────────────────────────────

    def _advance(self) -> None:
        now = time.monotonic()
        steps = int((now - self._last) / TIME_STEP) if self.state == ModelState.RUNNING else 0
        self._last = now
        p = self.parameters
        tau = max(p[f"{_SM}/Plant/TimeConstant"], TIME_STEP)
        for _ in range(min(steps, 200_000)):
            error = self.setpoint - self._y
            self._integral += error * TIME_STEP
            u = p[f"{_SM}/Controller/Kp"] * error + p[f"{_SM}/Controller/Ki"] * self._integral
            self._y += (u - self._y) / tau * TIME_STEP
            self.sim_time += TIME_STEP

    def value(self, path: str) -> float:
        p = self.parameters
        disturbance = p[f"{_SM}/Sine Wave/Amplitude"] * math.sin(
            2 * math.pi * p[f"{_SM}/Sine Wave/Frequency"] * self.sim_time
        )
        error = self.setpoint - self._y
        values = {
            f"{_SM}/Sine Wave/port1": disturbance,
            f"{_SM}/Sum/port1": error,
            f"{_SM}/Controller/port1": p[f"{_SM}/Controller/Kp"] * error
            + p[f"{_SM}/Controller/Ki"] * self._integral,
            f"{_SM}/Plant/port1": self._y + disturbance,
            f"{_SM}/Clock/port1": self.sim_time,
            f"{_SC}/port1": self.setpoint,
            f"{_SM}/port1": self.setpoint,
            f"{_SM}/port2": self._y + disturbance,
        }
        return values.get(path, 0.0)

    # ─── Test controls ──────────────────────────────────────────────────────

    def set_state(self, state: ModelState) -> None:
        with self.lock:
            self._advance()
            self.state = state

    def recompile(self, extra: str = "", remove: str = "") -> None:
        """A rebuilt model: signal ids move (`extra`, a path under sm_plant, first;
        `remove` gone)."""
        with self.lock:
            if extra:
                self.signals.insert(0, (SignalType.DYNAMIC, f"{_SM}/{extra}", extra))
            if remove:
                self.signals = [s for s in self.signals if s[1] != f"{_SM}/{remove}"]


class DemoLink:
    """RtlabApi's answers from a `DemoModel`."""

    def __init__(self, model: DemoModel) -> None:
        self.model = model
        self.opened = False

    def _enter(self) -> DemoModel:
        m = self.model
        if m.stall:
            time.sleep(m.stall)
        if m.fail_next is not None:
            error, m.fail_next = m.fail_next, None
            raise error
        m._advance()
        return m

    def _loaded(self, m: DemoModel) -> None:
        if m.state not in LOADED:
            raise RtlabError(
                EINVAL, "Error 22: Could not set/get signals because the model is not loaded."
            )

    def open(self) -> str:
        if self.model.open_error is not None:
            raise self.model.open_error
        self.opened = True
        return DEMO_PROJECT

    def close(self) -> None:
        self.opened = False

    def model_state(self) -> tuple[ModelState, str]:
        with self.model.lock:
            return self._enter().state, "SOFT_SIM_MODE"

    def info(self) -> dict[str, Any]:
        return {
            "model": f"{MODEL}.slx",
            "time_step": TIME_STEP,
            "time_factor": 1.0,
            "rtlab_version": "demo",
            "target_platform": "NT_TARGET",
            "subsystems": [
                {"path": _SM, "id": 1, "node": "localhost"},
                {"path": _SC, "id": 2, "node": ""},
            ],
        }

    def _rows(self, m: DemoModel) -> list[tuple[int, int, str, str, str, int, float]]:
        counts: dict[SignalType, int] = {}
        rows = []
        for signal_type, path, label in m.signals:
            counts[signal_type] = counts.get(signal_type, 0) + 1
            readonly = 0 if signal_type == SignalType.CONTROL else 1
            rows.append(
                (int(signal_type), counts[signal_type], path, label, "", readonly, m.value(path))
            )
        return rows

    def signals(self) -> Sequence[Sequence[Any]]:
        with self.model.lock:
            return self._rows(self._enter())

    def parameters(self) -> Sequence[Sequence[Any]]:
        with self.model.lock:
            m = self._enter()
            return [
                (i, path.rsplit("/", 1)[0], path.rsplit("/", 1)[1], "", value)
                for i, (path, value) in enumerate(m.parameters.items())
            ]

    def variables(self) -> Sequence[Sequence[Any]]:
        with self.model.lock:
            m = self._enter()
            return [(i, name, value) for i, (name, value) in enumerate(m.variables.items())]

    def read_signals(self, signal_type: int, ids: Sequence[int]) -> Sequence[float]:
        with self.model.lock:
            m = self._enter()
            self._loaded(m)
            by_id = {(r[0], r[1]): r[2] for r in self._rows(m)}
            missing = [i for i in ids if (signal_type, i) not in by_id]
            if missing:
                raise RtlabError(EINVAL, f"Invalid signal id {missing[0]}")
            return tuple(m.value(by_id[(signal_type, i)]) for i in ids)

    def read_signals_by_name(self, names: Sequence[str]) -> Sequence[float]:
        with self.model.lock:
            m = self._enter()
            if m.fail_read_back is not None:
                raise m.fail_read_back
            self._loaded(m)
            paths = {p for _, p, _ in m.signals}
            for name in names:
                if name not in paths:
                    raise RtlabError(EINVAL, f"Signal not found: {name}")
            return tuple(m.value(n) for n in names)

    def read_parameters(self, names: Sequence[str]) -> Sequence[float]:
        with self.model.lock:
            m = self._enter()
            for name in names:
                if name not in m.parameters:
                    raise RtlabError(ENOENT, f"Error 2: Parameter not found: {name}")
            return tuple(m.parameters[n] for n in names)

    def _control(self, m: DemoModel) -> None:
        if m.control_holders:
            raise RtlabError(EPERM, "Control requested, but already given to another client.")
        m.control_holders += 1

    def write_signals(self, names: Sequence[str], values: Sequence[float]) -> None:
        with self.model.lock:
            m = self._enter()
            self._loaded(m)
            self._control(m)
            try:
                for name, value in zip(names, values, strict=True):
                    if name != f"{_SC}/port1":
                        raise RtlabError(EINVAL, f"Not a control signal: {name}")
                    m.setpoint = float(value)
                    m.writes.append((name, float(value)))
            finally:
                m.control_holders -= 1

    def write_parameters(self, names: Sequence[str], values: Sequence[float]) -> None:
        with self.model.lock:
            m = self._enter()
            self._control(m)
            try:
                for name, value in zip(names, values, strict=True):
                    if name not in m.parameters:
                        raise RtlabError(ENOENT, f"Parameter not found: {name}")
                    m.parameters[name] = float(value)
                    m.writes.append((name, float(value)))
            finally:
                m.control_holders -= 1

    def write_variable(self, var_id: int, value: float) -> None:
        with self.model.lock:
            m = self._enter()
            names = list(m.variables)
            if not 0 <= var_id < len(names):
                raise RtlabError(EINVAL, "One of the variables doesn't exist.")
            m.variables[names[var_id]] = float(value)
            m.writes.append((names[var_id], float(value)))

    def active_projects(self) -> list[dict[str, Any]]:
        return [
            {
                "project": DEMO_PROJECT,
                "machine": "localhost",
                "address": "127.0.0.1",
                "models": [{"model": MODEL, "state": self.model.state.name}],
            }
        ]
