# Strict Configuration Is an Effective-Input Property

- **Status:** Accepted (resolves the policy question deferred by the strict-configuration research, issue #159)
- **Date:** 2026-10-02

## Context

The strict-configuration research recorded the semantics of each Harness's strict flags: Codex's `--strict-config` refuses unknown fields in the loaded effective configuration before inference, while Claude Code documents no all-settings strict flag — `--strict-mcp-config` only selects MCP sources, invalid ordinary-settings entries can be skipped silently, and on the certified version `claude doctor` exits 0 without reporting a deliberately unknown top-level settings key. The research deferred to a policy decision whether strictness must additionally mean auditing configuration sources the certified invocation excludes.

The applied configuration surface of the certified invocations is already closed. Codex runs with `--ignore-user-config`, so the personal `config.toml` is not an effective input, and `--strict-config` validates every override Factory sends. Claude Code runs with `--restricted`, which ignores user, project, and local settings files entirely; `--strict-mcp-config` confines MCP servers to Factory's `--mcp-config`; and the managed-policy gate refuses any present, configured, or uninspected managed surface. The one remaining applied Claude settings source is Factory's own inline `--settings` document, of which Factory authors every key.

## Decision

**Strict configuration is an effective-input property.** A Run is admissible only when every configuration input that can affect the certified launch is Factory-authored, independently gated, or proven absent. Configuration sources excluded by the certified invocation are outside the Run's effective configuration and carry no admission obligation merely because they exist on the host.

Factory proves the effectiveness of the security-relevant settings it authors — the Claude read fence, permission behavior, and `--permission-mode dontAsk` — with version-bound behavioral evidence for the exact supported tuple: the Host Admission Probe establishes the behaviors those settings exist to produce, so a silently ignored or unsupported key surfaces as a failed or unknown evidence condition, never as an inferred pass from parser acceptance. Inline settings that cannot affect the isolation contract carry no independent effectiveness proof obligation. `claude doctor`'s exit status is never treated as a configuration-validity gate.

The wording discipline is deliberate: the guarantee is "all effective security-relevant configuration is controlled and behaviorally evidenced," never "all settings are validated."

The Project Doctor presents configuration classified by launch effectiveness — effective launch inputs, gated managed inputs, and ignored ambient configuration, the last explicitly identified as not a launch input — and the presence, absence, or validity of an ignored ambient source never affects admission.

## Consequences

No certified recipe changes. `--strict-config` on the Codex side and `--restricted` plus `--strict-mcp-config` on the Claude side already close the effective surface, so no implementation slice adds a flag, and the only real-host certification in the implementation sequence is the standing Publication Gate run. Factory never audits ignored ambient files: malformed or surprising ambient state is host context — reportable by the doctor, never admission state — because auditing it would enlarge the trust surface without changing what executes.

## Alternatives considered

### Per-Run settings-effectiveness inspection

Rejected. A new fail-closed inspector over settings-error output or per-key behavioral canaries would duplicate the signal the Host Admission Probe already provides behaviorally, while adding an unrecognized-output failure surface without establishing a materially stronger property.

### Auditing ignored ambient configuration

Rejected outright. It converts intentionally ineffective ambient state into admission state, contradicts the isolation design that keeps those sources outside the Run, and cannot affect the effective configuration of the current launch by definition.

## Scope

Establishes the strict-configuration admission policy, the version-bound behavioral proof obligation for security-relevant inline settings, and the doctor's launch-effectiveness presentation classes. It changes no certified harness recipe and names no covering statement-row tests; the policy-binding slice adds them.
