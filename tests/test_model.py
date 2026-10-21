"""Descriptions as RT-LAB returns them, names, filters and layout."""

from __future__ import annotations

import pytest

from zelos_extension_opal import model as mdl

# Rows as RtlabApi v2025.4 returns them for rtdemo1.
SIGNAL_ROWS = [
    (1, 4, "rtdemo1/sm_computation/Sum3/port1", "Error", "", 1, 0.0),
    (1, 5, "rtdemo1/sm_computation/Gain Kp/port1", "signal1", "", 1, 0.0),
    (2, 1, "rtdemo1/sc_user_interface/port1", "signal1", "", 0, 0.0),
    (0, 1, "rtdemo1/sm_computation/port1", "signal1", "", 1, 0.0),
]
PARAMETER_ROWS = [(7, "rtdemo1/sm_computation/Gain Ki", "Gain", "", 5.0)]


def test_descriptions_to_items():
    signals = mdl.parse_signals(SIGNAL_ROWS)
    assert [(s.kind, s.signal_type, s.id, s.readonly) for s in signals] == [
        (mdl.Kind.SIGNAL, 1, 4, True),
        (mdl.Kind.SIGNAL, 1, 5, True),
        (mdl.Kind.CONTROL, 2, 1, False),
        (mdl.Kind.SIGNAL, 0, 1, True),
    ]
    assert (signals[1].event, signals[1].name) == ("rtdemo1/sm_computation/Gain Kp", "port1")
    (param,) = mdl.parse_parameters(PARAMETER_ROWS)
    assert param.path == "rtdemo1/sm_computation/Gain Ki/Gain"
    assert (param.event, param.name, param.readonly) == (
        "parameters/rtdemo1/sm_computation/Gain Ki",
        "Gain",
        False,
    )


def test_filters_and_collisions():
    items = mdl.parse_signals(SIGNAL_ROWS) + mdl.parse_parameters(PARAMETER_ROWS)
    keep = mdl.PathFilter.parse(["rtdemo1/sm_computation/**"], ["**/Gain*/*"])
    assert [i.path for i in items if keep.keeps(i.path)] == [
        "rtdemo1/sm_computation/Sum3/port1",
        "rtdemo1/sm_computation/port1",
    ]
    with pytest.raises(ValueError):
        mdl.PathFilter.parse(["rtdemo1//x"])
    # `port(1)` and `port_1` are both field `port_1`: the second is dropped, not renamed.
    twins = mdl.parse_signals(
        [(1, 1, "m/sm/B/port(1)", "", "", 1, 0.0), (1, 2, "m/sm/B/port_1", "", "", 1, 0.0)]
    )
    events, dropped = mdl.layout(twins)
    assert [i.path for i in events["m/sm/B"]] == ["m/sm/B/port(1)"] and dropped == ["m/sm/B/port_1"]


def test_parse_value():
    assert mdl.parse_value(" 2.5 ") == 2.5 and mdl.parse_value("0x1F") == 31.0
    with pytest.raises(ValueError, match="not a number"):
        mdl.parse_value("on")
