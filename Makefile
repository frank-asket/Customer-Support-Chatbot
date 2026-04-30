SHELL := /bin/bash

.PHONY: setup dev test backend-test build lint

setup:
	npm --prefix frontend install
	python3 -m venv backend/.venv
	source backend/.venv/bin/activate && pip install -r backend/requirements.txt

dev:
	source backend/.venv/bin/activate && uvicorn backend.app.main:app --reload --port 8000 & \
	BACK_PID=$$!; \
	trap "kill $$BACK_PID" EXIT; \
	npm run dev:frontend

backend-test:
	source backend/.venv/bin/activate && pytest backend/tests -q

test: backend-test

build:
	npm run build

lint:
	npm run lint
