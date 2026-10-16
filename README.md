# kb — keybinding browser

Reads your live configs and answers "what does this key do, and who owns it?" across AeroSpace, skhd, tmux, herdr,
Ghostty, Rectangle, Raycast, macOS, VS Code and zsh. Read-only: it never writes to any config.

    kb                 interactive browser        kb -k opt+t     who owns this combo
    kb tmux split      search (all words match)   kb -c           conflicts + shadowed shortcuts
    kb -t skhd         one tool                   kb --doctor     sources found / parse warnings
    kb --json          schema_version 1           kb --sync       refresh Apple + VS Code data
    kb -m command      every binding using ⌘      kb --by keys left   search the key combo only
    kb --by name copy  search what it does only   kb -m cmd,shift -t vscode   ⌘⇧ combos in one tool

Searching: free words match everything by default. Use `--by keys` to match only the key combo (so `command` means the
⌘ key, not the word in a description) or `--by name` to match only what a binding does. `-m/--mod` filters by
modifier — `command|cmd|⌘`, `option|alt|opt|⌥`, `ctrl|control|⌃`, `shift|⇧`, `fn` — repeat it or comma-separate
to require several. It also works on chords (`⌘K ⌘C`) and tmux-style keys. `-k` is for one exact combo.

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

    just check         # or: make check — ruff + ruff format --check + mypy, then the tests
    just test          # or: make test  — isolated uv env, 46 tests
    just fmt           # apply formatter and safe lint fixes
    uv run kb ...      # run from the checkout
    just sync          # refresh data and update the snapshot bundled in src/kb/data/

Versioning and releases: [SemVer](https://semver.org/) (features bump minor, fixes/tooling bump patch), with the
version stored only in `pyproject.toml` (the code reads it from there, or from installed package metadata). History is in
[CHANGELOG.md](CHANGELOG.md) (Keep a Changelog) and every release has an annotated `vX.Y.Z` git tag. The `--json`
output has its own `schema_version`, bumped only when that output breaks.

    # 1. describe the change under "## [Unreleased]" in CHANGELOG.md and commit it
    just release-dry minor   # preview: new version + release notes, changes nothing
    just release minor       # or: make release LEVEL=minor   (patch | minor | major | X.Y.Z)

`release` refuses to run unless the tree is clean, you are on `main`, the tag is new and `[Unreleased]` is not empty. It
then runs lint, type-check and tests, bumps the version with `uv version`, promotes the changelog section, commits
`release: vX.Y.Z` and creates the annotated tag (restoring the files if any step fails). It never pushes; do
`git push && git push --tags` yourself.

Quality gates: ruff (rules E,F,W,B,UP,SIM,S,C4,PIE,RET,PL,RUF,PTH,ARG,TRY,A,N,I; config and the few justified ignores in
`pyproject.toml`), `ruff format`, and mypy with `check_untyped_defs`. The package ships `py.typed`.

Troubleshooting a slow or "hanging" run: `kb --debug` prints per-source timings; `kb --doctor` flags slow sources.
Each source is bounded by `KB_LOADER_TIMEOUT` (default 10 s) and skipped with a warning if it stalls. Endpoint-security
tools (e.g. Microsoft Defender for Endpoint) scan every file a *new process* opens, which can make a source that opens
many small files take seconds; `kb` therefore reads as few files as possible (see below). If it still stalls, ask your
admin to exclude the `kb` tool environment (`~/.local/share/uv/tools/kb`) from real-time scanning.

Layout: `src/kb/cli.py` (the tool) · `src/kb/data/` (bundled snapshots) · `tests/` · `pyproject.toml` (uv_build
backend) + `uv.lock` · `.python-version` (3.12 for development; the code supports 3.11+).

## How it works

- **Only what a query needs is read.** `kb -t tmux ...` reads just the tmux config (about 60 ms); `-t vscode` reads
  VS Code's sources only. Plain searches, `-k`, `-m`, `-c` and `--doctor` read everything because they compare or
  count across tools. Unknown `-t` names are rejected with the list of valid tools.
- **VS Code extensions come from VS Code's own manifest cache** (`~/Library/Application Support/Code/CachedProfilesData/*/
  extensions.{builtin,user}.*.cache`, titles already localized): two files per profile instead of opening ~140
  `package.json` files. If the cache is missing, `kb` falls back to scanning the manifests and says so in `--doctor`.
- Sources run concurrently and independently (`vscode` defaults/your keybindings and `vscode` extensions are separate
  sources), so one slow read cannot hide the rest.

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
