"""Against a real RT-LAB project on this machine, read-only: ZELOS_OPAL_PROJECT=<path.llp>.

Discovery, model state and parameter reads work on any compiled model; signal
reads only on a loaded one (skipped otherwise). Nothing is written.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from tests.conftest import running, wait_until
from zelos_extension_opal import actions
from zelos_extension_opal.client import OpalClient
from zelos_extension_opal.model import HEALTH_EVENT, LOADED, Kind

PROJECT = os.environ.get("ZELOS_OPAL_PROJECT", "")
pytestmark = pytest.mark.skipif(not PROJECT, reason="ZELOS_OPAL_PROJECT not set")


async def test_real_project_traces_read_only():
    client = OpalClient(
        project_file=PROJECT,
        rtlab_path=os.environ.get("ZELOS_OPAL_RTLAB_PATH", ""),
        poll_interval=0.1,
        parameter_interval=1.0,
    )
    async with running(client) as (runner, rows):
        await wait_until(lambda: client._connected or runner.failed_at_start, 120.0)
        assert client._connected, client.last_error
        assert client.signals, "the model describes no signals"
        await wait_until(lambda: rows.of(HEALTH_EVENT, "state_name"), 10.0)
        if client.parameters:
            param = next(iter(client.parameters.values()))
            read = await asyncio.to_thread(actions.read_parameter, param.path)
            assert isinstance(read["value"], float)
            await wait_until(lambda: rows.of(param.event, param.name), 10.0)
        if client.state not in LOADED:
            pytest.skip(f"model {client.state.name}: signal reads need a loaded model")
        await wait_until(lambda: client._poll_count >= 10, 10.0)
        signal = next(i for i in client._traced.values() if i.kind != Kind.PARAMETER)
        assert rows.of(signal.event, signal.name)
        assert client.status()["errors"] == 0
