.PHONY: demo test

demo:
	python3 -m ticket_gate run \
		--project sample_project \
		--db runtime/run.db \
		--out runtime/kernel \
		--reference-out runtime/reference/reference.json \
		--reset

test:
	python3 -m unittest discover -s tests -v
