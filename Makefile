PYTHON ?= .venv/bin/python

.PHONY: test demo concurrent verify

test:
	$(PYTHON) -m unittest discover -v

demo:
	./scripts/demo.sh

concurrent:
	./scripts/concurrent.sh

verify: test demo concurrent

