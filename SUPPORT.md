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
    "harness_version": "0.158.0",
    "install_channel": "github-asset",
    "official_sha256": "167c0148a849d2444f1b5a7fb5f8bb2de1de5ae13a2a504b833fc765980f5cd9",
    "operating_system": "Linux",
    "architecture": "x86_64"
  },
  {
    "harness": "claude",
    "harness_version": "2.1.283",
    "install_channel": "native-installer",
    "official_sha256": "1859583ce32920595c61ef868bee52e1b1594f7486db209935e01f1e5e804ae2",
    "operating_system": "Linux",
    "architecture": "x86_64"
  },
  {
    "harness": "claude",
    "harness_version": "2.1.283",
    "install_channel": "npm",
    "official_sha256": "1859583ce32920595c61ef868bee52e1b1594f7486db209935e01f1e5e804ae2",
    "operating_system": "Linux",
    "architecture": "x86_64"
  }
]
```

Exactly one Codex artifact has completed real-host certification: the
official `0.158.0` x86_64-unknown-linux-musl standalone executable, certified
on the tested Linux host recorded under `certification/results/`. The npm
channel of the prior `0.157.1` release was rejected because its sandbox
re-exec target fell outside the certified runtime read. The `0.158.0` npm
channel has no certification decision and remains unsupported. The package
still installs and serves help, version, and `factory project` administration
everywhere, and every Run that does not resolve to exactly this artifact on
an identical host fails closed before a Codex process starts: a different
version, digest, platform, canonical path, or filesystem identity is refused
with the matching diagnostic from the Codex Compatibility Set check, and a
host whose kernel identity or sandbox behavior changed is refused by the
Host Admission Probe.

Two Claude Code tuples have completed real-host certification, both on the
tested Linux host recorded under `certification/results/`: the
`native-installer` and `npm` channels of release `2.1.283`, whose artifacts
are the same upstream binary verified through each channel's official
distribution surface. Every Run that does not resolve to exactly one of
these tuples on an identical host fails closed before the Run Request is
submitted, and Factory never falls back to another Harness.

Certification decisions — certified or rejected — are recorded under
`certification/results/`. A rejected tuple stays unsupported until a fresh
real-host certification run establishes that exact artifact, operating
system, and architecture and a new recorded decision adds it to that
Harness's Compatibility Set. A failed certification never blocks building
and releasing the fail-closed package itself.
