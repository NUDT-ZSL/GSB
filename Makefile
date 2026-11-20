PYTHON := .venv/bin/python

.PHONY: setup samples test acceptance verify

setup:
	uv venv --python 3.11 .venv
	uv pip install --python $(PYTHON) -e . PyYAML pytest

samples:
	$(PYTHON) scripts/build_samples.py

test:
	$(PYTHON) -m pytest -q

acceptance:
	$(PYTHON) scripts/run_acceptance.py

verify:
	$(PYTHON) -m deploy_convergence verify --state artifacts/state.sqlite
