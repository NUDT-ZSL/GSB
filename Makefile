.PHONY: test selfcheck run

test:
	.venv/bin/python -m unittest discover -s tests -v

selfcheck:
	.venv/bin/python -m layout_order self-check

run:
	.venv/bin/python -m layout_order generate
	.venv/bin/python -m layout_order run

