.PHONY: setup check web package

setup:
	uv sync --locked --extra dev
	npm --prefix apps/web ci

web:
	npm --prefix apps/web run build

check:
	uv run --locked --extra dev ruff check server tests extensions
	uv run --locked --extra dev ruff format --check server tests extensions
	npm --prefix apps/web run check
	npm --prefix apps/web test
	npm --prefix apps/web run build
	uv run --locked --extra dev pytest -q

package: web
	uv build --wheel
	cp packages/distribution/requirements-runtime.txt dist/requirements-runtime.txt
