"""The client end to end on the demo model: tracing, model state changes, actions,
write safety, recovery, startup and shutdown."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from tests.conftest import demo_client, running, wait_until
from zelos_extension_opal import actions
from zelos_extension_opal.cli import app
from zelos_extension_opal.demo import DemoModel
from zelos_extension_opal.link import EIO, EPERM, RtlabError
from zelos_extension_opal.model import HEALTH_EVENT, ModelState

SM = "zelos_demo/sm_plant"
SC = "zelos_demo/sc_console"


async def test_traces_signals_parameters_and_health():
    client = demo_client()
    async with running(client) as (_, rows):
        await wait_until(lambda: len(rows.of(f"{SM}/Plant", "port1")) >= 10, 10.0)
        await wait_until(lambda: rows.of(f"parameters/{SM}/Controller", "Kp"), 10.0)
        await wait_until(lambda: rows.of(HEALTH_EVENT, "state_name"), 10.0)
        clock = [v for _, v in rows.of(f"{SM}/Clock", "port1")]
        stamps = [t for t, _ in rows.of(f"{SM}/Clock", "port1")]
        assert clock == sorted(clock) and stamps == sorted(stamps) and None not in stamps
        assert rows.of(SC, "port1")[-1][1] == 1.0  # the control signal is traced too
        assert rows.of(f"parameters/{SM}/Controller", "Kp")[0][1] == 2.0
        assert rows.of(HEALTH_EVENT, "state_name")[-1][1] == "RUNNING"
        assert client.status()["traced_signals"] == 8 and client.status()["errors"] == 0


async def test_unloaded_model_then_reload_with_a_new_signal():
    """Unloaded: no signal reads (RT-LAB refuses them), parameters still traced. A
    recompile that adds a signal and the load after it: described again, a new
    segment for the new field, which is then traced."""
    model = DemoModel(state=ModelState.LOADABLE)
    client = demo_client(model)
    async with running(client) as (_, rows):
        await wait_until(lambda: rows.of(f"parameters/{SM}/Controller", "Ki"), 10.0)
        assert not rows.of(f"{SM}/Plant", "port1") and client._poll_count == 0
        model.set_state(ModelState.COMPILING)
        model.recompile(extra="Plant/port2")  # a field added to a declared event
        await wait_until(lambda: client.state == ModelState.COMPILING, 5.0)
        model.set_state(ModelState.RUNNING)
        await wait_until(lambda: len(rows.of(f"{SM}/Plant", "port2")) >= 3, 10.0)
        assert rows.segments == 2
        # Ids moved by one: every signal still reads its own value.
        assert rows.of(f"{SM}/Clock", "port1")[-1][1] > 0


@pytest.mark.parametrize("reconnect", [False, True])
async def test_reload_moves_ids(reconnect):
    """A reload seen only as LOADING (or reconnected into) re-describes: after it
    every field reads its own signal, the new one included, the removed one gone."""
    model = DemoModel()
    client = demo_client(model)
    async with running(client) as (_, rows):
        await wait_until(lambda: client._poll_count >= 3, 10.0)
        if reconnect:
            model.fail_next = RtlabError(EIO, "Error sending the request")
        model.set_state(ModelState.LOADING)
        model.recompile(extra="Plant/port2", remove="port2")
        await wait_until(lambda: client.state == ModelState.LOADING, 5.0)
        model.set_state(ModelState.RUNNING)
        await wait_until(lambda: len(rows.of(f"{SM}/Plant", "port2")) >= 3, 10.0)
        await asyncio.sleep(0.2)
        assert abs(rows.of(f"{SM}/Clock", "port1")[-1][1] - model.sim_time) < 0.5
        first = next(
            i for i, r in enumerate(rows.rows) if r[0] == f"{SM}/Plant" and "port2" in r[2]
        )
        after = rows.rows[first:]
        assert all(set(f) == {x[0] for x in client._declared[e]} for e, _, f in after)
        assert not any(e == SM and "port2" in f for e, _, f in after)  # removed: gone


async def test_actions_read_write_and_trace_the_read_back():
    model = DemoModel()
    client = demo_client(model)
    async with running(client) as (_, rows):
        await wait_until(lambda: client._connected, 10.0)
        written = await asyncio.to_thread(actions.write_signal, f"{SC}/port1", "0x2")
        assert written["read_back"] == 2.0 and model.setpoint == 2.0
        await wait_until(lambda: (None, 2.0) in [(None, v) for _, v in rows.of(SC, "port1")], 5.0)
        with pytest.raises(ValueError, match="read-only"):
            await asyncio.to_thread(actions.write_signal, f"{SM}/Plant/port1", "1")
        param = await asyncio.to_thread(actions.write_parameter, f"{SM}/Controller/Kp", "3.5")
        assert param["read_back"] == 3.5
        assert (await asyncio.to_thread(actions.read_parameter, f"{SM}/Controller/Kp"))[
            "value"
        ] == 3.5
        variable = await asyncio.to_thread(actions.write_variable, "setpoint_limit", "50")
        assert variable["read_back"] == 50.0
        assert model.control_holders == 0  # control released after every write
        assert rows.incomplete(client) == []  # read-backs log whole rows too
        listed = await asyncio.to_thread(actions.list_signals, "**/sc_console/*")
        assert listed["signals"] == [
            {
                "path": f"{SC}/port1",
                "type": "CONTROL",
                "label": "setpoint",
                "writable": True,
                "event": SC,
                "field": "port1",
            }
        ]
        with pytest.raises(ValueError, match="Unknown signal"):
            await asyncio.to_thread(actions.read_signal, f"{SM}/Nope/port1")
        # Applied, then the read-back fails: reported applied, not as a failed write.
        model.fail_read_back = RtlabError(13, "Could not get signals")
        partial = await asyncio.to_thread(actions.write_signal, f"{SC}/port1", "3")
        assert partial["read_back"] is None and "13" in partial["read_back_error"]
        assert model.setpoint == 3.0
        model.fail_read_back = None
        await asyncio.to_thread(actions.write_signal, f"{SC}/port1", "2")
        model.control_holders = 1  # another client (the RT-LAB UI) holds control
        with pytest.raises(ValueError, match=f"RT-LAB error {EPERM}"):
            await asyncio.to_thread(actions.write_signal, f"{SC}/port1", "5")
        assert model.setpoint == 2.0


async def test_write_queued_past_the_timeout_is_never_sent():
    """A write stuck behind a slow request must not land later: cancelled, reported unsent."""
    model = DemoModel()
    client = demo_client(model, timeout=0.3)
    async with running(client):
        await wait_until(lambda: client._connected, 10.0)
        model.stall = 1.0  # the poll in flight now takes a second
        await asyncio.sleep(0.1)
        with pytest.raises(TimeoutError, match="not sent"):
            await asyncio.to_thread(actions.write_signal, f"{SC}/port1", "7")
        model.stall = 0.0
        await asyncio.sleep(1.5)
        assert model.setpoint == 1.0 and model.writes == []


async def test_write_sent_and_unanswered_may_have_applied():
    model = DemoModel()
    client = demo_client(model, timeout=0.3, poll_interval=5.0, parameter_interval=0)
    async with running(client):
        await wait_until(lambda: client._connected, 10.0)
        await asyncio.sleep(0.2)  # first poll done; the worker is idle
        model.stall = 1.0
        with pytest.raises(TimeoutError, match="may still apply"):
            await asyncio.to_thread(actions.write_signal, f"{SC}/port1", "7")
        model.stall = 0.0


async def test_lost_connection_reconnects_and_resumes():
    model = DemoModel()
    client = demo_client(model)
    async with running(client) as (_, rows):
        await wait_until(lambda: client._poll_count >= 3, 10.0)
        model.fail_next = RtlabError(EIO, "Error sending the request or receiving the reply")
        before = len(rows.of(f"{SM}/Plant", "port1"))
        await wait_until(lambda: len(rows.of(f"{SM}/Plant", "port1")) > before + 5, 10.0)
        assert [c for _, c in rows.of(HEALTH_EVENT, "connected")].count(False) == 1


async def test_first_connect_failure_fails_the_start():
    model = DemoModel()
    model.open_error = RtlabError(22, "Error 22: File not found: C:\\nope.llp.")
    client = demo_client(model)
    async with running(client) as (runner, _):
        await wait_until(lambda: runner.failed_at_start, 5.0)
    assert "File not found" in (client.last_error or "")


async def test_shutdown_is_bounded_when_rtlab_never_returns():
    model = DemoModel()
    client = demo_client(model)
    async with running(client) as (runner, _):
        await wait_until(lambda: client._poll_count >= 1, 10.0)
        model.stall = 30.0
        await asyncio.sleep(0.2)
        started = time.monotonic()
        runner.stop()
        await wait_until(lambda: runner.wedged, 6.0)
    assert time.monotonic() - started < 4.5
    model.stall = 0.0


def test_config_resolution():
    kwargs, advanced = app.resolve({"project_file": "C:/p.llp", "advanced": {"prefix": " "}})
    assert advanced["demo"] is False
    assert kwargs["project_file"] == "C:/p.llp" and kwargs["poll_interval"] == 0.1
    assert advanced["prefix"] == "OPAL-RT"
    with pytest.raises(SystemExit):
        app.build_client({**kwargs, "poll_interval": 0})


@pytest.mark.parametrize(
    ("config", "reason"),
    [
        (
            {"project_path": "C:/p.llp", "param_chunk_size": 100},
            "<root>: Additional properties are not allowed "
            "('param_chunk_size', 'project_path' were unexpected)",
        ),
        (
            {"project_file": "C:/p.llp", "advanced": {"param_chunk_size": 100}},
            "advanced: Additional properties are not allowed ('param_chunk_size' was unexpected)",
        ),
    ],
)
def test_saved_config_off_the_schema_exits(tmp_path, monkeypatch, caplog, config, reason):
    saved = tmp_path / "config.json"
    saved.write_text(json.dumps(config))
    monkeypatch.setenv("ZELOS_CONFIG_PATH", str(saved))
    monkeypatch.chdir(Path(__file__).parents[1])
    with pytest.raises(SystemExit) as exc:
        app.run_app_mode()
    assert exc.value.code == 1
    assert [r.getMessage() for r in caplog.records if r.levelname == "ERROR"] == [
        f"Invalid configuration: {reason}"
    ]
