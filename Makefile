PY ?= .venv/bin/python

.PHONY: sample demo test verify clean

sample:
	$(PY) scripts/make_sample.py

demo:
	$(PY) -m gsb run --sample samples/tilted --db .demo/state.sqlite --work .demo/work --out .demo/out

verify:
	$(PY) -m gsb verify --sample samples/tilted --db .demo/state.sqlite --work .demo/work --out .demo/out

test:
	$(PY) -m unittest discover -s tests -v

clean:
	rm -rf .demo state work out

