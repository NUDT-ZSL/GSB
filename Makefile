PYTHON ?= .venv/bin/python
PORT ?= 8000

.PHONY: run test reconcile recovery

run:
	PORT=$(PORT) $(PYTHON) -m app.main

test:
	$(PYTHON) -m pytest

reconcile:
	$(PYTHON) scripts/validate_consistency.py

recovery:
	$(PYTHON) scripts/recovery_demo.py
