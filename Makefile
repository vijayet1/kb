.PHONY: test install install-release uninstall sync lock doctor

test:               ## run the unit tests in an isolated uv-managed env
	uv run python -m unittest discover -s tests -v

install:            ## install `kb` onto PATH as a uv tool (editable: repo edits apply immediately)
	uv tool install --editable --force .

install-release:    ## install a regular (non-editable) copy
	uv tool install --force .

uninstall:
	uv tool uninstall kb

sync:               ## refresh Apple + VS Code data, then update the snapshot bundled in the package
	uv run kb --sync
	cp "$${XDG_DATA_HOME:-$$HOME/.local/share}"/kb/*.json src/kb/data/

lock:
	uv lock

doctor:
	uv run kb --doctor
