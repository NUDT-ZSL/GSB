PYTHON ?= .venv/bin/python
export PYTHONPATH := src

.PHONY: sample test acceptance serve

sample:
	$(PYTHON) scripts/make_sample.py

test:
	$(PYTHON) -m unittest discover -v

acceptance:
	$(PYTHON) scripts/acceptance_demo.py

serve:
	$(PYTHON) run.py --host 127.0.0.1 --port 8765
