.PHONY: generate run verify compare test inject-all recovery clean-artifacts

generate:
	python3 tools/generate_samples.py --output .

run:
	python3 main.py run --config config.json

verify:
	python3 main.py verify --config config.json

compare:
	python3 main.py compare --config config.json --strict

test:
	python3 -m unittest discover -s tests -v

inject-all:
	python3 main.py inject-all --root .

recovery:
	python3 tools/recovery_check.py --config config.json --stage 3

clean-artifacts:
	rm -rf snapshots out

