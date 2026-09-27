# Support decision: Codex 0.157.1 on Linux x86_64 (github-asset) — certified

Recorded 2026-09-27 (issue #111). The Codex publisher moved newest stable to
0.157.1 (GitHub `releases/latest` and the npm `latest` dist-tag agree), so the
live newest-stable gate refused every Run of the stale 0.155.1 tuple. This
recertification replaces the Codex Compatibility Set wholesale: the 0.155.1
tuple leaves the set and only the tuple below is supported.

## Candidate named before certification

- Manifest: `certification/candidates/linux-x86_64.json` (schema 2), selected
  from commit `82969d55303374eeea11775ec1eeea390f065b5f`
- Version: 0.157.1; install channel: `github-asset`; official SHA-256:
  `3e2584f3f3829a43a0495011a1cecb2facbe64a2403e2b682351fd9c2983f970`
- Official source: https://github.com/openai/codex/releases/tag/rust-v0.157.1
  (asset `codex-x86_64-unknown-linux-musl.tar.gz`). The digest was reviewed
  independently of the installed binary: GitHub's per-asset digest was
  checked against the downloaded asset, the asset was unpacked, and the
  unpacked executable's SHA-256 was computed before comparison with the
  installed artifact — both are `3e2584f3…f970`.
- Operating system: Linux; architecture: x86_64

The release's feature inventory changed, so the policy's full-output pin was
re-derived from the real artifact under the certified configuration and the
required states were re-verified: every isolation-relevant feature remains
off, `skip_host_skill_discovery` remains on, and `mcp list --json` reports
no servers. The inventory fixture `tests/data/codex-0.157.1-features.txt` is
the artifact's real output; its SHA-256 is the policy pin.

## Complete gate result

The complete unmodified production Run Admission gate passed against the
installed official artifact:

```console
PATH=/home/ricardoiarced/.codex/packages/standalone/releases/0.157.1-x86_64-unknown-linux-musl/bin:$PATH \
UV_CACHE_DIR=/tmp/software-factory-uv-cache \
uv run --no-project --with pytest --with platformdirs --with . \
python -m pytest certification -q -s --tb=short \
  --real-host-certification \
  --harness-candidate certification/candidates/linux-x86_64.json
```

Host: Linux x86_64, kernel `6.18.33.2-microsoft-standard-WSL2`
(`#1 SMP PREEMPT_DYNAMIC Thu Jun 18 21:54:43 UTC 2026`). Canonical
executable:
`/home/ricardoiarced/.codex/packages/standalone/releases/0.157.1-x86_64-unknown-linux-musl/bin/codex`
(device 2096, inode 140479). The suite ran unmodified production Run
Admission; no check was waived, relaxed, or skipped. The suite's first run
at commit `c51ab481167738e7a1e7649d8871723415daf173` already passed every
production check and control; the only change afterwards was recording the
probe-derived evidence properties the passing probe had established
(mirroring the Claude branch), and the gate was re-run at the selection
commit.

The strict-configuration inspectors, the permission profile, the complete
Host Admission Probe (parent, sibling, home, credential, and temporary-root
denials; runtime and network denials; Project Spec Exclusivity suppression;
the malformed-profile negative control), the stale-version and wrong-digest
refusal controls, forbidden-guidance suppression, and a real inference Run
that performed the docstring edit inside the certified boundary are all
recorded in the emitted report.

```text
FACTORY_REAL_HOST_CERTIFICATION={"architecture":"x86_64","canonical_executable":"/home/ricardoiarced/.codex/packages/standalone/releases/0.157.1-x86_64-unknown-linux-musl/bin/codex","evidence":{"capability":"established","extension":"established","filesystem":"established","forbidden-guidance-suppression":"established","inference-run":"established","negative-control":"established","network":"established","project-spec-exclusivity":"established","runtime":"established","stale-version-refusal":"established","temporary-root":"established","wrong-digest-refusal":"established"},"factory_version":"0.1.1","filesystem_identity":{"device":2096,"inode":140479},"harness":"codex","harness_version":"0.157.1","host_identity":{"release":"6.18.33.2-microsoft-standard-WSL2","version":"#1 SMP PREEMPT_DYNAMIC Thu Jun 18 21:54:43 UTC 2026"},"host_reported_version":"0.157.1","install_channel":"github-asset","official_sha256":"3e2584f3f3829a43a0495011a1cecb2facbe64a2403e2b682351fd9c2983f970","official_source":"https://github.com/openai/codex/releases/tag/rust-v0.157.1","operating_system":"Linux","production_checks":{"host-admission-probe":"established","identity":"established","newest-stable-verification":"established","permission-profile":"established","strict-configuration-inspectors":"established","strict-invocation-builder":"established"},"result":"certified","schema":2,"selection_commit":"82969d55303374eeea11775ec1eeea390f065b5f","selection_manifest":"certification/candidates/linux-x86_64.json"}
```

## Decision and scope

The exact tuple (Codex 0.157.1, github-asset channel, digest `3e2584f3…f970`,
Linux, x86_64) is **supported on the tested host**. The Codex Compatibility
Set contains only this policy and also pins the certified canonical path and
filesystem identity. This decision covers exactly that tuple on exactly this
host identity: it certifies nothing for any other Codex version or digest,
the npm channel, any other Linux architecture, generic Linux support, or any
other operating system. The macOS arm64 tuple remains unsupported (its
2026-09-22 rejection stands; no macOS host has run the gate).
