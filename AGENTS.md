# Repository Guidelines

These notes keep contributions predictable for the PC-Assistant codebase. Keep changes small, runnable, and explained so others can review quickly.

## Project Structure & Module Organization
- `venv/` stays for local tooling only; do not commit generated binaries from it.
- Place application modules in `src/pc_assistant/` (create if missing) with `__init__.py`; prefer a clear entry module such as `src/main.py` or `src/pc_assistant/app.py`.
- Keep helper/CLI automation scripts in `scripts/` and shared data or prompts in `assets/` or `data/` with README notes on formats.
- Tests mirror package paths in `tests/`, e.g., `tests/pc_assistant/test_parser.py`.
- Config samples live in `.env.example` or `config/`; document expected keys near the code that consumes them.

## Build, Test, and Development Commands
```powershell
.\venv\Scripts\activate  # Windows
source venv/bin/activate   # macOS/Linux shells
python -m pip install -r requirements.txt
python -m pip install -r requirements-dev.txt  # linters, formatters, test tools
python -m pytest  # run the test suite
python -m black src tests  # auto-format (add to dev deps if missing)
python -m ruff check src tests  # lint; use ruff --fix for safe autofixes
python -m pc_assistant  # run the main package; fallback: python src/main.py
```

## Coding Style & Naming Conventions
- Follow PEP 8 with 4-space indents; keep line length reasonable (88-100 chars) unless readability suffers.
- Use type hints on public functions and dataclasses where structures are stable.
- Naming: snake_case for functions/variables, PascalCase for classes, UPPER_SNAKE_CASE for constants, lower_snake_case for modules and files.
- Prefer pure functions and small units; include docstrings (Google-style or reST) for public APIs and non-obvious helpers.
- Use the `logging` module instead of print for runtime diagnostics.

## Testing Guidelines
- Use `pytest`; name files `test_*.py` and align test module paths with the code they cover.
- Add regression tests with every bug fix; cover edge cases (empty input, large payloads, bad tokens) and markers for slow/integration tests.
- Use fixtures in `tests/conftest.py` to share setup; avoid network or nondeterministic dependencies unless explicitly marked.

## Commit & Pull Request Guidelines
- Write commits in the imperative and prefer Conventional Commits (`feat:`, `fix:`, `chore:`, `docs:`) unless aligning with existing history is required.
- One concern per commit; include context in the body (why, not just what) and reference issue IDs when available.
- PRs: add a short summary, test evidence (commands run), linked issues, and screenshots or terminal captures when behavior/UI changes.
- Keep PRs small; flag breaking changes or new config/env keys in the description.

## Security & Configuration Tips
- Never commit secrets; load them from `.env` and keep an updated `.env.example` for required keys.
- Pin dependencies in `requirements*.txt`; rebuild the lock when upgrading and note major bumps in PRs.
- Validate inputs at module boundaries and sanitize any file or network interactions.
- If adding new external services, document auth scopes and rotate tokens before merging.
