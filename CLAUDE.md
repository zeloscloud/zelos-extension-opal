# CLAUDE.md

## Commands

```bash
just install      # deps + pre-commit hooks
just format       # ruff format + ruff check --fix
just check        # ruff check + ruff format --check
just test         # pytest against the in-process demo model
just hil          # ZELOS_OPAL_PROJECT=<path.llp> [ZELOS_OPAL_RTLAB_PATH=..]: real RT-LAB, read-only
just dev          # app mode, reads the Zelos App config
just demo         # built-in demo model
just trace *ARGS  # an RT-LAB project from the CLI
```

## Key Files

| Path | Role |
|------|------|
| `main.py` | Click entry point; re-exports `ACTION_PREFIX` |
| `zelos_extension_opal/__init__.py` | `ACTION_PREFIX = "OPAL-RT"` - must match `name` in `extension.toml` |
| `zelos_extension_opal/model.py` | Model states, description parsing, trace names, `PathFilter`, `layout` |
| `zelos_extension_opal/link.py` | `Link` protocol, `RtlabLink` (RtlabApi import and calls), `RtlabError`, error classes |
| `zelos_extension_opal/client.py` | `OpalClient` (connect, discovery, jobs, reconnect, rotation, `submit`) and `OpalRunner` (loop, signals) |
| `zelos_extension_opal/actions.py` | Free-function action surface + `register_actions()` |
| `zelos_extension_opal/demo.py` | `DemoModel` (PI loop, setpoint, gains, fault injection) and `DemoLink` |
| `zelos_extension_opal/cli/app.py` | Config load, resolution, startup validation, `serve()` |
| `tests/test_model.py` | Descriptions (real RT-LAB row shapes), filters, collisions |
| `tests/test_client.py` | Demo model end to end: tracing, reload, actions, write safety, recovery, start, shutdown |
| `tests/test_hil.py` | Real project, `ZELOS_OPAL_PROJECT` only |

## Architecture

### Startup

- `cli/app.serve()`: config -> client -> `set_client` -> `register_actions` -> source + log handler -> `zelos_sdk.init(name=ACTION_PREFIX)` -> `client.start` -> `runner.run()`. Registration precedes `init()`.
- Startup problems are one `logger.error` + `sys.exit(1)`. The project must open at start (first contact); a model that is not loaded is fine.
- The prototype's flat config (`project_path`, `param_chunk_size`) is refused with a reason, not misread.

### RtlabApi

- Thread-bound: a project belongs to the thread that called `OpenProject`. Every link call runs on the client's one worker (`ThreadPoolExecutor(max_workers=1)`, which keeps its thread): jobs via `_call`, actions via `submit`.
- Imported from PYTHONPATH, else `advanced.rtlab_path`, else the newest `C:\OPAL-RT\RT-LAB\v*` (`common/python` + `common/bin` appended to `sys.path`). The `.pyd` exists for py3.7-3.13; the manifest pins 3.11.
- `OpenProject(..., returnOnAmbiguity=True)`: otherwise a partial match opens a picker dialog nobody sees.
- Errors: `OpalApiPy.Exception(code, msg)` -> `RtlabError`. EIO/EBADF = connection (reconnect); EINVAL/EACCES on a signal read = not loaded or stale ids (re-read state, describe again once settled; 5 in a row reconnect).
- `GetSignalsDescription` lists every type, control signals included (readonly 0); ids count per type, so reads are one `GetSignalsById` per type. Descriptions and `GetParametersByName` answer on an unloaded model; signal reads need it loaded.
- Signal control is taken for logical id 0 (every subsystem) per write and released in a `finally`; parameter control likewise.
- The extension never calls Load, Execute, Pause, Reset or `SetAcqBlockLastVal` (that one persists to the project's .probe file).

### Client

- Jobs on the loop, one request at a time: health (1 s, `GetModelState`, liveness), signal poll (`poll_interval`, only while LOADED/PAUSED/RUNNING), one parameter chunk per tick (`parameter_interval / chunks`, any settled state). Late jobs restart from now, no burst.
- Re-discovery: on connect, and when the state settles after a transitional one or crosses loaded/unloaded (ids move on reload and recompile). Fields added to a declared event rotate the source (ADS `_rotate_source`); new events are declared in place.
- Rows: every row carries every field of its event. Parameters live under `parameters/`, chunks hold whole events, write read-backs re-read the whole event (`siblings`): the latest value of a field never reads empty.
- Stamps: mid-request host time (`log_at`). RT-LAB gives no sample time on this path.
- A parameter chunk that fails is re-read item by item; a bad path is one ERROR per connection.

### Actions

- Free functions, bound via `set_client`. Failure convention: raise; RT-LAB refusals become `ValueError` with RT-LAB's message and code.
- `submit` waits `timeout`: a request still queued is cancelled (`NotSent`, never runs); a write already started is `MaybeApplied`.
- `auto_config` is standalone and hidden (the schema's `ui:options.autoconfig` names it); running, it goes through the worker.

### Shutdown

- Signals via `loop.add_signal_handler`, else `signal.signal` on Windows (the agent stops a Windows extension with Ctrl+C); both only set the stop event.
- `CloseProject` under `wait_for(..., 3 s)`; an RtlabApi call that never returns leaves `runner.wedged`, and `serve` flushes the trace for 0.3 s and `os._exit`s.

## Code Style

- ruff, line length 100, google docstrings
- `contextlib.suppress(Exception)` over bare `try/except: pass`
- comments state the constraint, not the code
