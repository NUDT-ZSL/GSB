.PHONY: generate demo test workbench validate

generate:
	python3 -m dimdiag generate

demo:
	python3 -m dimdiag demo

test:
	python3 -m unittest discover -v

validate:
	bash scripts/run_validation.sh

workbench:
	python3 -m dimdiag workbench --open

