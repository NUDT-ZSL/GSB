PYTHON ?= python3
DB ?= var/workbench.db

.PHONY: seed test verify serve clean

seed:
	$(PYTHON) -m modelwb.seed --out samples --force

test:
	$(PYTHON) -m unittest discover -v

verify:
	$(PYTHON) -m modelwb --db $(DB) verify \
		--patch samples/patch_v1_to_v2.json \
		--full samples/full_replacement/classifier-v2.json \
		--input-dir samples

serve:
	MODELWB_DB=$(DB) $(PYTHON) app.py

clean:
	$(PYTHON) -c "import shutil; shutil.rmtree('var', ignore_errors=True)"
