.PHONY: install migrate run test lint typecheck compile migration-check quality openapi viewer-contract-schema seed backup

install:
	python -m pip install -e '.[dev]'

migrate:
	alembic upgrade head

run:
	uvicorn fire_viewer.main:app --reload --host 0.0.0.0 --port 8000

test:
	pytest

lint:
	ruff check .
	ruff format --check .

typecheck:
	mypy

compile:
	python -m compileall -q src

migration-check:
	python -m fire_viewer.scripts.check_migrations

quality: lint typecheck test migration-check compile

openapi:
	python -m fire_viewer.scripts.export_openapi

viewer-contract-schema:
	python -m fire_viewer.scripts.export_viewer_manifest_contract

seed:
	python -m fire_viewer.scripts.seed_demo

backup:
	python -m fire_viewer.scripts.backup_sqlite
