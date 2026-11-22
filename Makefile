PY := .venv/bin/python

.PHONY: bootstrap generate test demo acceptance

bootstrap:
	uv python install 3.12
	uv venv --python 3.12 .venv
	uv pip install --python $(PY) pip
	$(PY) -m pip install -e '.[test]'

generate:
	$(PY) tools/generate_samples.py

test:
	$(PY) -m pytest -q

demo:
	./scripts/run_demo.sh

acceptance:
	$(PY) tools/run_acceptance.py
