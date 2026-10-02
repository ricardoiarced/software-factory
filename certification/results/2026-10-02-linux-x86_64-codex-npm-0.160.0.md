# Support decision: Codex 0.160.0 on Linux x86_64 (npm) — certified

Recorded 2026-10-02 for issue #111, the v0.1.2 recertification cycle at
Codex 0.160.0. The committed candidate completed the unchanged real-host
production gate: every required evidence property and production check
was established, including an inference Run that added a docstring
inside the disposable Project Boundary. This record authorizes the
exact tuple below in the Codex Compatibility Set.

## Reviewed candidate

- Manifest:
  `certification/candidates/codex-npm-0.160.0-linux-x86_64.json`,
  selected at commit `61b215b247f116223162c7f0cf0ae47afc310a04`.
- Official source: https://www.npmjs.com/package/@openai/codex. The
  `@openai/codex` 0.160.0 wrapper's platform package is now published
  as an alias: the registry resolves `@openai/codex-linux-x64` to
  `npm:@openai/codex@0.160.0-linux-x64`, and the formerly standalone
  platform package 404s. The unpacked folder name remains
  `@openai/codex-linux-x64`, so the installed-tree layout and the
  `_NPM_PLATFORM_LAYOUT` matching are unaffected — recorded as an
  observation, with no production-layout change. The platform
  tarball's registry integrity is
  `sha512-KI/73OqGrHmR18s7ya7E1NqV6rT0y3lxr0s8S1qR2m6zU6QRF/HlR529jALLh5vjdUnsRT4Ahoxt0axb4kY99g==`;
  the independently downloaded tarball matched it, and its unpacked
  `package/vendor/x86_64-unknown-linux-musl/bin/codex` SHA-256 was
  `12eb3e81114588aca3b7998f4f19e8997b056aca08e57a7ca7c8a3ec8c652aad`,
  identical to the separately verified GitHub release asset binary
  (its companion record of today) and to the installed npm binary.
- Exact tuple: Codex 0.160.0, `npm`, Linux x86_64, binary digest
  `12eb3e81114588aca3b7998f4f19e8997b056aca08e57a7ca7c8a3ec8c652aad`.

## Complete gate result

Command: `PATH=/home/ricardoiarced/.local/node-v24/bin:$PATH UV_CACHE_DIR=/tmp/software-factory-uv-cache uv run --no-project --with pytest --with platformdirs --with . python -m pytest certification -q -s --tb=short --real-host-certification --harness-candidate certification/candidates/codex-npm-0.160.0-linux-x86_64.json`

```text
FACTORY_REAL_HOST_CERTIFICATION={"architecture":"x86_64","canonical_executable":"/home/ricardoiarced/.local/node-v24/lib/node_modules/@openai/codex/node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex","evidence":{"capability":"established","extension":"established","filesystem":"established","forbidden-guidance-suppression":"established","inference-run":"established","negative-control":"established","network":"established","project-spec-exclusivity":"established","runtime":"established","stale-version-refusal":"established","temporary-root":"established","wrong-digest-refusal":"established"},"factory_version":"0.1.2","filesystem_identity":{"device":2096,"inode":110184},"harness":"codex","harness_version":"0.160.0","host_identity":{"release":"6.18.40.1-microsoft-standard-WSL2","version":"#1 SMP PREEMPT_DYNAMIC Fri Jul 31 22:12:15 UTC 2026"},"host_reported_version":"0.160.0","install_channel":"npm","official_sha256":"12eb3e81114588aca3b7998f4f19e8997b056aca08e57a7ca7c8a3ec8c652aad","official_source":"https://www.npmjs.com/package/@openai/codex","operating_system":"Linux","production_checks":{"host-admission-probe":"established","identity":"established","newest-stable-verification":"established","permission-profile":"established","strict-configuration-inspectors":"established","strict-invocation-builder":"established"},"result":"certified","schema":2,"selection_commit":"61b215b247f116223162c7f0cf0ae47afc310a04","selection_manifest":"certification/candidates/codex-npm-0.160.0-linux-x86_64.json"}
```

The tested executable was at the canonical path in the report, device
2096, inode 110184, on kernel `6.18.40.1-microsoft-standard-WSL2`
(`#1 SMP PREEMPT_DYNAMIC Fri Jul 31 22:12:15 UTC 2026`), Ubuntu 24.04.3
(WSL2). The gate verified newest stable 0.160.0 live and exercised a
fresh Host Admission Probe without waived checks. The exact tuple is
**supported on this tested host identity**; this decision does not
extend to another digest, version, channel, operating system,
architecture, or host identity.
