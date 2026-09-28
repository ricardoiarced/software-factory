# Support decision: Codex 0.158.0 on Linux x86_64 (github-asset) — certified

Recorded 2026-09-28 for issue #110. The committed candidate completed the unchanged real-host production gate. Every required evidence property and production check was established, including an inference Run that added a docstring inside the disposable Project Boundary. This record authorizes the exact tuple below in the Codex Compatibility Set.

## Reviewed candidate

- Manifest: `certification/candidates/codex-github-asset-0.158.0-linux-x86_64.json`, selected at commit `290f90b7df3283ed72117be1a664a20a96fbcce9`.
- Official source: https://github.com/openai/codex/releases/tag/rust-v0.158.0, asset `codex-x86_64-unknown-linux-musl.tar.gz`. The publisher's archive SHA-256 was `af9f5aa6e6662accf9d707cef0d9ca083880a173a9c2b6c22947edb7830e5778`; the downloaded archive matched. Its unpacked binary SHA-256 was `167c0148a849d2444f1b5a7fb5f8bb2de1de5ae13a2a504b833fc765980f5cd9`, the manifest digest, reviewed independently of the installed executable.
- Exact tuple: Codex 0.158.0, `github-asset`, Linux x86_64, binary digest `167c0148a849d2444f1b5a7fb5f8bb2de1de5ae13a2a504b833fc765980f5cd9`.

## Complete gate result

Command: `PATH=/home/ricardoiarced/.codex/packages/standalone/releases/0.158.0-x86_64-unknown-linux-musl/bin:$PATH python -m pytest certification -q -s --tb=short --real-host-certification --harness-candidate certification/candidates/codex-github-asset-0.158.0-linux-x86_64.json`

```text
FACTORY_REAL_HOST_CERTIFICATION={"architecture":"x86_64","canonical_executable":"/home/ricardoiarced/.codex/packages/standalone/releases/0.158.0-x86_64-unknown-linux-musl/bin/codex","evidence":{"capability":"established","extension":"established","filesystem":"established","forbidden-guidance-suppression":"established","inference-run":"established","negative-control":"established","network":"established","project-spec-exclusivity":"established","runtime":"established","stale-version-refusal":"established","temporary-root":"established","wrong-digest-refusal":"established"},"factory_version":"0.1.1","filesystem_identity":{"device":2096,"inode":364055},"harness":"codex","harness_version":"0.158.0","host_identity":{"release":"6.18.33.2-microsoft-standard-WSL2","version":"#1 SMP PREEMPT_DYNAMIC Thu Jun 18 21:54:43 UTC 2026"},"host_reported_version":"0.158.0","install_channel":"github-asset","official_sha256":"167c0148a849d2444f1b5a7fb5f8bb2de1de5ae13a2a504b833fc765980f5cd9","official_source":"https://github.com/openai/codex/releases/tag/rust-v0.158.0","operating_system":"Linux","production_checks":{"host-admission-probe":"established","identity":"established","newest-stable-verification":"established","permission-profile":"established","strict-configuration-inspectors":"established","strict-invocation-builder":"established"},"result":"certified","schema":2,"selection_commit":"290f90b7df3283ed72117be1a664a20a96fbcce9","selection_manifest":"certification/candidates/codex-github-asset-0.158.0-linux-x86_64.json"}
```

The tested executable was at the canonical path in the report, device 2096, inode 364055, on kernel `6.18.33.2-microsoft-standard-WSL2`. The gate verified newest stable live and exercised a fresh Host Admission Probe without waived checks. The exact tuple is **supported on this tested host identity**; this decision does not extend to another digest, version, channel, operating system, architecture, or host identity.

An earlier attempt from `/tmp/factory-codex-01580/github/bin/codex` refused at the Host Admission Probe with `exit status 1`: bubblewrap could not execute the binary under the denied temporary root (`Permission denied`). Its full emitted report was not retained, so that attempt establishes no evidence and is documented here only as an audit note. The complete result above used the installed official standalone path.
