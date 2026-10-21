"""RT-LAB client with Zelos SDK integration: one project, traced until shutdown.

RtlabApi binds the project to the thread that opened it, so every link call runs
on the client's one worker thread (requests serialized). The event loop
schedules the jobs and logs; actions submit to the same worker directly, so a
request that times out still queued is cancelled, never sent late.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import logging
import signal
import time
from collections.abc import Awaitable, Callable, Sequence
from functools import partial
from typing import Any

import zelos_sdk
from zelos_sdk import schemas
from zelos_sdk.hooks.logging import TraceLoggingHandler

from zelos_extension_opal import model as mdl
from zelos_extension_opal.link import (
    Link,
    RtlabError,
    RtlabLink,
    describe_error,
    is_connection_error,
    not_loaded,
)
from zelos_extension_opal.model import (
    HEALTH_EVENT,
    LOADED,
    TRANSITIONAL,
    Item,
    Kind,
    ModelState,
    PathFilter,
    Variable,
)

logger = logging.getLogger(__name__)

# Reconnect backoff: a controller down for hours costs one attempt a minute.
RECONNECT_INITIAL = 3.0
RECONNECT_MAX = 60.0
# Ceiling on the disconnect at shutdown: a wedged RtlabApi call must not make the
# process unkillable.
SHUTDOWN_TIMEOUT = 3.0
# Unclassified request failures in a row that force a reconnect.
FAILURES_BEFORE_RECONNECT = 5
# The model state read doubles as the liveness probe.
HEALTH_INTERVAL = 1.0

#: Log event on the trace source (`<prefix>/log`).
LOG_EVENT = "log"

HEALTH_FIELDS: list[tuple[str, zelos_sdk.DataType, str]] = [
    ("state", zelos_sdk.DataType.Int32, ""),
    ("state_name", zelos_sdk.DataType.String, ""),
    ("real_time_mode", zelos_sdk.DataType.String, ""),
    ("connected", zelos_sdk.DataType.Boolean, ""),
    ("signals", zelos_sdk.DataType.UInt32, ""),
    ("parameters", zelos_sdk.DataType.UInt32, ""),
    ("polls", zelos_sdk.DataType.UInt64, ""),
    ("errors", zelos_sdk.DataType.UInt32, ""),
    ("poll_ms", zelos_sdk.DataType.Float64, "ms"),
    ("parameter_sweep_s", zelos_sdk.DataType.Float64, "s"),
    ("last_error", zelos_sdk.DataType.String, ""),
]

Declared = tuple[str, zelos_sdk.DataType, str]


def install_log_handler(source: zelos_sdk.TraceSource) -> TraceLoggingHandler:
    """Route INFO and above into the trace; DEBUG would flood it."""
    handler = TraceLoggingHandler(source)
    handler.setLevel(logging.INFO)
    logging.getLogger().addHandler(handler)
    return handler


class NotSent(TimeoutError):
    """A request timed out still queued behind another: cancelled, never sent."""


class MaybeApplied(TimeoutError):
    """A write was sent and not answered in time: RT-LAB may still apply it."""


class _Job:
    """A periodic request: health, the signal poll, one parameter chunk."""

    def __init__(self, period: float, run: Callable[[], Awaitable[None]]) -> None:
        self.due = time.monotonic()
        self.period = period
        self.run = run


class OpalClient:
    """Traces one RT-LAB project's signals and parameters into a trace source."""

    def __init__(
        self,
        project_file: str = "",
        rtlab_path: str = "",
        poll_interval: float = 0.1,
        parameter_interval: float = 10.0,
        parameter_chunk: int = 100,
        include: Sequence[str] = (),
        exclude: Sequence[str] = (),
        timeout: float = 10.0,
        link_factory: Callable[[], Link] | None = None,
    ) -> None:
        """
        Args:
            project_file: RT-LAB project (.llp); empty = the one active project with a model
            rtlab_path: RT-LAB version directory; empty = newest under C:\\OPAL-RT\\RT-LAB
            poll_interval: Signal poll period, seconds
            parameter_interval: One full parameter sweep, seconds; 0 = parameters not traced
            parameter_chunk: Parameters per read
            include: Path globs to trace; empty traces everything
            exclude: Path globs not traced; win over include
            timeout: Action request timeout, seconds
            link_factory: Makes the link (demo, tests); default RtlabApi

        Raises:
            ValueError: If an interval or a filter is invalid
        """
        if poll_interval <= 0:
            raise ValueError(f"poll_interval must be positive, got {poll_interval}")
        if parameter_interval < 0:
            raise ValueError(f"parameter_interval must be 0 or more, got {parameter_interval}")
        if parameter_chunk < 1:
            raise ValueError(f"parameter_chunk must be at least 1, got {parameter_chunk}")
        self.project_file = project_file.strip()
        self.rtlab_path = rtlab_path.strip()
        self.poll_interval = poll_interval
        self.parameter_interval = parameter_interval
        self.parameter_chunk = parameter_chunk
        self.filters = PathFilter.parse(include, exclude)
        self.timeout = timeout
        self._link_factory = link_factory

        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="rtlab"
        )
        self.in_call = False  # the worker is inside a link call (set on the worker)
        self._link: Link | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._connected = False
        self.ever_connected = False
        self.last_error: str | None = None
        self.project = ""
        self.info: dict[str, Any] = {}
        self.state = ModelState.NOT_CONNECTED
        self.real_time_mode = ""

        # What the model holds (by path), and what is traced.
        self.signals: dict[str, Item] = {}
        self.parameters: dict[str, Item] = {}
        self.variables: dict[str, Variable] = {}
        self._traced: dict[str, Item] = {}
        self._discovered_in: ModelState | None = None  # state of the last discovery
        self._groups: list[tuple[int, tuple[int, ...], list[Item]]] = []
        self._chunks: list[list[Item]] = []
        self._chunk = 0
        self._sweep_started = 0.0
        self._failed: set[str] = set()
        self._jobs: list[_Job] = []
        self._poll_count = 0
        self._refused = 0  # signal reads refused in a row
        self._error_count = 0
        self._poll_ms = 0.0
        self._sweep_s = 0.0

        # Trace plumbing; see start / _rotate_source.
        self._source: zelos_sdk.TraceSource | None = None
        self._log_handler: TraceLoggingHandler | None = None
        self._trace_events: dict[str, Any] = {}
        self._declared: dict[str, list[Declared]] = {}
        self._schemas: dict[str, set[str]] = {}

    # ─── Trace source ───────────────────────────────────────────────────────

    def start(self, source: zelos_sdk.TraceSource, log_handler: TraceLoggingHandler | None) -> None:
        """Declare the health event on `source`; the log handler follows rotations."""
        self._source, self._log_handler = source, log_handler
        self._trace_events, self._declared, self._schemas = {}, {}, {}
        self._declare(HEALTH_EVENT, list(HEALTH_FIELDS))

    def _declare(self, event: str, fields: list[Declared]) -> None:
        assert self._source is not None
        self._declared[event] = fields
        self._schemas[event] = {f[0] for f in fields}
        self._trace_events[event] = self._source.add_event(
            event, [zelos_sdk.TraceEventFieldMetadata(*f) for f in fields]
        )

    def _declare_events(self, events: dict[str, list[Item]]) -> list[str]:
        """Declare new events; a declared event whose fields changed takes the new set
        (a rotation follows: rows must carry every declared field).

        Returns:
            Fields added to or removed from declared events, as `+event/name` / `-event/name`
        """
        changed = []
        for event, schema in list(self._schemas.items()):
            if event != HEALTH_EVENT and event not in events and schema:
                changed += [f"-{event}/{f}" for f in sorted(schema)]
                self._schemas[event], self._declared[event] = set(), []
        for event, items in events.items():
            fields = [(i.name, zelos_sdk.DataType.Float64, "") for i in items]
            schema = self._schemas.get(event)
            if schema is None:
                self._declare(event, fields)
                continue
            names = {f[0] for f in fields}
            if names != schema:
                changed += [f"+{event}/{f}" for f in sorted(names - schema)]
                changed += [f"-{event}/{f}" for f in sorted(schema - names)]
                self._schemas[event], self._declared[event] = names, fields
        return changed

    def _rotate_source(self) -> None:
        """Move to a new segment of the same source: a declared schema is fixed per
        TraceSource instance. Synchronous: no await between the last write and the switch."""
        assert self._source is not None
        handler = self._log_handler
        with handler.lock if handler else contextlib.nullcontext():
            name = self._source.name
            self._source.flush()
            self._source, self._trace_events = None, {}
            if handler:
                handler.trace_source = handler.trace_event = None
            source = zelos_sdk.TraceSource(name)
            events = {
                event: source.add_event(event, [zelos_sdk.TraceEventFieldMetadata(*f) for f in fs])
                for event, fs in self._declared.items()
                if fs
            }
            self._source, self._trace_events = source, events
            if handler:
                handler.trace_source = source
                handler.trace_event = source.add_event(LOG_EVENT, schemas.Log)

    # ─── Worker thread ──────────────────────────────────────────────────────

    def _blocking(self, fn: Callable[..., Any], args: tuple[Any, ...]) -> Any:
        """Worker thread: `in_call` tells shutdown whether a call never came back."""
        self.in_call = True
        try:
            return fn(*args)
        finally:
            self.in_call = False

    async def _call(self, fn: Callable[..., Any], *args: Any) -> Any:
        """Run a blocking link call on the worker, from the loop."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, partial(self._blocking, fn, args))

    def submit(self, fn: Callable[..., Any], *args: Any, write: bool = False) -> Any:
        """Run `fn(link, *args)` on the worker from an action thread, within `timeout`.

        Raises:
            RuntimeError: If the project is not connected
            NotSent: If it timed out still queued (cancelled: never runs)
            MaybeApplied: If a write timed out after it started
        """
        link = self._link
        if link is None or not self._connected:
            raise RuntimeError("not connected to an RT-LAB project")
        future = self._executor.submit(self._blocking, fn, (link, *args))
        try:
            return future.result(timeout=self.timeout)
        except concurrent.futures.TimeoutError:
            if future.cancel():
                raise NotSent(
                    f"not sent: RT-LAB busy for {self.timeout:.0f}s with an earlier request"
                ) from None
            if write:
                raise MaybeApplied(
                    f"no answer from RT-LAB within {self.timeout:.0f}s; it may still apply "
                    "the write, read the value to confirm"
                ) from None
            raise TimeoutError(f"no answer from RT-LAB within {self.timeout:.0f}s") from None

    def siblings(self, item: Item) -> list[Item]:
        """The traced items logged in one row with `item`: its event's, else itself."""
        if item.path not in self._traced:
            return [item]
        return [i for i in self._traced.values() if i.event == item.event]

    def log_values(self, items: Sequence[Item], values: Sequence[float]) -> None:
        """Any thread: log traced items' values on the loop, one row per event."""
        rows: dict[str, dict[str, float]] = {}
        for item, value in zip(items, values, strict=True):
            if item.path in self._traced:
                rows.setdefault(item.event, {})[item.name] = float(value)
        if rows and self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._log_rows, rows, time.time_ns())

    def _log_rows(self, rows: dict[str, dict[str, float]], stamp_ns: int) -> None:
        for event, values in rows.items():
            trace_event = self._trace_events.get(event)
            if trace_event is not None:
                trace_event.log_at(stamp_ns, **values)

    # ─── Connection and discovery ───────────────────────────────────────────

    def _open(self) -> None:
        """Worker thread: link, project, model info."""
        link = (
            self._link_factory()
            if self._link_factory
            else RtlabLink(self.project_file, self.rtlab_path)
        )
        self.project = link.open()
        try:
            self.info = link.info()
            self.state, self.real_time_mode = link.model_state()
        except BaseException:
            with contextlib.suppress(Exception):
                link.close()
            raise
        self._link = link

    def _describe(self) -> tuple[list[Item], list[Item], list[Variable]]:
        """Worker thread: everything the model holds."""
        assert self._link is not None
        return (
            mdl.parse_signals(self._link.signals()),
            mdl.parse_parameters(self._link.parameters()) if self.parameter_interval else [],
            mdl.parse_variables(self._link.variables()),
        )

    async def connect(self) -> bool:
        """Open the project and discover the model; False on failure (logged)."""
        try:
            await self._call(self._open)
            # Nothing of the last connection's layout is trusted: ids may have moved.
            self._failed, self._discovered_in = set(), None
            self._groups, self._chunks, self._traced = [], [], {}
            if self.state not in TRANSITIONAL:
                await self._discover()
            self._connected = True
            self.last_error, self.ever_connected = None, True
            self._jobs = [_Job(HEALTH_INTERVAL, self._poll_health)]
            self._jobs.append(_Job(self.poll_interval, self._poll))
            if self.parameter_interval:
                self._jobs.append(_Job(self.parameter_interval, self._poll_parameters))
            self._reschedule_parameters()
            logger.info(
                "Connected to %s (%s, %s, RT-LAB %s, step %g s)",
                self.project,
                self.info.get("model", ""),
                self.state.name,
                self.info.get("rtlab_version", ""),
                self.info.get("time_step", 0.0),
            )
            return True
        except Exception as e:
            self._connected = False
            self.last_error = describe_error(e)
            await self._close_link()
            if self.ever_connected:
                logger.warning("Connecting to RT-LAB failed: %s", self.last_error)
            return False

    async def _discover(self) -> None:
        """Describe the model, select and lay out what is traced, declare it."""
        started = time.monotonic()
        signals, parameters, variables = await self._call(self._describe)
        self.signals = {i.path: i for i in signals}
        self.parameters = {i.path: i for i in parameters}
        self.variables = {v.name: v for v in variables}
        events, dropped = mdl.layout(
            i for i in [*signals, *parameters] if self.filters.keeps(i.path)
        )
        if dropped:
            logger.warning(
                "%d paths share a trace name with another and are skipped: %s",
                len(dropped),
                ", ".join(dropped[:10]),
            )
        if (signals or parameters) and not events:
            logger.warning("Nothing left after include/exclude; nothing traced")
        changed = self._declare_events(events)
        if changed:
            logger.info("Fields changed %s: new trace segment", ", ".join(changed[:10]))
            self._rotate_source()
        self._traced = {i.path: i for items in events.values() for i in items}
        groups: dict[int, list[Item]] = {}
        for item in self._traced.values():
            if item.kind != Kind.PARAMETER:
                groups.setdefault(item.signal_type, []).append(item)
        self._groups = [(t, tuple(i.id for i in items), items) for t, items in groups.items()]
        params = [i for i in self._traced.values() if i.kind == Kind.PARAMETER]
        # Whole events per chunk, so every row is complete; a block larger than a
        # chunk is read alone.
        self._chunks = []
        for items in events.values():
            if items[0].kind != Kind.PARAMETER:
                continue
            if self._chunks and len(self._chunks[-1]) + len(items) <= self.parameter_chunk:
                self._chunks[-1].extend(items)
            else:
                self._chunks.append(list(items))
        self._chunk = 0
        self._discovered_in = self.state
        traced_signals = len(self._traced) - len(params)
        logger.info(
            "Model %s: %d signals, %d parameters, %d variables; tracing %d signals and "
            "%d parameters in %d events (%.1fs)",
            self.state.name,
            len(signals),
            len(parameters),
            len(variables),
            traced_signals,
            len(params),
            len(events),
            time.monotonic() - started,
        )

    def _reschedule_parameters(self) -> None:
        """One chunk per tick, the sweep spread over parameter_interval."""
        for job in self._jobs:
            if job.run == self._poll_parameters:
                job.period = self.parameter_interval / max(len(self._chunks), 1)

    async def _close_link(self) -> None:
        link, self._link = self._link, None
        if link is not None:
            with contextlib.suppress(Exception):
                await self._call(link.close)

    async def disconnect(self) -> None:
        was = self._connected
        self._connected = False
        await self._close_link()
        if was:
            self._log_health(connected=False)
            logger.info("Disconnected from %s", self.project)

    # ─── Jobs ───────────────────────────────────────────────────────────────

    async def _poll_health(self) -> None:
        """The model state; a settled change re-describes the model (ids move on a reload)."""
        assert self._link is not None
        previous = self.state
        self.state, self.real_time_mode = await self._call(self._link.model_state)
        if self.state != previous:
            logger.info("Model state %s -> %s", previous.name, self.state.name)
        last = self._discovered_in
        if self.state not in TRANSITIONAL and (
            last is None or previous in TRANSITIONAL or (last in LOADED) != (self.state in LOADED)
        ):
            await self._discover()
            self._reschedule_parameters()
        self._log_health(connected=True)

    async def _poll(self) -> None:
        """Every traced signal, one GetSignalsById per signal type, one row per event."""
        if self.state not in LOADED or self._link is None or not self._groups:
            return
        rows: dict[str, dict[str, float]] = {}
        started = time.time_ns()
        for signal_type, ids, items in self._groups:
            try:
                values = await self._call(self._link.read_signals, signal_type, ids)
            except RtlabError as e:
                self._refused += 1
                if not not_loaded(e) or self._refused >= FAILURES_BEFORE_RECONNECT:
                    raise
                # Unloaded since the last health read, or ids moved: re-read the
                # state now and describe the model again once it settles.
                if self._refused == 1:
                    logger.info("Signal read refused (%s); re-reading the model", describe_error(e))
                self._discovered_in = None
                for job in self._jobs:
                    if job.run == self._poll_health:
                        job.due = time.monotonic()
                return
            # A short reply would shift every value onto the wrong field: raise.
            for item, value in zip(items, values, strict=True):
                rows.setdefault(item.event, {})[item.name] = float(value)
        ended = time.time_ns()
        # Stamped mid-request: RT-LAB gives no sample time on this path.
        stamp = (started + ended) // 2
        self._poll_ms = (ended - started) / 1e6
        self._refused = 0
        self._log_rows(rows, stamp)
        self._poll_count += 1

    async def _poll_parameters(self) -> None:
        """One parameter chunk per tick; a sweep is every chunk once."""
        if not self._chunks or self.state in TRANSITIONAL or self._link is None:
            return
        if self._chunk == 0:
            self._sweep_started = time.monotonic()
        items = self._chunks[self._chunk]
        started = time.time_ns()
        try:
            values = await self._call(self._link.read_parameters, [i.path for i in items])
        except RtlabError as e:
            if is_connection_error(e):
                raise
            # One missing name fails the chunk: read it item by item to find it.
            values = await self._call(self._read_one_by_one, items)
        rows: dict[str, dict[str, float]] = {}
        for item, value in zip(items, values, strict=True):
            if value is not None:
                rows.setdefault(item.event, {})[item.name] = float(value)
        self._log_rows(rows, (started + time.time_ns()) // 2)
        self._chunk = (self._chunk + 1) % len(self._chunks)
        if self._chunk == 0:
            self._sweep_s = time.monotonic() - self._sweep_started

    def _read_one_by_one(self, items: list[Item]) -> list[float | None]:
        """Worker thread: each parameter alone; a failing one is reported once."""
        assert self._link is not None
        out: list[float | None] = []
        for item in items:
            try:
                out.append(self._link.read_parameters([item.path])[0])
            except RtlabError as e:
                if is_connection_error(e):
                    raise
                out.append(None)
                self._report_failure(item, describe_error(e))
        return out

    def _report_failure(self, item: Item, reason: str) -> None:
        """One ERROR per bad path per connection: per cycle would flood the log."""
        self._error_count += 1
        if item.path in self._failed:
            return
        self._failed.add(item.path)
        logger.error("'%s' unreadable: %s - further reports suppressed", item.path, reason)

    def _log_health(self, connected: bool) -> None:
        event = self._trace_events.get(HEALTH_EVENT)
        if event is None:
            return
        event.log(
            state=int(self.state),
            state_name=self.state.name,
            real_time_mode=self.real_time_mode,
            connected=connected,
            signals=sum(1 for i in self._traced.values() if i.kind != Kind.PARAMETER),
            parameters=sum(1 for i in self._traced.values() if i.kind == Kind.PARAMETER),
            polls=self._poll_count,
            errors=self._error_count,
            poll_ms=self._poll_ms,
            parameter_sweep_s=self._sweep_s,
            last_error=self.last_error or "",
        )

    # ─── Lifecycle ──────────────────────────────────────────────────────────

    async def run_async(self, stop_event: asyncio.Event) -> bool:
        """Run the jobs, one request at a time, until `stop_event`.

        Returns:
            False if the first connect failed (the caller exits 1)
        """
        self._stop_event = stop_event
        self._loop = asyncio.get_running_loop()
        backoff = RECONNECT_INITIAL
        losses = failures = 0
        try:
            while not stop_event.is_set():
                if not self._connected:
                    if self.ever_connected:
                        logger.info("Reconnecting to %s...", self.project or "RT-LAB")
                    # Raced: OpenProject on a dead controller can sit for minutes.
                    stopped, connected = await self._or_stop(self.connect())
                    if stopped:
                        break
                    if not connected:
                        if not self.ever_connected:
                            return False
                        logger.warning("Retrying in %.0fs", backoff)
                        if await self._wait_or_stop(backoff):
                            break
                        backoff = min(backoff * 2, RECONNECT_MAX)
                        continue
                job = min(self._jobs, key=lambda j: j.due)
                if await self._wait_or_stop(max(0.0, job.due - time.monotonic())):
                    break
                # Behind schedule, the next run starts from now rather than bursting.
                job.due = max(job.due + job.period, time.monotonic())
                try:
                    if (await self._or_stop(job.run()))[0]:
                        break
                    backoff, losses, failures = RECONNECT_INITIAL, 0, 0
                except Exception as e:
                    self._error_count += 1
                    self.last_error = describe_error(e)
                    if is_connection_error(e):
                        logger.warning("Connection to RT-LAB lost: %s", self.last_error)
                        await self.disconnect()
                        losses += 1
                        # The first loss reconnects at once; from the second, back off.
                        if losses > 1 and await self._wait_or_stop(backoff):
                            break
                        backoff = min(backoff * 2, RECONNECT_MAX) if losses > 1 else backoff
                        continue
                    logger.error("Request failed: %s", self.last_error)
                    failures += 1
                    if failures >= FAILURES_BEFORE_RECONNECT:
                        logger.warning("%d failures in a row, reconnecting", failures)
                        await self.disconnect()
                        failures = 0
            return True
        finally:
            try:
                await asyncio.wait_for(self.disconnect(), SHUTDOWN_TIMEOUT)
            except TimeoutError:
                logger.warning("CloseProject exceeded %.0fs, abandoning it", SHUTDOWN_TIMEOUT)
            self._executor.shutdown(wait=False, cancel_futures=True)

    async def _or_stop(self, coro: Awaitable[Any]) -> tuple[bool, Any]:
        """(True, None) if shutdown wins, `coro` cancelled; else (False, its result)."""
        assert self._stop_event is not None
        work = asyncio.ensure_future(coro)
        stop = asyncio.ensure_future(self._stop_event.wait())
        try:
            await asyncio.wait({work, stop}, return_when=asyncio.FIRST_COMPLETED)
            if work.done():
                return False, work.result()
            return True, None
        finally:
            stop.cancel()
            if not work.done():
                work.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await work

    async def _wait_or_stop(self, seconds: float) -> bool:
        """Wait up to `seconds`; True if shutdown was requested meanwhile."""
        assert self._stop_event is not None
        try:
            await asyncio.wait_for(self._stop_event.wait(), seconds)
        except TimeoutError:
            return False
        return True

    # ─── Action support ─────────────────────────────────────────────────────

    def signal(self, path: str) -> Item:
        return self._lookup(self.signals, path, "signal", "list_signals")

    def parameter(self, path: str) -> Item:
        return self._lookup(self.parameters, path, "parameter", "list_parameters")

    def variable(self, name: str) -> Variable:
        if not self._connected:
            raise RuntimeError("not connected to an RT-LAB project")
        v = self.variables.get(name.strip())
        if v is None:
            raise ValueError(f"Unknown variable '{name}'; use list_variables")
        return v

    def _lookup(self, items: dict[str, Item], path: str, what: str, hint: str) -> Item:
        if not self._connected:
            raise RuntimeError("not connected to an RT-LAB project")
        item = items.get(path.strip().strip("/"))
        if item is None:
            raise ValueError(f"Unknown {what} '{path}'; use {hint}")
        return item

    def describe(self, item: Item) -> dict[str, Any]:
        traced = self._traced.get(item.path)
        out: dict[str, Any] = {"path": item.path}
        if item.kind != Kind.PARAMETER:
            out["type"] = mdl.SignalType(item.signal_type).name
            out["label"] = item.label
            out["writable"] = not item.readonly
        out["event"] = traced.event if traced else ""
        out["field"] = traced.name if traced else ""
        return out

    def status(self) -> dict[str, Any]:
        params = sum(1 for i in self._traced.values() if i.kind == Kind.PARAMETER)
        return {
            "connected": self._connected,
            "project": self.project or self.project_file,
            "model": self.info.get("model", ""),
            "model_state": self.state.name,
            "real_time_mode": self.real_time_mode,
            "rtlab_version": self.info.get("rtlab_version", ""),
            "time_step": self.info.get("time_step", 0.0),
            "signals": len(self.signals),
            "parameters": len(self.parameters),
            "variables": len(self.variables),
            "traced_signals": len(self._traced) - params,
            "traced_parameters": params,
            "poll_interval": self.poll_interval,
            "parameter_interval": self.parameter_interval,
            "polls": self._poll_count,
            "poll_ms": round(self._poll_ms, 3),
            "parameter_sweep_s": round(self._sweep_s, 3),
            "errors": self._error_count,
            "last_error": self.last_error,
        }


class OpalRunner:
    """The event loop the client runs in: signals and stop."""

    def __init__(self, client: OpalClient) -> None:
        self.client = client
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event = asyncio.Event()
        self.failed_at_start = False
        self.wedged = False  # the worker is still inside a call after shutdown

    def run(self) -> None:
        """Run the client until shutdown (blocking)."""
        asyncio.run(self.run_async())

    async def run_async(self) -> None:
        self._loop = asyncio.get_running_loop()
        remove_signal_handlers = self._install_signal_handlers()
        try:
            ok = await self.client.run_async(self._stop_event)
            if not ok:
                self.failed_at_start = True
                logger.error(
                    "Cannot open the RT-LAB project%s: %s",
                    f" '{self.client.project_file}'" if self.client.project_file else "",
                    self.client.last_error,
                )
        finally:
            remove_signal_handlers()
            self.wedged = self.client.in_call

    def stop(self) -> None:
        """Request shutdown. Safe from any thread, including a signal handler."""
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self._stop_event.set)
        else:
            self._stop_event.set()

    def _install_signal_handlers(self) -> Callable[[], None]:
        """Set the stop event on SIGTERM/SIGINT/SIGBREAK; returns the uninstaller.

        sys.exit from a signal.signal handler would drop the project uncleanly.
        """
        loop, event = self._loop, self._stop_event
        if loop is None:
            return lambda: None

        def request_stop() -> None:
            logger.info("Shutdown signal received, stopping")
            event.set()

        installed: list[int] = []
        replaced: dict[int, Any] = {}
        sigs = [signal.SIGTERM, signal.SIGINT]
        if hasattr(signal, "SIGBREAK"):  # Windows: CTRL_BREAK_EVENT
            sigs.append(signal.SIGBREAK)
        for sig in sigs:
            try:
                loop.add_signal_handler(sig, request_stop)
                installed.append(sig)
            except (NotImplementedError, RuntimeError, ValueError):
                # Windows, or off the main thread.
                with contextlib.suppress(ValueError, OSError):
                    replaced[sig] = signal.signal(
                        sig, lambda *_: loop.call_soon_threadsafe(request_stop)
                    )

        def remove() -> None:
            for sig in installed:
                with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                    loop.remove_signal_handler(sig)
            for sig, prior in replaced.items():
                with contextlib.suppress(ValueError, OSError, TypeError):
                    signal.signal(sig, prior if prior is not None else signal.SIG_DFL)

        return remove
