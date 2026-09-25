# Conventions violation audit (ticket #121)

Read-only reconnaissance for map #119. Audits the working tree at branch
`feat/issue-105-claude-host-probe` (HEAD `b064058`) against the conventions in
`AGENTS.md` ("Conventions", "Do not modify"). No code was changed.

Sources: command output from `ruff`/`pyright` runs quoted below (versions and
exact invocations inlined per section) plus direct reads of the named
`file:line` locations.

## Headline numbers

| Area | ruff | pyright | `#` comments | hand-check violations |
|---|---|---|---|---|
| Production (8 modules, 3,751 LOC) | 29 | 3 errors | 38 lines (37 charted + 1 inline) | 1 (string forward ref) |
| `tests/` (8,336 LOC) | 60 | 150 code errors (+9 env artifacts) | n/a | 10 (4 unannotated attrs, 6 string-fwd `__enter__`) |
| `certification/` (762 LOC) | 4 | 5 code errors (+2 env artifacts) | n/a | 0 |
| **Total** | **93** | **3 + 166** | **38** | **11** |

Fixability split for ruff (93 findings): **41 safe-fix / 43 unsafe-fix / 9 manual**
— matches ruff's own repo-wide tally "41 fixable with `--fix` (43 hidden fixes
can be enabled with `--unsafe-fixes`)".

## 0. Environment sensitivity (decision-relevant)

- No ruff version is pinned and `[tool.ruff]` (working tree) sets only
  `target-version = "py311"` — no `select`. `uv run --with ruff` floats.
- Ruff **0.16.9** (current float) defaults to the full rule set (920 rules
  enabled per `ruff check --show-settings`): repo-wide **93 findings**.
- Ruff **0.14.0** defaults to E4/E7/E9/F: repo-wide **3 findings**, all F401
  (`_codex.py:4` `errno`, `_codex.py:13` `stat`, `tests/test_codex.py:21`
  `_publisher`).
- Implication: every number below is against ruff 0.16.9 defaults. A policy
  decision on pinning / explicit `select` is a prerequisite for any "get to
  zero" target.
- Pyright **1.1.414** (basic) via the working-tree `[tool.pyright]` config
  (8 production modules).

## 1. Tool surface

### 1.1 Ruff — production modules (29 findings; 12 safe-fix, 16 unsafe-fix, 1 manual)

`uv run --no-project --with ruff ruff check --output-format concise factory.py _harness.py _codex.py _claude.py _publisher.py _project.py _project_guidance.py _project_registry.py`

| Rule | Count | Locations (file:line) | Fixability | Classification |
|---|---|---|---|---|
| I001 unsorted-imports | 8 | one per module, all at `3:1` except `_publisher.py:12` | safe-fix | manual-mechanical (but 3 of the 8 are normative-area files, see §4) |
| ISC004 implicit string concat in collection | 10 | `_claude.py:457,462,467,508,516,522,528,534`, `_project_registry.py:310,348` | unsafe-fix | behavior-adjacent (probe-prompt literals in `_claude.py`); normative in `_claude.py` |
| UP022 `capture_output` | 6 | `_claude.py:346,740`, `_codex.py:198,339,495`, `_harness.py:186` | unsafe-fix | behavior-adjacent subprocess kwargs; normative (admission/probe paths) |
| F401 unused import | 2 | `_codex.py:4` (`errno`), `_codex.py:13` (`stat`) | safe-fix | manual-mechanical; normative-area file |
| UP035 import `Iterator` from `collections.abc` | 1 | `_claude.py:17` | safe-fix | manual-mechanical; normative-area file |
| FURB188 `str.removesuffix()` | 1 | `_publisher.py:271` | safe-fix | manual-mechanical; not normative |
| SIM115 open without context manager | 1 | `factory.py:462` | no fix (manual) | false-positive-ish: `TemporaryFile` is wrapped in `with spool:` at `factory.py:464`; inlining the assignment into the `with` is a manual, behavior-adjacent edit |

Per-module split: `_claude.py` 12, `_codex.py` 6, `_harness.py` 2,
`_project_registry.py` 3, `_publisher.py` 2, `factory.py` 2, `_project.py` 1,
`_project_guidance.py` 1.

### 1.2 Ruff — `tests/` (60 findings; 26 safe-fix, 26 unsafe-fix, 8 manual)

| Rule | Count | Files | Fixability |
|---|---|---|---|
| ISC004 | 15 | `test_claude.py` ×2, `test_codex.py` ×3, `test_run_request.py` ×10 | unsafe-fix |
| PIE804 unnecessary `**kwargs` | 15 | `test_publisher.py` ×12, `test_claude.py` ×1, `test_codex.py` ×2 | safe-fix |
| I001 | 9 | 9 test files | safe-fix |
| PLW1510 `subprocess.run` without `check` | 7 | `test_cli.py` ×4, `test_factory.py` ×1, `test_run_request.py` ×2 | manual (intent-dependent) |
| PYI034 `__enter__` should return `Self` | 6 | `test_claude.py:1378`, `test_codex.py:245,321`, `test_factory.py:422`, `test_publisher.py:333,379` | unsafe-fix |
| RUF059 unused unpacked variable | 3 | `test_claude.py:576,606,870` | unsafe-fix |
| UP022 | 2 | `test_claude.py:1074`, `test_run_request.py:599` | unsafe-fix |
| BLE001 blind `except BaseException` | 1 | `test_codex.py:549` | manual |
| SIM117 nested `with` | 1 | `test_claude.py:1127` | safe-fix |
| F401 | 1 | `test_codex.py:21` (`_publisher`) | safe-fix |

### 1.3 Ruff — `certification/` (4 findings; 3 safe-fix, 1 unsafe-fix, 0 manual)

| Rule | Count | Locations | Fixability |
|---|---|---|---|
| I001 | 3 | `_selection.py:3`, `conftest.py:3`, `test_exact_tuple.py:3` | safe-fix |
| C401 set comprehension | 1 | `test_exact_tuple.py:440` | unsafe-fix |

Certification files are part of the maintainer-only real-host gate; treat all 4
as review-gated in spirit even though they are mechanical.

### 1.4 Pyright — production (3 errors)

`uv run --no-project --with pyright --with platformdirs --with . pyright`

| Location | Rule | Note | Classification |
|---|---|---|---|
| `_harness.py:68` | reportReturnType | `Runner.admit` Protocol stub (docstring body) read as returning `None` | inside normative Runner contract; manual, review-gated |
| `_harness.py:71` | reportReturnType | `Runner.run` Protocol stub, same shape | inside normative Runner contract; manual, review-gated |
| `_publisher.py:45` | reportReturnType | `tuple[int, ...]` returned where `tuple[int, int, int] \| None` declared in `parse_stable_version` | manual-mechanical (construct the 3-tuple explicitly or `cast`); not normative |

### 1.5 Pyright — `tests certification` (166 errors; measurement only, no config change)

`uv run --no-project --with pyright --with platformdirs --with . pyright tests certification`

- 11 of 166 are `reportMissingImports` for `pytest` — an artifact of the
  ephemeral env (`--with pyright --with platformdirs --with .` has no pytest);
  **155 code errors** would remain in a real env.
- Error classes: reportArgumentType 131, reportReturnType 16,
  reportMissingImports 11, reportCallIssue 4, reportOperatorIssue 2,
  reportAssignmentType 1, reportIndexIssue 1.
- By file: `test_codex.py` 94 (dominated by `str`-method "Argument of type
  `object`" mismatches in fixture helpers), `test_run_request.py` 41
  (25 reportArgumentType, 15 reportReturnType around `_selected_harness`
  handoff lambdas, e.g. `1520:84`, `1571:73`), `test_claude.py` 11,
  `test_exact_tuple.py` 5 (4 reportArgumentType + 1 pytest import),
  `test_factory.py` 5, `tests/conftest.py` 2 (1 reportReturnType + 1 pytest
  import), `test_cli.py` 2, `test_platform_claims.py` 2, `test_harness.py` 1,
  `test_certification.py` 1, `test_publisher.py` 1, `certification/conftest.py` 2
  (1 reportReturnType + 1 pytest import).
- Adding these would be a >50x jump over the production error count; nothing in
  pyproject currently asks tests/certification to typecheck.

## 2. Comment enumeration (production modules)

Pragmas excluded (there are none: no `# noqa`, no `# type: ignore` anywhere in
production). Chart snapshot verified exactly: **32 in `_claude.py`, 3 in
`_codex.py`, 2 in `_harness.py`** — 37 lines. The chart missed one **inline**
trailing comment: `_publisher.py:357` (total 38).

### `_claude.py` — 32 lines (all in the normative probing/admission module)

| Lines | Content (gist) | Disposition |
|---|---|---|
| 38–39 | Why `_CLAUDE_COMPATIBILITY_SET` is empty: no release certified yet | convert — move rationale to the compatibility-set name / traceability row |
| 42–43 | `_INSTALL_CHANNELS` = certified first-scope install channels, digest-anchored | convert to name; delete |
| 47–50 | Managed-policy sources refuse on detection; gate is read-only, uncached, per-Run | convert — normative rationale belongs in `docs/traceability.md` |
| 55–57 | `_DOCTOR_UNAVAILABLE_ATTRIBUTION`: custom-endpoint no-signal shape; bare/network not-fetched refuse | delete after renaming constants to carry the distinction |
| 59 | `_DOCTOR_BENIGN_STATES` = doctor states reporting no policy | delete (name already says it) |
| 69–70 | `_SANDBOX_DEPENDENCIES`: missing deps refuse, no unsandboxed fallback | delete / move to spec |
| 73–75 | Certified nonessential-traffic pins; evidence covers production | delete / move to spec |
| 421–431 | Host Admission Probe design: non-interactive Run, deterministic prompt, shell-parser guard, ask-denied trap | delete — 11 lines of design rationale that belongs in `docs/` (normative) |
| 444–445 | Probe step group: inside-boundary success through every admitted tool | delete (self-evident step names) or convert into a helper-function name |
| 470 | Probe step group: forbidden operations through Bash | delete |
| 512 | Probe step group: same forbidden operations via native file tools | delete |

### `_codex.py` — 3 lines (normative admission module)

| Lines | Content | Disposition |
|---|---|---|
| 252–253 | Feature pin covers the complete official inventory; new capabilities fail closed | delete / move to spec |
| 582 | Provenance: "Certified by the 2026-09-22 Linux x86_64 real-host decision for issue #92" | convert — provenance belongs in `certification/results/` or traceability, not a code comment |

### `_harness.py` — 2 lines (normative Runner/admission module)

| Lines | Content | Disposition |
|---|---|---|
| 215–216 | `_CHILD_RUNTIME_SIGNALS` kept to the foreground-group contract; sync faults and process-local signals keep native dispositions | convert — contract rationale into the constant's name or docs |

### Outside the chart

| Lines | Content | Disposition |
|---|---|---|
| `_publisher.py:357` | trailing `# the lookup only returns stable versions` on an `assert` | delete — fold into the assert message if the invariant needs stating; only comment outside the normative trio |

## 3. Hand-checked conventions (no tool coverage in production today)

Scanned all 8 production classes/functions via AST plus grep; `tests/` and
`certification/` re-scanned with the same patterns.

| Check | Production | Tests | Certification |
|---|---|---|---|
| (a) class attributes lacking annotations | **clean** (0) | 4: `_TimedOutProbeProcess.pid` `test_claude.py:1363`; `SuccessfulProbeProcess.returncode` `test_codex.py:238`; `FailedProbeProcess.returncode` `test_codex.py:1278`; `SlowDrip.status` `test_publisher.py:321` | 0 |
| (b) `__init__` without `-> None` | **clean** | 0 | 0 |
| (c) string forward references | **1**: `_claude.py:40` `tuple["_ClaudePolicy", ...]` (class defined at `:408`, so the quotes are runtime-load-bearing — not mechanically removable) | 6: the `__enter__` sites in row (d) | 0 (`test_exact_tuple.py:108` is `Literal[...]`, not a forward ref) |
| (d) `cls`/`self`-returning methods typed as the class instead of `typing.Self` | **clean** — production defines no `__enter__`/builder at all (zero `Self` imports needed) | 6: `_QuietListener.__enter__` `test_claude.py:1378`; `ListeningSocket.__enter__` `test_codex.py:245`; `Process.__enter__` `test_codex.py:321`; `FailingWriter.__enter__` `test_factory.py:422`; `SlowDrip.__enter__` `test_publisher.py:333`; `Redirected.__enter__` `test_publisher.py:379` — all coincide with ruff PYI034 | 0 |
| (e) ABC / `abstractmethod` / `NotImplementedError` interfaces | **clean** — none anywhere; the one contract, `Runner` (`_harness.py:65`), is already `typing.Protocol` | 0 | 0 |

Informational, not a stated convention: 32 module-level constants across the 8
production modules are assigned without an explicit annotation (e.g.
`factory.py:42,49,95,103`, `_harness.py:75,94,217`, `_claude.py:34,44-45,51-54,58,60-61,71`,
`_publisher.py:25-28,30-32,34,309-311`, `_project.py:34`, `_project_registry.py:34-36,223`,
`_codex.py:595`). The convention text covers parameters/returns and class
attributes only.

## 4. Normative-area classification ("Do not modify": admission / sandbox probing in `_harness.py`, `_codex.py`, `_claude.py`; certification gating)

Every production finding inside the normative trio, named explicitly — the
effort treats these as review-gated regardless of mechanical fixability:

- **Ruff (20 of 29)**: `_claude.py` 12 (I001, UP035, UP022 ×2, ISC004 ×8),
  `_codex.py` 6 (I001, F401 ×2, UP022 ×3), `_harness.py` 2 (I001, UP022). The
  ISC004/UP022 items touch probe-prompt literals and subprocess launch kwargs —
  behavior-adjacent, not mere style.
- **Pyright (2 of 3)**: `_harness.py:68,71` — inside the `Runner` Protocol
  contract itself.
- **Comments (37 of 38)**: all `_claude.py` / `_codex.py` / `_harness.py`
  comment lines; the sole outlier is `_publisher.py:357`.
- **Hand-check (1)**: `_claude.py:40` string forward reference (runtime-required
  today; would need reordering the class or PEP 563/649 semantics).
- **Certification (review-gated by role)**: ruff 4; pyright 6.

Clean (non-normative) production surface: `factory.py` (SIM115 + I001),
`_publisher.py` (I001, FURB188, pyright `:45`), `_project.py` (I001),
`_project_guidance.py` (I001), `_project_registry.py` (I001, ISC004 ×2).

## Summary

- The codebase is convention-clean where the hand-checks look: no ABC-based
  interfaces, no unannotated class attributes or bare `__init__` in production,
  no `Self`-violating builders in production.
- The real debt is (1) a floating ruff whose default rule set flipped from 3
  findings (0.14 defaults) to 93 (0.16.9 defaults) with no pin and no explicit
  `select`; (2) 38 production comment lines, 37 of them in normative-area
  modules; (3) tests/certification were never held to the tool surface (60 ruff
  findings, 155 real pyright errors); (4) one production string forward ref
  (`_claude.py:40`) plus three production pyright return-type errors.
