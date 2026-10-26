"""Pins each action's flags and the action namespace, so a change to either is deliberate."""

from __future__ import annotations

import inspect
import tomllib
from pathlib import Path

from zelos_extension_opal import ACTION_PREFIX, actions


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


def test_actions_register_under_the_manifest_name():
    manifest = tomllib.loads((Path(__file__).parents[1] / "extension.toml").read_text())
    assert manifest["name"] == ACTION_PREFIX
