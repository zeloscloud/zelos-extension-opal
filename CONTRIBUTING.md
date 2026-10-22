# Contributing

## Development Workflow

```bash
just install  # dependencies + pre-commit hooks
just dev      # run app mode locally
just demo     # run against the built-in demo model
just test     # pytest
just check    # ruff lint + format check
just format   # auto-fix
```

1. Make your changes
2. `just format`
3. `just test`, and `just hil` on an RT-LAB command station
4. Commit (pre-commit hooks run ruff automatically)

## Project Structure

```
zelos-extension-opal/
├── extension.toml                  # Extension manifest (name, version, host, package paths)
├── config.schema.json              # Configuration UI schema
├── main.py                         # Click entry point
├── pyproject.toml                  # Dependencies, ruff and pytest config
├── uv.lock                         # Locked dependency versions
├── Justfile                        # Development commands
├── CLAUDE.md                       # Architecture notes
├── zelos_extension_opal/
│   ├── __init__.py                 # ACTION_PREFIX, package exports
│   ├── model.py                    # States, descriptions, names, filters, layout
│   ├── link.py                     # Link protocol, RtlabApi link, errors
│   ├── client.py                   # Connection, jobs, re-discovery, shutdown
│   ├── actions.py                  # Zelos actions
│   ├── demo.py                     # Demo model and link
│   └── cli/app.py                  # App mode, config resolution, serve
└── tests/
    ├── conftest.py                 # Demo client, runner, row recorder
    ├── test_model.py
    ├── test_client.py
    └── test_hil.py                 # Real RT-LAB, ZELOS_OPAL_PROJECT only
```
