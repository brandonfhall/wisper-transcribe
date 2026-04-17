# wisper-transcribe — developer convenience targets
# Requires Docker and docker compose v2.
#
# Local (no Docker):
#   make setup      — create .venv and install dependencies
#   make test       — run the test suite
#   make tailwind   — rebuild compiled Tailwind CSS
#
# Docker (web UI):
#   make start      — CPU web UI at http://localhost:8080
#   make start-gpu  — GPU web UI at http://localhost:8080
#   make stop       — stop all containers
#   make logs       — follow container logs
#   make build      — (re)build all images
#   make build-cpu  — rebuild CPU image only
#   make build-gpu  — rebuild GPU image only
#   make shell      — open a shell in the CPU container
#   make shell-gpu  — open a shell in the GPU container

.PHONY: start start-gpu stop logs build build-cpu build-gpu shell shell-gpu \
        setup test tailwind clean

# ── Docker targets ────────────────────────────────────────────────────────────

start:
	@echo "Starting wisper web UI (CPU) at http://localhost:8080"
	docker compose up wisper-cpu-web

start-gpu:
	@echo "Starting wisper web UI (GPU) at http://localhost:8080"
	docker compose up wisper-web

stop:
	docker compose down

logs:
	docker compose logs -f

build:
	docker compose build

build-cpu:
	docker compose build wisper-cpu

build-gpu:
	docker compose build wisper

shell:
	docker compose run --rm wisper-cpu bash

shell-gpu:
	docker compose run --rm wisper bash

# ── Local targets ─────────────────────────────────────────────────────────────

setup:
	bash setup.sh

test:
	.venv/bin/pytest tests/ -v

tailwind:
	.venv/bin/python -m pytailwindcss \
	    -i src/wisper_transcribe/static/input.css \
	    -o src/wisper_transcribe/static/tailwind.min.css \
	    --minify

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
	rm -rf .pytest_cache htmlcov .coverage
