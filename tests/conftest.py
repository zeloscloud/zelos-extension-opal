"""Shared helpers: a client on the demo model, run as `serve` runs it, its rows recorded."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from typing import Any

import zelos_sdk

from zelos_extension_opal import actions
from zelos_extension_opal.client import OpalClient, OpalRunner
from zelos_extension_opal.demo import DemoLink, DemoModel


class Rows:
    """Every row the client logs after this wraps it: (event, time_ns or None, fields)."""

    def __init__(self, client: OpalClient) -> None:
        self.rows: list[tuple[str, int | None, dict[str, Any]]] = []
        self.segments = 1
        declare, rotate = client._declare, client._rotate_source

        def declared(name: str, fields: Any) -> None:
            declare(name, fields)
            client._trace_events[name] = self._wrap(name, client._trace_events[name])

        def rotated() -> None:
            rotate()
            self.segments += 1
            for name, event in list(client._trace_events.items()):
                client._trace_events[name] = self._wrap(name, event)

        client._declare = declared  # type: ignore[method-assign]
        client._rotate_source = rotated  # type: ignore[method-assign]
        for name, event in list(client._trace_events.items()):
            client._trace_events[name] = self._wrap(name, event)

    def _wrap(self, name: str, event: Any) -> Any:
        rows = self.rows

        class Event:
            def log(self, **fields: Any) -> None:
                rows.append((name, None, fields))
                event.log(**fields)

            def log_at(self, stamp: int, **fields: Any) -> None:
                rows.append((name, stamp, fields))
                event.log_at(stamp, **fields)

        return Event()

    def incomplete(self, client: OpalClient) -> list[tuple[str, set[str]]]:
        """Rows missing a declared field of their event: the latest value of that
        field would read empty."""
        declared = {e: {f[0] for f in fs} for e, fs in client._declared.items()}
        return [(e, declared[e] - set(f)) for e, _, f in self.rows if set(f) != declared[e]]

    def of(self, event: str, field: str) -> list[tuple[int | None, Any]]:
        return [(t, f[field]) for e, t, f in self.rows if e == event and field in f]


def demo_client(model: DemoModel | None = None, **kwargs: Any) -> OpalClient:
    model = model or DemoModel()
    kwargs.setdefault("poll_interval", 0.02)
    kwargs.setdefault("parameter_interval", 0.2)
    return OpalClient(link_factory=lambda: DemoLink(model), **kwargs)


@contextlib.asynccontextmanager
async def running(client: OpalClient) -> AsyncIterator[tuple[OpalRunner, Rows]]:
    """`client` started on a fresh source and run in a runner, as `serve` does; stopped on exit."""
    client.start(zelos_sdk.TraceSource("OPAL-RT"), None)
    rows = Rows(client)
    runner = OpalRunner(client)
    actions.set_client(client)
    task = asyncio.create_task(runner.run_async())
    try:
        yield runner, rows
    finally:
        actions.set_client(None)
        runner.stop()
        await asyncio.wait_for(task, 10.0)


async def wait_until(predicate: Any, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"Condition not met within {timeout}s")
        await asyncio.sleep(0.02)
