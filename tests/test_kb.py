"""Run: uv run python -m unittest discover -s tests -v   (or: make test)"""

import ast
import contextlib
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

import kb.cli as kb
from kb import net

# Tests must never touch the network: first-run downloads are opt-in per test (see AutoDownload).
os.environ["KB_NO_AUTO_SYNC"] = "1"


class Sandbox(unittest.TestCase):
    """Point kb at a throwaway HOME so tests never read real configs."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.cfg = self.home / ".config"
        self.cfg.mkdir()
        self.saved = (kb.HOME, kb.CFG, kb.DARWIN, list(kb.WARNINGS), kb.DATA_DIR, kb.VSC_APP, kb.VSC_USER)
        kb.VSC_APP, kb.VSC_USER = self.home / "VSCode.app", self.home / "Code/User"
        kb.DATA_DIR = self.home / ".local/share/kb"
        kb.HOME, kb.CFG, kb.DARWIN = self.home, self.cfg, False
        kb.WARNINGS.clear()

    def tearDown(self):
        kb.HOME, kb.CFG, kb.DARWIN = self.saved[:3]
        kb.WARNINGS[:] = self.saved[3]
        kb.DATA_DIR, kb.VSC_APP, kb.VSC_USER = self.saved[4:7]
        self.tmp.cleanup()

    def write(self, rel, text):
        p = self.cfg / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text))
        return p


class Combos(unittest.TestCase):
    def test_aliases_normalise_alike(self):
        for t in ("opt+t", "alt-t", "option t", "⌥t", "alt - t"):
            self.assertEqual(kb.parse_combo(t), "alt+t", t)

    def test_modifier_order_is_canonical(self):
        self.assertEqual(kb.parse_combo("cmd+shift+alt+ctrl+x"), "ctrl+alt+shift+cmd+x")

    def test_minus_and_plus_keys(self):
        self.assertEqual(kb.parse_combo("cmd+-"), "cmd+-")
        self.assertEqual(kb.parse_combo("cmd+plus"), "cmd+plus")
        self.assertEqual(kb.parse_combo("alt-minus"), "alt+-")

    def test_pretty(self):
        self.assertEqual(kb.pretty("alt+shift+left"), "⌥⇧←")
        self.assertEqual(kb.pretty("cmd+f11"), "⌘F11")


class Security(unittest.TestCase):
    def test_control_chars_stripped(self):
        b = kb.Binding("x", "k", "evil\x1b]0;pwned\x07 text", raw="a\x1b[31mb")
        self.assertNotIn("\x1b", b.desc + b.raw)
        self.assertNotIn("\x07", b.desc)

    def test_secrets_redacted(self):
        for raw in (
            "curl -H 'Authorization: Bearer abc123def456' x",
            "run --token=supersecret",
            "API_KEY=hunter2 cmd",
            "echo ghp_" + "a" * 30,
        ):
            b = kb.Binding("x", "k", "d", raw=raw)
            self.assertIn("«redacted»", b.raw, raw)
            self.assertNotIn("supersecret", b.raw)
            self.assertNotIn("hunter2", b.raw)

    def test_plain_commands_untouched(self):
        self.assertEqual(kb.Binding("x", "k", "d", raw="open -a Ghostty").raw, "open -a Ghostty")

    def test_snapshot_rejects_group_readable_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.json"
            p.write_text("[]")
            os.chmod(p, 0o644)
            with self.assertRaises(SystemExit):
                kb.show_detail(0, str(p))

    def test_system_tools_use_absolute_path(self):
        self.assertTrue(kb.DEFAULTS_BIN.startswith("/"))


class Parsers(Sandbox):
    def test_aerospace(self):
        self.write(
            "aerospace/aerospace.toml",
            """
            [mode.main.binding]
            alt-h = 'focus left'
            alt-shift-1 = 'move-node-to-workspace 1 --focus-follows-window'
            alt-w = 'exec-and-forget open -a Ghostty'
            [mode.service.binding]
            esc = ['reload-config', 'mode main']
        """,
        )
        by = {b.keys: b for b in kb.load_aerospace()}
        self.assertEqual(by["⌥H"].desc, "Focus window left")
        self.assertEqual(by["⌥⇧1"].desc, "Send window to workspace 1 and follow")
        self.assertEqual(by["⌥W"].desc, "Open Ghostty")
        self.assertEqual(by["⎋"].scope, "mode:service")

    def test_skhd(self):
        self.write(
            "skhd/skhdrc",
            """
            # comment
            alt - t : open -a Ghostty
            shift + alt - h : echo hi
        """,
        )
        got = {b.norm: b.desc for b in kb.load_skhd()}
        self.assertEqual(got["alt+t"], "Open Ghostty")
        self.assertIn("alt+shift+h", got)

    def test_tmux_prefix_flags_and_override(self):
        self.write(
            "tmux/tmux.reset.conf",
            """
            bind -r -T prefix - resize-pane -D 7
            bind l refresh-client
            bind l select-pane -R
            bind v split-window -h
        """,
        )
        self.write("tmux/tmux.conf", "set -g prefix ^A\n")
        bs = {(b.keys, b.desc): b for b in kb.load_tmux()}
        self.assertIn(("Ctrl-A › -", "Resize pane down by 7"), bs)
        self.assertTrue(bs[("Ctrl-A › l", "Refresh client")].note)
        self.assertFalse(bs[("Ctrl-A › l", "Select pane right")].note)

    def test_broken_config_is_isolated(self):
        self.write("aerospace/aerospace.toml", "this is = = not toml")
        self.write("skhd/skhdrc", "alt - t : open -a Ghostty\n")
        items = kb.load_all()
        self.assertTrue(any(b.tool == "skhd" for b in items))
        self.assertTrue(any("aerospace" in w for w in kb.WARNINGS))

    def test_missing_configs_are_fine(self):
        self.assertEqual(kb.load_all(), [])


class Analysis(unittest.TestCase):
    def B(self, tool, norm, scope="global"):
        return kb.Binding(tool, norm, tool, norm, scope)

    def test_cross_tool_conflict(self):
        cs = kb.conflicts([self.B("skhd", "alt+t"), self.B("aerospace", "alt+t")])
        self.assertEqual(list(cs), [("global", "alt+t")])

    def test_modes_do_not_conflict_with_main(self):
        self.assertEqual(kb.conflicts([self.B("aerospace", "b"), self.B("aerospace", "b", "mode:apps")]), {})

    def test_shadow_detected(self):
        items = [self.B("skhd", "cmd+c"), kb.Binding("macos", "⌘C", "Copy", "cmd+c", "app")]
        self.assertEqual(len(kb.shadows(items)), 1)

    def test_search_requires_all_words_and_key_lookup(self):
        items = [self.B("skhd", "alt+t"), self.B("tmux", "")]
        self.assertEqual(len(kb.search(items, ["skhd"], None, None)), 1)
        self.assertEqual(len(kb.search(items, [], None, "opt+t")), 1)
        self.assertEqual(kb.search(items, ["skhd", "nope"], None, None), [])


APPLE_HTML = """
<h2>Cut, copy, paste, and other common shortcuts</h2>
<ul><li><p><b>Command-X</b>: Cut the selected item.</p></li>
<li><p><b>Command–Grave accent (`):</b> Switch windows.</p></li>
<li><p><b>Shift–Command–Question mark (?)</b>: Open the Help menu.</p></li></ul>
<h2>Finder and system shortcuts</h2>
<ul><li><p><b>Control-F2</b>: or Fn-Control-F2: Move focus to the menu bar.</p></li>
<li><p><b>Option key while dragging</b>: Copy the dragged item.</p></li>
<li><p><b>Option–Brightness Up</b>: Open Displays settings.</p></li></ul>
<h2>Unrelated section</h2><ul><li><p><b>Command-Z</b>: ignored scope</p></li></ul>
"""


class AppleDocs(Sandbox):
    def test_combo_notation(self):
        self.assertEqual(kb.apple_combo("Shift–Command–Question mark (?)"), "shift+cmd+?")
        self.assertEqual(kb.apple_combo("Command–Grave accent (`):"), "cmd+`")
        self.assertEqual(kb.apple_combo("Control–Command–Space bar"), "ctrl+cmd+space")
        self.assertEqual(kb.apple_combo("Fn-Delete"), "fn+backspace")
        self.assertEqual(kb.apple_combo("Control-F2"), "ctrl+f2")
        self.assertEqual(kb.apple_combo("Option–Brightness Up"), "alt+brightness-up")

    def test_mouse_and_modifier_only_rows_rejected(self):
        for t in (
            "Option key while dragging",
            "Command-click a window title",
            "Option",
            "Command + Command (press twice)",
        ):
            self.assertIsNone(kb.apple_combo(t), t)

    def test_html_parse_and_alternatives(self):
        rows = kb.parse_apple_html(APPLE_HTML)
        self.assertEqual(
            rows[0],
            {
                "section": "Cut, copy, paste, and other common shortcuts",
                "combo": "Command-X",
                "desc": "Cut the selected item.",
            },
        )
        combos, desc = kb.apple_split_alternatives("Control-F2", "or Fn-Control-F2: Move focus to the menu bar.")
        self.assertEqual(combos, ["Control-F2", "Fn-Control-F2"])
        self.assertEqual(desc, "Move focus to the menu bar.")

    def test_loader_uses_sections_and_keeps_provenance(self):
        kb.DATA_DIR = self.home / "data"
        kb.DATA_DIR.mkdir()
        doc = {
            "meta": {"source": kb.APPLE_URL, "fetched_at": "2026-01-02T00:00:00Z"},
            "rows": kb.parse_apple_html(APPLE_HTML),
        }
        (kb.DATA_DIR / kb.APPLE_JSON).write_text(json.dumps(doc))
        got = kb.load_apple_docs()
        norms = {(b.norm, b.scope) for b in got}
        self.assertIn(("cmd+x", "app"), norms)
        self.assertIn(("ctrl+f2", "system"), norms)
        self.assertIn(
            ("fn+ctrl+f2".replace("fn+ctrl", "ctrl+fn"), "system"), norms
        )  # alternative from 'or Fn-Control-F2'
        self.assertNotIn("cmd+z", {b.norm for b in got})  # unknown section ignored
        self.assertTrue(all(kb.APPLE_URL in b.src for b in got))

    def test_missing_data_warns_instead_of_crashing(self):
        kb.DATA_DIR = self.home / "nodata"
        self.assertEqual(kb.load_apple_docs(), [])
        self.assertTrue(any("sync-apple" in w for w in kb.WARNINGS))

    def test_sync_only_talks_to_apple_over_https(self):
        for url in (
            "http://support.apple.com/en-us/102650",
            "https://evil.example/102650",
            "https://support.apple.com.evil.example/x",
            "file:///etc/passwd",
        ):
            with self.assertRaises(ValueError, msg=url):
                kb.sync_apple(url)


class KeyBindingDict(Sandbox):
    def test_parse_standard_key_binding(self):
        import plistlib

        p = self.home / "skb.dict"
        p.write_bytes(
            plistlib.dumps(
                {
                    "^a": "moveToBeginningOfParagraph:",
                    "~\uf702": "moveWordBackward:",
                    "^V": "pageDownAndModifySelection:",
                    "~\uf714": "noop:",
                    "^k": {"^b": "deleteBackward:"},
                },
                fmt=plistlib.FMT_BINARY,
            )
        )
        got = {b.keys: b for b in kb.load_standard_key_binding(p)}
        self.assertEqual(got["⌃A"].desc, "Move to beginning of paragraph")
        self.assertEqual(got["⌥←"].norm, "alt+left")
        self.assertEqual(got["⌃⇧V"].norm, "ctrl+shift+v")  # upper-case letter implies shift
        self.assertFalse(any("noop" in b.raw for b in got.values()))
        self.assertIn("⌃K then ⌃B", got)  # multi-key sequence kept, never compared as a single combo
        self.assertEqual(got["⌃K then ⌃B"].norm, "")

    def test_disabled_system_shortcut_does_not_count_as_shadow(self):
        std = kb.Binding(
            "macos", "⌘Space", "Spotlight", "cmd+space", "app", note="this system shortcut is disabled on this Mac"
        )
        mine = kb.Binding("raycast", "⌘Space", "Open Raycast", "cmd+space", "global")
        self.assertEqual(kb.shadows([std, mine]), [])


VSC_HTML = (
    '<h2 id="basic">Basic Editing</h2><table><thead><tr><th>Command</th><th>Key</th>'
    "<th>Command id</th></tr></thead><tbody>\n"
    '<tr><td>Cut line</td><td><span class="dynamic-keybinding" data-commandId="editor.action.clipboardCutAction" '
    'data-osx="⌘X" data-win="Ctrl+X"><span class="keybinding">⌘X</span></span></td>'
    "<td><code>editor.action.clipboardCutAction</code></td></tr>\n"
    '<tr><td>Comment <b>line</b></td><td><span class="dynamic-keybinding" data-commandId="c.comment" '
    'data-osx="⌘K ⌘C" data-win="Ctrl+K Ctrl+C">x</span></td><td><code>c.comment</code></td></tr>\n'
    "<tr><td>No key</td><td></td><td><code>c.nokey</code></td></tr>\n"
    "</tbody></table>\n"
)


class VSCode(Sandbox):
    def setUp(self):
        super().setUp()
        kb.DATA_DIR = self.home / "data"
        kb.DATA_DIR.mkdir()

    def test_notation(self):
        self.assertEqual(kb.vsc_mac_chord("⇧⌘P"), ["shift+cmd+p"])
        self.assertEqual(kb.vsc_mac_chord("⌘K ⌘C"), ["cmd+k", "cmd+c"])
        self.assertEqual(kb.vsc_mac_chord("⌃⇥"), ["ctrl+tab"])
        self.assertEqual(kb.vsc_mac_chord("⌥↑"), ["alt+up"])
        self.assertEqual(kb.vsc_key("meta+shift+p"), ["shift+cmd+p"])  # 'meta' is cmd on mac, not alt
        self.assertEqual(kb.vsc_key("ctrl+k ctrl+c"), ["ctrl+k", "ctrl+c"])

    def test_html_rows(self):
        rows = kb.parse_vscode_html(VSC_HTML)
        self.assertEqual([r["command"] for r in rows], ["editor.action.clipboardCutAction", "c.comment", "c.nokey"])
        self.assertEqual(rows[1]["osx"], "⌘K ⌘C")
        self.assertEqual(rows[0]["section"], "Basic Editing")

    def test_jsonc(self):
        txt = '[ // c\n {"key": "cmd+x", /* b */ "command": "a//b", "when": "x",}, ]'
        self.assertEqual(json.loads(kb.strip_jsonc(txt))[0]["command"], "a//b")

    def test_defaults_user_removal_and_extensions(self):
        doc = {
            "meta": {"source": kb.VSC_URL, "fetched_at": "2026-01-01T00:00:00Z"},
            "rows": kb.parse_vscode_html(VSC_HTML),
        }
        (kb.DATA_DIR / kb.VSC_JSON).write_text(json.dumps(doc))
        kb.VSC_USER.mkdir(parents=True)
        (kb.VSC_USER / "keybindings.json").write_text(
            '// mine\n[{"key": "cmd+x", "command": "-editor.action.clipboardCutAction"},\n'
            ' {"key": "ctrl+alt+t", "command": "workbench.action.terminal.toggleTerminal", "when": "editorFocus"}]'
        )
        ext = self.home / ".vscode/extensions/acme.thing-1.0"
        ext.mkdir(parents=True)
        (ext / "package.json").write_text(
            json.dumps(
                {
                    "name": "thing",
                    "displayName": "%dn%",
                    "contributes": {
                        "commands": [{"command": "thing.run", "title": "%run%"}],
                        "keybindings": [
                            {"command": "thing.run", "key": "ctrl+shift+r", "mac": "cmd+shift+r", "when": "editorFocus"}
                        ],
                    },
                }
            )
        )
        (ext / "package.nls.json").write_text(json.dumps({"dn": "Thing", "run": "Run Thing"}))
        saved = kb.HOME
        kb.HOME = self.home
        try:
            got = kb.load_vscode() + kb.load_vscode_extensions()
        finally:
            kb.HOME = saved
        by = {(b.keys, b.raw): b for b in got}
        self.assertEqual(by[("⌘X", "editor.action.clipboardCutAction")].note, "removed by your keybindings.json")
        self.assertIn("⌘K ⌘C", {b.keys for b in got})
        self.assertEqual(next(b for b in got if b.raw == "c.comment").norm, "")  # chord: never single-combo compared
        mine = by[("⌃⌥T", "workbench.action.terminal.toggleTerminal")]
        self.assertIn("when: editorFocus", mine.desc)
        e = by[("⇧⌘R", "thing.run")]  # 'mac' key wins over 'key'; nls titles resolved
        self.assertEqual(e.desc, "Run Thing  when: editorFocus")
        self.assertIn("Thing", e.tags)
        self.assertNotIn("c.nokey", {b.raw for b in got})

    def test_broken_user_file_warns(self):
        kb.VSC_USER.mkdir(parents=True)
        (kb.VSC_USER / "keybindings.json").write_text("[{not json")
        kb.load_vscode()
        self.assertTrue(any("cannot parse" in w for w in kb.WARNINGS))

    def test_sync_host_allowlist(self):
        for url in (
            "https://code.visualstudio.com.evil.example/x",
            "http://code.visualstudio.com/x",
            "https://support.apple.com/en-us/102650",
        ):
            with self.assertRaises(ValueError):
                kb._fetch(url, kb.VSC_HOST)


class Watchdog(Sandbox):
    def test_stalled_source_is_skipped_not_waited_for(self):
        import time

        def hang():
            time.sleep(30)
            return []

        hang.__name__ = "load_hang"

        def ok():
            return [kb.Binding("t", "k", "d")]

        ok.__name__ = "load_ok"
        saved = (kb.LOADERS, kb.LOADER_BUDGET, list(kb.STUCK))
        kb.LOADERS, kb.LOADER_BUDGET = [hang, ok], 0.4
        try:
            t0 = time.monotonic()
            items = kb.load_all()
            took = time.monotonic() - t0
            self.assertLess(took, 3)
            self.assertEqual([b.tool for b in items], ["t"])  # the healthy source still answers
            self.assertTrue(any("hang: no answer" in w for w in kb.WARNINGS))
            self.assertIn("load_hang", kb.STUCK)
        finally:
            kb.LOADERS, kb.LOADER_BUDGET = saved[0], saved[1]
            kb.STUCK[:] = saved[2]

    def test_results_keep_loader_order(self):
        import time

        def slow():
            time.sleep(0.3)
            return [kb.Binding("first", "k", "d")]

        slow.__name__ = "load_slow"

        def fast():
            return [kb.Binding("second", "k", "d")]

        fast.__name__ = "load_fast"
        saved = kb.LOADERS
        kb.LOADERS = [slow, fast]
        try:
            self.assertEqual([b.tool for b in kb.load_all()], ["first", "second"])
        finally:
            kb.LOADERS = saved

    def test_debug_prints_timings(self):
        r = subprocess.run(
            [sys.executable, "-m", "kb", "--debug", "--no-fzf", "zzz-nothing"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertRegex(r.stderr, r"kb: \w+\s+\d+ ms")


class SearchModes(unittest.TestCase):
    def setUp(self):
        B = kb.Binding
        self.cmd_k = B(
            "vscode", "⌘K", "Open keyboard shortcuts", "cmd+k", "vscode", "workbench.action.openGlobalKeybindings"
        )
        self.opt_p = B("skhd", "⌥P", "Open the command palette", "alt+p", "global", "open -a Palette")
        self.chord = B("vscode", "⌘K ⌘C", "Add line comment", "", "vscode", "editor.action.addCommentLine")
        self.tmux = B("tmux", "Ctrl-A › v", "Split pane horizontally", "", "prefix", "split-window -h")
        self.shift = B("macos", "⇧⌘G", "Go to Folder", "shift+cmd+g", "system", "")
        self.items = [self.cmd_k, self.opt_p, self.chord, self.tmux, self.shift]

    def test_mods_of_handles_combos_chords_and_tmux_text(self):
        self.assertEqual(kb.mods_of(self.cmd_k), {"cmd"})
        self.assertEqual(kb.mods_of(self.shift), {"shift", "cmd"})
        self.assertEqual(kb.mods_of(self.chord), {"cmd"})  # no norm, derived from the display text
        self.assertEqual(kb.mods_of(self.tmux), {"ctrl"})
        self.assertEqual(kb.mods_of(kb.Binding("x", "Plain", "d")), set())

    def test_parse_mods_aliases_and_errors(self):
        self.assertEqual(kb.parse_mods("command"), ["cmd"])
        self.assertEqual(kb.parse_mods("opt,shift"), ["alt", "shift"])
        self.assertEqual(sorted(kb.parse_mods("⌘⇧")), ["cmd", "shift"])
        with self.assertRaises(ValueError):
            kb.parse_mods("hyper")

    def test_mod_filter_requires_all_modifiers(self):
        got = kb.search(self.items, [], mods=["cmd"])
        self.assertEqual({b.keys for b in got}, {"⌘K", "⌘K ⌘C", "⇧⌘G"})
        got = kb.search(self.items, [], mods=["cmd", "shift"])
        self.assertEqual([b.keys for b in got], ["⇧⌘G"])

    def test_by_keys_ignores_descriptions_and_by_name_ignores_keys(self):
        # 'command' appears in one description but is a modifier word for the ⌘ bindings
        by_keys = kb.search(self.items, ["command"], by="keys")
        self.assertNotIn(self.opt_p, by_keys)
        self.assertIn(self.cmd_k, by_keys)
        by_name = kb.search(self.items, ["command"], by="name")
        self.assertEqual(by_name, [self.opt_p])
        self.assertEqual(len(kb.search(self.items, ["command"], by="all")), 4)

    def test_by_keys_matches_key_names(self):
        self.assertEqual(kb.search(self.items, ["shift"], by="keys"), [self.shift])
        self.assertEqual(kb.search(self.items, ["alt"], by="keys"), [self.opt_p])

    def test_cli_rejects_unknown_modifier(self):
        r = subprocess.run(
            [sys.executable, "-m", "kb", "-m", "hyper"], capture_output=True, text=True, timeout=30, check=False
        )
        self.assertEqual(r.returncode, 2)
        self.assertIn("unknown modifier", r.stderr)


class VSCodeExtensionCache(Sandbox):
    """Extensions come from VS Code's own manifest caches: 2 reads per profile, not ~140 package.json opens."""

    def setUp(self):
        super().setUp()
        self.profile = kb.VSC_USER.parent / "CachedProfilesData" / "__default__profile__"
        self.profile.mkdir(parents=True)

    def cache(self, name, extensions):
        (self.profile / name).write_text(json.dumps({"input": {}, "result": extensions}))

    @staticmethod
    def ext(name, display, kbs, commands=()):
        return {
            "identifier": {"id": name},
            "manifest": {
                "name": name,
                "displayName": display,
                "contributes": {"keybindings": kbs, "commands": list(commands)},
            },
        }

    def test_reads_cache_prefers_localized_and_dedupes_profiles(self):
        e = self.ext(
            "thing",
            "Thing",
            [{"command": "t.run", "key": "ctrl+r", "mac": "cmd+r", "when": "editorFocus"}],
            [{"command": "t.run", "title": "Run Thing"}],
        )
        self.cache("extensions.user.en.cache", [e])
        self.cache("extensions.user.cache", [self.ext("other", "Other", [{"command": "o", "key": "cmd+o"}])])  # ignored
        other = kb.VSC_USER.parent / "CachedProfilesData" / "work"
        other.mkdir()
        (other / "extensions.user.en.cache").write_text(json.dumps({"result": [e]}))  # same extension, 2nd profile
        got = kb.load_vscode_extensions()
        self.assertEqual([(b.keys, b.desc) for b in got], [("⌘R", "Run Thing  when: editorFocus")])
        self.assertIn("extension: Thing", got[0].note)

    def test_unresolved_nls_titles_fall_back_to_command_id(self):
        self.cache(
            "extensions.builtin.en.cache",
            [
                self.ext(
                    "x",
                    "%x.name%",
                    [{"command": "x.go", "key": "cmd+g"}],
                    [{"command": "x.go", "title": "%x.go.title%"}],
                )
            ],
        )
        got = kb.load_vscode_extensions()
        self.assertEqual(got[0].desc, "x.go")
        self.assertIn("extension: x", got[0].note)

    def test_corrupt_cache_warns_and_does_not_fall_back_to_scanning(self):
        (self.profile / "extensions.user.en.cache").write_text("{not json")
        self.assertEqual(kb.load_vscode_extensions(), [])
        self.assertTrue(any("cannot read extension cache" in w for w in kb.WARNINGS))

    def test_no_cache_falls_back_to_package_json_scan_with_a_note(self):
        for f in self.profile.iterdir():
            f.unlink()
        self.profile.rmdir()
        (kb.VSC_USER.parent / "CachedProfilesData").rmdir()
        ext = kb.HOME / ".vscode/extensions/acme.thing-1.0"
        ext.mkdir(parents=True)
        (ext / "package.json").write_text(
            json.dumps({"name": "thing", "contributes": {"keybindings": [{"command": "t.run", "key": "cmd+k"}]}})
        )
        got = kb.load_vscode_extensions()
        self.assertEqual([b.keys for b in got], ["⌘K"])
        self.assertTrue(any("package.json" in w for w in kb.WARNINGS_INFO))


class ToolScopedLoading(Sandbox):
    def setUp(self):
        super().setUp()
        self.calls = []
        self.saved_loaders = kb.LOADERS

        def make(name):
            def fn():
                self.calls.append(name)
                return [kb.Binding(name, "k", "d")]

            fn.__name__ = f"load_{name}"
            return fn

        kb.LOADERS = [make("tmux"), make("vscode"), make("vscode_extensions"), make("macos")]
        kb.LOADER_TOOL = {"load_vscode_extensions": "vscode"}

    def tearDown(self):
        kb.LOADERS = self.saved_loaders
        kb.LOADER_TOOL = {"load_vscode_extensions": "vscode"}
        super().tearDown()

    def test_only_requested_sources_are_read(self):
        items = kb.load_all(tools=["tmux"])
        self.assertEqual(self.calls, ["tmux"])
        self.assertEqual([b.tool for b in items], ["tmux"])

    def test_one_tool_can_own_several_loaders(self):
        kb.load_all(tools=["VSCode"])  # case-insensitive
        self.assertEqual(sorted(self.calls), ["vscode", "vscode_extensions"])

    def test_no_filter_reads_everything(self):
        kb.load_all()
        self.assertEqual(len(self.calls), 4)

    def test_known_tools_are_unique_tool_names(self):
        self.assertEqual(kb.known_tools(), ["macos", "tmux", "vscode"])

    def test_cli_rejects_unknown_tool_with_choices(self):
        r = subprocess.run(
            [sys.executable, "-m", "kb", "-t", "emacs"], capture_output=True, text=True, timeout=30, check=False
        )
        self.assertEqual(r.returncode, 2)
        self.assertIn("unknown tool 'emacs'", r.stderr)
        self.assertIn("vscode", r.stderr)


class VersionSource(Sandbox):
    def setUp(self):
        super().setUp()
        self.saved_file = kb.__file__
        (self.home / "src/kb").mkdir(parents=True)
        kb.__file__ = str(self.home / "src/kb/cli.py")

    def tearDown(self):
        kb.__file__ = self.saved_file
        super().tearDown()

    def test_checkout_reads_pyproject(self):
        (self.home / "pyproject.toml").write_text('[project]\nname = "kb"\nversion = "9.8.7"\n')
        self.assertEqual(kb._version(), "9.8.7")

    def test_foreign_pyproject_is_ignored_in_favour_of_installed_metadata(self):
        import importlib.metadata as md

        (self.home / "pyproject.toml").write_text('[project]\nname = "something-else"\nversion = "0.0.1"\n')
        saved = md.version
        md.version = lambda name: "4.5.6"
        try:
            self.assertEqual(kb._version(), "4.5.6")
        finally:
            md.version = saved

    def test_unknown_when_neither_available(self):
        import importlib.metadata as md

        saved = md.version

        def missing(name):
            raise md.PackageNotFoundError(name)

        md.version = missing
        try:
            self.assertEqual(kb._version(), "0+unknown")
        finally:
            md.version = saved

    def test_running_version_matches_pyproject(self):
        kb.__file__ = self.saved_file
        self.assertEqual(kb._version(), kb.VERSION)


class CLI(unittest.TestCase):
    def run_kb(self, *args, env=None):
        e = dict(os.environ, NO_COLOR="1", **(env or {}))
        return subprocess.run(
            [sys.executable, "-m", "kb", *args], capture_output=True, text=True, env=e, timeout=30, check=False
        )

    def test_exit_codes(self):
        self.assertEqual(self.run_kb("--version").returncode, 0)
        self.assertEqual(self.run_kb("zzz-no-such-binding-zzz", "--no-fzf").returncode, 1)
        self.assertEqual(self.run_kb("--bogus").returncode, 2)

    def test_json_schema(self):
        with tempfile.TemporaryDirectory() as d:
            r = self.run_kb("--json", env={"HOME": d, "XDG_CONFIG_HOME": d})
            data = json.loads(r.stdout)
            self.assertEqual(data["schema_version"], kb.SCHEMA_VERSION)
            self.assertIsInstance(data["bindings"], list)


class TerminalSafety(Sandbox):
    def test_sanitize_neutralises_layout_and_spoofing_characters(self):
        self.assertEqual(kb.sanitize("a\tb\nc"), "a b c")
        self.assertEqual(kb.sanitize("x\u202eevil\u202c"), "xevil")  # bidi override
        self.assertEqual(kb.sanitize("a\u200bb\u2028c\u2029d"), "abcd")  # zero-width, line/para separators
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


class Packaging(Sandbox):
    def test_version_matches_pyproject(self):
        import tomllib

        pp = Path(__file__).resolve().parents[1] / "pyproject.toml"
        self.assertEqual(tomllib.loads(pp.read_text())["project"]["version"], kb.VERSION)

    def test_no_runtime_dependencies(self):
        import tomllib

        pp = Path(__file__).resolve().parents[1] / "pyproject.toml"
        self.assertEqual(tomllib.loads(pp.read_text())["project"]["dependencies"], [])

    def test_no_shortcut_data_ships_in_the_package(self):
        pkg = Path(kb.__file__).resolve().parent
        self.assertEqual(sorted(p.name for p in pkg.rglob("*.json")), [])
        self.assertFalse((pkg / "data").exists())


class AutoDownload(Sandbox):
    """Apple/Microsoft shortcut lists are created locally on first use, never shipped."""

    def setUp(self):
        super().setUp()
        self.synced = []
        self.saved_sync = (kb.sync_apple, kb.sync_vscode)

        def fake(name):
            def sync(timeout=20):
                self.synced.append((name, timeout))
                kb.DATA_DIR.mkdir(parents=True, exist_ok=True)
                (kb.DATA_DIR / name).write_text(json.dumps({"meta": {}, "rows": []}))

            return sync

        kb.sync_apple, kb.sync_vscode = fake(kb.APPLE_JSON), fake(kb.VSC_JSON)
        patcher = mock.patch.dict(os.environ, {"KB_NO_AUTO_SYNC": ""})
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        kb.sync_apple, kb.sync_vscode = self.saved_sync
        super().tearDown()

    def test_existing_file_is_used_without_downloading(self):
        kb.DATA_DIR.mkdir(parents=True)
        (kb.DATA_DIR / kb.APPLE_JSON).write_text("{}")
        self.assertEqual(kb.ensure_data(kb.APPLE_JSON), kb.DATA_DIR / kb.APPLE_JSON)
        self.assertEqual(self.synced, [])

    def test_missing_file_is_downloaded_once_with_a_short_timeout_and_announced(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            p1 = kb.ensure_data(kb.VSC_JSON)
            p2 = kb.ensure_data(kb.VSC_JSON)
        self.assertEqual(p1, kb.DATA_DIR / kb.VSC_JSON)
        self.assertEqual(p2, p1)
        self.assertEqual(self.synced, [(kb.VSC_JSON, kb.AUTO_SYNC_TIMEOUT)])  # second call finds the file
        self.assertIn("downloading it once from https://code.visualstudio.com/", err.getvalue())

    def test_opt_out_env_disables_downloads_and_loaders_warn_instead(self):
        with mock.patch.dict(os.environ, {"KB_NO_AUTO_SYNC": "1"}):
            self.assertIsNone(kb.ensure_data(kb.APPLE_JSON))
            self.assertEqual(kb.load_apple_docs(), [])
            self.assertEqual(kb.load_vscode(), [])
        self.assertEqual(self.synced, [])
        self.assertTrue(any("kb --sync-apple" in w for w in kb.WARNINGS))
        self.assertTrue(any("kb --sync-vscode" in w for w in kb.WARNINGS))

    def test_download_failure_warns_and_does_not_crash(self):
        def boom(timeout=20):
            raise OSError("network is unreachable")

        kb.sync_apple = boom
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertIsNone(kb.ensure_data(kb.APPLE_JSON))
        self.assertTrue(any("could not download" in w and "unreachable" in w for w in kb.WARNINGS))

    def test_tool_scoped_run_never_downloads_unrelated_data(self):
        kb.DARWIN = True
        with contextlib.redirect_stderr(io.StringIO()):
            kb.load_all(tools=["tmux"])
        self.assertEqual(self.synced, [])

    def test_save_leaves_no_temp_files_behind(self):
        rows = [{"section": "s", "combo": "c", "desc": "d"}] * 60
        kb._save_doc(kb.APPLE_JSON, kb.APPLE_URL, b"raw", rows, 50)
        self.assertEqual(sorted(p.name for p in kb.DATA_DIR.iterdir()), [kb.APPLE_JSON])


class ScriptingContract(Sandbox):
    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = kb.main(list(argv))
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue()

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
        rc, _out, err = self.run_main("-t", "tmx")
        self.assertEqual(rc, 2)
        self.assertIn("did you mean 'tmux'", err)

    def test_closed_pipe_is_silent(self):
        code = "import kb.cli as k; k.main = lambda: [print('x' * 80) for _ in range(5000)] and 0; k.run()"
        cmd = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)} | head -n 1 >/dev/null"
        r = subprocess.run(["sh", "-c", cmd], check=False, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.stderr, "")


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
            self.assertTrue(kb.want_color(tty))  # empty NO_COLOR is unset (no-color.org)
        with self.env(FORCE_COLOR="1"):
            self.assertTrue(kb.want_color(pipe))
        with self.env(FORCE_COLOR="0"):
            self.assertFalse(kb.want_color(pipe))
        with self.env(NO_COLOR="1", FORCE_COLOR="1"):
            self.assertFalse(kb.want_color(pipe))  # NO_COLOR wins


class FzfPreview(unittest.TestCase):
    def test_preview_command_quotes_interpreter_and_script(self):
        with (
            mock.patch.object(kb.sys, "executable", "/tmp/my dir/py;touch pwn"),
            mock.patch.object(kb.sys, "argv", ["/tmp/my dir/kb"]),
        ):
            cmd = kb.preview_command(Path("/tmp/snap dir/s.json"))
        parts = shlex.split(cmd)
        self.assertEqual(parts[0], "/tmp/my dir/py;touch pwn")
        self.assertEqual(parts[1], str(Path("/tmp/my dir/kb").resolve()))
        self.assertEqual(parts[-1], "/tmp/snap dir/s.json")
        self.assertIn("{1}", cmd)


class NetIsolation(unittest.TestCase):
    SRC = Path(kb.__file__).parent

    BANNED = ("urllib.request", "http.client", "socket", "ssl")

    @staticmethod
    def imports_in(source, package="kb"):
        """Every module name a source text can import, with relative imports resolved against `package`."""
        names = set()
        for n in ast.walk(ast.parse(source)):
            if isinstance(n, ast.Import):
                names |= {a.name for a in n.names}
            elif isinstance(n, ast.ImportFrom):
                base = n.module or ""
                if n.level:
                    parts = package.split(".")[: len(package.split(".")) - (n.level - 1)]
                    base = ".".join(parts + ([n.module] if n.module else []))
                names.add(base)
                names |= {f"{base}.{a.name}" for a in n.names}
        return names

    def imports_of(self, path):
        return self.imports_in(path.read_text())

    @classmethod
    def banned_in(cls, imported):
        return {m for m in imported if any(m == b or m.startswith(b + ".") for b in cls.BANNED)}

    def test_only_net_module_imports_the_network_stack(self):
        offenders = {}
        for f in self.SRC.glob("*.py"):
            bad = self.banned_in(self.imports_of(f))
            if bad and f.name != "net.py":
                offenders[f.name] = bad
        self.assertEqual(offenders, {})

    def test_net_module_is_stateless_and_does_not_import_cli(self):
        imported = self.imports_of(self.SRC / "net.py")
        self.assertFalse({"kb.cli", "cli"} & imported, imported)  # absolute or relative import of the CLI module

    def test_guard_detects_from_import_forms_of_the_network_stack(self):
        for src in (
            "from urllib import request",
            "from http import client",
            "from urllib.request import urlopen",
            "import socket",
            "from ssl import create_default_context",
        ):
            self.assertTrue(self.banned_in(self.imports_in(src)), src)
        self.assertFalse(self.banned_in(self.imports_in("import urllib.parse\nfrom . import net")))

    def test_guard_detects_cli_imports_in_all_forms(self):
        for src in (
            "from . import cli",
            "from .cli import x",
            "from kb import cli",
            "import kb.cli",
            "from kb.cli import x",
        ):
            self.assertIn("kb.cli", self.imports_in(src), src)
        self.assertIn("kb.net", self.imports_in("from .net import x"))

    def test_cli_wrappers_still_delegate(self):
        self.assertTrue(callable(kb._fetch) and callable(kb._save_doc))
        from kb import net

        self.assertTrue(callable(net.fetch) and callable(net.save_doc))


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


class Fetch(unittest.TestCase):
    def test_redirect_to_another_host_is_refused_before_following(self):
        h = net.SameHostRedirect("example.com")
        req = urllib.request.Request("https://example.com/a")
        with self.assertRaises(ValueError):
            h.redirect_request(req, None, 302, "Found", {}, "https://evil.example/b")
        with self.assertRaises(ValueError):
            h.redirect_request(req, None, 302, "Found", {}, "http://example.com/b")  # downgrade
        ok = h.redirect_request(req, None, 302, "Found", {}, "https://example.com/b")
        self.assertEqual(ok.full_url, "https://example.com/b")

    def test_total_deadline_aborts_a_slow_drip(self):
        class Slow:
            def read(self, n):
                time.sleep(0.05)
                return b"x"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class Op:
            def open(self, req, timeout=None):
                return Slow()

        with self.assertRaises(ValueError) as cm:
            kb._fetch("https://example.com/", "example.com", timeout=0.2, opener=Op())
        self.assertIn("deadline", str(cm.exception))

    def test_size_cap_is_enforced_while_reading(self):
        class Big:
            def read(self, n):
                return b"x" * n

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class Op:
            def open(self, req, timeout=None):
                return Big()

        with self.assertRaises(ValueError):
            kb._fetch("https://example.com/", "example.com", timeout=5, opener=Op())

    def test_wrong_host_or_scheme_still_refused(self):
        for url in ("http://example.com/", "https://evil.example/"):
            with self.assertRaises(ValueError):
                kb._fetch(url, "example.com")


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


if __name__ == "__main__":
    unittest.main()
