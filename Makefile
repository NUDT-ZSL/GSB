.PHONY: generate test demo

generate:
	python3 generate_samples.py --output samples/default --seed 20261126

test:
	./run_tests.sh

demo:
	./demo.sh
