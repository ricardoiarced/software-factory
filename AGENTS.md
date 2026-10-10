# AGENTS.md

## Project overview
Personal software factory that orchestrates AI agents to turn structured intents into verified code within strictly isolated execution sandboxes.

**Stack**: Python 3.11+, `platformdirs`, pytest, GitHub Actions, Codex / Claude Code harnesses.  
Package management: `uv`.

## Commands
The `uv run --no-project --with …` form builds a throwaway environment per
invocation — no persistent install needed. The conformance tools (lint,
typecheck) run in project mode instead: their versions are governed solely by
the pins in `pyproject.toml`. All commands run from the repo root.

- Run tests (full suite): `uv run --no-project --with build --with pytest --with platformdirs --with . -m pytest tests -q`
  - Without `--with build`, the wheel-smoke test fails (it packages the project).
- Run a single test: `uv run --no-project --with pytest --with platformdirs --with . -m pytest tests/test_cli.py -q` (append `::test_name` for one test)
- Certification selection check (must report all skipped): `uv run --no-project --with pytest --with platformdirs --with . -m pytest certification -q -rs`
- Run the CLI from source: `uv run --no-project --with platformdirs --with . factory --help`
- Persistent env instead of ephemeral: `uv venv && uv pip install -e . pytest platformdirs build ruff pyright`, then plain `python -m pytest -q` / `ruff check .` / `pyright` (the deterministic-suite CI job does the equivalent `pip install platformdirs pytest build -e .` — the checkout is installed, since admission's Factory-version probe-cache key reads distribution metadata; the Conformance job uses the project-mode `uv run` forms below)
- Build / package: `python -m build` — CI (`.github/workflows/release-verification.yml`) requires exactly one `py3-none-any.whl` and one `.tar.gz`
- Lint: `uv run ruff check .`
- Format: `uv run ruff format .` (CI enforces the `--check` form)
- Typecheck: `uv run pyright` (production modules only; both configured in `pyproject.toml` `[tool.ruff]` / `[tool.pyright]`)
- Conformance: CI (`.github/workflows/conformance.yml`) runs the format check and counts ruff findings, production pyright errors, and banned `#` comments on every push and PR — the format check fails on any regression, and each count must not exceed the baseline ref's count at run time (`docs/conventions-enforcement.md`)

## Structure
- `factory.py` — CLI entry point (`factory:main`), orchestration, Run Requests
- `_harness.py`, `_codex.py`, `_claude.py` — Runner protocol, per-harness admission and run
- `_publisher.py` — live newest-stable publisher lookups; `_project*.py` — project registry/guidance
- `_rungate.py` — the Shared Run Gate: identity, Git, runtime, and policy admission stages
- `_runrecord.py` — the durable Run Record envelope and launch atomicity
- `_identity.py` — the version-two identity store: records, administration, and migration
- Production surface is frozen to these modules (S2.1, `docs/traceability.md`)
- `tests/` — deterministic suite; `certification/` — maintainer-only real-host suite (`certification/results/` holds records)
- `docs/` — `traceability.md`, `adr/`, agent docs

No `src/` layout: production is flat top-level modules, frozen by spec S2.1
(`docs/traceability.md`) to `factory.py`, `_harness.py`, `_rungate.py`,
`_runrecord.py`, `_publisher.py`, `_codex.py`, `_claude.py`, `_identity.py`. New features extend these modules (or add a new
`_<name>.py` listed in `pyproject.toml` `py-modules`), spec-first: add/extend
the statement rows in `docs/traceability.md` and the tests they name.

## Conventions
- Spec-driven: every normative behavior has a row in `docs/traceability.md` naming the tests that cover it — keep that mapping true when changing behavior.
- Fail closed: admission, compatibility matching, and sandbox probing refuse rather than guess.
- Protocol-based interfaces: Define architectural contracts using typing.Protocol for statically verifiable duck-typing rather than relying on abstract base classes or runtime NotImplementedError exceptions.
- Strictly typed classes: Full attribute annotations, explicit `__init__ -> None` returns, and modern typing.Self for factory or builder methods instead of string forward references.
- Strict typing: full annotations on parameters and returns (modern `X | None` style).
- Formatting: `ruff format` at the pinned ruff version, default settings — no `[tool.ruff.format]` overrides.
- Code should be self-explanatory — `#` comments are banned except pragmas (`# noqa`, `# type: ignore`); docstrings are fine.
- Keep functions and modules small and focused; private helpers are `_`-prefixed.
- A change is accepted only when the full deterministic suite passes (`uv run --no-project --with build --with pytest --with platformdirs --with . -m pytest tests -q`) and the Conformance check is green (`.github/workflows/conformance.yml`); real-host certification is maintainer-only and never implied by green tests.
- Test economy: iterate with single test files or `-k` filters; a commit that touches no Python needs no pytest at all — `ruff format --check .` suffices. The full suite still gates acceptance per the rule above.
- Wrap-ups state the landing: end every session summary with one line of `git status --porcelain` plus the current branch, so uncommitted or wrong-branch state is visible at wrap-up rather than discovered later.
- Each convention's enforcement — linter rules, type-checker, CI checks, or review-only — is mapped in `docs/conventions-enforcement.md`.

## Do not modify
- `skills-lock.json` (the only lockfile tracked in this repo)
- Generated code or build artifacts (`build/`, `dist/`, `*.egg-info/`, `__pycache__/`)
- `.env*` files and any secrets
- Files under `docs/agents/` unless the change is specifically about agent skills
- Sandbox isolation / admission / certification-gating code without explicit review — it is normative, spec-covered behavior

## Agent skills
### Issue tracker
Issues are tracked in this repository's GitHub Issues.  
See `docs/agents/issue-tracker.md`.

### Triage labels
The default five canonical triage labels are used.  
See `docs/agents/triage-labels.md`.

### Domain docs
This is a single-context layout.  
See `docs/agents/domain.md`.

## More context
- Architecture / deeper docs: `docs/` — `traceability.md` is normative, `adr/` holds decisions
