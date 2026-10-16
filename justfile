# Same recipes as the Makefile - use whichever runner you prefer. `just` lists them.
set shell := ["bash", "-euo", "pipefail", "-c"]

# list recipes
default:
    @just --list

# run the unit tests in an isolated uv-managed env
test:
    uv run python -m unittest discover -s tests -v

# install `kb` onto PATH as a uv tool (editable: repo edits apply immediately)
install:
    uv tool install --editable --force .

# install a regular (non-editable) copy, e.g. for a machine you don't develop on
install-release:
    uv tool install --force .

uninstall:
    uv tool uninstall kb

# refresh Apple + VS Code data, then update the snapshot bundled in the package
sync:
    uv run kb --sync
    cp "${XDG_DATA_HOME:-$HOME/.local/share}"/kb/*.json src/kb/data/

# re-resolve uv.lock
lock:
    uv lock

# check sources and parse warnings
doctor:
    uv run kb --doctor
