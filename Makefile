.PHONY: test demo verify selfcheck clean

test:
	python3 -m unittest discover -s tests -v

demo:
	python3 -m sampling_ops demo --out artifacts/demo_output.json --tasks-out artifacts/tasks.jsonl --cache-dir artifacts/cache --pretty

verify:
	python3 scripts/verify.py --out artifacts/verification_summary.json --tasks-out artifacts/tasks.jsonl --pretty

selfcheck:
	python3 -m sampling_ops selfcheck --tasks artifacts/tasks.jsonl --out artifacts/selfcheck_output.json --cache-dir artifacts/cache --pretty

clean:
	rm -rf artifacts/cache artifacts/demo_output.json artifacts/run_output.json artifacts/selfcheck_output.json

