.PHONY: samples test verify demo clean

samples:
	python3 -m aqua_edge generate --kind all --out-dir samples

test:
	python3 -m unittest discover -v

verify:
	python3 -m aqua_edge verify samples/pond_network.json
	python3 -m aqua_edge verify samples/boundary_network.json

demo: samples
	./run_demo.sh

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
