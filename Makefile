PYTHON ?= python3

.PHONY: generate test run repl verify

generate:
	$(PYTHON) generate_data.py --out data --force

test:
	$(PYTHON) -m unittest discover -s tests -v

run:
	$(PYTHON) -m ifdata run --data data --state .ifdata-state --out export

repl:
	$(PYTHON) -m ifdata repl --data data --state .ifdata-state --out export

verify: generate test run

