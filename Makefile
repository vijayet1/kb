PREFIX ?= $(HOME)/.local
DATA   ?= $(or $(XDG_DATA_HOME),$(HOME)/.local/share)/kb

.PHONY: test install uninstall sync doctor
test:
	PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v

install:            ## symlink the tool, seed the downloaded data if absent
	mkdir -p $(PREFIX)/bin $(DATA)
	ln -sfn $(CURDIR)/bin/kb $(PREFIX)/bin/kb
	for f in data/*.json; do [ -e $(DATA)/$$(basename $$f) ] || cp $$f $(DATA)/; done

uninstall:
	rm -f $(PREFIX)/bin/kb

sync:               ## refresh Apple + VS Code data in the repo copy too
	bin/kb --sync && cp $(DATA)/*.json data/

doctor:
	bin/kb --doctor
