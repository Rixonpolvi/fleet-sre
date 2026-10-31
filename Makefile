.PHONY: check lint test test-live test-gcp demo record

check: lint
	python scripts/check_trace.py --doc docs/deliverables.md --tests tests/acceptance
	python -m pytest -m "not live and not gcp"

lint:
	ruff check .
	mypy fleetsre scripts
	lint-imports

test:
	python -m pytest -m "not live and not gcp"

test-live:
	python -m pytest -m live

test-gcp:
	python -m pytest -m gcp

demo:
	@echo "not yet: D8"

record:
	if [ -z "$(FAULT)" ]; then echo "usage: make record FAULT=<F>" >&2; exit 2; fi
	python scripts/record.py --fault $(FAULT)
