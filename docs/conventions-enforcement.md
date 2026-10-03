# Conventions enforcement

How each convention in the AGENTS.md `## Conventions` section is enforced. This
document is normative for tooling: the enforced rule set is exactly what is
written here. Decided on the conformance map (issues #123 and #124).

## The gate

The CI checks below run as the dedicated Conformance workflow
(`.github/workflows/conformance.yml`): single job, every push and pull
request. It counts three things — ruff findings, production pyright errors,
banned `#` comment lines (scanned over all tracked Python code) — and
enforces a **no-regression ratchet**: a run passes only when every count is
at most the baseline ref's count at run time. The head side is the checked
out commit (for a pull request, its merge commit — what would land); the
baseline is the pull request's target ref, `main` for normal PRs, so on
pushes to `main` the comparison is against itself. Counts are per-check
totals, not per-rule.
During remediation the ratchet lets lanes land while strictly improving; once
the counts reach their floor it is the strict gate — nothing to flip. Ahead of
the counts, the gate also runs a strict `ruff format --check` on the head ref:
formatting is binary and the tree lands already formatted, so there is no
baseline to ratchet against — any regression fails outright (see
"Formatting").

## Registers

- **ruff rule / pyright** — carried by the pinned tool configs in `pyproject.toml`.
- **CI check** — surfaced through the Conformance workflow under the
  no-regression ratchet (see "The gate"); blocking by convention through the
  AGENTS.md acceptance rule, and release-blocking through the Publication Gate.
- **review-only** — human enforcement through PR review; its strongest
  instance is the "Do not modify" gate.

## Tool pinning

| Tool | Pin | Mechanism |
|---|---|---|
| ruff | `0.16.10` | `[tool.ruff] required-version = "==0.16.10"` — ruff refuses to run off-spec, so every invocation (including `uv run --with ruff`) is bound. Upgrades are deliberate policy changes. The dev-group `ruff` entry stays unpinned on purpose: the config pin is the single source of truth; two pins would drift. A fresh resolution past the pin fail-closes (ruff exits 2) until the pin is bumped — the 0.16.10 release broke CI exactly this way, and the fix is the bump. |
| pyright | `1.1.414` | exact pin in the dev dependency group — pyright has no config-level version lock. |

## Lint selection

- **Base: ruff's default rule set, frozen by the pin.** Ruff 0.16 defaults are
  already a broad curated set (bugbear, simplify, pyupgrade, pylint,
  ISC/PIE/PYI/RUF, …). Transcribing them as an explicit `select` is
  deliberately avoided: at a pinned version the defaults are deterministic,
  and writing out only the codes that currently fire would narrow enforcement
  below the defaults and drop latent coverage (undefined-name detection and
  friends).
- **Additions: `extend-select = ["ANN", "TID251", "PLR0912", "PLR0913",
  "PLR0915"]`** — every addition is convention-derived (see the register).
- **All selected rules fire as errors. No warning tier.**
- Scope: all tracked code (`ruff check .`).

## Formatting

- **Formatter: `ruff format`, default settings.** No `[tool.ruff.format]`
  section exists on purpose — transcribing the defaults would only invite
  drift, the same reasoning as the lint selection. Formatter behavior is
  frozen by the same `required-version = "==0.16.10"` pin as the linter, so a
  version bump is a deliberate policy change on both sides.
- **Scope: all tracked code** (`ruff format .`) — production modules,
  `tests/`, and `certification/`.
- **Adopted clean**: the adoption commit reformats the 22 files the formatter
  wants (AST-equivalent, verified) and lands the tree already formatted, which
  is why the gate's check is strict from birth rather than ratcheted.

## Type checking

- **Scope: the nine production modules only** (`[tool.pyright] include`).
  Excluding `tests/` + `certification/` is a declared decision, not an
  oversight: at `basic` those directories carry ~141 real errors with pytest
  resolved, 131 of them `reportArgumentType` dominated by `monkeypatch.setattr`
  test-doubles passed where `subprocess.run` types are expected
  (`tests/test_codex.py` alone: 93). Test annotations are enforced by the ANN
  rules instead. Growth is parked on the conformance map's fog; note
  per-environment `typeCheckingMode` does not work on pyright 1.1.414
  (per-rule overrides inside execution environments do).
- **Mode: `standard`.** Measured at 1.1.414: basic = 3 production errors,
  standard = 5 (+2 `reportPossiblyUnboundVariable`); strict = 45, dominated by
  Unknown-family noise plus 9 `reportPrivateUsage`, which conflicts with the
  idiomatic cross-module `_`-prefixed access of the flat modules.
- Severities stay at mode defaults: no overrides, no warning tier.

## The register

| Convention (AGENTS.md) | Enforced by | Why |
|---|---|---|
| Spec-driven: traceability rows name their tests | CI check (`tests/test_traceability.py`, run by the deterministic suite job) | The mapping is structural; the validator inventories `tests/` and `certification/` via AST and enforces the mapping bidirectionally — every row's named tests exist, every test is named — on every push and PR. |
| Fail closed: refuse rather than guess | Review-only, reinforced by the "Do not modify" gate | A design property of admission/compat/sandbox code, not a syntactic pattern a linter can see. |
| Protocol-based interfaces, not ABC | Ruff TID251 bans `abc.ABC`; the `NotImplementedError` half stays review-only | TID251's banned-API fires on dotted usage (verified at 0.16.9); bare builtins are not dotted accesses, so they cannot be banned mechanically. Zero current violations. |
| Strictly typed classes | Review-only; pyright flags the inferable parts (e.g. a non-None `__init__` return) | No stock rule covers attribute annotations, `__init__ -> None`, or `Self` returns; production hand-checks are clean today. |
| Strict typing: full annotations, modern style | Ruff ANN (annotations) + pyright `standard` (type correctness); modern `X \| None` style via the UP rules in defaults | Division of labor: pyright at basic/standard does not flag missing annotations; ANN does, at +5 findings — production is already fully annotated. |
| Formatting: `ruff format`, defaults, pinned version | CI check (`ruff format --check` in the Conformance workflow — strict head-ref check ahead of the ratchet counts) | Formatting is binary, so a plain check is exact; the adoption commit makes the floor "already formatted", leaving nothing to count — a regression fails outright. |
| Self-explanatory; `#` comments banned except pragmas | CI check (comment scan in the Conformance workflow, allowlist `# noqa` / `# type: ignore`) | No ruff rule bans prose comments; a scan is exact and false-positive-free under the pragma allowlist. |
| Small and focused; `_`-prefixed helpers | Ruff PLR0912/0913/0915 for the function-complexity half (CI check under the gate's ratchet); review-only for the module-size half and helper naming | The three stock function-complexity rules joined the selection at the 0.16.9 defaults (12 branches / 5 arguments / 50 statements, tree-wide, all errors). Adopted lane-first: the #142 remediation refactored every over-limit function before the rules landed, so the selection inherits zero findings and the ratchet never rises. Module length has no stock rule and gets no invented threshold — it stays review-only, parked on the map's fog. |
| Full suite green before acceptance | CI check (the deterministic-suite job plus the Conformance workflow); certification stays maintainer-only | The acceptance rule requires both green; the Publication Gate makes it release-blocking. |

## Baseline at adoption

The pinned policy inherits the violations it will govern; remediation is a
separate policy decision. At adoption (ruff 0.16.9 defaults + ANN, pyright
1.1.414 `standard`): 98 ruff findings (production 30 / tests 64 /
certification 4), 5 production pyright errors, 38 production comment lines
(the gate's all-tracked comment scan counts 106 over tracked Python at the
same commit).
