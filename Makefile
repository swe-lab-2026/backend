# Override on systems where the interpreter is named differently, e.g.
# `make PYTHON=python3 test`.
PYTHON ?= python

.PHONY: install infra run migrate test lint typecheck check

install:
	$(PYTHON) -m pip install -e ".[dev]"

infra:
	docker compose up -d postgres redis

run:
	$(PYTHON) -m uvicorn app.main:app --reload --port 8000

migrate:
	$(PYTHON) -m app.db.migrate

test:
	$(PYTHON) -m pytest

lint:
	$(PYTHON) -m ruff check app tests

typecheck:
	$(PYTHON) -m mypy

check: lint typecheck test
