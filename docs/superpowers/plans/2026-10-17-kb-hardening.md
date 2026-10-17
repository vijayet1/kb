# kb Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the security, scripting-UX and download-safety findings from the four reviews (generic, SecOps, architect, UX) of kb 1.6.0, in releasable slices.

**Architecture:** Stay in `src/kb/cli.py` for Phases 1-2 (small, test-first fixes, no restructuring). CI lands first as a safety net. The module split and typed key model are out of scope here and listed as separate follow-up plans (Phase 4) because they are independent subsystems.

**Tech Stack:** Python 3.11+, stdlib only, unittest, ruff, mypy, uv, just. Verification command: `just check` (the only one).

**Spec:** the four review reports in this conversation, consolidated in the "Finding index" below.

## Global Constraints

- Stdlib-only runtime dependencies (zero).
- Python floor 3.11 (`requires-python` in `pyproject.toml`).
- `kb` stays read-only and portable; it never writes outside `DATA_DIR`.
- Auto-download only from `https://support.apple.com` and `https://code.visualstudio.com`, 8 s auto-sync timeout, `KB_NO_AUTO_SYNC=1` disables.
- Apple and VS Code data are never committed or packaged.
- Version has one source: `pyproject.toml`. Do not tag, push or publish unless asked; releases use `just release`.
- Exit codes stay 0 ok / 1 no match, conflicts or doctor warnings / 2 usage error.
- Tests use `Sandbox` (tests/test_kb.py) and never touch the real HOME or network.
- Always commit before changing anything (working tree must be clean at the start of each task).

## Finding index (what each task closes)

| ID | Source | Finding | Task |
|---|---|---|---|
| S9 | SecOps | no CI, no SECURITY.md | 1 |
| S1 | SecOps | `sanitize` keeps tab/newline, bidi, zero-width | 2 |
| S11 | SecOps | U+2028 in a config comment becomes a phantom live binding (`splitlines`); reproduced | 2 |
| S7 | SecOps | loader warnings printed unsanitised | 2 |
| F1 | UX | `kb -k ''` dumps everything, rc 0 | 3 |
| F2 | UX | `--json` silently ignored with `-c`/`--tools`/`--doctor` | 3 |
| F3 | UX | BrokenPipe traceback on `\| head` | 3 |
| F4 | UX | "N bindings" footer on stdout | 3 |
| F9 | UX | no "did you mean" for `-t` | 3 |
| F8 | UX | `FORCE_COLOR` ignored, empty `NO_COLOR` disables | 4 |
| F12/F13 | UX | "shadow" unexplained, no summary line | 5 |
| S4 | SecOps | `sys.executable` unquoted in fzf preview | 6 |
| rel-XDG, S3 | generic, SecOps | relative `XDG_DATA_HOME`, data dir mode, tmp file | 7 |
| redirect, deadline | generic | redirect checked after request; per-read timeout; leftover tmp | 8 |
| corrupt, S2 | generic, SecOps, arch | corrupt/odd data file breaks a source forever | 9 |
| net-guard | generic | tests protected from the network only by env var | 10 |
| isolation | arch, SecOps, generic | network and data-file code mixed into a 2000-line module; no guard that nothing else touches the network | 6c |

Deferred (Phase 4, separate plans): the rest of the module split (sources, model, query, render), width-aware output/pager/TSV/completions/ranking (F5-F7, F10, F11, F14-F24), `Env` + source registry, download/load separation, typed `Chord` model, exit-code/diagnostics redesign, module split.

## Release slices

| Phase | Tasks | Release |
|---|---|---|
| 0 | 1 | none (CI and docs) |
| 1 | 2-6 | `1.7.0` (output safety and scripting) |
| 2 | 6c, 7-10 | `1.8.0` (network isolation and download hardening) |

Run `just release-dry <level>` first; the user chooses to release.

---

## Phase 0

### Task 1: CI and SECURITY.md

**Files:**
- Create: `.github/workflows/ci.yml`
- Create: `.github/dependabot.yml`
- Create: `SECURITY.md`

- [ ] **Step 1: Write the workflow**

```yaml
name: CI
on:
  pull_request:
  push:
    branches: [main]
permissions:
  contents: read
jobs:
  check:
    name: just check (${{ matrix.os }}, py${{ matrix.python }})
    runs-on: ${{ matrix.os }}
    strategy:
      fail-fast: false
      matrix:
        os: [macos-latest, ubuntu-latest]
        python: ["3.11", "3.13"]
    steps:
      - uses: actions/checkout@v6
      - uses: astral-sh/setup-uv@v7
      - uses: extractions/setup-just@v3
      - run: uv python install ${{ matrix.python }}
      - run: uv sync --python ${{ matrix.python }}
      - run: just check
        env:
          KB_NO_AUTO_SYNC: "1"
```

- [ ] **Step 2: Dependabot and SECURITY.md**

`.github/dependabot.yml`:
```yaml
version: 2
updates:
  - package-ecosystem: github-actions
    directory: /
    schedule: {interval: weekly}
  - package-ecosystem: uv
    directory: /
    schedule: {interval: weekly}
```
`SECURITY.md`: supported version = latest release; report privately via GitHub "Report a vulnerability" on the repo's Security tab; state that kb is read-only, makes one-time downloads from two named hosts, and has no runtime dependencies.

- [ ] **Step 3: Verify locally and on GitHub**

Run: `cd ~/Work/kb && just check` -> Expected: PASS (75 tests).
Already verified before writing this plan: in `python:3.11-slim` and `python:3.13-slim` Linux containers, `just check` passes (ruff, ruff format, mypy on `src scripts`, 75 tests), so the Linux jobs are expected to be green. Note that the recipe type-checks `src scripts` only; `mypy .` reports 11 errors in `tests/`, so do not "tighten" the CI command to `mypy .` without fixing those first.
Open a PR and wait for the four matrix jobs. If one fails, fix the cause; do not skip the job. Pin actions by SHA only after Dependabot proposes updates (say so in the PR).

- [ ] **Step 4: Commit**

```bash
git add .github SECURITY.md
git commit -m "ci: run just check on macOS and Linux, add SECURITY.md and Dependabot"
```

---

## Phase 1 (release 1.7.0)

### Task 2: Terminal-safe text (S1, S11, S7)

**Files:**
- Modify: `src/kb/cli.py:140,151-152` (`_CTRL`, `sanitize`), `:262,295,390,485,1523` (`splitlines`), `:1613` (warnings print), `doctor`
- Test: `tests/test_kb.py` (new class `TerminalSafety(Sandbox)`)

**Interfaces:**
- Produces: `sanitize(text: object) -> str` (drops categories Cc, Cf, Zl, Zp; tab/newline/CR become one space), `read_lines(p: Path) -> list[str]` (splits on `\n` only).

- [ ] **Step 1: Write the failing tests**

```python
class TerminalSafety(Sandbox):
    def test_sanitize_neutralises_layout_and_spoofing_characters(self):
        self.assertEqual(kb.sanitize("a\tb\nc"), "a b c")
        self.assertEqual(kb.sanitize("x\u202eevil\u202c"), "xevil")      # bidi override
        self.assertEqual(kb.sanitize("a\u200bb\u2028c\u2029d"), "abcd")   # zero-width, line/para separators
        self.assertEqual(kb.sanitize("\x1b[31mred\x1b[0m"), "[31mred[0m")  # ESC dropped as before

    def test_binding_text_has_no_tabs_or_newlines(self):
        b = kb.Binding("x", "k\tk", "line1\nline2")
        self.assertNotIn("\t", b.keys)
        self.assertNotIn("\n", b.desc)

    def test_read_lines_splits_on_newline_only(self):
        p = self.home / "f.conf"
        p.write_text("a\u2028b\r\nc\n")
        self.assertEqual(kb.read_lines(p), ["a\u2028b", "c", ""])

    def test_unicode_line_separator_in_a_comment_cannot_create_a_binding(self):
        # splitlines() treats U+2028 as a line break, so the text after it in a *comment* became a live binding.
        self.write("tmux/tmux.conf", "# harmless comment\u2028bind z kill-server\nbind b split-window\n")
        keys = {b.keys for b in kb.load_tmux()}
        self.assertNotIn("Ctrl-B › z", keys)
        self.assertIn("Ctrl-B › b", keys)

    def test_warnings_are_sanitised_on_stderr(self):
        kb.WARNINGS.append("bad \x1b]0;pwn\x07 thing")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            kb.load_all(tools=["tmux"])
        self.assertNotIn("\x1b", err.getvalue())
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run python -m unittest tests.test_kb.TerminalSafety -v` -> Expected: FAIL (tab kept, `read_lines` missing, phantom `Ctrl-B › z` binding present, ESC in stderr). I confirmed against current code that the comment case yields `('Ctrl-B › z', 'kill-server')`.

- [ ] **Step 3: Implement**

Add `import unicodedata` after `import tempfile`. Replace `_CTRL` and `sanitize`:

```python
_DROP_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})


def sanitize(text: object) -> str:
    """Make config/web text safe to print: no escape sequences, bidi/zero-width tricks, or layout characters."""
    return "".join(
        " " if ch in "\t\n\r" else ch for ch in str(text) if ch in "\t\n\r" or unicodedata.category(ch) not in _DROP_CATEGORIES
    )


def read_lines(p: Path) -> list[str]:
    """Lines split on \\n only: str.splitlines() also splits on U+2028/U+0085 and would let a file hide a binding."""
    return p.read_text().replace("\r\n", "\n").split("\n")
```
Delete `_CTRL` (nothing else uses it). Replace each `p.read_text().splitlines()` at the five lines with `read_lines(p)`. Change the final warnings loop in `load_all` to `print(f"kb: warning: {sanitize(w)}", file=sys.stderr)`, and sanitise warnings the same way where `doctor` prints them.

- [ ] **Step 4: Run the new tests, then the gate**

Run: `uv run python -m unittest tests.test_kb.TerminalSafety -v` -> PASS.
Run: `just check` -> PASS. If an older test expected a tab or newline inside a field, change that expectation (the behaviour is the fix) and say so in the commit body.

- [ ] **Step 5: Commit**

```bash
git add src/kb/cli.py tests/test_kb.py
git commit -m "fix(security): strip bidi/zero-width/layout characters, split config lines on \\n only, sanitise warnings"
```

### Task 3: Scripting contract (F1, F2, F3, F4, F9)

**Files:**
- Modify: `src/kb/cli.py` (`print_table:1772`, `_tool_arg:1895`, `main` after `ap.parse_args`, `run:2020`)
- Test: `tests/test_kb.py` (new class `ScriptingContract(Sandbox)`)

**Interfaces:**
- Produces: `main()` errors with rc 2 for empty `-k` and for `--json` with `-c/--tools/--doctor`; `run()` never prints a traceback on a closed pipe.

- [ ] **Step 1: Write the failing tests**

```python
class ScriptingContract(Sandbox):
    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = kb.main(list(argv))
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue()

    def test_empty_key_is_a_usage_error(self):
        rc, out, err = self.run_main("-k", "  ", "--no-fzf")
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("--key", err)

    def test_json_with_unsupported_modes_fails_loudly(self):
        for flag in ("-c", "--tools", "--doctor"):
            rc, out, err = self.run_main("--json", flag)
            self.assertEqual((rc, out), (2, ""), flag)
            self.assertIn("--json", err)

    def test_table_footer_goes_to_stderr(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            kb.print_table([kb.Binding("x", "⌘A", "do a"), kb.Binding("x", "⌘B", "do b")])
        self.assertEqual(len(out.getvalue().splitlines()), 2)
        self.assertIn("2 bindings", err.getvalue())

    def test_unknown_tool_suggests_the_closest_name(self):
        rc, out, err = self.run_main("-t", "tmx")
        self.assertEqual(rc, 2)
        self.assertIn("did you mean 'tmux'", err)

    def test_closed_pipe_is_silent(self):
        code = (
            "import kb.cli as k; k.main = lambda: print('x' * 200000) or 0; k.run()"
        )
        cmd = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)} | head -c 1 >/dev/null"
        r = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.stderr, "")
```
Add `import shlex` to the test imports.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run python -m unittest tests.test_kb.ScriptingContract -v` -> Expected: 5 FAIL.

- [ ] **Step 3: Implement**

`print_table` footer: `print(C("2", ...), file=sys.stderr)` with the leading `\n` removed from the string.

`_tool_arg`:
```python
def _tool_arg(value):
    tool = value.strip().lower()
    if tool not in known_tools():
        close = difflib.get_close_matches(tool, known_tools(), n=1)
        hint = f" - did you mean {close[0]!r}?" if close else ""
        raise argparse.ArgumentTypeError(
            f"unknown tool {value!r}{hint} (choose from: {', '.join(known_tools())})"
        )
    return tool
```
Add `import difflib` at the top (alphabetical, before `hashlib`).

In `main`, directly after `a = ap.parse_args(argv)` and the `--detail` block:
```python
    if a.key is not None and not a.key.strip():
        ap.error("--key needs a key combo, e.g. -k opt+t")
    if a.json and (a.conflicts or a.tools or a.doctor):
        ap.error("--json is only supported for searches (not with --conflicts, --tools or --doctor)")
```
In `run`, replace the `BrokenPipeError` branch:
```python
    except BrokenPipeError:
        code = 0
        with contextlib.suppress(OSError, ValueError):
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())  # silence the flush at exit
```

- [ ] **Step 4: Verify**

Run: `uv run python -m unittest tests.test_kb.ScriptingContract -v` -> PASS. Run `just check` -> PASS (fix any older test that read the footer from stdout).
Manual: `kb --no-fzf </dev/null | head -2` prints two rows and no traceback; `kb -k ''; echo $?` prints a usage error and `2`.

- [ ] **Step 5: Commit**

```bash
git add src/kb/cli.py tests/test_kb.py
git commit -m "fix(cli): empty -k and unsupported --json are usage errors, footer to stderr, quiet closed pipe, tool suggestions"
```

### Task 4: Colour control (F8)

**Files:**
- Modify: `src/kb/cli.py:72` (`Settings.color` default), `:1882` (color assignment)
- Test: `tests/test_kb.py` (new class `Colour`)

**Interfaces:**
- Produces: `want_color(stream) -> bool` precedence: non-empty `NO_COLOR` off, then `FORCE_COLOR`/`CLICOLOR_FORCE` (any value except empty or `0`) on, then `stream.isatty()`.

- [ ] **Step 1: Failing tests**

```python
class Colour(unittest.TestCase):
    class Tty(io.StringIO):
        def isatty(self):
            return True

    def env(self, **kw):
        return mock.patch.dict(os.environ, kw, clear=True)

    def test_precedence(self):
        tty, pipe = self.Tty(), io.StringIO()
        with self.env():
            self.assertTrue(kb.want_color(tty))
            self.assertFalse(kb.want_color(pipe))
        with self.env(NO_COLOR="1"):
            self.assertFalse(kb.want_color(tty))
        with self.env(NO_COLOR=""):
            self.assertTrue(kb.want_color(tty))          # empty NO_COLOR is unset (no-color.org)
        with self.env(FORCE_COLOR="1"):
            self.assertTrue(kb.want_color(pipe))
        with self.env(FORCE_COLOR="0"):
            self.assertFalse(kb.want_color(pipe))
        with self.env(NO_COLOR="1", FORCE_COLOR="1"):
            self.assertFalse(kb.want_color(pipe))        # NO_COLOR wins
```

- [ ] **Step 2: Run** `uv run python -m unittest tests.test_kb.Colour -v` -> FAIL (`want_color` missing).

- [ ] **Step 3: Implement**

```python
def want_color(stream) -> bool:
    env = os.environ
    if env.get("NO_COLOR"):
        return False
    if any(env.get(v, "") not in ("", "0") for v in ("FORCE_COLOR", "CLICOLOR_FORCE")):
        return True
    return stream.isatty()
```
Place it above `Settings`. Change the `Settings.color` default factory to `lambda: want_color(sys.stdout)` and line 1882 to `SETTINGS.color = want_color(sys.stdout)`.

- [ ] **Step 4: Verify** `uv run python -m unittest tests.test_kb.Colour -v` PASS; `just check` PASS.
- [ ] **Step 5: Commit** `git commit -am "feat(cli): honour FORCE_COLOR/CLICOLOR_FORCE, treat empty NO_COLOR as unset"` (after `git add tests/test_kb.py`).

### Task 5: Explain shadows (F12, F13)

**Files:**
- Modify: `src/kb/cli.py:1785-1802` (`report_conflicts`)
- Test: `tests/test_kb.py` (extend `ScriptingContract`)

- [ ] **Step 1: Failing test**

```python
    def test_conflict_report_explains_shadows_and_summarises(self):
        items = [
            kb.Binding("aerospace", "⌥⇧←", "Join left", "alt+shift+left"),
            kb.Binding("macos", "⌥⇧←", "Select word left", "alt+shift+left", "text"),
        ]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            kb.report_conflicts(items)
        text = out.getvalue()
        self.assertIn("shadow = ", text)
        self.assertRegex(text, r"\d+ conflicts?, \d+ shadows? \(informational\)")
```
(Fixture checked against `shadows()`: aerospace is a global tool, and scope `"text"` is an app scope, so this yields exactly one shadow.)

- [ ] **Step 2: Run** -> FAIL.
- [ ] **Step 3: Implement.** In `report_conflicts`, before the shadow loop `for g, std in sh:` add `if sh: print(C("2", "shadow = a global tool captures this key before the app sees it (informational, not a failure)"))`. Replace the final `if not cs and not sh:` block with:
```python
    if not cs and not sh:
        print("No conflicts found.")
    else:
        print(C("2", f"\n{hard} conflict{'s' if hard != 1 else ''}, {len(sh)} shadow{'s' if len(sh) != 1 else ''} (informational)"))
```
- [ ] **Step 4: Verify** the new test and `just check`.
- [ ] **Step 5: Commit** `fix(cli): explain shadows and print a conflict/shadow summary`.

### Task 6: Quote the fzf preview command (S4)

**Files:**
- Modify: `src/kb/cli.py:1875` (the `--preview` argument)
- Test: `tests/test_kb.py`

**Interfaces:**
- Produces: `preview_command(snapshot: Path) -> str` returning a shell-safe string.

- [ ] **Step 1: Failing test**

```python
class FzfPreview(unittest.TestCase):
    def test_preview_command_quotes_interpreter_and_script(self):
        with mock.patch.object(kb.sys, "executable", "/tmp/my dir/py;touch pwn"):
            cmd = kb.preview_command(Path("/tmp/snap dir/s.json"))
        import shlex
        parts = shlex.split(cmd)
        self.assertEqual(parts[0], "/tmp/my dir/py;touch pwn")
        self.assertEqual(parts[-1], "/tmp/snap dir/s.json")
        self.assertIn("{1}", cmd)
```

- [ ] **Step 2: Run** -> FAIL (`preview_command` missing).
- [ ] **Step 3: Implement.** The script argument is what the code already uses today: `exe = shlex.quote(str(Path(sys.argv[0]).resolve()))` at `cli.py:1860`. Replace that `exe` line by passing the raw path, so quoting happens once:
```python
def preview_command(snap: Path) -> str:
    script = Path(sys.argv[0]).resolve()
    return " ".join(
        [shlex.quote(sys.executable), shlex.quote(str(script)), "--detail", "{1}", "--snapshot", shlex.quote(str(snap))]
    )
```
Delete the old `exe = ...` line and change the `--preview` argument to `preview_command(snap)`. Update the test to patch `kb.sys.argv` as well: `mock.patch.object(kb.sys, "argv", ["/tmp/my dir/kb"])` and assert `parts[1] == "/tmp/my dir/kb"` (resolved path of a non-existent file stays as given on POSIX). `{1}` stays unquoted so fzf substitutes it.
- [ ] **Step 4: Verify** test + `just check`; run `kb` in a terminal once and confirm the preview pane still renders (manual, needs a TTY; say so if not run).
- [ ] **Step 5: Commit** `fix(security): shell-quote the interpreter in the fzf preview command`.

### Task 6b: Release 1.7.0 prep (user decides)

- [ ] Add CHANGELOG `[Unreleased]` entries under Fixed/Changed for Tasks 2-6 (sanitising, scripting contract, colour, shadows, preview quoting; note: the "N bindings" footer moved to stderr, and `-k ''` / `--json -c` now exit 2).
- [ ] `just release-dry minor` and show the output. Stop; the user runs or approves `just release minor`.

---

## Phase 2 (release 1.8.0)

### Task 6c: Isolate the network and data-file code in `src/kb/net.py` (pure move)

Why first: the reviews call this the security-critical part. Moving it before hardening means Tasks 7-9 change one small, auditable module, and a guard test can prove nothing else touches the network.

**Files:**
- Create: `src/kb/net.py`
- Modify: `src/kb/cli.py` (lines 18, 31-32 imports; 779-793 `_fetch`; 796-814 `_save_doc`; 843 exception tuple)
- Test: `tests/test_kb.py` (new class `NetIsolation`)

**Interfaces:**
- Produces (in `kb.net`, stateless: no import from `kb.cli`, no module globals, no `DATA_DIR`/`WARNINGS`):
  - `fetch(url: str, host: str, *, timeout: float, max_bytes: int, user_agent: str) -> bytes`
  - `save_doc(data_dir: Path, name: str, url: str, raw: bytes, rows: list[dict], min_rows: int, *, version: str, extra: dict | None = None) -> dict`
  - `NETWORK_ERRORS: tuple[type[BaseException], ...] = (OSError, ValueError, http.client.HTTPException)`
- Keeps in `kb.cli` (tests patch these names, so they stay): `_fetch(url, host, timeout=20)`, `_save_doc(name, url, raw, rows, min_rows, extra=None)` as thin wrappers; `DATA_DIR`, `WARNINGS`, `APPLE_*`, `VSC_*`, `sync_apple`, `sync_vscode`, `auto_sync_enabled`, `ensure_data`, the HTML parsers.

- [ ] **Step 1: Write the failing tests**

```python
import ast


class NetIsolation(unittest.TestCase):
    SRC = Path(kb.__file__).parent

    def imports_of(self, path):
        tree = ast.parse(path.read_text())
        names = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                names |= {a.name for a in n.names}
            elif isinstance(n, ast.ImportFrom):
                names.add(n.module or "")
        return names

    def test_only_net_module_imports_the_network_stack(self):
        offenders = {}
        for f in self.SRC.glob("*.py"):
            bad = {m for m in self.imports_of(f) if m in ("urllib.request", "http.client", "socket", "ssl")}
            if bad and f.name != "net.py":
                offenders[f.name] = bad
        self.assertEqual(offenders, {})

    def test_net_module_is_stateless_and_does_not_import_cli(self):
        imported = self.imports_of(self.SRC / "net.py")
        self.assertFalse({"kb.cli", "cli"} & imported, imported)  # absolute or relative import of the CLI module

    def test_cli_wrappers_still_delegate(self):
        self.assertTrue(callable(kb._fetch) and callable(kb._save_doc))
        from kb import net
        self.assertTrue(callable(net.fetch) and callable(net.save_doc))
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run python -m unittest tests.test_kb.NetIsolation -v` -> Expected: FAIL (`cli.py` imports `urllib.request`/`http.client`; `kb.net` missing).

- [ ] **Step 3: Move the code**

Create `src/kb/net.py` containing the current behaviour, parameterised instead of reading globals:

```python
"""Network and downloaded-data-file code. Nothing else in kb may import the network stack (see tests)."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

NETWORK_ERRORS: tuple[type[BaseException], ...] = (OSError, ValueError, http.client.HTTPException)


def fetch(url: str, host: str, *, timeout: float, max_bytes: int, user_agent: str) -> bytes:
    """GET url over https from exactly `host`; bounded size/time; refuses redirects to other hosts."""
    u = urllib.parse.urlparse(url)
    if u.scheme != "https" or u.hostname != host:
        raise ValueError(f"refusing to fetch {url!r}: only https://{host}/ is allowed")
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})  # noqa: S310 - https + exact host validated
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - same validated request
        if urllib.parse.urlparse(r.geturl()).hostname != host:
            raise ValueError(f"redirected away from {host}; aborting")
        raw = r.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError("response too large")
    return raw
```
For `save_doc`, cut the body of `cli._save_doc` (cli.py:796-814, read it first) and paste it into `net.save_doc`, replacing `DATA_DIR` with the `data_dir` parameter and `VERSION` with the `version` parameter; keep the `meta` keys, the per-pid tmp file and `replace` exactly as they are. `time` is imported for Task 8; remove the import now if ruff flags it unused, and re-add it in Task 8.

In `cli.py`:
```python
from . import net
from .net import NETWORK_ERRORS

def _fetch(url: str, host: str, timeout: float = 20) -> bytes:
    return net.fetch(
        url, host, timeout=timeout, max_bytes=APPLE_MAX_BYTES, user_agent=f"kb/{VERSION} (+local keybinding index)"
    )

def _save_doc(name, url, raw, rows, min_rows, extra=None):
    return net.save_doc(DATA_DIR, name, url, raw, rows, min_rows, version=VERSION, extra=extra)
```
Delete `import http.client`, `import urllib.parse` and `import urllib.request` from `cli.py` (check each is unused elsewhere with `grep -n "urllib\|http\." src/kb/cli.py`; the earlier grep shows only `_fetch` and line 843 use them). Change line 843 to `except NETWORK_ERRORS as e:`. Rename `APPLE_MAX_BYTES` to `MAX_DOWNLOAD_BYTES` only if you also update both references; otherwise leave it.

- [ ] **Step 4: Run tests and the gate**

Run: `uv run python -m unittest tests.test_kb.NetIsolation -v` -> PASS.
Run: `just check` -> PASS with the same 75 tests plus the 3 new ones (no behaviour change: the existing allow-list, sync and `ensure_data` tests must pass untouched).

- [ ] **Step 5: Commit**

```bash
git add src/kb/net.py src/kb/cli.py tests/test_kb.py
git commit -m "refactor: move network and data-file code into kb.net, with a test that nothing else imports the network stack"
```

### Task 7: Private, absolute data directory (rel-XDG, S3)

**Files:**
- Modify: `src/kb/net.py` (`save_doc`, new `xdg_dir`, `write_private`); `src/kb/cli.py:61-65` (CFG), `:649` (DATA_DIR)
- Test: `tests/test_kb.py` (new class `DataDirSafety`)

**Interfaces:**
- Produces: `kb.net.xdg_dir(var: str, fallback: Path) -> Path` (ignores empty and non-absolute values; re-exported in `kb.cli` as `xdg_dir`), `kb.net.write_private(path: Path, data: str)` (mode 0600, `O_EXCL|O_NOFOLLOW`).

- [ ] **Step 1: Failing tests**

```python
class DataDirSafety(Sandbox):
    def test_relative_xdg_values_are_ignored(self):
        fb = Path("/home/u/.local/share")
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": "rel/dir"}):
            self.assertEqual(kb.xdg_dir("XDG_DATA_HOME", fb), fb)
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": "/abs/data"}):
            self.assertEqual(kb.xdg_dir("XDG_DATA_HOME", fb), Path("/abs/data"))
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": ""}):
            self.assertEqual(kb.xdg_dir("XDG_DATA_HOME", fb), fb)

    def test_saved_data_is_private(self):
        kb._save_doc("t.json", "https://x", b"raw", [{"a": 1}], 1)
        self.assertEqual(oct((kb.DATA_DIR / "t.json").stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct(kb.DATA_DIR.stat().st_mode & 0o777), "0o700")

    def test_write_refuses_a_symlinked_temp_file(self):
        kb.DATA_DIR.mkdir(parents=True)
        victim = self.home / "victim.txt"
        victim.write_text("keep")
        (kb.DATA_DIR / f"t.json.{os.getpid()}.tmp").symlink_to(victim)
        with self.assertRaises(OSError):
            kb._save_doc("t.json", "https://x", b"raw", [{"a": 1}], 1)
        self.assertEqual(victim.read_text(), "keep")
```

- [ ] **Step 2: Run** -> FAIL.
- [ ] **Step 3: Implement**

```python
def xdg_dir(var: str, fallback: Path) -> Path:
    value = os.environ.get(var, "")
    return Path(value) if value and os.path.isabs(value) else fallback
```
In `src/kb/net.py` add:
```python
def xdg_dir(var: str, fallback: Path) -> Path:
    value = os.environ.get(var, "")
    return Path(value) if value and os.path.isabs(value) else fallback


def write_private(path: Path, data: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(data)
```
and in `net.save_doc` replace the mkdir / write_text / replace lines with:
```python
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = data_dir / f"{name}.{os.getpid()}.tmp"
    try:
        write_private(tmp, json.dumps(doc, indent=1, ensure_ascii=False) + "\n")
        tmp.replace(data_dir / name)
    finally:
        tmp.unlink(missing_ok=True)
    return doc
```
In `cli.py` add `from .net import NETWORK_ERRORS, xdg_dir`, then set `DATA_DIR = xdg_dir("XDG_DATA_HOME", HOME / ".local/share") / "kb"` and make `CFG` use `xdg_dir("XDG_CONFIG_HOME", HOME / ".config")` (keep the existing CFG expression's fallback). The tests call `kb._save_doc`, which still delegates to `net.save_doc`.
Note `unlink(missing_ok=True)` in `finally` removes a pre-existing symlink itself (not its target), which is the desired recovery. `mkdir(mode=...)` only applies to newly created directories; the test uses a fresh sandbox dir.
- [ ] **Step 4: Verify** new tests + `just check`.
- [ ] **Step 5: Commit** `fix(security): ignore relative XDG paths, create private data files without following symlinks`.

### Task 8: Redirect-safe fetch with a total deadline, in `kb.net` (redirect, deadline)

**Files:**
- Modify: `src/kb/net.py` (`fetch`, new `SameHostRedirect`); `src/kb/cli.py` (`_fetch` wrapper passes `opener` through)
- Test: `tests/test_kb.py` (new class `Fetch`)

**Interfaces:**
- Produces: `kb.net.fetch(url, host, *, timeout, max_bytes, user_agent, opener=None) -> bytes` and `kb.net.SameHostRedirect(host)`. `opener` is an optional object with `.open(req, timeout=...)` for tests. `cli._fetch(url, host, timeout=20, *, opener=None)` forwards it.

- [ ] **Step 1: Failing tests** (HTTP, not HTTPS, so they test the policy layer via the injectable opener and the handler directly)

```python
from kb import net


class Fetch(unittest.TestCase):
    def test_redirect_to_another_host_is_refused_before_following(self):
        h = net.SameHostRedirect("example.com")
        req = urllib.request.Request("https://example.com/a")
        with self.assertRaises(ValueError):
            h.redirect_request(req, None, 302, "Found", {}, "https://evil.example/b")
        with self.assertRaises(ValueError):
            h.redirect_request(req, None, 302, "Found", {}, "http://example.com/b")   # downgrade
        ok = h.redirect_request(req, None, 302, "Found", {}, "https://example.com/b")
        self.assertEqual(ok.full_url, "https://example.com/b")

    def test_total_deadline_aborts_a_slow_drip(self):
        class Slow:
            def read(self, n):
                time.sleep(0.05)
                return b"x"
            def __enter__(self): return self
            def __exit__(self, *a): return False
        class Op:
            def open(self, req, timeout=None): return Slow()
        with self.assertRaises(ValueError) as cm:
            kb._fetch("https://example.com/", "example.com", timeout=0.2, opener=Op())
        self.assertIn("deadline", str(cm.exception))

    def test_size_cap_is_enforced_while_reading(self):
        class Big:
            def read(self, n): return b"x" * n
            def __enter__(self): return self
            def __exit__(self, *a): return False
        class Op:
            def open(self, req, timeout=None): return Big()
        with self.assertRaises(ValueError):
            kb._fetch("https://example.com/", "example.com", timeout=5, opener=Op())

    def test_wrong_host_or_scheme_still_refused(self):
        for url in ("http://example.com/", "https://evil.example/"):
            with self.assertRaises(ValueError):
                kb._fetch(url, "example.com")
```
Imports needed in the test file: `time`, `urllib.request`.

- [ ] **Step 2: Run** -> FAIL.
- [ ] **Step 3: Implement**

```python
class SameHostRedirect(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only if it stays on https and on the allowed host (checked before the next request)."""

    def __init__(self, host: str):
        self.host = host

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        u = urllib.parse.urlparse(newurl)
        if u.scheme != "https" or u.hostname != self.host:
            raise ValueError(f"refusing redirect to {newurl!r}: only https://{self.host}/ is allowed")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url: str, host: str, *, timeout: float, max_bytes: int, user_agent: str, opener=None) -> bytes:
    """GET url over https from exactly `host`; redirects stay on that host; bounded size and total time."""
    u = urllib.parse.urlparse(url)
    if u.scheme != "https" or u.hostname != host:
        raise ValueError(f"refusing to fetch {url!r}: only https://{host}/ is allowed")
    if opener is None:
        opener = urllib.request.build_opener(SameHostRedirect(host))
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})  # noqa: S310 - https + exact host validated
    deadline = time.monotonic() + timeout
    chunks: list[bytes] = []
    total = 0
    with opener.open(req, timeout=timeout) as r:
        while True:
            if time.monotonic() > deadline:
                raise ValueError(f"download from {host} exceeded its {timeout:g}s deadline")
            chunk = r.read(min(65536, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("response too large")
    return b"".join(chunks)
```
and in `cli.py`:
```python
def _fetch(url: str, host: str, timeout: float = 20, *, opener=None) -> bytes:
    return net.fetch(
        url, host, timeout=timeout, max_bytes=APPLE_MAX_BYTES, user_agent=f"kb/{VERSION} (+local keybinding index)",
        opener=opener,
    )
```
The tests call `kb._fetch(..., opener=...)`, so they exercise the full wrapper. If Task 6c removed the `time` import from `net.py`, re-add it.
Keep the existing allow-list tests green (`test_sync_only_talks_to_apple_over_https`, `test_sync_host_allowlist`).
- [ ] **Step 4: Verify** `uv run python -m unittest tests.test_kb.Fetch -v` PASS; `just check` PASS.
- [ ] **Step 5: Commit** `fix(security): validate redirects before following, enforce a total download deadline and size cap`.

### Task 9: Validate downloaded data on load (corrupt, S2)

**Files:**
- Modify: `src/kb/net.py` (`save_doc` meta), `src/kb/cli.py` (`read_doc`, `load_apple_docs` ~849, `load_vscode` ~1357)
- Test: `tests/test_kb.py` (new class `DataValidation(Sandbox)`)

**Interfaces:**
- Produces: `read_doc(name: str, required: tuple[str, ...]) -> dict | None` returns the parsed doc, or `None` after appending a warning that says to run `kb --sync`. `_save_doc` stores `"schema": 1` and `rows_sha256` in `meta`. Old files (no schema) still load.

- [ ] **Step 1: Failing tests**

```python
class DataValidation(Sandbox):
    def put(self, name, text):
        kb.DATA_DIR.mkdir(parents=True, exist_ok=True)
        (kb.DATA_DIR / name).write_text(text)

    def test_truncated_json_is_a_warning_not_a_crash(self):
        self.put(kb.APPLE_JSON, '{"meta": {}, "rows": [')
        self.assertEqual(kb.load_apple_docs(), [])
        self.assertTrue(any("kb --sync" in w for w in kb.WARNINGS))

    def test_rows_missing_fields_are_skipped_and_counted(self):
        self.put(kb.APPLE_JSON, json.dumps({"meta": {}, "rows": [{"section": "x"}, "nope"]}))
        self.assertEqual(kb.load_apple_docs(), [])
        self.assertTrue(any("skipped" in w for w in kb.WARNINGS))

    def test_saved_doc_records_schema_and_row_hash(self):
        doc = kb._save_doc("t.json", "https://x", b"raw", [{"a": 1}], 1)
        self.assertEqual(doc["meta"]["schema"], 1)
        self.assertEqual(len(doc["meta"]["rows_sha256"]), 64)
```

- [ ] **Step 2: Run** -> FAIL.
- [ ] **Step 3: Implement**

```python
def read_doc(name: str, required: tuple[str, ...]) -> dict | None:
    """Load a downloaded data file; on corruption warn (with the repair command) and return None."""
    p = ensure_data(name)
    if p is None:
        return None
    try:
        doc = json.loads(p.read_text())
        rows = doc["rows"]
        if not isinstance(rows, list):
            raise ValueError("rows is not a list")
    except (OSError, ValueError, KeyError, TypeError) as e:
        WARNINGS.append(f"{name} is unreadable ({type(e).__name__}) - run `kb --sync` to download it again")
        return None
    good = [r for r in rows if isinstance(r, dict) and all(isinstance(r.get(k), str) for k in required)]
    if len(good) != len(rows):
        WARNINGS.append(f"{name}: skipped {len(rows) - len(good)} malformed rows - run `kb --sync`")
    doc["rows"] = good
    return doc
```
Row keys (checked against the parsers): Apple rows are `{"section", "combo", "desc"}`; VS Code rows are `{"section", "title", "command", "osx"}` (`osx` may be an empty string).

`load_apple_docs` (cli.py:849) becomes:
```python
def load_apple_docs() -> list[Binding]:
    before = len(WARNINGS)
    doc = read_doc(APPLE_JSON, ("section", "combo", "desc"))
    if doc is None:
        if len(WARNINGS) == before:  # missing, not corrupt
            WARNINGS.append("apple docs unavailable - run `kb --sync-apple` when online to download Apple's shortcut list")
        return []
    meta = doc.get("meta", {})
    ...  # the existing loop, with `doc.get("rows", [])` unchanged
```
(delete the old `p = ensure_data(...)`, the `p is None` branch and the `json.loads(p.read_text())` line).

In `load_vscode` (cli.py:1357-1397) replace `docs = ensure_data(VSC_JSON)` with `doc = read_doc(VSC_JSON, ("section", "title", "command", "osx"))`, and the block at `if docs is not None: d = json.loads(docs.read_text())` with `if doc is not None: d = doc`. Keep the rest of the block as is. Where the loader previously warned "vscode defaults unavailable", keep that warning only when `doc is None` and no new warning was added (same `before = len(WARNINGS)` guard). In `net.save_doc`, add to `meta`: `"schema": 1, "rows_sha256": hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()`.
- [ ] **Step 4: Verify** new tests + `just check`.
- [ ] **Step 5: Commit** `fix: warn and keep going on corrupt downloaded data, record schema and row hash`.

### Task 10: Tests can never reach the network (net-guard)

**Files:**
- Modify: `tests/test_kb.py` (`Sandbox.setUp`/`tearDown`)

- [ ] **Step 1: Failing test** (add to `DataDirSafety`):

```python
    def test_sandbox_blocks_real_network(self):
        with self.assertRaises(AssertionError):
            urllib.request.urlopen("https://support.apple.com/")
```
- [ ] **Step 2: Run** -> FAIL (no guard; or it hits the network. Run it with the network off or accept one blocked attempt).
- [ ] **Step 3: Implement.** In `Sandbox.setUp` add:
```python
        def _no_net(*a, **k):
            raise AssertionError("test attempted a real network request")

        self._net = [
            mock.patch.object(urllib.request.OpenerDirector, "open", _no_net),
            mock.patch("urllib.request.urlopen", _no_net),
        ]
        for p in self._net:
            p.start()
```
and `for p in self._net: p.stop()` in `tearDown`. The `Fetch` tests use an injected opener and a stand-alone `unittest.TestCase`, so they are unaffected.
- [ ] **Step 4: Verify** `just check` PASS with `KB_NO_AUTO_SYNC` unset: `env -u KB_NO_AUTO_SYNC just check` (the module still sets it at import, so also run once with that line temporarily commented out locally, then restore it).
- [ ] **Step 5: Commit** `test: make the sandbox refuse real network access`.

### Task 10b: Release 1.8.0 prep (user decides)

- [ ] CHANGELOG entries for Tasks 7-10 and the README note that `XDG_DATA_HOME` must be absolute and data files are mode 0600. `just release-dry minor`. Stop for the user.

---

## Phase 4 - separate plans (write each when started)

Each is an independent subsystem; do not start without its own plan.

1. **Output polish:** width-aware truncation and `--full`, pager, `--format tsv`, `--color` flag, shell completions, Vim `<C-w>` key notation and "looked for ctrl+w" feedback, ranking own bindings first, duplicate/empty-key rows (F5-F7, F10, F11, F14-F24).
2. **Architecture step 2-3:** `Env` object replacing import-time globals; `Source` registry; `--doctor` covering all 11 sources; remove hard-coded `~/.dotfiles/zsh` path (use `ZDOTDIR` candidates).
3. **Architecture step 4-5:** diagnostics channel in `--json`, exit-code table and `--strict`, executor plus deadline model, separate network refresh from loaders, data TTL, committed HTML fixtures with golden-row tests.
4. **Architecture step 6:** typed `Chord`/`Keys`/`Context` model so tmux, zsh and VS Code sequences join conflict detection.
5. **Release hygiene:** collapse Makefile into justfile (or generate), align `release.py` mypy scope with `just lint`, roll back the commit when tagging fails, metadata-first `_version()`.

## Self-review

- **Coverage:** every row of the Finding index maps to a task; deferred items are listed with an owner (Phase 4).
- **Gaps closed before execution (verified, not assumed):**
  - Linux CI: `just check` passes on Linux with Python 3.11 and 3.13 (containers, exact recipe).
  - tmux fixture: `tmux/tmux.conf` in the sandbox config dir works with `load_tmux`; the real failing case is the comment/U+2028 phantom binding, reproduced.
  - Shadow fixture: checked against `shadows()` (one shadow).
  - Row keys: Apple `section/combo/desc`, VS Code `section/title/command/osx`.
  - Preview script path: `Path(sys.argv[0]).resolve()` (already quoted today); only `sys.executable` was unquoted.
- **Remaining risks (accepted, with mitigation):**
  - Task 3 changes observable behaviour (footer stream, two new exit-2 cases). Mitigation: CHANGELOG entry; no known consumers parse the footer, but `kb ... | wc -l` users get different counts.
  - Task 6c is a pure move guarded by the existing 75 tests plus an import-graph test; `cli._fetch` and `cli._save_doc` keep their names because tests call and patch them. Task 8 then changes the fetch path for both `--sync` and auto-sync. Mitigation: injected-opener tests; one manual `kb --sync` against the real sites before the 1.8.0 release (needs network; do it from a temp `XDG_DATA_HOME`).
  - Task 6 preview rendering needs a TTY; verified manually, not in CI.
  - The 11 mypy errors in `tests/` are pre-existing and out of scope; CI keeps the existing `just check` scope.
- **Placeholders:** none left; every location the plan names was read.
- **Consistency:** `sanitize`/`read_lines`, `want_color`, `net.xdg_dir`/`net.write_private`, `net.SameHostRedirect`/`net.fetch(opener=)` (with `cli._fetch` and `cli._save_doc` as thin wrappers), `read_doc` and `preview_command` are each defined once and used with one signature.
