PYTHON ?= .venv/bin/python

.PHONY: check test demo samples

check: test demo

test:
	$(PYTHON) -m unittest discover -v

demo:
	./scripts/demo.sh

samples:
	$(PYTHON) scripts/build_samples.py

