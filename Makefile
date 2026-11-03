.PHONY: check lint test test-live test-gcp demo record author infra-up infra-down

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

infra-up:
	infra/gcp.sh up

infra-down:
	infra/gcp.sh down

demo:
	@echo "not yet: D8"

record:
	if [ -z "$(FAULT)" ]; then echo "usage: make record FAULT=<F>" >&2; exit 2; fi
	python scripts/record.py --fault $(FAULT)

author:
	python -m fleetsre.authoring --brief fixtures/briefs/alert-ingestion.yaml --runs 1
