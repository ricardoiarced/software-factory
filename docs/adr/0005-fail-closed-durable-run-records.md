# Fail-Closed Durable Run Records

- **Status:** Accepted (amends the launch-tail ordering of ADR 0004)
- **Date:** 2026-10-01

## Context

Factory records nothing about a Run today. A refused preflight — the highest-value audit event on a fail-closed system, and its most common outcome — leaves no trace: no identity of what was attempted, no evidence of why admission stopped, no boundary or harness facts observed on the way to the refusal. A launched Run survives only as a process exit status. After the fact, no artifact can answer what Factory admitted, on what evidence, for which selected inputs.

Three constraints shape the fix. The record must remain audit evidence only — never a resume or replay surface. It must not persist secret-bearing bytes: Instruction, Context, Project Spec contents, environment values, and verbatim argv are exactly the material a record would most want and must not keep. And it must stay intelligible after identity records are removed or renamed, since removal is an explicit, non-cascading operation that historical Runs must not depend on retroactively.

ADR 0004 establishes that the Validated Run Plan and its outcome form one in-process chain, and explicitly leaves the fields of the auditable Run record to their own decision. This is that decision.

## Decision

**A Run is an admission attempt.** Every invocation that enters the Shared Run Gate on the Run execution path is one Run with exactly one terminal outcome. A refusal is a terminal Run outcome, not an absence of a Run. CLI usage errors that never enter the gate are not Runs.

**Every Run is recorded before it can proceed.** The Run orchestrator mints the Run ID and durably opens the record before the first gate stage, so a crash during admission still leaves the attempt visible. The Shared Run Gate itself stays observational and side-effect-free with respect to records: it returns a structured admission result, and the orchestrator commits that result into the already-open record. Read-only consumers — the Project Doctor above all — may run the same gate implementation and never mint a Run or a record.

**The Run ID is an identity token, not a label.** A cryptographically random, immutable `run_…` token in the same prefix discipline as `cli_…`, `prj_…`, `repo_…`, and `chk_…`, allocated before the first gate stage. It is never derived from names, paths, Git state, or timestamps, never reused across retries, and carries no ordering semantics.

**Records snapshot identity.** Each record carries the immutable Client, Project, Repository, and Checkout IDs plus the Client key, Project key, and canonical Project selector as they existed for that Run, and the freshly verified boundary and common-directory proofs admission relied on. The IDs are the authoritative identity facts; the keys are historical descriptive context. Historical rendering never consults the current identity store, and removal, renaming, or re-registration of identity records never rewrites, deletes, or invalidates history. No tombstone lifecycle is introduced.

**Digests, not bytes.** The record never contains Instruction, Context, or Project Spec bytes, environment values, or verbatim argv. Textual inputs appear as SHA-256 digests and byte counts with their source kind and label or path; the effective child environment appears as one digest over a deterministic canonical serialization plus a variable count and a redaction/pin/removal report of variable names only. Failure records obey the same rule: diagnostics, exception capture, and spawn-failure details carry only known-safe Factory-generated text, never arbitrary exception strings. The in-memory Validated Run Plan necessarily holds the executable truth; the durable record holds non-secret evidence about it.

**Launch evidence binds without persisting.** The record stores the Validated Run Plan fingerprint (SHA-256 over a deterministic canonical serialization, computed at plan seal), `argv_sha256` over `factory-argv-v1` — a versioned, unambiguous encoding: domain/version tag, argument count, then each argument as byte-length plus UTF-8 bytes, with `argv[0]` included — plus the launch-recipe identity and the resolved executable identity. An auditor reconstructs a candidate argv under the recorded recipe and compares digests; the exact argv that crossed the launch boundary is bound without its contents becoming part of the durable schema.

**One file per Run, crash-durable before launch.** Run Records live in a separate store — `<user_data>/runs/<run_id>.json`, Factory's first user-data resident, outside the four-record-type identity store — as one JSON document per Run. Every write that is a prerequisite to launching follows the crash-durable protocol: write the complete replacement record to a same-directory temporary file, fsync the file, atomically rename it over the record, fsync the directory. Storage is user-private. There is no journal, no cross-Run locking, and no retention or expiry policy: records accumulate until a future policy or operator action says otherwise. A finalized record is immutable; finalization is distinct from terminal status — REFUSED and LAUNCH_FAILED finalize immediately, a LAUNCHED record finalizes only when no further observable outcome fields can be added.

**The durability boundary is the Launch Commit.** Record creation and every pre-launch verdict transition are admission-critical: any failure refuses the Run, and a failed refusal-write leaves the record honestly in its pre-refusal state with a stderr warning rather than a fabricated terminal status. The Launch Commit is written crash-durably after the Launch Boundary Recheck and before spawn is permitted: boundary recheck → durable launch commit → launch attempt, with the commit the sole intervening operation. This refines ADR 0004's shorthand that the recheck sits immediately before process creation; nothing about bind-by-value, the two-observation fact set, or the recheck's authority changes. Post-spawn facts attempt the same durable protocol but are best-effort: a failed write warns on stderr and never retroactively refuses or misrepresents a process that already ran. The governing invariant: Factory never intentionally creates an unrecorded Run. Once the irreversible launch boundary is crossed, power loss may leave the outcome unknown, but it must not erase the durable evidence that authorized crossing it.

**Status describes observed outcome; readers derive crash states.** `ADMITTING` (written at gate entry) ends in exactly one of `REFUSED` (an admission invariant failed, including the Launch Boundary Recheck), `LAUNCH_FAILED` (admission fully passed; the observed process-creation operation failed), or `LAUNCHED` (process creation positively observed and durably recorded — written in the same atomic update as `spawned_at`). `launch_committed_at` means admission completed and Factory irreversibly authorized process creation as its next action, not that a child exists. A record with `ADMITTING` plus a Launch Commit means the launch outcome is unknown and readers infer neither success nor failure; `LAUNCHED` without exit fields means the process spawned and its completion was not recorded. Exit evidence is orthogonal to status: `exit_code` or `terminated_by_signal` with `exited_at`, never a fabricated integer.

**Refusals render standalone.** Each refusal carries the refusal family, the refusing stage, the authoritative machine-readable diagnostic code, and the canonical rendered diagnostic text with its concrete values inserted — without terminal decoration such as color or wrapping. Historical Run Records never require an old Factory binary or source tree to explain their refusal.

**History is audit only.** Run Records are evidence, never a resume or replay surface: no Run can be re-entered, continued, or replayed from its record. Traceability statements that exclude Run history are amended to except the Run Record explicitly as audit evidence.

## Consequences

Audit writes can refuse Runs. That is the accepted cost of never executing an unrecorded Run; on a fail-closed system the record's first duty is the refused preflight, which previously left no trace at all.

Records accumulate indefinitely with no deletion, expiry, or compaction. That is an intentional consequence of this decision, not an omission; removal policy is future work and requires an explicit decision.

Argv is not readable verbatim from a record. Verifying launch contents requires reconstructing candidate argv under the recorded recipe and comparing digests — exact binding without a perpetual obligation to prove new argv flags non-sensitive.

Crash gaps render honestly. The unknown states (committed-but-unspawned, spawned-but-unexited) are derived from present and absent fields, never invented, and no later Factory version rewrites a historical record into a better-looking state.

Snapshots keep history intact after identity removal while the identity store stays exactly four record types, and per-Run files keep the store free of journals and cross-Run locking.

The record schema is versioned from its first release. The canonical serialization encodings — plan fingerprint, environment digest, and `factory-argv-v1` — are pinned with the implementing slices named by the sequencing decision.

## Alternatives considered

### Record only launched Runs

Rejected. The refusals this system exists to produce would be precisely the unrecorded events, and stage-1 failures such as an unreadable identity store would be invisible.

### Verbatim argv

Rejected. It would make today's argv representation part of the durable audit schema and create a continuing security invariant: every future flag would need proof of non-sensitivity before shipment. The digest binds the exact argv without the content.

### Tombstones for removed identities

Rejected. Tombstones couple the run store to identity-store removal paths and add referential-integrity and garbage-collection obligations solely for historical display; snapshots stand alone.

### Atomic rename without fsync

Rejected. No-partial-record visibility is not durability: a power loss could erase the very record whose absence refuses Runs, hollowing out the fail-closed rule.

### Best-effort records everywhere

Rejected. A Run Factory cannot record is a Run Factory cannot admit. The lever only exists before launch — which is exactly where best-effort ends.

### Refuse when the post-launch outcome write fails

Rejected. The process already ran; converting a failed outcome write into a refusal would misrepresent history rather than repair it.

## Scope

This ADR establishes the Run Record contract: lifecycle, Run ID, identity snapshots, evidence policy, launch binding, storage and durability, status semantics, and history meaning.

It does not fix module placement or the canonical serialization encodings (implementation territory for the sequencing decision), does not name the covering statement-row tests, and does not extend to full event ingestion, dashboards, or analytics, which remain out of scope. It changes no certified harness recipe.
