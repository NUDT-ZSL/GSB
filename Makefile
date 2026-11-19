.PHONY: prepare generate demo test compile check

prepare:
	python3 -m numeric_workbench prepare --root .

generate:
	python3 -m numeric_workbench generate --root . --config configs/default.json

demo:
	python3 -m numeric_workbench demo --root . --config configs/default.json

test:
	python3 -m unittest discover -s tests -v

compile:
	python3 -m compileall -q numeric_workbench tests

check: compile test
