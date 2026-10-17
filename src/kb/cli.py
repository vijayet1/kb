"""kb - browse and search every keybinding on this Mac, read live from your configs.

kb                   interactive fuzzy browser (fzf), falls back to a full list
kb tmux split        search: every word must match (tool, keys, description, command)
kb -k opt+t          who owns this key combo? (alt/opt/option, cmd/super, ctrl, shift)
kb -m command        every binding that uses the Command (⌘) key (also: -m cmd,shift  /  -m ⌥)
kb --by keys left    search only the key combo;  kb --by name command  search only what it does
kb -t aerospace      only one tool (repeatable: -t tmux -t skhd)
kb -c                show conflicts (same key bound twice)
kb --tools           list sources and how many bindings each has
kb --json            machine-readable output
"""

import argparse
import contextlib
import ctypes
import difflib
import importlib.metadata
import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from pathlib import Path

from . import net
from .net import NETWORK_ERRORS, xdg_dir

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11; pip/uv enforce requires-python, this covers running the file directly
    sys.exit(f"kb: needs Python 3.11+ (found {sys.version.split()[0]}); try: brew install python")


def _version() -> str:
    """Single source of truth is pyproject.toml: read it in a checkout, else the installed package metadata."""
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    try:
        project = tomllib.loads(pyproject.read_text()).get("project", {})
        if project.get("name") == "kb" and project.get("version"):
            return str(project["version"])
    except (OSError, ValueError):
        pass
    try:
        return importlib.metadata.version("kb")
    except importlib.metadata.PackageNotFoundError:
        return "0+unknown"


VERSION = _version()
SCHEMA_VERSION = 1
HOME = Path.home()
CFG = xdg_dir("XDG_CONFIG_HOME", HOME / ".config")
DARWIN = sys.platform == "darwin"
DEFAULTS_BIN = "/usr/bin/defaults"  # absolute: never resolve system tools through $PATH
LOADER_BUDGET = float(os.environ.get("KB_LOADER_TIMEOUT", "10"))  # seconds any one source may take


def want_color(stream) -> bool:
    env = os.environ
    if env.get("NO_COLOR"):
        return False
    if any(env.get(v, "") not in ("", "0") for v in ("FORCE_COLOR", "CLICOLOR_FORCE")):
        return True
    return stream.isatty()


@dataclass
class Settings:
    """Process-wide switches set once from the command line (attribute assignment, not `global`)."""

    color: bool = field(default_factory=lambda: want_color(sys.stdout))
    include_unnamed: bool = False  # --all-system


SETTINGS = Settings()
LAST_TIMINGS: dict[str, float] = {}  # loader name -> seconds, filled by load_all (shown by --debug / --doctor)
STUCK: list[str] = []  # loaders that blew the budget; their threads are abandoned, so we exit with os._exit
WARNINGS_INFO: list[str] = []  # non-fatal notices for --doctor
WARNINGS: list[str] = []  # parse problems collected here; shown by --doctor and on stderr

# ---------------------------------------------------------------- model

MOD_ORDER = ["ctrl", "alt", "shift", "cmd", "fn"]
MOD_SYM = {"ctrl": "⌃", "alt": "⌥", "shift": "⇧", "cmd": "⌘", "fn": "fn "}
MOD_ALIAS = {
    "ctrl": "ctrl",
    "control": "ctrl",
    "ctl": "ctrl",
    "alt": "alt",
    "opt": "alt",
    "option": "alt",
    "meta": "alt",
    "lalt": "alt",
    "ralt": "alt",
    "shift": "shift",
    "lshift": "shift",
    "rshift": "shift",
    "cmd": "cmd",
    "command": "cmd",
    "super": "cmd",
    "lcmd": "cmd",
    "rcmd": "cmd",
    "fn": "fn",
}
KEY_ALIAS = {
    "minus": "-",
    "equal": "=",
    "slash": "/",
    "comma": ",",
    "period": ".",
    "semicolon": ";",
    "quote": "'",
    "backslash": "\\",
    "backtick": "`",
    "grave": "`",
    "leftsquarebracket": "[",
    "rightsquarebracket": "]",
    "lbracket": "[",
    "rbracket": "]",
    "enter": "return",
    "esc": "escape",
    "bspace": "backspace",
    "del": "delete",
}
KEY_SYM = {
    "left": "←",
    "right": "→",
    "up": "↑",
    "down": "↓",
    "return": "↩",
    "tab": "⇥",
    "escape": "⎋",
    "space": "Space",
    "backspace": "⌫",
    "delete": "⌦",
}


_SECRET = re.compile(
    r"(?i)((?:token|secret|passw(?:or)?d|passwd|api[_-]?key|apikey|auth|bearer|credential)s?[\w-]*\s*[=:]\s*|"
    r"authorization:\s*(?:bearer|basic)\s+|--?(?:token|password|passwd|secret|api-key)[= ])"
    r"(\S+)"
)
_TOKENISH = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9]{20,}|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,})"
)


_DROP_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})


def sanitize(text: object) -> str:
    """Make config/web text safe to print: no escape sequences, bidi/zero-width tricks, or layout characters."""
    return "".join(
        " " if ch in "\t\n\r" else ch
        for ch in str(text)
        if ch in "\t\n\r" or unicodedata.category(ch) not in _DROP_CATEGORIES
    )


def read_lines(p: Path) -> list[str]:
    """Lines split on \\n only: str.splitlines() also splits on U+2028/U+0085 and would let a file hide a binding."""
    return p.read_text().replace("\r\n", "\n").split("\n")


def redact(text: str) -> str:
    text = _SECRET.sub(lambda m: m[1] + "«redacted»", text)
    return _TOKENISH.sub("«redacted»", text)


@dataclass
class Binding:
    tool: str
    keys: str  # display
    desc: str  # what it does
    norm: str = ""  # canonical combo, e.g. "alt+t"; "" if not comparable
    scope: str = "global"  # global | <context inside an app>
    raw: str = ""
    src: str = ""
    tags: str = ""
    note: str = ""

    def __post_init__(self):
        # config text is untrusted for terminal purposes: strip control chars (escape-sequence injection)
        # and mask anything that looks like a credential before it can reach stdout, JSON or fzf.
        for f in ("keys", "desc", "raw", "src", "tags", "note", "scope"):
            setattr(self, f, sanitize(getattr(self, f)))
        self.raw = redact(self.raw)
        self.desc = redact(self.desc)


def norm_combo(mods: Iterable[str], key: str) -> str:
    ms = sorted(
        {MOD_ALIAS.get(m.lower(), m.lower()) for m in mods if m},
        key=lambda m: MOD_ORDER.index(m) if m in MOD_ORDER else 9,
    )
    k = key.lower()
    k = KEY_ALIAS.get(k, k)
    return "+".join([*ms, k])


def pretty(norm: str) -> str:
    parts = norm.split("+") if not norm.endswith("++") else [*norm[:-2].split("+"), "+"]
    *mods, key = parts
    k = KEY_SYM.get(key, key.upper() if len(key) == 1 or re.fullmatch(r"f\d{1,2}", key) else key)
    return "".join(MOD_SYM.get(m, m) for m in mods) + k


def parse_combo(text: str) -> str:
    """'alt-shift-left', 'opt+t', 'shift + alt - h', '⌥t' -> normalised combo."""
    t = text.strip()
    trailing = None
    if len(t) >= 1 and t[-1] in "-+" and (len(t) == 1 or t[-2] in "-+ "):
        trailing, t = t[-1], t[:-1].rstrip("-+ ")
    sym2name = {"⌃": "ctrl ", "⌥": "alt ", "⇧": "shift ", "⌘": "cmd "}
    for s, n in sym2name.items():
        t = t.replace(s, n)
    t = t.replace("+", " ").replace(" - ", " ").replace("-", " ")
    toks = t.split()
    if trailing:
        toks.append(trailing)
    if not toks:
        return ""
    mods = list(toks[:-1])
    last = toks[-1]
    if last.lower() in MOD_ALIAS and len(toks) == 1:
        return MOD_ALIAS[last.lower()]
    return norm_combo(mods, last)


# ---------------------------------------------------------------- AeroSpace


def aero_desc(cmd: str) -> str:
    cmd = cmd.strip()
    rules = [
        (r"focus (\w+)", lambda m: f"Focus window {m[1]}"),
        (r"move (\w+)", lambda m: f"Move window {m[1]}"),
        (r"join-with (\w+)", lambda m: f"Join with window {m[1]}"),
        (r"resize smart ([+-]?\d+)", lambda m: f"Resize window ({m[1]})"),
        (r"balance-sizes", lambda _: "Balance window sizes"),
        (r"flatten-workspace-tree", lambda _: "Flatten workspace tree"),
        (r"layout floating tiling", lambda _: "Toggle floating / tiling"),
        (r"layout (.+)", lambda m: f"Switch layout: {m[1]}"),
        (r"workspace-back-and-forth", lambda _: "Jump to previous workspace"),
        (r"workspace (\S+)", lambda m: f"Go to workspace {m[1]}"),
        (
            r"move-node-to-workspace (\S+)( --focus-follows-window)?",
            lambda m: f"Send window to workspace {m[1]}" + (" and follow" if m[2] else ""),
        ),
        (r"move-workspace-to-monitor.*", lambda _: "Move workspace to next monitor"),
        (r"move-node-to-monitor.*", lambda _: "Move window to next monitor"),
        (r"mode (\w+)", lambda m: f"Enter '{m[1]}' mode" if m[1] != "main" else "Back to main mode"),
        (r"reload-config", lambda _: "Reload AeroSpace config"),
        (r"close-all-windows-but-current", lambda _: "Close all windows except current"),
        (r"exec-and-forget\s+open\s+-na?\s+(.+)", lambda m: f"Open new instance: {m[1].split(' --args')[0]}"),
        (r"exec-and-forget\s+open\s+-a\s+(.+)", lambda m: f"Open {m[1].strip(chr(34) + chr(39))}"),
        (r"exec-and-forget\s+(.+)", lambda m: f"Run script: {Path(m[1].split()[0]).name}"),
    ]
    for pat, fn in rules:
        m = re.fullmatch(pat, cmd)
        if m:
            return fn(m)
    return cmd


def load_aerospace() -> list[Binding]:
    p = CFG / "aerospace" / "aerospace.toml"
    out: list[Binding] = []
    if not p.exists():
        return out
    data = tomllib.loads(p.read_text())
    lines = read_lines(p)
    for mode, body in data.get("mode", {}).items():
        for key, cmds in body.get("binding", {}).items():
            cl = cmds if isinstance(cmds, list) else [cmds]
            descs = [aero_desc(c) for c in cl if not c.startswith("mode main") or len(cl) == 1]
            desc = " → ".join(descs) if descs else "Back to main mode"
            if len(cl) > 1 and cl[-1] == "mode main":
                desc += "  (then back to main)"
            lineno = next((i + 1 for i, ln in enumerate(lines) if re.match(rf"\s*{re.escape(key)}\s*=", ln)), 0)
            n = parse_combo(key)
            out.append(
                Binding(
                    "aerospace",
                    pretty(n),
                    desc,
                    n,
                    "global" if mode == "main" else f"mode:{mode}",
                    " ; ".join(cl),
                    f"{p}:{lineno}",
                    f"window-manager tiling {mode}",
                )
            )
    return out


# ---------------------------------------------------------------- skhd


def load_skhd() -> list[Binding]:
    p = CFG / "skhd" / "skhdrc"
    out: list[Binding] = []
    if not p.exists():
        return out
    for i, line in enumerate(read_lines(p), 1):
        s = line.strip()
        if not s or s.startswith("#") or ":" not in s:
            continue
        lhs, cmd = s.split(":", 1)
        lhs = lhs.strip()
        if "-" not in lhs:
            continue
        mod_text, key = lhs.rsplit("-", 1)
        mods = [m for m in re.split(r"[+\s]+", mod_text) if m]
        n = norm_combo(mods, key.strip())
        cmd = cmd.strip()
        m = re.match(r"open\s+-a\s+(.+)", cmd)
        desc = f"Open {m[1].strip(chr(34))}" if m else f"Run: {cmd}"
        out.append(Binding("skhd", pretty(n), desc, n, "global", cmd, f"{p}:{i}", "hotkey launcher"))
    return out


# ---------------------------------------------------------------- tmux

TMUX_VERBS = {
    "new-window": "New window",
    "previous-window": "Previous window",
    "next-window": "Next window",
    "last-window": "Last window",
    "list-windows": "List windows",
    "rename-window": "Rename window",
    "source-file": "Reload config",
    "detach": "Detach from session",
    "list-clients": "List clients",
    "lock-server": "Lock server",
    "refresh-client": "Refresh client",
    "choose-window": "Choose window",
    "choose-session": "Choose session",
    "command-prompt": "tmux command prompt",
    "kill-pane": "Kill pane",
    "swap-pane": "Swap pane",
    "resize-window": "Resize window to fit",
    "send-keys": "Send keys",
    "set": "Toggle option",
    "setw": "Toggle window option",
}


def tmux_desc(cmd):
    c = cmd.split()
    if not c:
        return ""
    v, rest = c[0], " ".join(c[1:])
    if v == "command-prompt" and len(c) > 1:
        inner = c[1]
        return "Prompt: " + inner.replace("-", " ")
    if v == "split-window":
        return "Split pane " + (
            "horizontally (side by side)" if "-h" in c else "vertically (stacked)" if "-v" in c else "(default)"
        )
    if v == "select-pane":
        d = {"-L": "left", "-D": "down", "-U": "up", "-R": "right"}
        return "Select pane " + next((d[x] for x in c if x in d), "")
    if v == "resize-pane":
        if "-Z" in c:
            return "Zoom / unzoom pane"
        d = {"-L": "left", "-R": "right", "-D": "down", "-U": "up"}
        n = c[-1]
        return f"Resize pane {next((d[x] for x in c if x in d), '')} by {n}"
    if v == "setw" and "synchronize-panes" in rest:
        return "Toggle synchronize-panes (type in all panes)"
    if v == "set" and "pane-border-status" in rest:
        return "Toggle pane border status"
    if v == "send-keys" and "-X" in c:
        return f"Copy mode: {c[c.index('-X') + 1].replace('-', ' ')}"
    if v == "send-keys" and "clear" in rest:
        return "Clear screen"
    return TMUX_VERBS.get(v, cmd)


def tmux_key(k):
    if k.startswith("^") and len(k) == 2:
        return f"Ctrl-{k[1].upper()}"
    return {"C-a": "Ctrl-A"}.get(k, k)


def load_tmux() -> list[Binding]:
    files = [CFG / "tmux" / "tmux.reset.conf", CFG / "tmux" / "tmux.conf", HOME / ".tmux.conf"]
    seen = set()
    out, prefix = [], "Ctrl-B"
    for p in files:
        if p.exists():
            m = re.search(r"^\s*set(?:-option)?\s+-g\s+prefix\s+(\S+)", p.read_text(), re.M)
            if m:
                prefix = tmux_key(m[1])
    for p in files:
        if not p.exists() or p.resolve() in seen:
            continue
        seen.add(p.resolve())
        for i, line in enumerate(read_lines(p), 1):
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if re.match(r"set(?:-option)?\s+-g\s+prefix\s", s):
                continue
            m = re.match(r"set\s+-g\s+@(\w[\w-]*?)-bind\s+['\"]?([^'\"\s]+)", s)
            if m:
                out.append(
                    Binding("tmux", f"{prefix} › {m[2]}", f"Plugin: {m[1]}", "", "prefix", s, f"{p}:{i}", "plugin")
                )
                continue
            if not re.match(r"bind(-key)?\s", s):
                continue
            try:
                t = shlex.split(s.replace("\\;", " ;"))
            except ValueError:
                continue
            t = t[1:]
            table, root = "prefix", False
            while t and t[0] in ("-n", "-r", "-T", "-N"):
                f = t.pop(0)
                if f in ("-T", "-N"):
                    val = t.pop(0)
                    if f == "-T":
                        table = val
                elif f == "-n":
                    root, table = True, "root"
            if len(t) < 2:
                continue
            key, cmd = t[0], " ".join(t[1:])
            kd = tmux_key(key)
            disp = kd if root else (f"{prefix} › {kd}" if table == "prefix" else f"[{table}] {kd}")
            out.append(Binding("tmux", disp, tmux_desc(cmd), "", table, cmd, f"{p}:{i}", f"multiplexer {table}"))
    # same table+key bound twice: the later one wins
    last = {}
    for b in out:
        last[(b.scope, b.keys)] = b
    for b in out:
        if last[(b.scope, b.keys)] is not b:
            b.note = "overridden by a later binding"
    return out


# ---------------------------------------------------------------- herdr


def load_herdr() -> list[Binding]:
    p = CFG / "herdr" / "config.toml"
    out: list[Binding] = []
    if not p.exists():
        return out
    d = tomllib.loads(p.read_text())
    keys = d.get("keys", {})
    prefix = keys.get("prefix")
    if prefix:
        out.append(
            Binding(
                "herdr",
                prefix.replace("ctrl+", "Ctrl-").upper().replace("CTRL-", "Ctrl-"),
                "Prefix key",
                "",
                "herdr",
                f"prefix = {prefix}",
                str(p),
            )
        )
    for c in keys.get("command", []):
        k = c.get("key", "")
        disp = k.replace(
            "prefix+", f"{(prefix or 'prefix').replace('ctrl+', 'Ctrl-').title().replace('Ctrl-', 'Ctrl-')} › "
        )
        out.append(
            Binding(
                "herdr",
                disp,
                f"{c.get('type', 'command')}: {c.get('command', '')} "
                f"({c.get('width', '')}×{c.get('height', '')})".replace(" (×)", ""),
                "",
                "herdr",
                json.dumps(c),
                str(p),
            )
        )
    return out


# ---------------------------------------------------------------- Ghostty / cmux


def load_ghostty() -> list[Binding]:
    out: list[Binding] = []
    for p in [CFG / "ghostty" / "config", CFG / "ghostty" / "picker.config"]:
        if not p.exists():
            continue
        for i, line in enumerate(read_lines(p), 1):
            m = re.match(r"\s*keybind\s*=\s*(.+?)=(.+)", line)
            if m:
                n = parse_combo(m[1].replace("+", "-").replace("super", "cmd"))
                out.append(
                    Binding(
                        "ghostty", pretty(n), m[2].strip().replace("_", " "), n, "ghostty", line.strip(), f"{p}:{i}"
                    )
                )
    return out


# ---------------------------------------------------------------- macOS

KEYCODES = {
    0: "a",
    1: "s",
    2: "d",
    3: "f",
    4: "h",
    5: "g",
    6: "z",
    7: "x",
    8: "c",
    9: "v",
    11: "b",
    12: "q",
    13: "w",
    14: "e",
    15: "r",
    16: "y",
    17: "t",
    18: "1",
    19: "2",
    20: "3",
    21: "4",
    22: "6",
    23: "5",
    24: "=",
    25: "9",
    26: "7",
    27: "-",
    28: "8",
    29: "0",
    30: "]",
    31: "o",
    32: "u",
    33: "[",
    34: "i",
    35: "p",
    36: "return",
    37: "l",
    38: "j",
    39: "'",
    40: "k",
    41: ";",
    42: "\\",
    43: ",",
    44: "/",
    45: "n",
    46: "m",
    47: ".",
    48: "tab",
    49: "space",
    50: "`",
    51: "delete",
    53: "escape",
    122: "f1",
    120: "f2",
    99: "f3",
    118: "f4",
    96: "f5",
    97: "f6",
    98: "f7",
    100: "f8",
    101: "f9",
    109: "f10",
    103: "f11",
    111: "f12",
    123: "left",
    124: "right",
    125: "down",
    126: "up",
}


def cocoa_mods(flags):
    return [
        n
        for bit, n in ((0x20000, "shift"), (0x40000, "ctrl"), (0x80000, "alt"), (0x100000, "cmd"), (0x800000, "fn"))
        if flags & bit
    ]


HOTKEY_NAMES = {
    7: "Focus menu bar",
    8: "Focus Dock",
    9: "Focus window toolbar",
    10: "Focus floating window",
    12: "Focus status menus",
    27: "Cycle windows in app",
    28: "Screenshot screen to file",
    29: "Screenshot screen to clipboard",
    30: "Screenshot area to file",
    31: "Screenshot area to clipboard",
    32: "Mission Control",
    33: "Application windows",
    34: "Mission Control (alt)",
    36: "Show Desktop",
    52: "Toggle Dock auto-hide",
    57: "Focus Dock (alt)",
    60: "Previous input source",
    61: "Next input source",
    64: "Spotlight search",
    65: "Finder search window",
    79: "Move left a space",
    81: "Move right a space",
    98: "Show help menu",
    118: "Switch to Desktop 1",
    119: "Switch to Desktop 2",
    120: "Switch to Desktop 3",
    121: "Switch to Desktop 4",
    163: "Notification Center",
    175: "Do Not Disturb",
    184: "Screenshot / recording options",
    190: "Quick Note",
}


# Apple labels no symbolic-hotkey IDs publicly. Only the *names* below are maintained by hand (community-documented);
# the key combo and the enabled state are always read live from macOS (CoreGraphics), never typed in.
SYSTEM_HOTKEY_NAMES = {
    7: "Move focus to the menu bar",
    8: "Move focus to the Dock",
    27: "Move focus to next window in app",
    28: "Screenshot: screen to file",
    29: "Screenshot: screen to clipboard",
    30: "Screenshot: selection to file",
    31: "Screenshot: selection to clipboard",
    32: "Mission Control",
    33: "Application windows",
    36: "Show Desktop",
    52: "Turn Dock hiding on/off",
    57: "Move focus to the status menus",
    60: "Select previous input source",
    61: "Select next input source in menu",
    64: "Spotlight search",
    65: "Finder search window",
    79: "Move left a space",
    81: "Move right a space",
    98: "Show Help menu",
    118: "Switch to Desktop 1",
    119: "Switch to Desktop 2",
    120: "Switch to Desktop 3",
    121: "Switch to Desktop 4",
    163: "Notification Center",
    175: "Do Not Disturb",
    184: "Screenshot and recording options",
    190: "Quick Note",
}

APPLE_URL = "https://support.apple.com/en-us/102650"
APPLE_HOST = "support.apple.com"
APPLE_MAX_BYTES = 5_000_000
DATA_DIR = xdg_dir("XDG_DATA_HOME", HOME / ".local/share") / "kb"
APPLE_JSON = "apple-shortcuts.json"
AUTO_SYNC_TIMEOUT = 8  # seconds; first-run downloads must fit inside the per-source budget
STANDARD_KEY_BINDING = Path("/System/Library/Frameworks/AppKit.framework/Resources/StandardKeyBinding.dict")

# Apple's article headings -> where the shortcut applies. "system" mixes Finder and OS-wide entries, so it is
# never treated as globally active (avoids false conflicts).
APPLE_SCOPES = {
    "cut, copy, paste": "app",
    "sleep, log out": "system",
    "finder and system": "system",
    "text-editing": "text",
    "vision": "accessibility",
    "keyboard focus": "accessibility",
    "other accessibility": "accessibility",
    "accessibility": "accessibility",
}
APPLE_MOD = {
    "command": "cmd",
    "control": "ctrl",
    "option": "alt",
    "shift": "shift",
    "fn": "fn",
    "globe": "fn",
    "caps lock": "caps",
}
APPLE_KEY = {
    "space bar": "space",
    "esc": "escape",
    "delete": "backspace",
    "forward delete": "delete",
    "return": "return",
    "enter": "return",
    "tab": "tab",
    "up arrow": "up",
    "down arrow": "down",
    "left arrow": "left",
    "right arrow": "right",
    "power button": "power",
    "eject": "eject",
}
APPLE_SKIP = re.compile(r"(?i)\b(while|click|drag|double|press twice|hold)\b|\bor\b.*\bwhile\b")
_PAREN_CHAR = re.compile(r"[A-Za-z ]+\(([^\w\s])\)")


def apple_combo(text: str) -> str | None:
    """Apple article notation ('Shift–Command–Question mark (?)') -> norm string, or None if not a plain key combo."""
    t = text.strip().rstrip(":").strip()
    if not t or APPLE_SKIP.search(t):
        return None
    t = _PAREN_CHAR.sub(lambda m: f"KEY{ord(m[1])}", t)
    t = re.sub(r"\([^)]*\)", "", t)
    toks = [x.strip() for x in re.split(r"[-–—]", t) if x.strip()]
    if not toks:
        return None
    mods, key = [], None
    for i, tok in enumerate(toks):
        low = tok.lower()
        if low in APPLE_MOD and i < len(toks) - 1:
            mods.append(APPLE_MOD[low])
        elif i == len(toks) - 1:
            if low in APPLE_MOD:
                return None  # modifier-only
            m = re.fullmatch(r"key(\d+)", low)
            key = chr(int(m[1])) if m else APPLE_KEY.get(low, low)
            if not m and len(key) > 1 and not re.fullmatch(r"f\d{1,2}|[a-z0-9]", key) and key not in APPLE_KEY.values():
                key = re.sub(r"\s+", "-", key)  # named hardware key, e.g. brightness-down
        else:
            return None
    return norm_combo(mods, key) if key else None


def apple_split_alternatives(combo_text, desc):
    """'Control-F2' + 'or Fn-Control-F2: Move focus...' -> combos [..], clean description."""
    combos = [combo_text]
    while True:
        m = re.match(r"^(?:or|and)\s+([^:]{2,60}?):\s*(.*)$", desc)
        if not m:
            break
        combos.append(m[1])
        desc = m[2]
    return combos, desc


class _AppleParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.sec, self.h, self.stack, self.rows, self.inb = "", None, [], [], 0

    def handle_starttag(self, t, _attrs):
        if t in ("h2", "h3"):
            self.h = ""
        elif t == "li":
            self.stack.append({"b": None, "txt": ""})
        elif t == "b" and self.stack:
            self.inb += 1

    def handle_endtag(self, t):
        if t in ("h2", "h3") and self.h is not None:
            self.sec, self.h = self.h.strip(), None
        elif t == "b" and self.stack:
            self.inb = max(0, self.inb - 1)
        elif t == "li" and self.stack:
            it = self.stack.pop()
            if it["b"]:
                self.rows.append(
                    {
                        "section": self.sec,
                        "combo": it["b"].strip().rstrip(":").strip(),
                        "desc": re.sub(r"\s+", " ", it["txt"]).strip(" :"),
                    }
                )

    def handle_data(self, d):
        if self.h is not None:
            self.h += d
        elif self.stack:
            it = self.stack[-1]
            if self.inb and not it["txt"].strip():
                it["b"] = (it["b"] or "") + d
            else:
                it["txt"] += d


def parse_apple_html(html_text):
    p = _AppleParser()
    p.feed(html_text)
    return p.rows


def _fetch(url: str, host: str, timeout: float = 20, *, opener=None) -> bytes:
    return net.fetch(
        url,
        host,
        timeout=timeout,
        max_bytes=APPLE_MAX_BYTES,
        user_agent=f"kb/{VERSION} (+local keybinding index)",
        opener=opener,
    )


def _save_doc(name, url, raw, rows, min_rows, extra=None):
    return net.save_doc(DATA_DIR, name, url, raw, rows, min_rows, version=VERSION, extra=extra)


def sync_apple(url: str = APPLE_URL, timeout: float = 20) -> dict:
    """Fetch Apple's 'Mac keyboard shortcuts' article and store the rows (plus provenance) in DATA_DIR."""
    raw = _fetch(url, APPLE_HOST, timeout)
    return _save_doc(APPLE_JSON, url, raw, parse_apple_html(raw.decode("utf-8", "replace")), 50)


def auto_sync_enabled() -> bool:
    return os.environ.get("KB_NO_AUTO_SYNC", "") in ("", "0")


def ensure_data(name: str) -> Path | None:
    """Path of a downloaded data file, fetching it once if it is missing (first run on this machine).

    Shortcut lists from Apple and Microsoft are never shipped in this repo or package; they are created locally from
    the official pages. Returns None when unavailable (offline, auto-download disabled, page layout changed)."""
    path = DATA_DIR / name
    if path.exists():
        return path
    sync, host = {APPLE_JSON: (sync_apple, APPLE_HOST), VSC_JSON: (sync_vscode, VSC_HOST)}[name]
    if not auto_sync_enabled():
        return None
    print(
        f"kb: {name} not found - downloading it once from https://{host}/ (KB_NO_AUTO_SYNC=1 disables)", file=sys.stderr
    )
    try:
        sync(timeout=AUTO_SYNC_TIMEOUT)
    except NETWORK_ERRORS as e:
        WARNINGS.append(f"could not download {name}: {e}")
        return None
    return path if path.exists() else None


def read_doc(name: str, required: tuple[str, ...]) -> dict | None:
    """Load a downloaded data file; on corruption warn (with the repair command) and return None."""
    p = ensure_data(name)
    if p is None:
        return None
    try:
        doc = json.loads(p.read_text())
        rows = doc["rows"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        WARNINGS.append(f"{name} is unreadable ({type(e).__name__}) - run `kb --sync` to download it again")
        return None
    if not isinstance(rows, list):
        WARNINGS.append(f"{name} is unreadable (rows is not a list) - run `kb --sync` to download it again")
        return None
    good = [r for r in rows if isinstance(r, dict) and all(isinstance(r.get(k), str) for k in required)]
    if len(good) != len(rows):
        WARNINGS.append(f"{name}: skipped {len(rows) - len(good)} malformed rows - run `kb --sync`")
    doc["rows"] = good
    return doc


def load_apple_docs() -> list[Binding]:
    before = len(WARNINGS)
    doc = read_doc(APPLE_JSON, ("section", "combo", "desc"))
    if doc is None:
        if len(WARNINGS) == before:  # missing, not corrupt
            WARNINGS.append(
                "apple docs unavailable - run `kb --sync-apple` when online to download Apple's shortcut list"
            )
        return []
    meta = doc.get("meta", {})
    out: list[Binding] = []
    for row in doc.get("rows", []):
        sec = row["section"].lower()
        scope = next((v for k, v in APPLE_SCOPES.items() if sec.startswith(k)), None)
        if scope is None:
            continue
        combos, desc = apple_split_alternatives(row["combo"], row["desc"])
        for c in combos:
            n = apple_combo(c)
            if n is None:
                continue
            keys = (
                pretty(n)
                if re.fullmatch(r"(?:[a-z]+\+)*(?:.|[a-z]+\d*|f\d+)", n) and "-" not in n.split("+")[-1]
                else "".join(MOD_SYM.get(m, m) for m in n.split("+")[:-1]) + n.split("+")[-1].replace("-", " ").title()
            )
            out.append(
                Binding(
                    "macos",
                    keys,
                    desc[:220] or row["combo"],
                    n,
                    scope,
                    c,
                    f"{meta.get('source', APPLE_URL)} (fetched {meta.get('fetched_at', '?')[:10]})",
                    f"apple support {row['section']}",
                    f"Apple: {row['section']}",
                )
            )
    return out


_UNI = {
    0xF700: "up",
    0xF701: "down",
    0xF702: "left",
    0xF703: "right",
    0xF728: "delete",
    0xF729: "home",
    0xF72B: "end",
    0xF72C: "pageup",
    0xF72D: "pagedown",
    0x7F: "backspace",
    9: "tab",
    10: "return",
    13: "return",
    27: "escape",
    32: "space",
}
_UNI.update({0xF704 + i: f"f{i + 1}" for i in range(12)})


def _kb_key(spec):
    """'^V' / '~\uf702' / '$@\uf700' (AppKit key-binding notation) -> norm combo."""
    mods, i = [], 0
    while i < len(spec) - 1 and spec[i] in "^~$@#":
        mods.append({"^": "ctrl", "~": "alt", "$": "shift", "@": "cmd", "#": "numpad"}[spec[i]])
        i += 1
    ch = spec[i:]
    key = _UNI.get(ord(ch[0]), ch.lower()) if len(ch) == 1 else ch
    if len(ch) == 1 and ch.isupper() and "shift" not in mods:
        mods.append("shift")
    return norm_combo([m for m in mods if m != "numpad"], key)


def _safe_kb_key(spec):
    try:
        return _kb_key(spec)
    except (ValueError, IndexError, TypeError):
        return None


def _humanize_selector(sel):
    base = sel.rstrip(":")
    words = re.sub(r"([A-Z])", r" \1", base).lower().split()
    return " ".join(words).capitalize()


def load_standard_key_binding(path=None):
    """Cocoa text-editing bindings straight from Apple's AppKit resource file."""
    p = Path(path) if path else STANDARD_KEY_BINDING
    out: list[Binding] = []
    if not p.exists():
        return out
    d = plistlib.loads(p.read_bytes())

    def walk(node, prefix):
        for k, v in node.items():
            n = _safe_kb_key(k)
            if n is None:
                continue
            seq = [*prefix, n]
            if isinstance(v, dict):
                walk(v, seq)
                continue
            sel = v[0] if isinstance(v, list) and v else v
            if not isinstance(sel, str) or sel in ("noop:", "noop"):
                continue
            disp = " then ".join(pretty(x) for x in seq)
            out.append(
                Binding(
                    "macos",
                    disp,
                    _humanize_selector(sel),
                    seq[0] if len(seq) == 1 else "",
                    "text",
                    sel if isinstance(v, str) else str(v),
                    str(p),
                    "cocoa text editing",
                    "Apple: AppKit StandardKeyBinding.dict",
                )
            )

    walk(d, [])
    return out


MENU_MOD = {"@": "cmd", "^": "ctrl", "~": "alt", "$": "shift"}


def live_symbolic_hotkeys():
    """Effective system shortcuts as macOS itself resolves them: (enabled, disabled) dicts, or None."""
    try:
        cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        get, enabled = cg.CGSGetSymbolicHotKeyValue, cg.CGSIsSymbolicHotKeyEnabled
    except (OSError, AttributeError):
        return None
    get.argtypes = [
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_uint16),
        ctypes.POINTER(ctypes.c_uint16),
        ctypes.POINTER(ctypes.c_uint32),
    ]
    get.restype = ctypes.c_int
    enabled.argtypes, enabled.restype = [ctypes.c_int], ctypes.c_bool
    res: dict[int, tuple[int, int]] = {}
    off: dict[int, tuple[int, int]] = {}
    for i in range(1, 260):
        a, k, m = ctypes.c_uint16(), ctypes.c_uint16(), ctypes.c_uint32()
        if get(i, ctypes.byref(a), ctypes.byref(k), ctypes.byref(m)) != 0:
            continue
        if k.value in (0xFFFF,) or (k.value == 0 and a.value == 0):
            continue
        (res if enabled(i) else off)[i] = (k.value, m.value)
    return res, off


def _combo_of(code, flags):
    mods = cocoa_mods(flags)
    if code in range(96, 127):  # arrow/F-keys carry an implicit fn bit
        mods = [m for m in mods if m != "fn"]
    return norm_combo(mods, KEYCODES.get(code, f"keycode{code}"))


def load_macos() -> list[Binding]:
    out: list[Binding] = []
    if not DARWIN:
        return out
    live = live_symbolic_hotkeys()
    docs = load_apple_docs()
    disabled = {}
    src = "CoreGraphics (live)"
    if live is None:  # private API gone/unavailable: fall back to what you customised in the plist
        src = "symbolichotkeys plist (customised only)"
        enabled = {}
        p = HOME / "Library/Preferences/com.apple.symbolichotkeys.plist"
        if p.exists():
            for k, v in plistlib.loads(p.read_bytes()).get("AppleSymbolicHotKeys", {}).items():
                if v.get("enabled") and "value" in v:
                    a = v["value"]["parameters"]
                    enabled[int(k)] = (a[1], a[2])
        WARNINGS.append("macos: live system shortcuts unavailable; showing only the ones you customised")
    else:
        enabled, disabled = live
    # name a live shortcut from (1) a verified label, else (2) Apple's article describing the same combo
    apple_by_norm: dict[str, str] = {}
    for d in docs:
        if d.scope in ("system", "accessibility"):
            apple_by_norm.setdefault(d.norm, d.desc.split(". ")[0].rstrip(".")[:90])
    hidden, seen = 0, set()
    # verified labels first so a duplicate id for the same combo never outranks them
    for k, (code, flags) in sorted(enabled.items(), key=lambda kv: (kv[0] not in SYSTEM_HOTKEY_NAMES, kv[0])):
        n = _combo_of(code, flags)
        if n in seen:
            continue
        seen.add(n)
        name = SYSTEM_HOTKEY_NAMES.get(k) or apple_by_norm.get(n)
        if not name:
            if not SETTINGS.include_unnamed:
                hidden += 1
                continue
            name = f"System shortcut #{k} (not named by Apple)"
        out.append(
            Binding(
                "macos",
                pretty(n),
                name,
                n,
                "global",
                f"symbolichotkey {k}",
                src,
                "system shortcut",
                "effective value as macOS resolves it",
            )
        )
    if hidden:
        WARNINGS_INFO.append(f"{hidden} unnamed system shortcuts hidden (use --all-system to include)")
    off = {_combo_of(c, f) for k, (c, f) in disabled.items() if k in SYSTEM_HOTKEY_NAMES}
    kbd = load_standard_key_binding()
    for b in docs + kbd:
        if b.norm in off:
            b.note = "this system shortcut is disabled on this Mac"
    return out + docs + kbd + load_app_menu_shortcuts() + load_default_key_binding()


def _plist_with_key(path, needle, max_bytes=8_000_000):
    """Parse a plist only if it is small and mentions `needle` (cheap pre-filter); None on any read/parse problem."""
    try:
        if path.stat().st_size > max_bytes:
            return None
        raw = path.read_bytes()
        return plistlib.loads(raw) if needle in raw else None
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None


def _defaults(*args: str) -> bytes | None:
    """Run /usr/bin/defaults with fixed arguments; stdout text, or None if it fails or times out."""
    try:
        r = subprocess.run(  # noqa: S603 - absolute path, argv built from constants, no shell
            [DEFAULTS_BIN, *args], capture_output=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def load_app_menu_shortcuts() -> list[Binding]:
    """Custom menu shortcuts set in System Settings > Keyboard > App Shortcuts (NSUserKeyEquivalents)."""
    out: list[Binding] = []
    prefs = HOME / "Library/Preferences"
    for f in sorted(prefs.glob("*.plist")):
        d = _plist_with_key(f, b"NSUserKeyEquivalents")
        ke = d.get("NSUserKeyEquivalents") if isinstance(d, dict) else None
        if not isinstance(ke, dict):
            continue
        app = "all apps" if f.stem == ".GlobalPreferences" else f.stem
        for title, spec in ke.items():
            mods = [MENU_MOD[c] for c in spec if c in MENU_MOD]
            key = "".join(c for c in spec if c not in MENU_MOD)
            n = norm_combo(mods, key)
            out.append(
                Binding(
                    "macos",
                    pretty(n),
                    f"Menu item “{title}”",
                    n,
                    f"app:{app}",
                    spec,
                    str(f),
                    "app shortcut custom menu",
                    "custom app shortcut",
                )
            )
    return out


def load_default_key_binding() -> list[Binding]:
    p = HOME / "Library/KeyBindings/DefaultKeyBinding.dict"
    out: list[Binding] = []
    if not p.exists():
        return out
    try:
        d = plistlib.loads(p.read_bytes())
    except Exception:
        return out
    for k, v in d.items():
        mods, key = [], k
        for c in k:
            if c in MENU_MOD:
                mods.append(MENU_MOD[c])
        key = "".join(c for c in k if c not in MENU_MOD)
        n = norm_combo(mods, key)
        out.append(Binding("macos", pretty(n), f"Text binding: {v}", n, "text", str(v), str(p), "text editing custom"))
    return out


def load_rectangle() -> list[Binding]:
    out: list[Binding] = []
    if not DARWIN:
        return out
    raw = _defaults("export", "com.knollsoft.Rectangle", "-")
    try:
        d = plistlib.loads(raw) if raw else None
    except (ValueError, plistlib.InvalidFileException):
        d = None
    if not d:
        return out
    for k, v in sorted(d.items()):
        if isinstance(v, dict) and "keyCode" in v and "modifierFlags" in v:
            name = re.sub(r"([A-Z])", r" \1", k).strip().lower().capitalize()
            n = norm_combo(cocoa_mods(v["modifierFlags"]), KEYCODES.get(v["keyCode"], f"keycode{v['keyCode']}"))
            out.append(
                Binding(
                    "rectangle",
                    pretty(n),
                    name,
                    n,
                    "global",
                    k,
                    "defaults com.knollsoft.Rectangle",
                    "window snapping",
                    "custom shortcut; other actions use Rectangle's defaults",
                )
            )
    return out


def load_raycast() -> list[Binding]:
    out: list[Binding] = []
    if not DARWIN:
        return out
    raw = _defaults("read", "com.raycast.macos", "raycastGlobalHotkey")
    if raw is None:
        return out
    spec = raw.decode("utf-8", "replace").strip()
    m = re.fullmatch(r"((?:\w+-)*)(\d+)", spec)
    if m:
        mods = [x for x in m[1].split("-") if x]
        n = norm_combo(mods, KEYCODES.get(int(m[2]), f"keycode{m[2]}"))
        out.append(
            Binding(
                "raycast",
                pretty(n),
                "Open Raycast",
                n,
                "global",
                spec,
                "defaults com.raycast.macos",
                "launcher",
                "other Raycast hotkeys live in its own store and aren't readable",
            )
        )
    return out


# ---------------------------------------------------------------- VS Code

VSC_URL = "https://code.visualstudio.com/docs/reference/default-keybindings"
VSC_HOST = "code.visualstudio.com"
VSC_JSON = "vscode-default-keybindings.json"
VSC_APP = Path("/Applications/Visual Studio Code.app")
VSC_USER = HOME / "Library/Application Support/Code/User"
VSC_ARROWS = {
    "↑": "up",
    "↓": "down",
    "←": "left",
    "→": "right",
    "⏎": "enter",
    "⌫": "backspace",
    "⌦": "delete",
    "⇥": "tab",
    "⎋": "escape",
    "␣": "space",
}
VSC_MODS = {"⌃": "ctrl", "⌥": "alt", "⇧": "shift", "⌘": "cmd"}


def vsc_mac_chord(text: str) -> list[str] | None:
    """VS Code's mac notation ('⇧⌘P', '⌘K ⌘C', '⌃Tab') -> [norm, ...] (one entry per chord step), or None."""
    steps = []
    for tok in text.split():
        mods, rest = [], tok
        while rest and rest[0] in VSC_MODS:
            mods.append(VSC_MODS[rest[0]])
            rest = rest[1:]
        if not rest:
            return None
        key = VSC_ARROWS.get(rest, rest.lower())
        steps.append(norm_combo(mods, key))
    return steps or None


def vsc_key(text: str) -> list[str]:
    """keybindings.json notation ('cmd+k cmd+c', 'ctrl+shift+/') -> [norm, ...]. 'meta/win/super' mean cmd on mac."""
    steps = []
    for tok in text.strip().split():
        parts = tok.split("+") if not tok.endswith("++") else [*tok[:-2].split("+"), "+"]
        key = parts[-1].lower()
        mods = ["cmd" if m.lower() in ("meta", "win", "super") else m for m in parts[:-1]]
        steps.append(norm_combo(mods, key))
    return steps


def vsc_display(steps: Iterable[str]) -> str:
    return " ".join(pretty(x) for x in steps)


class _VscParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.sec, self.h, self.row, self.cell, self.rows, self.inrow = "", None, None, None, [], False

    def handle_starttag(self, t, a):
        a = dict(a)
        if t == "h2":
            self.h = ""
        elif t == "tr":
            self.row = {"cells": [], "osx": []}
        elif t in ("td", "th") and self.row is not None:
            self.cell = ""
        elif t == "span" and self.row is not None and "data-osx" in a:
            self.row["osx"].append(a["data-osx"])
            self.row["cmd"] = a.get("data-commandid", "")

    def handle_endtag(self, t):
        if t == "h2" and self.h is not None:
            self.sec, self.h = self.h.strip(), None
        elif t in ("td", "th") and self.row is not None and self.cell is not None:
            self.row["cells"].append(re.sub(r"\s+", " ", self.cell).strip())
            self.cell = None
        elif t == "tr" and self.row is not None:
            c = self.row["cells"]
            if len(c) >= 3 and c[0] != "Command":
                self.rows.append(
                    {
                        "section": self.sec,
                        "title": c[0],
                        "command": c[2],
                        "osx": self.row["osx"][0] if self.row["osx"] else "",
                    }
                )
            self.row = None

    def handle_data(self, d):
        if self.h is not None:
            self.h += d
        elif self.cell is not None:
            self.cell += d


def parse_vscode_html(html_text):
    p = _VscParser()
    p.feed(html_text)
    return p.rows


def vscode_installed_version():
    try:
        return plistlib.loads((VSC_APP / "Contents/Info.plist").read_bytes()).get("CFBundleShortVersionString", "")
    except Exception:
        return ""


def sync_vscode(url: str = VSC_URL, timeout: float = 20) -> dict:
    raw = _fetch(url, VSC_HOST, timeout)
    rows = parse_vscode_html(raw.decode("utf-8", "replace"))
    return _save_doc(VSC_JSON, url, raw, rows, 100, {"installed_vscode": vscode_installed_version()})


def strip_jsonc(text: str) -> str:
    """Remove // and /* */ comments and trailing commas without touching string contents."""
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
            out.append(c)
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
            continue
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        else:
            out.append(c)
        i += 1
    return re.sub(r",(\s*[\]}])", r"\1", "".join(out))


def _nls(ext_dir, value):
    """Resolve '%key%' titles through the extension's package.nls.json."""
    m = re.fullmatch(r"%(.+)%", value or "")
    if not m:
        return value or ""
    try:
        v = json.loads((ext_dir / "package.nls.json").read_text()).get(m[1], value)
        return v["message"] if isinstance(v, dict) else v
    except Exception:
        return value


def load_vscode() -> list[Binding]:
    out: list[Binding] = []
    before = len(WARNINGS)
    doc = read_doc(VSC_JSON, ("section", "title", "command", "osx"))
    meta = {}
    removed = set()  # (command, chord) pairs the user removed with a "-command" entry
    # 1) your own keybindings (default profile + named profiles)
    user_files = [VSC_USER / "keybindings.json", *sorted((VSC_USER / "profiles").glob("*/keybindings.json"))]
    for f in user_files:
        if not f.exists() or f.stat().st_size > 1_000_000:
            continue
        label = "your keybindings.json" if f.parent == VSC_USER else f"profile {f.parent.name}"
        try:
            entries = json.loads(strip_jsonc(f.read_text()))
        except ValueError as e:
            WARNINGS.append(f"vscode: cannot parse {f}: {e}")
            continue
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict) or "key" not in entry or "command" not in entry:
                continue
            steps = vsc_key(str(entry["key"]))
            cmd = str(entry["command"])
            if cmd.startswith("-"):
                removed.add((cmd[1:], tuple(steps)))
                continue
            when = f"  when: {entry['when']}" if entry.get("when") else ""
            out.append(
                Binding(
                    "vscode",
                    vsc_display(steps),
                    f"{cmd}{when}",
                    steps[0] if len(steps) == 1 else "",
                    "vscode",
                    cmd,
                    f"{f}",
                    "vscode user custom",
                    f"set in {label}",
                )
            )
    # 2) official defaults (synced from Microsoft's reference page)
    if doc is not None:
        d = doc
        meta = d.get("meta", {})
        for r in d.get("rows", []):
            if not r.get("osx"):
                continue
            chord = vsc_mac_chord(r["osx"])
            if not chord:
                continue
            b = Binding(
                "vscode",
                vsc_display(chord),
                r["title"],
                chord[0] if len(chord) == 1 else "",
                "vscode",
                r["command"],
                f"{meta.get('source', VSC_URL)} (fetched {meta.get('fetched_at', '?')[:10]})",
                f"vscode default {r['section']}",
                f"VS Code default: {r['section']}",
            )
            if (r["command"], tuple(chord)) in removed:
                b.note = "removed by your keybindings.json"
            out.append(b)
    elif len(WARNINGS) == before:  # missing, not corrupt
        WARNINGS.append("vscode defaults unavailable - run `kb --sync-vscode` when online to download them")
    return out


def _extension_bindings(pkg: dict, source: str, ext_dir: Path | None) -> list[Binding]:
    """Keybindings an extension contributes. `ext_dir` (when known) lets %nls% titles resolve from disk."""

    def text(value: object) -> str:
        if ext_dir is not None:
            return str(_nls(ext_dir, value))
        return "" if str(value or "").startswith("%") else str(value or "")

    contributes = pkg.get("contributes") or {}
    kbs = contributes.get("keybindings") or []
    if isinstance(kbs, dict):
        kbs = [kbs]
    titles = {
        c.get("command"): text(c.get("title", "")) for c in contributes.get("commands", []) if isinstance(c, dict)
    }
    ext = text(pkg.get("displayName")) or str(pkg.get("name") or "extension")
    out: list[Binding] = []
    for entry in kbs:
        if not isinstance(entry, dict):
            continue
        key = entry.get("mac") or entry.get("key")
        command = entry.get("command")
        if not key or not command:
            continue
        steps = vsc_key(str(key))
        when = f"  when: {entry['when']}" if entry.get("when") else ""
        out.append(
            Binding(
                "vscode",
                vsc_display(steps),
                f"{titles.get(command) or command}{when}",
                steps[0] if len(steps) == 1 else "",
                "vscode",
                str(command),
                source,
                f"vscode extension {ext}",
                f"extension: {ext}",
            )
        )
    return out


def _vscode_manifest_caches() -> list[Path]:
    """VS Code's own per-profile caches of every extension manifest (built-in + user), one file each."""
    found: list[Path] = []
    base = VSC_USER.parent / "CachedProfilesData"
    if not base.is_dir():
        return found
    for profile in sorted(p for p in base.iterdir() if p.is_dir()):
        for kind in ("builtin", "user"):
            en, plain = profile / f"extensions.{kind}.en.cache", profile / f"extensions.{kind}.cache"
            found += [f for f in (en, plain) if f.exists()][:1]  # localized (titles resolved) wins
    return found


def load_vscode_extensions() -> list[Binding]:
    """Extension keybindings. Reads VS Code's manifest caches (2 files per profile) instead of opening ~140
    package.json files: far fewer reads, and endpoint-security scanners make every extra open expensive."""
    out: list[Binding] = []
    seen: set[tuple[str, str, str]] = set()
    caches = _vscode_manifest_caches()
    for cache in caches:
        try:
            if cache.stat().st_size > 30_000_000:
                continue
            result = json.loads(cache.read_text()).get("result", [])
        except (OSError, ValueError):
            WARNINGS.append(f"vscode: cannot read extension cache {cache}")
            continue
        for item in result:
            if not isinstance(item, dict) or not isinstance(item.get("manifest"), dict):
                continue
            for b in _extension_bindings(item["manifest"], str(cache), None):
                ident = (b.tags, b.raw, b.keys + b.desc)
                if ident not in seen:
                    seen.add(ident)
                    out.append(b)
    if caches:
        return out
    WARNINGS_INFO.append("vscode: extension cache not found; reading each extension's package.json (slower)")
    for root in (VSC_APP / "Contents/Resources/app/extensions", HOME / ".vscode/extensions"):
        if not root.is_dir():
            continue
        for pj in sorted(root.glob("*/package.json")):
            try:
                if pj.stat().st_size > 2_000_000:
                    continue
                pkg = json.loads(pj.read_text())
            except (OSError, ValueError):
                continue
            out += _extension_bindings(pkg, str(pj), pj.parent)
    return out


def load_zsh() -> list[Binding]:
    out: list[Binding] = []
    for p in [HOME / ".dotfiles/zsh/.zshrc", HOME / ".zshrc"]:
        if not p.exists():
            continue
        for i, line in enumerate(read_lines(p), 1):
            m = re.match(r"\s*bindkey\s+(?:-\w\s+)?'([^']+)'\s+(\S+)", line)
            if m:
                k = m[1].replace("^x", "Ctrl-X ").replace("^X", "Ctrl-X ").replace("^e", "e")
                out.append(
                    Binding(
                        "zsh",
                        k.replace("Ctrl-X e", "Ctrl-X e").replace("^", "Ctrl-"),
                        m[2].replace("-", " ").capitalize() + " (zle widget)",
                        "",
                        "shell",
                        line.strip(),
                        f"{p}:{i}",
                    )
                )
        break
    return out


LOADERS = [
    load_aerospace,
    load_skhd,
    load_tmux,
    load_herdr,
    load_ghostty,
    load_rectangle,
    load_macos,
    load_raycast,
    load_vscode,
    load_vscode_extensions,
    load_zsh,
]
# loaders whose tool name differs from their function name
LOADER_TOOL = {"load_vscode_extensions": "vscode"}


def loader_tool(fn) -> str:
    return LOADER_TOOL.get(fn.__name__, fn.__name__[5:])


def known_tools() -> list[str]:
    return sorted({loader_tool(fn) for fn in LOADERS})


def load_all(debug: bool = False, tools: Iterable[str] | None = None) -> list[Binding]:
    """Run the needed sources concurrently, each bounded by LOADER_BUDGET, so one stalled read can never hang kb.

    `tools` limits which sources are read at all (e.g. `-t tmux` never touches VS Code or macOS)."""
    wanted = {t.lower() for t in tools} if tools else None
    loaders = [fn for fn in LOADERS if wanted is None or loader_tool(fn) in wanted]
    results, timings = {}, {}

    def work(fn):
        t0 = time.monotonic()
        try:
            results[fn.__name__] = fn()
        except Exception as e:  # never let one broken config hide the rest
            WARNINGS.append(f"{fn.__name__[5:]}: {type(e).__name__}: {e}")
        timings[fn.__name__] = time.monotonic() - t0

    threads = [(fn, threading.Thread(target=work, args=(fn,), daemon=True)) for fn in loaders]
    t_start = time.monotonic()
    for _, t in threads:
        t.start()
    noticed = False
    for fn, t in threads:
        while t.is_alive():
            left = LOADER_BUDGET - (time.monotonic() - t_start)
            if left <= 0:
                break
            t.join(min(left, 0.25))
            if not noticed and time.monotonic() - t_start > 2 and sys.stderr.isatty():
                noticed = True
                slow = [f.__name__[5:] for f, th in threads if th.is_alive()]
                print(f"kb: still reading {', '.join(slow)}… (gives up after {LOADER_BUDGET:g}s)", file=sys.stderr)
        if t.is_alive():
            STUCK.append(fn.__name__)
            WARNINGS.append(
                f"{fn.__name__[5:]}: no answer after {LOADER_BUDGET:g}s - skipped "
                f"(slow disk, security scanner or a stalled system call; KB_LOADER_TIMEOUT=60 waits longer)"
            )
    out: list[Binding] = []
    for fn in loaders:  # stable order regardless of which thread finished first
        out += results.get(fn.__name__, [])
    LAST_TIMINGS.clear()
    LAST_TIMINGS.update(timings)
    if debug:
        for name, sec in sorted(timings.items(), key=lambda kv: -kv[1]):
            print(f"kb: {name[5:]:<10} {sec * 1000:7.0f} ms", file=sys.stderr)
    for w in WARNINGS:
        print(f"kb: warning: {sanitize(w)}", file=sys.stderr)
    return out


# ---------------------------------------------------------------- query

MOD_WORDS = {
    "ctrl": "ctrl control ⌃",
    "alt": "alt opt option ⌥",
    "shift": "shift ⇧",
    "cmd": "cmd command super ⌘",
    "fn": "fn globe",
}
_DISPLAY_MODS = (("⌃", "ctrl"), ("⌥", "alt"), ("⇧", "shift"), ("⌘", "cmd"))


def mods_of(b: Binding) -> set[str]:
    """Modifier keys a binding uses. Works for single combos, chords (⌘K ⌘C) and tmux-style display text."""
    if b.norm:
        return set(b.norm.split("+")[:-1]) & set(MOD_WORDS)
    found = {name for sym, name in _DISPLAY_MODS if sym in b.keys}
    low = b.keys.lower()
    for word, name in (("ctrl-", "ctrl"), ("alt-", "alt"), ("shift-", "shift"), ("cmd-", "cmd"), ("fn ", "fn")):
        if word in low:
            found.add(name)
    return found


def keys_text(b: Binding) -> str:
    """Everything that describes the key combo itself - never the description."""
    aliases = " ".join(MOD_WORDS[m] for m in sorted(mods_of(b)))
    return f"{b.keys} {b.norm} {aliases}".lower()


def name_text(b: Binding) -> str:
    """What the binding does: description plus the underlying command."""
    return f"{b.desc} {b.raw}".lower()


def haystack(b: Binding) -> str:
    return f"{b.tool} {keys_text(b)} {name_text(b)} {b.scope} {b.tags}".lower()


SEARCH_TEXT = {"all": haystack, "keys": keys_text, "name": name_text}


def parse_mods(spec: str) -> list[str]:
    """'command', 'opt,shift', '⌘⇧' -> canonical modifier names; raises ValueError for unknown words."""
    names, spec = [], spec.strip()
    for sym, name in _DISPLAY_MODS:
        if sym in spec:
            names.append(name)
            spec = spec.replace(sym, " ")
    for tok in re.split(r"[,+\s-]+", spec):
        if not tok:
            continue
        m = MOD_ALIAS.get(tok.lower())
        if m not in MOD_WORDS:
            raise ValueError(f"unknown modifier {tok!r} (use ctrl, alt/option, shift, cmd/command, fn)")
        names.append(m)
    return names


def search(items, words, tools=None, key=None, mods=None, by="all"):
    """Filter bindings. `by` chooses what free-text words match: keys, name, or all fields."""
    res = items
    if tools:
        want = {t.lower() for t in tools}
        res = [b for b in res if b.tool in want]
    if key:
        n = parse_combo(key)
        *km, kk = n.split("+")
        disp = f"ctrl-{kk}" if km == ["ctrl"] else kk if not km else None

        def hit(b):
            if b.norm:
                return b.norm == n
            kl = b.keys.lower()
            return disp is not None and (kl == disp or kl.endswith(" " + disp))

        res = [b for b in res if hit(b)]
    if mods:
        res = [b for b in res if set(mods) <= mods_of(b)]
    text = SEARCH_TEXT[by]
    for word in words:
        needle = word.lower()
        res = [b for b in res if needle in text(b)]
    return res


GLOBAL_TOOLS = {"aerospace", "skhd", "rectangle", "raycast", "macos"}
APP_SCOPES = {"app", "text", "vscode"}


def conflicts(items: list[Binding]) -> dict[tuple[str, str], list[Binding]]:
    """Two live global bindings on one combo. Returns {(scope, combo): [Binding, ...]}."""
    groups: dict[tuple[str, str], list[Binding]] = {}
    for b in items:
        if b.norm and (b.scope == "global" or b.scope.startswith("mode:")):
            groups.setdefault((b.scope, b.norm), []).append(b)
    return {k: v for k, v in groups.items() if len(v) > 1}


def shadows(items: list[Binding]) -> list[tuple[Binding, list[Binding]]]:
    """A global hotkey you added that swallows a standard in-app macOS shortcut."""
    std: dict[str, list[Binding]] = {}
    for b in items:
        if b.tool == "macos" and b.scope in APP_SCOPES and b.norm:
            std.setdefault(b.norm, []).append(b)

    def live(lst):
        return [x for x in lst if "disabled" not in x.note]

    return [
        (g, live(std[g.norm]))
        for g in items
        if g.tool in GLOBAL_TOOLS - {"macos"} and g.scope == "global" and live(std.get(g.norm, []))
    ]


# ---------------------------------------------------------------- output


def C(code: str, s: str) -> str:  # noqa: N802 - tiny colour helper, kept short on purpose
    return f"\033[{code}m{s}\033[0m" if SETTINGS.color else s


TOOL_COLOR = {
    "aerospace": "38;5;111",
    "skhd": "38;5;180",
    "tmux": "38;5;114",
    "herdr": "38;5;176",
    "ghostty": "38;5;215",
    "rectangle": "38;5;80",
    "macos": "38;5;252",
    "raycast": "38;5;203",
    "zsh": "38;5;222",
    "vscode": "38;5;75",
}


def row(b: Binding, kw: int | None = None, tw: int | None = None) -> str:
    t = C(TOOL_COLOR.get(b.tool, "37"), f"{b.tool:<{tw or 9}}")
    k = C("1", f"{b.keys:<{kw or 14}}")
    ctx = (
        C("2", f" [{b.scope}]") if b.scope not in ("global", "prefix", b.tool) and f"[{b.scope}]" not in b.keys else ""
    )
    warn = C("33", f"  ⚠ {b.note}") if b.note and ("overridden" in b.note or "disabled" in b.note) else ""
    d = b.desc if len(b.desc) <= 110 else b.desc[:107].rstrip() + "…"
    return f"{t}  {k}  {d}{ctx}{warn}"


def print_table(items: list[Binding]) -> None:
    if not items:
        print("No bindings match.", file=sys.stderr)
        return
    kw = min(max(len(b.keys) for b in items), 28)
    for b in items:
        print(row(b, kw))
    print(C("2", f"{len(items)} binding{'s' if len(items) != 1 else ''}"), file=sys.stderr)


def detail(b: Binding, items: list[Binding]) -> str:
    lines = [f"{b.tool}  ·  {b.keys}", "", b.desc, "", f"scope   {b.scope}", f"command {b.raw}", f"source  {b.src}"]
    if b.note:
        lines.append(f"note    {b.note}")
    same = [x for x in items if x.norm and x.norm == b.norm and x is not b and x.scope == b.scope]
    if same:
        lines += ["", "also bound by:"] + [f"  {x.tool}: {x.desc}" for x in same]
    return "\n".join(lines)


def report_conflicts(items: list[Binding]) -> int:
    """Print findings; return number of cross-tool conflicts (the ones worth failing a check on)."""
    cs, sh = conflicts(items), shadows(items)
    hard = 0
    for (scope, norm), bs in sorted(cs.items()):
        cross = len({b.tool for b in bs}) > 1
        hard += cross
        print(f"{C('31', 'CONFLICT') if cross else C('33', 'duplicate')}  {C('1', pretty(norm))}  ({scope})")
        for b in bs:
            print(f"    {C(TOOL_COLOR.get(b.tool, '37'), f'{b.tool:<10}')} {b.desc}   {C('2', b.src)}")
    if sh:
        print(C("2", "shadow = a global tool captures this key before the app sees it (informational, not a failure)"))
    for g, std in sh:
        print(f"{C('33', 'shadow')}  {C('1', g.keys)}  {g.tool}: {g.desc}")
        for x in std:
            print(f"    hides in-app macOS shortcut: {x.desc[:100]}  {C('2', '[' + x.scope + ']')}")
    if not cs and not sh:
        print("No conflicts found.")
    else:
        conflict_word = "conflict" if hard == 1 else "conflicts"
        shadow_word = "shadow" if len(sh) == 1 else "shadows"
        print(C("2", f"\n{hard} {conflict_word}, {len(sh)} {shadow_word} (informational)"))
    return hard


def _meta_line(meta, what):
    return f"{meta.get('rows', '?')} {what} from {meta.get('source')} fetched {meta.get('fetched_at', '?')[:10]}"


def doctor(items: list[Binding]) -> int:
    """Which sources exist, how many bindings each produced, and what failed to parse."""
    srcs = {
        "aerospace": CFG / "aerospace/aerospace.toml",
        "skhd": CFG / "skhd/skhdrc",
        "tmux": CFG / "tmux/tmux.conf",
        "herdr": CFG / "herdr/config.toml",
        "ghostty": CFG / "ghostty/config",
        "zsh": HOME / ".zshrc",
    }
    counts: dict[str, int] = {}
    for b in items:
        counts[b.tool] = counts.get(b.tool, 0) + 1
    print(f"kb {VERSION}  python {sys.version.split()[0]}  platform {sys.platform}\n")
    for t in sorted(set(srcs) | set(counts)):
        p = srcs.get(t)
        state = "n/a" if p is None else ("found  " if p.exists() else "missing")
        print(f"  {t:<10} {state}  {counts.get(t, 0):>3} bindings  {C('2', str(p) if p else '')}")
    for w in WARNINGS:
        print(f"  {C('31', 'warning')}  {sanitize(w)}")
    slow = {k[5:]: v for k, v in LAST_TIMINGS.items() if v > 1.0}
    for name, sec in sorted(slow.items(), key=lambda kv: -kv[1]):
        print(f"  {C('33', 'slow')}     {name} took {sec:.1f}s")
    ap_meta = DATA_DIR / APPLE_JSON
    if ap_meta.exists():
        m = json.loads(ap_meta.read_text()).get("meta", {})
        print(f"  {C('2', 'apple')}    {_meta_line(m, 'rows')}")
    vm = DATA_DIR / VSC_JSON
    if vm.exists():
        m = json.loads(vm.read_text()).get("meta", {})
        inst = vscode_installed_version()
        print(f"  {C('2', 'vscode')}   {_meta_line(m, 'default rows')}  (installed VS Code {inst or 'not found'})")
    for w in WARNINGS_INFO:
        print(f"  {C('2', 'note')}     {sanitize(w)}")
    print("\nread-only: kb never writes to your configs.")
    return 1 if WARNINGS else 0


def preview_command(snap: Path) -> str:
    script = Path(sys.argv[0]).resolve()
    return " ".join(
        [shlex.quote(sys.executable), shlex.quote(str(script)), "--detail", "{1}", "--snapshot", shlex.quote(str(snap))]
    )


def interactive(items: list[Binding]) -> None:
    fzf = shutil.which("fzf")
    if not fzf:
        print_table(items)
        return
    SETTINGS.color = True
    kw = min(max(len(b.keys) for b in items), 28)
    lines = [f"{i}\t{row(b, kw)}" for i, b in enumerate(items)]
    # The preview must not re-parse every config on each cursor move: hand it a private snapshot instead.
    with tempfile.TemporaryDirectory(prefix="kb-") as d:
        snap = Path(d) / "snapshot.json"
        fd = os.open(snap, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump([asdict(b) for b in items], f)
        cmd = [
            fzf,
            "--ansi",
            "--delimiter",
            "\t",
            "--with-nth",
            "2..",
            "--layout",
            "reverse",
            "--prompt",
            "keybinding> ",
            "--header",
            "type to search · e.g. alt t, tmux split · ctrl-/ preview",
            "--preview",
            preview_command(snap),
            "--preview-window",
            "right:45%:wrap",
            "--bind",
            "ctrl-/:toggle-preview",
        ]
        subprocess.run(cmd, input="\n".join(lines), text=True, check=False)  # noqa: S603 - fzf argv built here, no shell
    SETTINGS.color = want_color(sys.stdout)


def show_detail(idx, snapshot):
    p = Path(snapshot).resolve()
    st = p.stat()
    if st.st_uid != os.getuid() or st.st_mode & 0o077:  # refuse a snapshot we didn't write ourselves
        sys.exit("kb: refusing to read snapshot with unsafe ownership/permissions")
    items = [Binding(**d) for d in json.loads(p.read_text())]
    if 0 <= idx < len(items):
        print(detail(items[idx], items))


def _tool_arg(value):
    tool = value.strip().lower()
    if tool not in known_tools():
        close = difflib.get_close_matches(tool, known_tools(), n=1)
        hint = f" - did you mean {close[0]!r}?" if close else ""
        raise argparse.ArgumentTypeError(f"unknown tool {value!r}{hint} (choose from: {', '.join(known_tools())})")
    return tool


def _mod_arg(value):
    try:
        return parse_mods(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="kb",
        description="Browse and search all keybindings.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("\n", 2)[2]
        + "\nexit codes: 0 ok · 1 no match / conflicts found / doctor warnings · 2 usage error",
    )
    ap.add_argument("query", nargs="*", help="words that must all match")
    ap.add_argument(
        "-t",
        "--tool",
        action="append",
        type=_tool_arg,
        metavar="TOOL",
        help="only this tool, and only read its sources (repeatable; see --tools)",
    )
    ap.add_argument("-k", "--key", help="look up a key combo, e.g. opt+t, 'alt-shift-h', ⌘⇧p")
    ap.add_argument(
        "-m",
        "--mod",
        action="append",
        type=_mod_arg,
        metavar="MOD",
        help="only combos that use this modifier: ctrl, alt/option, shift, cmd/command, fn "
        "(repeatable or comma-separated; all must be present)",
    )
    ap.add_argument(
        "--by",
        choices=["all", "keys", "name"],
        default="all",
        help="what the search words match: the key combo itself, what it does (name), or everything",
    )
    ap.add_argument(
        "-c", "--conflicts", action="store_true", help="show conflicts and shadowed shortcuts (exit 1 on conflict)"
    )
    ap.add_argument("--tools", action="store_true", help="list sources")
    ap.add_argument("--doctor", action="store_true", help="check sources and parse warnings")
    ap.add_argument("--json", action="store_true", help=f"JSON output (schema_version {SCHEMA_VERSION})")
    ap.add_argument(
        "--sync-apple",
        action="store_true",
        help="download Apple's 'Mac keyboard shortcuts' article (support.apple.com) into the local data file",
    )
    ap.add_argument(
        "--sync-vscode",
        action="store_true",
        help="download VS Code's default keyboard shortcuts (code.visualstudio.com)",
    )
    ap.add_argument("--sync", action="store_true", help="refresh every downloaded source (Apple + VS Code)")
    ap.add_argument("--debug", action="store_true", help="print how long each source took (to stderr)")
    ap.add_argument("--all-system", action="store_true", help="also list system shortcuts Apple doesn't name")
    ap.add_argument("--no-fzf", action="store_true", help="never launch the interactive browser")
    ap.add_argument("--version", action="version", version=f"kb {VERSION}")
    ap.add_argument("--detail", type=int, help=argparse.SUPPRESS)
    ap.add_argument("--snapshot", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)

    if a.detail is not None:
        if not a.snapshot:
            ap.error("--detail needs --snapshot")
        show_detail(a.detail, a.snapshot)
        return 0
    if a.key is not None and not a.key.strip():
        ap.error("--key needs a key combo, e.g. -k opt+t")
    if a.json and (a.conflicts or a.tools or a.doctor):
        ap.error("--json is only supported for searches (not with --conflicts, --tools or --doctor)")
    if a.sync_apple or a.sync_vscode or a.sync:
        rc = 0
        for flag, fn, name in (
            (a.sync_apple or a.sync, sync_apple, APPLE_JSON),
            (a.sync_vscode or a.sync, sync_vscode, VSC_JSON),
        ):
            if not flag:
                continue
            try:
                m = fn()["meta"]
                print(f"fetched {m['rows']} rows from {m['source']}")
                print(f"  sha256 {m['sha256'][:16]}…  saved to {DATA_DIR / name}")
            except Exception as e:
                print(f"kb: sync of {name} failed: {e}", file=sys.stderr)
                rc = 1
        return rc
    SETTINGS.include_unnamed = a.all_system
    # only a plain search can skip sources; --doctor/--tools/-c compare or count everything
    scoped = None if (a.doctor or a.tools or a.conflicts) else a.tool
    items = load_all(debug=a.debug, tools=scoped)
    if a.doctor:
        return doctor(items)
    if a.tools:
        counts: dict[str, int] = {}
        for b in items:
            counts[b.tool] = counts.get(b.tool, 0) + 1
        for t, n in sorted(counts.items()):
            print(f"{C(TOOL_COLOR.get(t, '37'), f'{t:<10}')} {n:>3} bindings")
        return 0
    if a.conflicts:
        return 1 if report_conflicts(items) else 0
    res = search(items, a.query, a.tool, a.key, [m for grp in (a.mod or []) for m in grp], a.by)
    if a.json:
        print(
            json.dumps(
                {"schema_version": SCHEMA_VERSION, "bindings": [asdict(b) for b in res]}, indent=2, ensure_ascii=False
            )
        )
        return 0 if res else 1
    tty = sys.stdout.isatty() and sys.stdin.isatty() and not a.no_fzf
    filtered = bool(a.query or a.key or a.tool or a.mod)
    if tty and res and (not filtered or len(res) > 25):
        interactive(res)
    else:
        print_table(res)
    return 0 if res else 1


def run() -> None:
    """Console-script entry point (uv/pip install) and `python -m kb`."""
    code = 0
    try:
        code = main()
    except BrokenPipeError:
        code = 0
        with contextlib.suppress(OSError, ValueError):
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())  # silence the flush at exit
    except KeyboardInterrupt:
        code = 130
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        if isinstance(e.code, str):
            print(e.code, file=sys.stderr)
    if STUCK:  # abandoned threads may sit in an uninterruptible syscall; don't wait for them at shutdown
        for stream in (sys.stdout, sys.stderr):
            with contextlib.suppress(OSError, ValueError):
                stream.flush()
        os._exit(code)
    sys.exit(code)


if __name__ == "__main__":
    run()
