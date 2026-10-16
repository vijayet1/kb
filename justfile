# Same recipes as the Makefile - use whichever runner you prefer. `just` lists them.
set shell := ["bash", "-euo", "pipefail", "-c"]

# list recipes
default:
    @just --list

# run the unit tests in an isolated uv-managed env
test:
    uv run python -m unittest discover -s tests -v

# lint, format check and type-check (what CI would run)
lint:
    uv run ruff check src tests scripts
    uv run ruff format --check src tests scripts
    uv run mypy src scripts

# apply formatter and safe lint fixes
fmt:
    uv run ruff format src tests scripts
    uv run ruff check --fix src tests scripts

# cut a release: level is patch | minor | major | X.Y.Z (checks, bump, changelog, commit, tag; never pushes)
release level:
    uv run python scripts/release.py {{level}}

# show what a release would do without changing anything
release-dry level:
    uv run python scripts/release.py {{level}} --dry-run

# lint + tests
check: lint test

# install `kb` onto PATH as a uv tool (editable: repo edits apply immediately)
install:
    uv tool install --editable --force .

# install a regular (non-editable) copy, e.g. for a machine you don't develop on
install-release:
    uv tool install --force .

uninstall:
    uv tool uninstall kb

# re-download the Apple + VS Code shortcut data into ~/.local/share/kb
sync:
    uv run kb --sync

# re-resolve uv.lock
lock:
    uv lock

# check sources and parse warnings
doctor:
    uv run kb --doctor
