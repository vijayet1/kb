.PHONY: release release-dry lint fmt check test install install-release uninstall sync lock doctor

test:               ## run the unit tests in an isolated uv-managed env
	uv run python -m unittest discover -s tests -v

lint:               ## ruff check + format check + mypy
	uv run ruff check src tests scripts
	uv run ruff format --check src tests scripts
	uv run mypy src scripts

fmt:                ## apply formatter and safe lint fixes
	uv run ruff format src tests scripts
	uv run ruff check --fix src tests scripts

release:            ## cut a release: make release LEVEL=patch|minor|major|X.Y.Z (never pushes)
	@test -n "$(LEVEL)" || { echo "usage: make release LEVEL=patch|minor|major|X.Y.Z"; exit 2; }
	uv run python scripts/release.py $(LEVEL)

release-dry:        ## show what a release would do: make release-dry LEVEL=minor
	@test -n "$(LEVEL)" || { echo "usage: make release-dry LEVEL=patch|minor|major|X.Y.Z"; exit 2; }
	uv run python scripts/release.py $(LEVEL) --dry-run

check: lint test    ## lint + tests

install:            ## install `kb` onto PATH as a uv tool (editable: repo edits apply immediately)
	uv tool install --editable --force .

install-release:    ## install a regular (non-editable) copy
	uv tool install --force .

uninstall:
	uv tool uninstall kb

sync:               ## re-download the Apple + VS Code shortcut data into ~/.local/share/kb
	uv run kb --sync

lock:
	uv lock

doctor:
	uv run kb --doctor
