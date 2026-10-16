# kb — keybinding browser

Python is managed with [uv](https://docs.astral.sh/uv/) (no runtime dependencies; Python >= 3.11, pinned to 3.12 in
`.python-version`).

    make install    # uv tool install --editable . -> `kb` on your PATH (~/.local/bin)
    make test       # uv run python -m unittest ...  (isolated env, 37 tests)
    make sync       # refresh Apple + VS Code data and update the snapshot bundled in src/kb/data/
    uv run kb ...   # run from the repo without installing
    uvx --from git+<repo-url> kb ...   # run anywhere without cloning

Layout: `src/kb/cli.py` (tool) · `src/kb/data/` (bundled Apple + VS Code snapshots, used until you run `kb --sync`,
which writes your own copy to `~/.local/share/kb/`) · `tests/` · `pyproject.toml` + `uv.lock`.

Reads your live configs and answers "what does this key do / who owns it".
Read-only: it never writes to any config. `fzf` is optional.

    kb                 interactive browser        kb -k opt+t     who owns this combo
    kb tmux split      search (all words match)   kb -c           conflicts + shadowed shortcuts
    kb -t skhd         one tool                   kb --doctor     sources found / parse warnings
    kb --json          schema_version 1           make test

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
