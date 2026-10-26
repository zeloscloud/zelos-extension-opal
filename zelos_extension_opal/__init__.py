"""Zelos OPAL-RT Extension - RT-LAB support for Zelos.

This extension provides:
- One RT-LAB project traced: every signal (acquisition, dynamic, control) and block parameter
- Re-discovery when the model is reloaded or recompiled
- Zelos SDK integration for trace events and actions (read, write, model state)
- Demo model for testing without RT-LAB
"""

#: Equals `name` in extension.toml, so actions sit under the name users see.
ACTION_PREFIX = "OPAL-RT"
