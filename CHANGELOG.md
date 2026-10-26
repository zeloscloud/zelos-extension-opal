# Changelog

All notable changes to Zelos OPAL-RT Extension will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- Needs Zelos 26.0.10 or later.
- The marketplace listing says what the extension traces and writes, and search finds it by RT-LAB, Simulink, MATLAB and HIL terms.
- Zelos AI runs Auto-configure without asking first: it only reads. Get Status, Model State, List Signals, List Parameters and List Variables are hidden from the app's action list; they still run by name.

## [0.1.2] - 2026-10-23

### Added
- Quick start clip in the README

## [0.1.1] - 2026-10-23

### Added
- Advanced > Demo: the built-in demo model from the config form
- OPAL-RT emblem as the extension icon

## [0.1.0] - 2026-10-21

### Changed
- Rebuilt on the Zelos agent extension structure: `project_file` / `poll_interval` / `parameter_interval` / `include` / `exclude` with an `advanced` section; the earlier config is refused with a reason
- Every RtlabApi call on one worker thread; actions no longer run on the main loop
- Parameters trace under `parameters/<block>`; signals under `<block>`; model health under `_model`
- One action per kind (`write_signal`, `write_parameter`, ...) instead of one per path
- Signal control taken for every subsystem per write, released after it

### Added
- Re-discovery on model reload and recompile
- Writes queued past the timeout are cancelled, never sent late
- Demo model, `trace` CLI, `auto_config`, include/exclude globs
- Bounded shutdown; reconnect with backoff

### Fixed
- Signal labels read from the right description column
- `OpenProject` no longer opens a picker dialog on an ambiguous project
