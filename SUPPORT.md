# Supported platforms

Factory release verification and platform support are separate claims. The
deterministic suite and the packaging smoke tests prove behavior and
packaging; they are never evidence that a host enforces Project Data
Isolation. Factory advertises support only for the exact tuples below — each
names one Harness, one Harness version, one artifact family install channel,
one official executable SHA-256, one operating system, and one architecture
that completed real-host certification and that production Run Admission
embeds in `_codex._COMPATIBILITY_SET` or `_claude._CLAUDE_COMPATIBILITY_SET`.
This document and those embedded sets are kept equal by
`tests/test_platform_claims.py`; no tuple is advertised without certified
real-host evidence, and certified evidence never generalizes from one host,
architecture, or Harness artifact to another.

## Advertised tuples

```json
[
  {
    "harness": "codex",
    "harness_version": "0.157.1",
    "install_channel": "github-asset",
    "official_sha256": "3e2584f3f3829a43a0495011a1cecb2facbe64a2403e2b682351fd9c2983f970",
    "operating_system": "Linux",
    "architecture": "x86_64"
  }
]
```

Exactly one Codex artifact has completed real-host certification: the
official `0.157.1` x86_64-unknown-linux-musl standalone executable, certified
on the tested Linux host recorded under `certification/results/`. The package
still installs and serves help, version, and `factory project` administration
everywhere, and every Run that does not resolve to exactly this artifact on
an identical host fails closed before a Codex process starts: a different
version, digest, platform, canonical path, or filesystem identity is refused
with the matching diagnostic from the Codex Compatibility Set check, and a
host whose kernel identity or sandbox behavior changed is refused by the
Host Admission Probe.

Selecting Claude Code with `--harness claude` always fails closed today: no
Claude Code release has completed real-host certification, so the Claude
Compatibility Set is empty and every selected Run is refused before the Run
Request is submitted. Factory never falls back to another Harness.

Certification decisions — certified or rejected — are recorded under
`certification/results/`. A rejected tuple stays unsupported until a fresh
real-host certification run establishes that exact artifact, operating
system, and architecture and a new recorded decision adds it to the Codex
Compatibility Set. A failed certification never blocks building and
releasing the fail-closed package itself.
