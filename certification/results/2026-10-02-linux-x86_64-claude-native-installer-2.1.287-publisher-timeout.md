# Claude Code 2.1.287 on Linux x86_64 (native-installer)

Recorded 2026-10-02 for issue #111. The candidate
`certification/candidates/claude-native-installer-2.1.287-linux-x86_64.json`,
committed at `61b215b247f116223162c7f0cf0ae47afc310a04` before the gate
ran, was rejected on this invocation by infrastructure alone.

The primary newest-stable verification and the executable identity
check had already passed, and the managed-policy gate and the Linux
sandbox dependency preflight were established, when the negative
control's separate newest-stable fetch to the Claude Code publisher
timed out and the run refused with `Factory could not reach the Claude
Code publisher to verify the newest stable release (timed out); check
connectivity and retry`. No Host Admission Probe spend occurred
(`host-admission-probe` and `strict-invocation-builder`
not-established). This establishes no host isolation evidence. No
policy or test was relaxed; a direct probe of the publisher endpoint
immediately afterwards returned HTTP 200 naming 2.1.287, and the
identical command was rerun for the certified result recorded in the
companion record of today.

## Rejected invocation

```console
UV_CACHE_DIR=/tmp/software-factory-uv-cache uv run --no-project --with pytest --with platformdirs --with . python -m pytest certification -q -s --tb=short --real-host-certification --harness-candidate certification/candidates/claude-native-installer-2.1.287-linux-x86_64.json
```

```text
FACTORY_REAL_HOST_CERTIFICATION={"architecture":"x86_64","canonical_executable":"/home/ricardoiarced/.local/share/claude/versions/2.1.287","evidence":{"capability":"not-established","extension":"not-established","filesystem":"not-established","forbidden-guidance-suppression":"not-established","inference-run":"not-established","managed-policy-refusal":"established","negative-control":"not-established","network":"not-established","project-spec-exclusivity":"not-established","runtime":"not-established","stale-version-refusal":"not-established","temporary-root":"not-established","wrong-digest-refusal":"established"},"factory_version":"0.1.2","failure":"the negative control refused with an unexpected diagnostic: Factory could not reach the Claude Code publisher to verify the newest stable release (timed out); check connectivity and retry","filesystem_identity":{"device":2096,"inode":108258},"harness":"claude","harness_version":"2.1.287","host_identity":{"release":"6.18.40.1-microsoft-standard-WSL2","version":"#1 SMP PREEMPT_DYNAMIC Fri Jul 31 22:12:15 UTC 2026"},"host_reported_version":"2.1.287","install_channel":"native-installer","official_sha256":"3920489a5109cff5786a1a392c25277408ff22bc796d5edb9c16a60e5a1718f0","official_source":"https://downloads.claude.ai/claude-code-releases/2.1.287/manifest.json","operating_system":"Linux","production_checks":{"host-admission-probe":"not-established","identity":"established","managed-policy-gate":"established","newest-stable-verification":"established","sandbox-dependency-preflight":"established","strict-invocation-builder":"not-established"},"result":"rejected","schema":2,"selection_commit":"61b215b247f116223162c7f0cf0ae47afc310a04","selection_manifest":"certification/candidates/claude-native-installer-2.1.287-linux-x86_64.json"}
```

This rejected invocation supports no tuple.
