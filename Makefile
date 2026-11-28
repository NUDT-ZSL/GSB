.PHONY: demo test clean

demo:
	python3 -m patrol_stats demo

test:
	python3 -m unittest discover -s tests -v

clean:
	rm -rf storage __pycache__ patrol_stats/__pycache__ tests/__pycache__
