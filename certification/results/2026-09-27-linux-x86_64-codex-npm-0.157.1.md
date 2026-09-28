# Support decision: Codex 0.157.1 on Linux x86_64 (npm) — rejected

Recorded 2026-09-27 (issue #110). The npm-channel candidate of the
certified first scope ran the complete production gate on the real host
and was **rejected** at the Host Admission Probe: the npm layout's
sandbox re-exec target is absent inside the certified sandbox namespace,
so `bwrap` cannot launch the probe. The failure is structural to the
npm channel's installed layout on this artifact, not a transient host
or network condition, so the Codex Compatibility Set is unchanged.

## Candidate named before certification

- Manifest: `certification/candidates/codex-npm-0.157.1-linux-x86_64.json`
  (schema 2), selected from commit
  `cb681b588e49494c557b433b9baea6c2e6066d54`
- Version: 0.157.1; install channel: `npm`; official SHA-256:
  `61b0194f3bb6534439c8d26a3ed57d0805f84b884588b761795323eeb92fcf70`
- Official source: https://www.npmjs.com/package/@openai/codex — the
  digest anchors the package's `bin/codex.js` entry point, the file the
  npm install places on `PATH` and Factory resolves. Reviewed
  independently of the installed tree: the registry tarball's
  `dist.integrity` (sha512) was verified against the downloaded
  `@openai/codex` 0.157.1 tarball, the tarball was unpacked, and the
  unpacked `bin/codex.js` digest was compared with the installed file —
  both are `61b0194f…cf70`.
- Operating system: Linux; architecture: x86_64

## The complete gate result

The unmodified production Run Admission gate passed identity (the
installed artifact's digest equals the manifest digest) and reached the
Host Admission Probe, which failed:

```text
FACTORY_REAL_HOST_CERTIFICATION={"architecture":"x86_64","canonical_executable":"/home/ricardoiarced/.local/node-v24/lib/node_modules/@openai/codex/bin/codex.js","evidence":{"capability":"not-established","extension":"not-established","filesystem":"not-established","forbidden-guidance-suppression":"not-established","inference-run":"not-established","negative-control":"not-established","network":"not-established","project-spec-exclusivity":"not-established","runtime":"not-established","stale-version-refusal":"not-established","temporary-root":"not-established","wrong-digest-refusal":"not-established"},"factory_version":"0.1.1","failure":"Codex executable \"/home/ricardoiarced/.local/node-v24/lib/node_modules/@openai/codex/bin/codex.js\" Host Admission Probe failed: exit status 1; this host cannot enforce Project Data Isolation","filesystem_identity":{"device":2096,"inode":187369},"harness":"codex","harness_version":"0.157.1","host_identity":{"release":"6.18.33.2-microsoft-standard-WSL2","version":"#1 SMP PREEMPT_DYNAMIC Thu Jun 18 21:54:43 UTC 2026"},"host_reported_version":"0.157.1","install_channel":"npm","official_sha256":"61b0194f3bb6534439c8d26a3ed57d0805f84b884588b761795323eeb92fcf70","official_source":"https://www.npmjs.com/package/@openai/codex","operating_system":"Linux","production_checks":{"host-admission-probe":"not-established","identity":"not-established","newest-stable-verification":"not-established","permission-profile":"not-established","strict-configuration-inspectors":"not-established","strict-invocation-builder":"not-established"},"result":"rejected","schema":2,"selection_commit":"cb681b588e49494c557b433b9baea6c2e6066d54","selection_manifest":"certification/candidates/codex-npm-0.157.1-linux-x86_64.json"}
```

Host: Linux x86_64, kernel `6.18.33.2-microsoft-standard-WSL2`
(`#1 SMP PREEMPT_DYNAMIC Thu Jun 18 21:54:43 UTC 2026`). Canonical
executable:
`/home/ricardoiarced/.local/node-v24/lib/node_modules/@openai/codex/bin/codex.js`
(device 2096, inode 187369).

## The classified refusal

A faithful reproduction of the same probe invocation (identical
fixture, configuration, profile, and probe script) captured the stderr
the production path does not retain:

```text
bwrap: execvp /home/ricardoiarced/.local/node-v24/lib/node_modules/@openai/codex/node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex: No such file or directory
```

The mechanism: the certified permission profile reads the harness
runtime from exactly one path — the canonical executable's parent
directory (`…/@openai/codex/bin`). The npm artifact is a Node launcher
that dispatches to the vendored platform binary under
`…/@openai/codex/node_modules/@openai/codex-linux-x64/vendor/…`, and
that binary re-executes itself from its vendored path inside the
sandbox. The vendored subtree is outside the bound `bin` directory, so
the path does not exist inside the namespace and `bwrap` refuses before
any probe step runs (`exit status 1`). The github-asset tuple is
unaffected: its canonical executable is the self-contained musl binary
that the profile's runtime read already covers.

Extending the profile to bind the npm channel's resolved vendored
runtime subtree is a recipe amendment that requires its own isolation
decision; this certification run may not grant it.

## Decision and scope

The exact tuple (Codex 0.157.1, npm channel, digest `61b0194f…cf70`,
Linux, x86_64) is **not supported on the tested host**. The Codex
Compatibility Set is unchanged: it still contains only the
github-asset 0.157.1 tuple (issue #111). This rejection covers exactly
that tuple on exactly this host identity; it certifies nothing about
other npm versions, other architectures, or other operating systems.
