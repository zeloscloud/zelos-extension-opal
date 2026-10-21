"""What an RT-LAB model holds, as the trace addresses it.

RtlabApi describes signals, control signals, parameters and workspace variables
as tuples; this module turns them into `Item`s with a trace event and field,
filters them by path and lays them out without ever re-pointing a name.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any

import zelos_sdk

HEALTH_EVENT = "_model"
#: Parameters trace under their own tree: a row is every field of its event, so
#: 10 Hz signal rows never leave a block's parameters empty in the latest value.
PARAMETER_ROOT = "parameters"


class ModelState(enum.IntEnum):
    """OP_MODEL_STATE."""

    NOT_CONNECTED = 0
    NOT_LOADABLE = 1
    COMPILING = 2
    LOADABLE = 3
    LOADING = 4
    RESETTING = 5
    LOADED = 6
    PAUSED = 7
    RUNNING = 8
    DISCONNECTED = 9

    @classmethod
    def of(cls, raw: Any) -> ModelState:
        try:
            return cls(int(raw))
        except (TypeError, ValueError):
            return cls.DISCONNECTED


#: The model is changing: descriptions and reads are not trusted until it settles.
TRANSITIONAL = frozenset({ModelState.COMPILING, ModelState.LOADING, ModelState.RESETTING})
#: The model runs on its target (or waits to): signals are readable and writable.
LOADED = frozenset({ModelState.LOADED, ModelState.PAUSED, ModelState.RUNNING})


class SignalType(enum.IntEnum):
    """OP_SIGNAL_TYPE."""

    ACQUISITION = 0
    DYNAMIC = 1
    CONTROL = 2


class Kind(enum.Enum):
    SIGNAL = "signal"
    CONTROL = "control"
    PARAMETER = "parameter"


@dataclass(frozen=True)
class Item:
    """One signal, control signal or parameter and its place in the trace."""

    kind: Kind
    path: str  # the RT-LAB name: signal path, or `<block>/<parameter>`
    id: int
    signal_type: int  # OP_SIGNAL_TYPE; -1 for a parameter
    event: str
    name: str
    label: str = ""
    readonly: bool = True

    @property
    def key(self) -> str:
        return f"{self.event}/{self.name}"


@dataclass(frozen=True)
class Variable:
    """One MATLAB workspace variable."""

    id: int
    name: str
    value: float


def trace_names(path: str) -> tuple[str, str]:
    """(event, field): the block path and its last segment, the SDK's grammar applied
    (`rtdemo1/sm_computation/Gain Kp/port1` -> `rtdemo1/sm_computation/Gain Kp`, `port1`)."""
    parts = [p for p in path.split("/") if p.strip()]
    if not parts:
        return "", ""
    event = "/".join(parts[:-1]) or "signals"
    return zelos_sdk.sanitize_name(event, kind="event"), zelos_sdk.sanitize_name(
        parts[-1], kind="field"
    )


def parse_signals(raw: Iterable[Sequence[Any]]) -> list[Item]:
    """`GetSignalsDescription` rows: (type, id, path, label, reserved, readonly, value).

    Every type in one list, control signals included (readonly 0); ids count per type.
    """
    items = []
    for row in raw:
        path = str(row[2])
        event, name = trace_names(path)
        signal_type = int(row[0])
        items.append(
            Item(
                kind=Kind.CONTROL if signal_type == SignalType.CONTROL else Kind.SIGNAL,
                path=path,
                id=int(row[1]),
                signal_type=signal_type,
                event=event,
                name=name,
                label=str(row[3]),
                readonly=bool(row[5]),
            )
        )
    return items


def parse_parameters(raw: Iterable[Sequence[Any]]) -> list[Item]:
    """`GetParametersDescription` rows: (id, block path, name, variable, value)."""
    items = []
    for row in raw:
        path = f"{row[1]}/{row[2]}"
        event, name = trace_names(f"{PARAMETER_ROOT}/{path}")
        items.append(
            Item(
                kind=Kind.PARAMETER,
                path=path,
                id=int(row[0]),
                signal_type=-1,
                event=event,
                name=name,
                label=str(row[3]),
                readonly=False,
            )
        )
    return items


def parse_variables(raw: Iterable[Sequence[Any]]) -> list[Variable]:
    """`GetVariablesDescription` rows: (id, name, value)."""
    return [Variable(int(r[0]), str(r[1]), float(r[2])) for r in raw]


def parse_value(text: str) -> float:
    """A number as typed in an action: decimal, or hex (`0x1F`)."""
    text = str(text).strip()
    try:
        return float(int(text, 16)) if text.lower().startswith("0x") else float(text)
    except ValueError:
        raise ValueError(f"'{text}' is not a number (decimal, or hex as 0x...)") from None


# ─── Filters and layout ─────────────────────────────────────────────────────


def _match(pattern: tuple[str, ...], path: tuple[str, ...]) -> bool:
    if not pattern:
        return not path
    head, rest = pattern[0], pattern[1:]
    if head == "**":
        return any(_match(rest, path[i:]) for i in range(len(path) + 1))
    return bool(path) and fnmatchcase(path[0], head) and _match(rest, path[1:])


def match(pattern: str, path: str) -> bool:
    """Glob over a `/` path: `*` within one segment, `**` any number."""
    return _match(tuple(pattern.split("/")), tuple(path.split("/")))


@dataclass(frozen=True)
class PathFilter:
    """Include and exclude globs over the RT-LAB path. Empty include keeps everything;
    exclude wins."""

    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()

    @classmethod
    def parse(cls, include: Sequence[str] = (), exclude: Sequence[str] = ()) -> PathFilter:
        """Raises ValueError on an empty pattern or segment."""
        for raw in (*include, *exclude):
            if not raw.strip() or not all(raw.strip().split("/")):
                raise ValueError(f"invalid path pattern '{raw}'")
        return cls(tuple(p.strip() for p in include), tuple(p.strip() for p in exclude))

    def keeps(self, path: str) -> bool:
        if any(match(p, path) for p in self.exclude):
            return False
        return not self.include or any(match(p, path) for p in self.include)


def layout(items: Iterable[Item]) -> tuple[dict[str, list[Item]], list[str]]:
    """Items by event, in model order; a name taken twice within an event is dropped
    (and listed), never silently re-pointed."""
    events: dict[str, list[Item]] = {}
    taken: set[str] = set()
    dropped = []
    for item in items:
        if not item.event or not item.name or item.key in taken:
            dropped.append(item.path)
            continue
        taken.add(item.key)
        events.setdefault(item.event, []).append(item)
    return events, dropped
