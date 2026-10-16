#!/usr/bin/env python3
"""Cut a release: checks -> bump version -> promote CHANGELOG -> commit -> annotated tag. Never pushes.

    python scripts/release.py patch|minor|major|X.Y.Z [--dry-run] [--branch main] [--no-checks]

The version lives only in pyproject.toml; `uv version` edits it (and uv.lock). Everything is verified before any
file is touched, and files are restored if a later step fails.
"""

from __future__ import annotations

import argparse
import datetime
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
UNRELEASED = "## [Unreleased]"


class ReleaseError(Exception):
    pass


def run(*cmd: str, capture: bool = False) -> str:
    r = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=capture, check=False)
    if r.returncode != 0:
        detail = (r.stderr or r.stdout or "").strip()
        raise ReleaseError(f"`{' '.join(cmd)}` failed ({r.returncode}){': ' + detail if detail else ''}")
    return r.stdout if capture else ""


def current_version() -> str:
    return str(tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"])


def bump_version(current: str, level: str) -> str:
    """'1.6.0' + patch|minor|major -> next version, or validate an explicit X.Y.Z that is newer than current."""
    m = SEMVER.match(current)
    if not m:
        raise ReleaseError(f"current version {current!r} is not X.Y.Z")
    major, minor, patch = (int(x) for x in m.groups())
    if level == "patch":
        return f"{major}.{minor}.{patch + 1}"
    if level == "minor":
        return f"{major}.{minor + 1}.0"
    if level == "major":
        return f"{major + 1}.0.0"
    n = SEMVER.match(level)
    if not n:
        raise ReleaseError(f"{level!r} is not patch, minor, major or X.Y.Z")
    if tuple(int(x) for x in n.groups()) <= (major, minor, patch):
        raise ReleaseError(f"{level} is not newer than the current version {current}")
    return level


def unreleased_body(changelog: str) -> str:
    """Text under '## [Unreleased]' up to the next '## [' heading ('' if missing/empty)."""
    start = changelog.find(UNRELEASED)
    if start < 0:
        return ""
    body = changelog[start + len(UNRELEASED) :]
    nxt = re.search(r"^## \[", body, re.M)
    return (body[: nxt.start()] if nxt else body).strip()


def promote_changelog(changelog: str, version: str, date: str) -> str:
    """Turn the Unreleased section into `## [version] - date` and leave a fresh empty Unreleased on top."""
    if not unreleased_body(changelog):
        raise ReleaseError("CHANGELOG.md has nothing under [Unreleased]; describe the change before releasing")
    return changelog.replace(UNRELEASED, f"{UNRELEASED}\n\n## [{version}] - {date}", 1)


def preflight(branch: str, new: str) -> None:
    if run("git", "status", "--porcelain", capture=True).strip():
        raise ReleaseError("working tree is not clean; commit or stash first")
    current_branch = run("git", "rev-parse", "--abbrev-ref", "HEAD", capture=True).strip()
    if current_branch != branch:
        raise ReleaseError(f"on branch {current_branch!r}, releases are cut from {branch!r} (use --branch to override)")
    if run("git", "tag", "-l", f"v{new}", capture=True).strip():
        raise ReleaseError(f"tag v{new} already exists")


def checks() -> None:
    for cmd in (
        ("uv", "run", "ruff", "check", "src", "tests", "scripts"),
        ("uv", "run", "ruff", "format", "--check", "src", "tests", "scripts"),
        ("uv", "run", "mypy", "src"),
        ("uv", "run", "python", "-m", "unittest", "discover", "-s", "tests"),
    ):
        print(f"  $ {' '.join(cmd)}")
        run(*cmd)


def verify_version(new: str) -> None:
    got = run("uv", "run", "kb", "--version", capture=True).strip()
    if got != f"kb {new}":
        raise ReleaseError(f"`kb --version` printed {got!r}, expected 'kb {new}'")


def apply_release(new: str, promoted: str, notes: str) -> None:
    files = ("pyproject.toml", "uv.lock", "CHANGELOG.md")
    try:
        run("uv", "version", new)
        run("uv", "lock")
        (ROOT / "CHANGELOG.md").write_text(promoted)
        verify_version(new)
        run("git", "add", *files)
        run("git", "commit", "-q", "-m", f"release: v{new}")
        run("git", "tag", "-a", f"v{new}", "-m", f"kb {new}\n\n{notes}")
    except ReleaseError:
        run("git", "checkout", "--", *files)  # leave the tree as we found it
        raise


def release(level: str, *, dry_run: bool, branch: str, run_checks: bool) -> None:
    old = current_version()
    new = bump_version(old, level)
    changelog = (ROOT / "CHANGELOG.md").read_text()
    notes = unreleased_body(changelog)
    preflight(branch, new)
    promoted = promote_changelog(changelog, new, datetime.date.today().isoformat())
    print(f"release {old} -> {new}\n\n--- release notes ---\n{notes}\n---------------------")
    if dry_run:
        print("dry run: nothing changed")
        return
    if run_checks:
        print("running checks")
        checks()
    apply_release(new, promoted, notes)
    print(f"\nreleased v{new}: commit + annotated tag created locally.\n  push with: git push && git push --tags")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("level", help="patch | minor | major | X.Y.Z")
    ap.add_argument("--dry-run", action="store_true", help="show what would happen, change nothing")
    ap.add_argument("--branch", default="main", help="branch releases must be cut from (default: main)")
    ap.add_argument("--no-checks", action="store_true", help="skip lint/type/test run (not recommended)")
    a = ap.parse_args(argv)
    try:
        release(a.level, dry_run=a.dry_run, branch=a.branch, run_checks=not a.no_checks)
    except ReleaseError as e:
        print(f"release: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
