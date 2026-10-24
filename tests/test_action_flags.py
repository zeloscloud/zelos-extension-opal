"""Pins each action's flags, so a new action or a changed flag is a deliberate choice."""

from __future__ import annotations

import inspect

from zelos_extension_opal import actions


def test_every_action_has_its_flags():
    actual = {
        name: (obj._action.read_only, obj._action.hidden)
        for name, obj in inspect.getmembers(actions, inspect.isfunction)
        if not name.startswith("_") and hasattr(obj, "_action")
    }
    assert actual == {
        "get_status": (True, True),
        "model_state": (True, True),
        "list_signals": (True, True),
        "list_parameters": (True, True),
        "list_variables": (True, True),
        "read_signal": (True, False),
        "write_signal": (False, False),
        "read_parameter": (True, False),
        "write_parameter": (False, False),
        "read_variable": (True, False),
        "write_variable": (False, False),
        "auto_config": (True, True),
    }
