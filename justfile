set shell := ["bash", "-euo", "pipefail", "-c"]

# List recipes.
default:
    @just --list

# Create/refresh the virtualenv from the lockfile.
install:
    uv sync --locked

# Re-resolve and rewrite uv.lock.
lock:
    uv lock

# Format the code.
fmt:
    uv run ruff format src tests

# Lint (includes import sorting and security rules).
lint:
    uv run ruff check src tests

# Check formatting without changing files.
format-check:
    uv run ruff format --check src tests

# Static type checking.
typecheck:
    uv run mypy

# Run the fast test suite.
test:
    uv run pytest

# Run tests with coverage (fails below the configured threshold).
coverage:
    uv run pytest --cov --cov-report=term-missing --cov-report=html

# Everything CI runs.
check: format-check lint typecheck test

# Audit the locked dependencies for known vulnerabilities.
audit:
    uv run pip-audit

# Run the *arr configuration self-check.
preflight:
    uv run seedpath preflight

# Print the effective configuration (secrets redacted).
config:
    uv run seedpath config

# Start the proxy locally on :8080.
run:
    uv run seedpath serve

# Build sdist and wheel.
build:
    uv build

# Build the container image.
docker:
    docker build -t seedpath:dev .

# Run the image with a local state directory.
docker-run:
    docker run --rm --network host --env-file .env -v "$PWD/data:/config" seedpath:dev

# Start the example compose stack.
compose-up:
    docker compose up -d --build

# Stop the example compose stack.
compose-down:
    docker compose down

# Follow the proxy logs.
compose-logs:
    docker compose logs -f seedpath

# Remove build and cache artifacts.
clean:
    rm -rf dist .mypy_cache .ruff_cache .pytest_cache htmlcov .coverage data
