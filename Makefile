.PHONY: install check hooks demo test

install:  ## project env, including Playwright for browser QA
	uv sync --all-extras

# Local CI: run before every push. The pre-commit hook runs it too (make hooks).
check:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy
	uv run pytest

hooks:
	uv run pre-commit install

demo:  ## offline end-to-end demo with a scripted model (no API keys)
	uv run factory demo --out .demo
