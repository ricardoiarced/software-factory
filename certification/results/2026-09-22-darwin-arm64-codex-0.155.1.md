# Support decision: Codex 0.155.1 on macOS arm64 — unsupported

Recorded 2026-09-22 (issue #45). The named candidate tuple is rejected; the
Codex Compatibility Set remains empty and no Run is supported.

## Candidate named before certification

- Manifest: `certification/candidates/darwin-arm64.json` (commit `a3b6486b8a39f219d92de61c7c9a9617ad64f7f6`)
- Codex version: 0.155.1
- Official SHA-256: `8eaf1ad12fe6bf89b1710330f58900014322c7c5af677e43be116d8ac5fc0a9e`
- Official source: https://github.com/openai/codex/releases/tag/rust-v0.155.1
  (asset `codex-aarch64-apple-darwin.tar.gz`; the digest was computed from the
  Mach-O arm64 executable inside that official asset and reviewed
  independently of anything installed on a host)
- Operating system: Darwin; architecture: arm64 (the `platform.system()` /
  `platform.machine()` values an Apple Silicon macOS host reports)

The issue text names Codex 0.154.0 as the macOS candidate. Production Run
Admission has exactly one certification policy, for 0.155.1
(`_codex._Codex01551Policy`), so a 0.154.0 manifest could never pass the gate
on any host — 0.155.1 is the only version the complete gate can exercise.
macOS x86_64 is a different tuple and is deliberately not named here.

## What ran, and what it could not establish

No macOS host is available to this repository: the only real host is Linux
x86_64 under WSL2. The complete real-host gate therefore ran there, with no
waiver, from the Factory checkout whose `HEAD` was the manifest commit
`a3b6486` (parent `69f3727`, the merge-base with `main`) — the suite requires
the manifest to match `HEAD` exactly:

```console
UV_CACHE_DIR=/tmp/software-factory-uv-cache \
uv run --no-project --with pytest --with platformdirs --with . \
python -m pytest certification -q -s \
  --real-host-certification \
  --codex-candidate certification/candidates/darwin-arm64.json
```

Host: Linux x86_64, kernel `6.18.33.2-microsoft-standard-WSL2`
(`#1 SMP PREEMPT_DYNAMIC Thu Jun 18 21:54:43 UTC 2026`). Canonical
executable: `/home/ricardoiarced/.codex/packages/standalone/releases/0.155.1-x86_64-unknown-linux-musl/bin/codex`
(device 2096, inode 13422). The suite ran unmodified production Run
Admission; no check was waived, relaxed, or skipped.

This run does **not** exercise macOS sandbox enforcement (Seatbelt). Every
evidence property and production check is `not-established` with respect to
macOS: the gate resolved the Linux executable this host provides and rejected
the tuple there. The macOS tuple is unsupported because certification was
never established — not because a macOS property failed on a macOS host.

## Exact failure

The identity property failed before any other evidence was established:

> Codex executable "/home/ricardoiarced/.codex/packages/standalone/releases/0.155.1-x86_64-unknown-linux-musl/bin/codex" reported an unrecognized version; install a certified Codex release

Emitted report (verbatim):

```text
FACTORY_REAL_HOST_CERTIFICATION={"architecture":"arm64","canonical_executable":"/home/ricardoiarced/.codex/packages/standalone/releases/0.155.1-x86_64-unknown-linux-musl/bin/codex","codex_version":"0.155.1","evidence":{"capability":"not-established","extension":"not-established","filesystem":"not-established","negative-control":"not-established","network":"not-established","project-spec-exclusivity":"not-established","runtime":"not-established","temporary-root":"not-established"},"factory_version":"0.1.0","failure":"Codex executable \"/home/ricardoiarced/.codex/packages/standalone/releases/0.155.1-x86_64-unknown-linux-musl/bin/codex\" reported an unrecognized version; install a certified Codex release","filesystem_identity":{"device":2096,"inode":13422},"host_identity":{"release":"6.18.33.2-microsoft-standard-WSL2","version":"#1 SMP PREEMPT_DYNAMIC Thu Jun 18 21:54:43 UTC 2026"},"host_reported_version":null,"official_sha256":"8eaf1ad12fe6bf89b1710330f58900014322c7c5af677e43be116d8ac5fc0a9e","official_source":"https://github.com/openai/codex/releases/tag/rust-v0.155.1","operating_system":"Darwin","production_checks":{"host-admission-probe":"not-established","identity":"not-established","permission-profile":"not-established","strict-configuration-inspectors":"not-established","strict-invocation-builder":"not-established"},"result":"rejected","schema":1,"selection_commit":"a3b6486b8a39f219d92de61c7c9a9617ad64f7f6","selection_manifest":"certification/candidates/darwin-arm64.json"}
```

## Root cause

The same identity barrier recorded for Linux (#44): production identity
requires `--version` output matching `codex <version>`
(`_codex._VERSION_OUTPUT`), while every official Codex release reports
`codex-cli <version>`. Amending the identity banner is tracked as its own
production change (#92) and would need its own tests, review, a fresh
certification run, and a new recorded decision.

Establishing the macOS tuple additionally requires a real macOS host that
enforces Seatbelt: no Linux run — including this one — and no other macOS
architecture can substitute for that gate.

## Decision and scope

The tuple (Codex 0.155.1, official digest `8eaf1ad1…0a9e`, Darwin, arm64) is
**unsupported**. `_codex._COMPATIBILITY_SET` stays empty. This decision
covers exactly that tuple on exactly this host identity: it certifies nothing
for macOS x86_64, any other Codex version or digest, or any other operating
system. The Linux rejection (#44) and this rejection are independent records;
neither substitutes for the other, and only a fresh gate run on a real macOS
host can produce contrary evidence for this tuple.
