# Support decision: Codex 0.160.0 on Linux x86_64 (github-asset) — certified

Recorded 2026-10-02 for issue #111. Both Codex publishers moved past the
certified set (npm `latest` and GitHub `releases/latest` named 0.160.0
against the embedded 0.158.0), so the release re-certified all four
first-scope tuples at the new versions and replaced the sets wholesale.
The committed candidate completed the unchanged real-host production
gate: every required evidence property and production check was
established, including an inference Run that added a docstring inside
the disposable Project Boundary. This record authorizes the exact tuple
below in the Codex Compatibility Set.

## Reviewed candidate

- Manifest:
  `certification/candidates/codex-github-asset-0.160.0-linux-x86_64.json`,
  selected at commit `61b215b247f116223162c7f0cf0ae47afc310a04`.
- Official source:
  https://github.com/openai/codex/releases/tag/rust-v0.160.0. The
  release API's asset digest for `codex-x86_64-unknown-linux-musl.tar.gz`
  is `sha256:306865417d4ee7a927785852910a527f41e1e159add390ac5ae3accb67d44a13`;
  the independently downloaded archive matched it, and its unpacked
  binary SHA-256 was `12eb3e81114588aca3b7998f4f19e8997b056aca08e57a7ca7c8a3ec8c652aad`,
  the exact manifest digest and the digest of the installed binary.
  The npm platform package at 0.160.0 vendors a byte-identical binary
  (the issue #150 finding holds at 0.160.0; both channels therefore
  distribute the same upstream artifact through independently verified
  surfaces).
- Configured feature inventory: `tests/data/codex-0.160.0-features.txt`,
  pinned by SHA-256
  `6aef61c26e6caff34cb888bf781a4b36e4e3813f9595b308a5226f6cb2e6063e`.
  Against 0.158.0, three new features appeared, all in an
  under-development state; the twelve isolation-relevant features keep
  their required states.
- Exact tuple: Codex 0.160.0, `github-asset`, Linux x86_64, binary
  digest `12eb3e81114588aca3b7998f4f19e8997b056aca08e57a7ca7c8a3ec8c652aad`.

## Complete gate result

Command: `UV_CACHE_DIR=/tmp/software-factory-uv-cache uv run --no-project --with pytest --with platformdirs --with . python -m pytest certification -q -s --tb=short --real-host-certification --harness-candidate certification/candidates/codex-github-asset-0.160.0-linux-x86_64.json`

```text
FACTORY_REAL_HOST_CERTIFICATION={"architecture":"x86_64","canonical_executable":"/home/ricardoiarced/.codex/packages/standalone/releases/0.160.0-x86_64-unknown-linux-musl/bin/codex","evidence":{"capability":"established","extension":"established","filesystem":"established","forbidden-guidance-suppression":"established","inference-run":"established","negative-control":"established","network":"established","project-spec-exclusivity":"established","runtime":"established","stale-version-refusal":"established","temporary-root":"established","wrong-digest-refusal":"established"},"factory_version":"0.1.2","filesystem_identity":{"device":2096,"inode":110018},"harness":"codex","harness_version":"0.160.0","host_identity":{"release":"6.18.40.1-microsoft-standard-WSL2","version":"#1 SMP PREEMPT_DYNAMIC Fri Jul 31 22:12:15 UTC 2026"},"host_reported_version":"0.160.0","install_channel":"github-asset","official_sha256":"12eb3e81114588aca3b7998f4f19e8997b056aca08e57a7ca7c8a3ec8c652aad","official_source":"https://github.com/openai/codex/releases/tag/rust-v0.160.0","operating_system":"Linux","production_checks":{"host-admission-probe":"established","identity":"established","newest-stable-verification":"established","permission-profile":"established","strict-configuration-inspectors":"established","strict-invocation-builder":"established"},"result":"certified","schema":2,"selection_commit":"61b215b247f116223162c7f0cf0ae47afc310a04","selection_manifest":"certification/candidates/codex-github-asset-0.160.0-linux-x86_64.json"}
```

The tested executable was at the canonical path in the report, device
2096, inode 110018, on kernel `6.18.40.1-microsoft-standard-WSL2`
(`#1 SMP PREEMPT_DYNAMIC Fri Jul 31 22:12:15 UTC 2026`), Ubuntu 24.04.3
(WSL2) — a kernel update since the 2026-09-28 records' host identity,
recorded fresh here as the gate observed it. The gate verified newest
stable 0.160.0 live and exercised a fresh Host Admission Probe without
waived checks. The exact tuple is **supported on this tested host
identity**; this decision does not extend to another digest, version,
channel, operating system, architecture, or host identity.
