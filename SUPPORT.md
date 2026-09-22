# Supported platforms

Factory release verification and platform support are separate claims. The
deterministic suite and the packaging smoke tests prove behavior and
packaging; they are never evidence that a host enforces Project Data
Isolation. Factory advertises support only for the exact tuples below — each
names one Codex version, one official executable SHA-256, one operating
system, and one architecture that completed real-host certification and that
production Run Admission embeds in `_codex._COMPATIBILITY_SET`. This
document and that embedded set are kept equal by
`tests/test_platform_claims.py`; no tuple is advertised without certified
real-host evidence, and certified evidence never generalizes from one host,
architecture, or Codex artifact to another.

## Advertised tuples

```json
[]
```

No Codex artifact has completed real-host certification, so Factory supports
no Run on any platform today. The package still installs and serves help,
version, and `factory project` administration, but every Run fails closed
before a Codex process starts. When a Codex executable resolves on `PATH`,
the refusal is:

```text
factory: error: Codex Compatibility Set is empty; no Run is supported.
```

and when none resolves, Run Admission refuses earlier with the exact
diagnostic for that host.

Certification decisions — certified or rejected — are recorded under
`certification/results/`. A rejected tuple stays unsupported until a fresh
real-host certification run establishes that exact artifact, operating
system, and architecture and a new recorded decision adds it to the Codex
Compatibility Set. A failed certification never blocks building and
releasing the fail-closed package itself.
