# Contributing

## Prerequisites

- [Zelos](https://zeloscloud.io) 26.0.10 or later
- [Zelos CLI](https://docs.zeloscloud.io/reference/cli/) 0.1.11 or later
- [just](https://github.com/casey/just)
- [uv](https://docs.astral.sh/uv/)

## Layout

`extension.toml` describes everything the extension contributes:

| Part | Files | Manifest |
| ---- | ----- | -------- |
| Agent runtime | `main.py`, the Python package, `pyproject.toml`, `config.schema.json` | `[agent]`, `[config]` |

The Python package, `zelos_extension_opal/`:

| Module | Role |
| ------ | ---- |
| `__init__.py` | `ACTION_PREFIX`, the action namespace |
| `model.py` | Model states, descriptions, trace names, filters, layout |
| `link.py` | The `Link` protocol, the RtlabApi link, RT-LAB errors |
| `client.py` | Connection, jobs, re-discovery, shutdown |
| `actions.py` | Zelos actions |
| `demo.py` | The demo model and its link |
| `cli/app.py` | App mode, config resolution, `serve` |

## Commands

| Command                | Description                                        |
| ---------------------- | -------------------------------------------------- |
| `just install`         | Install dependencies and the pre-commit hooks      |
| `just format`          | Format code with ruff                              |
| `just check`           | Check formatting and lint (ruff)                   |
| `just test`            | Run tests (pytest)                                 |
| `just package`         | Build and package for the Zelos marketplace        |
| `just release VERSION` | Bump version, check, test, build, commit, tag      |
| `just clean`           | Remove build output, caches and packages           |

`just --list` also shows the recipes each part adds. `just install` installs the
pre-commit hooks in `.pre-commit-config.yaml`.

This repository adds:

| Command          | Description                                                      |
| ---------------- | ---------------------------------------------------------------- |
| `just demo`      | Run against the built-in demo model (no RT-LAB required)         |
| `just trace ...` | Trace an RT-LAB project from the CLI; see `main.py trace --help` |
| `just hil`       | Read-only tests against a real RT-LAB project                    |

## Testing

`just test` runs against the in-process demo model, so it needs no RT-LAB. On an RT-LAB
command station, `just hil` runs `tests/test_hil.py` against a real project and never
writes to it:

```bash
ZELOS_OPAL_PROJECT=C:/projects/rtdemo1/rtdemo1.llp just hil
```

Set `ZELOS_OPAL_RTLAB_PATH` to an RT-LAB version directory to load RtlabApi from it when it
is not already importable (otherwise the newest under `C:/OPAL-RT/RT-LAB`).

## Run it in Zelos

```bash
zelos extensions install-local .
```

A local install runs from your project directory, so you do not install again after a code
change:

- Agent runtime: stop and start the extension. After a dependency change, stop it and run
  `zelos extensions reinstall <id>`.
- `extension.toml`: run `zelos extensions install-local .` again.

## Packaging

```bash
just package
```

The archive lands next to `extension.toml` as `<name>-<version>.tar.gz`, where `<name>` is the
manifest name in lowercase with every other run of characters turned into one hyphen. It holds
`extension.toml`, the README, the icon, the config schema and the paths listed in
`[package] paths`. With an agent runtime it also holds `pyproject.toml`, `uv.lock` and
`actions.json`, the list of actions that run while the extension is stopped.

## Release

```bash
just release 1.0.0
git push --follow-tags
```

The release workflow checks, tests, builds and packages the extension, then attaches the
archive to a GitHub release.
