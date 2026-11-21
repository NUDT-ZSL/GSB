PYTHON ?= python3
export PYTHONPATH := $(CURDIR)/src

.PHONY: generate test demo validate

generate:
	$(PYTHON) scripts/generate_data.py --root . --seed 20261122 --samples 240

test:
	$(PYTHON) -m pytest -q

demo: generate
	$(PYTHON) -m linear_path_engine train -c configs/float64-serial.json
	$(PYTHON) -m linear_path_engine train -c configs/float64.json
	$(PYTHON) -m linear_path_engine train -c configs/float32.json
	$(PYTHON) -m linear_path_engine train -c configs/fixed-point.json
	$(PYTHON) -m linear_path_engine train -c configs/float64-l2.json

validate:
	$(PYTHON) -m linear_path_engine validate -c configs/float64.json -r artifacts/run-float64/result.json
	$(PYTHON) -m linear_path_engine validate -c configs/float32.json -r artifacts/run-float32/result.json
	$(PYTHON) -m linear_path_engine validate -c configs/fixed-point.json -r artifacts/run-fixed-point/result.json
	$(PYTHON) -m linear_path_engine validate -c configs/float64-l2.json -r artifacts/run-float64-l2/result.json

