# Codex 0.158.0 on Linux x86_64 (github-asset)

Recorded 2026-09-28 for [Recertify Codex at its newest stable release](https://github.com/ricardoiarced/software-factory/issues/111). The candidate is `certification/candidates/linux-x86_64.json`, committed at `3f678e9c6cc8174346c43e2eeb11c27ffc3499af` before the gate ran.

GitHub `releases/latest` and npm `latest` both named 0.158.0. GitHub's asset digest for `codex-x86_64-unknown-linux-musl.tar.gz` was `af9f5aa6e6662accf9d707cef0d9ca083880a173a9c2b6c22947edb7830e5778`; the independently downloaded archive matched. The executable unpacked from it hashed to `167c0148a849d2444f1b5a7fb5f8bb2de1de5ae13a2a504b833fc765980f5cd9`, matching the installed binary. The configured 0.158.0 feature output is saved in `tests/data/codex-0.158.0-features.txt` and pinned by SHA-256 `5a04b7dbaefad3228c33429f7bdd8f488226864c1ec70ddcf9ebc1b3e647aed5`; every required isolation-relevant feature has the required state.

## Certified real-host invocation

The complete production path passed on the recorded Linux x86_64 host: exact artifact identity, live newest-stable lookup, strict configuration inspection, permission profile, Host Admission Probe, fixed refusal controls, and a real inference Run that added the requested docstring inside the project boundary. Every required evidence property and production check was established. The negative controls refused as expected. The suite reported `1 passed, 1 skipped` (the Claude selection skipped).

```console
UV_CACHE_DIR=/tmp/software-factory-uv-cache uv run --no-project --with pytest --with platformdirs --with . python -m pytest certification -q -s --tb=short --real-host-certification --harness-candidate certification/candidates/linux-x86_64.json
```

```text
FACTORY_REAL_HOST_CERTIFICATION={"architecture":"x86_64","canonical_executable":"/home/ricardoiarced/.codex/packages/standalone/releases/0.158.0-x86_64-unknown-linux-musl/bin/codex","evidence":{"capability":"established","extension":"established","filesystem":"established","forbidden-guidance-suppression":"established","inference-run":"established","negative-control":"established","network":"established","project-spec-exclusivity":"established","runtime":"established","stale-version-refusal":"established","temporary-root":"established","wrong-digest-refusal":"established"},"factory_version":"0.1.2","filesystem_identity":{"device":2096,"inode":364055},"harness":"codex","harness_version":"0.158.0","host_identity":{"release":"6.18.33.2-microsoft-standard-WSL2","version":"#1 SMP PREEMPT_DYNAMIC Thu Jun 18 21:54:43 UTC 2026"},"host_reported_version":"0.158.0","install_channel":"github-asset","official_sha256":"167c0148a849d2444f1b5a7fb5f8bb2de1de5ae13a2a504b833fc765980f5cd9","official_source":"https://github.com/openai/codex/releases/tag/rust-v0.158.0","operating_system":"Linux","production_checks":{"host-admission-probe":"established","identity":"established","newest-stable-verification":"established","permission-profile":"established","strict-configuration-inspectors":"established","strict-invocation-builder":"established"},"result":"certified","schema":2,"selection_commit":"3f678e9c6cc8174346c43e2eeb11c27ffc3499af","selection_manifest":"certification/candidates/linux-x86_64.json"}
```

## Support decision

The exact Codex 0.158.0 github-asset executable digest on this Linux x86_64 host is certified. The production Codex Compatibility Set replaces the stale 0.157.1 tuple wholesale with this tuple and pins its canonical path, device, and inode. This record does not certify the npm channel, Darwin arm64, or any other host identity.
