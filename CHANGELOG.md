# Changelog

All notable changes to `kb`. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [Semantic Versioning](https://semver.org/) — new features bump the minor number, fixes and
tooling-only changes bump the patch number. The `--json` output has its own `schema_version` (currently 1) that
changes only when that output breaks.

## [Unreleased]

### Changed
- The version now lives only in `pyproject.toml`; the code reads it from there (checkout) or from the installed
  package metadata.

### Added
- `just release <patch|minor|major>` (`make release LEVEL=…`): checks the tree is clean and on `main`, runs lint and
  tests, bumps the version with `uv version`, promotes this changelog section, commits and creates an annotated
  `vX.Y.Z` tag. Nothing is pushed.
- This changelog, and tags `v1.3.0` … `v1.6.0` for the existing history.

## [1.6.0] - 2026-10-16

### Changed
- Only the sources a query needs are read: `kb -t tmux …` no longer touches VS Code or macOS files (about 60 ms).
- VS Code extension keybindings come from VS Code's own per-profile manifest cache (2 files per profile) instead of
  opening ~140 `package.json` files; falls back to scanning when the cache is missing.
- VS Code defaults/your keybindings and VS Code extensions are separate sources, so a stall in one no longer hides
  the other.

### Fixed
- Runs that took 10+ seconds (the VS Code source hit its time budget under endpoint-security file scanning and was
  skipped entirely).

### Added
- `-t` rejects unknown tool names with the list of valid ones (exit 2).

## [1.5.1] - 2026-10-16

### Added
- `just lint|fmt|check` and matching `make` targets (ruff, ruff format, mypy, tests); `py.typed` marker.

### Changed
- Lint-clean under ruff (E,F,W,B,UP,SIM,S,C4,PIE,RET,PL,RUF,PTH,ARG,TRY,A,N,I) and mypy with `check_untyped_defs`.
- Module globals replaced by a `Settings` object; broad `except` blocks narrowed; one audited call site for
  `/usr/bin/defaults`.

## [1.5.0] - 2026-10-16

### Added
- `--by keys|name|all` to match search words against the key combo only, what a binding does only, or everything.
- `-m/--mod` to filter by modifier (`command`/`cmd`/`⌘`, `option`/`alt`/`⌥`, `ctrl`, `shift`, `fn`); works on single
  combos, chords and tmux-style keys.

## [1.4.1] - 2026-10-16

### Fixed
- Every source now runs concurrently under a watchdog (`KB_LOADER_TIMEOUT`, default 10 s). A stalled source is
  skipped with a warning instead of freezing `kb`; progress notice after 2 s on a TTY.

### Added
- `kb --debug` (per-source timings) and a slow-source line in `kb --doctor`.

## [1.4.0] - 2026-10-16

### Changed
- Python is managed with uv: `pyproject.toml` (uv_build), `uv.lock`, `.python-version`, `src/kb/` package with a
  `kb` console script and `python -m kb`.
- Apple and VS Code data snapshots are bundled in the package; your synced copy in `~/.local/share/kb/` wins.

### Added
- `justfile` alongside the `Makefile`; README install matrix for uv, pipx, pip, just and make.

## [1.3.0] - 2026-10-16

### Added
- Initial release: a searchable index of keybindings read from AeroSpace, skhd, tmux, herdr, Ghostty, Rectangle,
  Raycast, macOS (live CoreGraphics values, Apple Support article, AppKit `StandardKeyBinding.dict`), VS Code
  (Microsoft defaults, your keybindings, extensions) and zsh.
- Conflict and shadow detection (`-c`), fzf browser, `--json` (schema 1), `--doctor`, `--sync*` downloads with host
  allow-listing, secret redaction and terminal-escape stripping of config text.

[Unreleased]: https://example.invalid/kb/compare/v1.6.0...HEAD
