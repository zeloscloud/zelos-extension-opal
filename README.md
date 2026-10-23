# Zelos OPAL-RT

A Zelos extension for OPAL-RT RT-LAB. Trace every signal and block parameter of an RT-LAB model, and read and write control signals, parameters and workspace variables from the Zelos App. Runs on the RT-LAB command station (Windows) through RT-LAB's own Python API.

## Features

- 📡 **Every signal**: Acquisition, dynamic and control signals, read by id each poll while the model is loaded
- 🎛️ **Every parameter**: Block parameters traced across a configurable sweep, loaded or not
- ✏️ **Read & write actions**: Control signals, parameters and MATLAB workspace variables, read back after each write
- 🔄 **Follows the model**: A reload or recompile is described again; new signals join the trace
- 🛡️ **Never drives the model**: No load, execute, pause or reset; a write that cannot be sent in time is dropped, never sent late
- 🧪 **Demo mode**: Built-in demo model for testing without RT-LAB

## Quick Start

[![Install the OPAL-RT extension, turn on Advanced > Demo, start it and plot the demo model's plant response](assets/howto/quickstart/quickstart.avif)](assets/howto/quickstart/quickstart.mp4)

On the RT-LAB command station, with the agent running:

```bash
zelos extensions install zeloscloud/zelos-extension-opal
zelos extensions start zeloscloud.zelos-extension-opal \
  --config '{"project_file": "C:/projects/rtdemo1/rtdemo1.llp"}'
```

In the app:

1. **Install** the extension from the Zelos App
2. **Configure** the Project File, or leave it empty with the project open in RT-LAB, or press Auto-configure (no RT-LAB yet: Advanced > Demo)
3. **Start** the extension to begin streaming data
4. **View** real-time model signals in your Zelos App

## Configuration

| Setting | Default | Description |
|---------|---------|-------------|
| Project File | | RT-LAB project (`.llp`); empty = the one project RT-LAB has open with a model |
| Poll Interval | `0.1` s | Signal read period while the model is loaded |
| Parameter Interval | `10` s | One pass over every traced parameter; `0` = parameters not traced |
| Include / Exclude | | Path globs (`rtdemo1/sm_computation/**`, `**/OpComm/**`) |

### Advanced

| Setting | Default | Description |
|---------|---------|-------------|
| `prefix` | `OPAL-RT` | Trace source |
| `rtlab_path` | newest | RT-LAB version directory (`C:/OPAL-RT/RT-LAB/v2025.4.7.43`) |
| `timeout` | `10` s | Action wait for RT-LAB; a write still queued when it expires is cancelled |
| `parameter_chunk` | `100` | Parameters per read |
| `log_level` | `INFO` | Logging verbosity |
| `demo` | off | Built-in demo model instead of a project; no RT-LAB needed |

The project must open at start, or the extension stops with an error naming it. Once running, a lost controller connection is retried with backoff (3 s doubling to 60 s). A model that is not loaded is not an error: its parameters are traced, its signals once it loads.

### Trace layout

| Event | Fields |
|-------|--------|
| `OPAL-RT/<model>/<subsystem>/<block>` | the block's signal ports (`port1`) |
| `OPAL-RT/parameters/<model>/<subsystem>/<block>` | the block's parameters (`Gain`) |
| `OPAL-RT/_model` | state, real-time mode, counters, poll time, last error |
| `OPAL-RT/log` | the extension's log |

Signals are stamped with the host time of the read. Two paths that sanitize to the same name are traced once, the other skipped with a warning.

## Actions

| Action | Description |
|--------|-------------|
| `get_status` | Connection, model state, counters |
| `model_state` | State, real-time mode, time step and subsystems, read now |
| `list_signals` / `list_parameters` / `list_variables` | What the model holds and where it is traced; `pattern` filters |
| `read_signal` / `read_parameter` / `read_variable` | One value, read now |
| `write_signal` | Set a control signal (signal control taken for the write only), read back |
| `write_parameter` | Set a block parameter (parameter control taken for the write only), read back |
| `write_variable` | Set a MATLAB workspace variable, read back |
| `auto_config` | Fill in Project File from the projects RT-LAB has open |

```bash
zelos actions execute OPAL-RT/write_signal --params '{"path":"rtdemo1/sc_user_interface/port1","value":"2.5"}'
zelos actions execute OPAL-RT/read_parameter --params '{"path":"rtdemo1/sm_computation/Gain Ki/Gain"}'
```

RT-LAB accepts a parameter write on a model that is not loaded: it changes the value the next load starts from.

## Development

```bash
just install      # deps + pre-commit hooks
just format       # ruff format + fix
just check        # ruff lint + format check
just test         # pytest, against the demo model
just hil          # read-only against a real project: ZELOS_OPAL_PROJECT=<path.llp> just hil
just dev          # run app mode locally
just demo         # built-in demo model
just trace ...    # trace a project from the CLI
```

## CLI Usage

```bash
uv run main.py                                           # app mode (Zelos App config)
uv run main.py demo                                      # built-in demo model
uv run main.py trace C:/projects/rtdemo1/rtdemo1.llp     # one project
uv run main.py trace --include 'rtdemo1/sm_computation/**' -i 0.05
```

## Links

- [Zelos Documentation](https://docs.zeloscloud.io)
- [SDK Guide](https://docs.zeloscloud.io/sdk)
- [GitHub Issues](https://github.com/zeloscloud/zelos-extension-opal/issues)

## Support

- 📖 [Zelos Documentation](https://docs.zeloscloud.io)
- 🐛 [GitHub Issues](https://github.com/zeloscloud/zelos-extension-opal/issues)
- 📧 help@zeloscloud.io

## License

MIT License - see [LICENSE](LICENSE) for details.

---

**Built with [Zelos](https://zeloscloud.io)**
