PYTHON ?= $(shell [ -x .venv/bin/python ] && echo .venv/bin/python || echo python3.11)
RUFF ?= $(PYTHON) -m ruff

.PHONY: check lint test

check: lint test

lint:
	$(RUFF) check .

test:
	$(PYTHON) -m unittest discover -s tests -v
