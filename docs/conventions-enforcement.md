# Conventions enforcement

How each convention in the AGENTS.md `## Conventions` section is enforced. This
document is normative for tooling: the enforced rule set is exactly what is
written here. Decided on the conformance map (issue #123); the CI checks it
names are designed by the gate ticket and do not exist yet.

## Registers

- **ruff rule / pyright** — carried by the pinned tool configs in `pyproject.toml`.
- **CI check** — a repo script surfaced through the conformance gate; designed
  by the gate ticket, review-only until it exists.
- **review-only** — human enforcement through PR review; its strongest
  instance is the "Do not modify" gate.

## Tool pinning

| Tool | Pin | Mechanism |
|---|---|---|
| ruff | `0.16.9` | `[tool.ruff] required-version = "==0.16.9"` — ruff refuses to run off-spec, so every invocation (including `uv run --with ruff`) is bound. Upgrades are deliberate policy changes. The dev-group `ruff` entry stays unpinned on purpose: the config pin is the single source of truth; two pins would drift. |
| pyright | `1.1.414` | exact pin in the dev dependency group — pyright has no config-level version lock. |

## Lint selection

- **Base: ruff's default rule set, frozen by the pin.** Ruff 0.16 defaults are
  already a broad curated set (bugbear, simplify, pyupgrade, pylint,
  ISC/PIE/PYI/RUF, …). Transcribing them as an explicit `select` is
  deliberately avoided: at a pinned version the defaults are deterministic,
  and writing out only the codes that currently fire would narrow enforcement
  below the defaults and drop latent coverage (undefined-name detection and
  friends).
- **Additions: `extend-select = ["ANN", "TID251"]`** — every addition is
  convention-derived (see the register).
- **All selected rules fire as errors. No warning tier.**
- Scope: all tracked code (`ruff check .`).

## Type checking

- **Scope: the eight production modules only** (`[tool.pyright] include`).
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
| Spec-driven: traceability rows name their tests | CI check (row→test validator), review-only until the gate exists | The mapping is structural; a script can verify every named test exists. Design lands with the gate. |
| Fail closed: refuse rather than guess | Review-only, reinforced by the "Do not modify" gate | A design property of admission/compat/sandbox code, not a syntactic pattern a linter can see. |
| Protocol-based interfaces, not ABC | Ruff TID251 bans `abc.ABC`; the `NotImplementedError` half stays review-only | TID251's banned-API fires on dotted usage (verified at 0.16.9); bare builtins are not dotted accesses, so they cannot be banned mechanically. Zero current violations. |
| Strictly typed classes | Review-only; pyright flags the inferable parts (e.g. a non-None `__init__` return) | No stock rule covers attribute annotations, `__init__ -> None`, or `Self` returns; production hand-checks are clean today. |
| Strict typing: full annotations, modern style | Ruff ANN (annotations) + pyright `standard` (type correctness); modern `X \| None` style via the UP rules in defaults | Division of labor: pyright at basic/standard does not flag missing annotations; ANN does, at +5 findings — production is already fully annotated. |
| Self-explanatory; `#` comments banned except pragmas | CI check (comment scan with a `# noqa` / `# type: ignore` allowlist), review-only until the gate exists | No ruff rule bans prose comments; a scan is exact and false-positive-free under the pragma allowlist. |
| Small and focused; `_`-prefixed helpers | Review-only; the module-size half is parked on the map's fog | No stock rule enforces helper naming; size thresholds are an undecided policy. |
| Full suite green before acceptance | CI check (the gate itself); certification stays maintainer-only | Already the acceptance rule; the gate makes it mechanical. |

## Baseline at adoption

The pinned policy inherits the violations it will govern; remediation is a
separate policy decision. At adoption (ruff 0.16.9 defaults + ANN, pyright
1.1.414 `standard`): 98 ruff findings (production 30 / tests 64 /
certification 4), 5 production pyright errors, 38 production comment lines.
