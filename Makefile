.PHONY: check lint test test-live test-gcp demo

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
