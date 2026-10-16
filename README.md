# kb — keybinding browser

Reads your live configs and answers "what does this key do, and who owns it?" across AeroSpace, skhd, tmux, herdr,
Ghostty, Rectangle, Raycast, macOS, VS Code and zsh. Read-only: it never writes to any config.

    kb                 interactive browser        kb -k opt+t     who owns this combo
    kb tmux split      search (all words match)   kb -c           conflicts + shadowed shortcuts
    kb -t skhd         one tool                   kb --doctor     sources found / parse warnings
    kb --json          schema_version 1           kb --sync       refresh Apple + VS Code data

## Install

Requirements: Python 3.11+ (stdlib only, no runtime dependencies). `fzf` is optional and enables the interactive
browser. The macOS sources (system shortcuts, Rectangle, Raycast) are skipped on other platforms; the rest work
anywhere.

Pick whichever tool you already use — all of them install the same `kb` command:

| Tool | Install | Run without installing | Uninstall |
|---|---|---|---|
| **uv** (recommended) | `uv tool install .` | `uvx --from . kb` | `uv tool uninstall kb` |
| **pipx** | `pipx install .` | `pipx run --spec . kb` | `pipx uninstall kb` |
| **pip** | `pip install .` (use a venv) | `python -m kb` after installing | `pip uninstall kb` |
| **just** | `just install` (editable) or `just install-release` | `just --list` shows all recipes | `just uninstall` |
| **make** | `make install` (editable) or `make install-release` | | `make uninstall` |

`just` and `make` are thin wrappers over uv with identical recipes (`test`, `install`, `install-release`,
`uninstall`, `sync`, `lock`, `doctor`), so use either. Once the repo has a remote, `uvx --from git+<repo-url> kb`
runs it anywhere without cloning. Check an install with `kb --version` and `kb --doctor`.

`kb` ships with a snapshot of Apple's and VS Code's default shortcuts, so it works immediately after install.
`kb --sync` downloads fresh copies into `~/.local/share/kb/` (yours win over the bundled snapshot).

## Develop

    just test          # or: make test  — isolated uv env, 37 tests
    uv run kb ...      # run from the checkout
    just sync          # refresh data and update the snapshot bundled in src/kb/data/

Layout: `src/kb/cli.py` (the tool) · `src/kb/data/` (bundled snapshots) · `tests/` · `pyproject.toml` (uv_build
backend) + `uv.lock` · `.python-version` (3.12 for development; the code supports 3.11+).

## How it works

Sources: AeroSpace, skhd, tmux, herdr, Ghostty, Rectangle, Raycast (global hotkey), macOS system/standard/app
shortcuts, VS Code, zsh bindkey. Add a source = write `load_x() -> [Binding]` and append it to `LOADERS`.

Design notes
- Config text is untrusted for display: control characters are stripped and credential-looking strings are
  redacted in descriptions, commands, `--json` and the fzf preview.
- System tools are called by absolute path (`/usr/bin/defaults`), never via `$PATH`.
- The fzf preview reads a 0600 snapshot in a private temp dir instead of re-parsing every config per keystroke.
- One broken config never hides the rest; failures surface in `--doctor` and on stderr.
- Exit codes: 0 ok · 1 no match / conflicts / doctor warnings · 2 usage error (usable in CI or bootstrap checks).

macOS shortcuts: where each comes from (nothing is typed in from memory)
- System shortcuts (Mission Control, screenshots, Spotlight, input sources…): the *effective* value and enabled
  state are read live from macOS via CoreGraphics `CGSGetSymbolicHotKeyValue` (private API; falls back to the
  `symbolichotkeys` plist, customised entries only). Apple publishes no names for these IDs, so a short list of
  verified labels lives in `SYSTEM_HOTKEY_NAMES`; other IDs are named from Apple's article by matching the combo,
  and anything still unnamed is hidden unless `--all-system`.
- Standard shortcuts (⌘C, Finder, text, accessibility): Apple Support "Mac keyboard shortcuts"
  (https://support.apple.com/en-us/102650), downloaded with `kb --sync-apple` into
  `~/.local/share/kb/apple-shortcuts.json` (a snapshot is bundled in `src/kb/data/`, with source URL, fetch time and SHA-256).
  Refresh it when a new macOS ships; it refuses to overwrite on a layout change (<50 rows) or a non-Apple host.
- Cocoa text editing: Apple's own `AppKit.framework/Resources/StandardKeyBinding.dict`.
- Your changes: custom App Shortcuts (`NSUserKeyEquivalents`) and `~/Library/KeyBindings/DefaultKeyBinding.dict`.
- System shortcuts that are disabled on this Mac (e.g. Spotlight when Raycast owns ⌘Space) are annotated and
  excluded from shadow warnings.

VS Code (`-t vscode`)
- Defaults: Microsoft's reference page https://code.visualstudio.com/docs/reference/default-keybindings (macOS column
  via its `data-osx` attribute), downloaded with `kb --sync-vscode` (or `kb --sync` for everything) into
  `~/.local/share/kb/vscode-default-keybindings.json`. VS Code ships its defaults compiled into the app, so the web
  reference is the only machine-readable source; the installed version is shown in `kb --doctor` so you can spot drift.
- Live from this Mac: your `keybindings.json` and profile keybindings (JSONC; `-command` entries mark a default as
  removed), and `contributes.keybindings` from built-in and installed extensions (`mac` key preferred, `%nls%` titles
  resolved).
- Chords (`⌘K ⌘C`) are shown and searchable but never compared as single combos. `when` clauses are kept in the text.
