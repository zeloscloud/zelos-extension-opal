"""Zelos OPAL-RT Extension - RT-LAB support for Zelos.

This extension provides:
- One RT-LAB project traced: every signal (acquisition, dynamic, control) and block parameter
- Re-discovery when the model is reloaded or recompiled
- Zelos SDK integration for trace events and actions (read, write, model state)
- Demo model for testing without RT-LAB
"""

from zelos_extension_opal.client import OpalClient

#: Action namespace for this extension. Single source for both surfaces - the
#: live registration (`zelos_sdk.init(name=ACTION_PREFIX)`) and the at-rest
#: inventory the packaging step dumps from `main.py`, which re-exports this.
#: A mismatch would silently produce two unrelated action trees.
#:
#: Matches `name` in `extension.toml`, which is what a user sees in the
#: extension list, so the address they read there is the address they type.
ACTION_PREFIX = "OPAL-RT"

__all__ = ["ACTION_PREFIX", "OpalClient"]

__version__ = "0.1.0"
