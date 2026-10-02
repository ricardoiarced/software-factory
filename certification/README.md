# Real-host certification

This suite certifies or rejects one exact Harness artifact and host tuple —
Codex or Claude Code — by running Factory's complete production pre-launch
path for the selected Harness. It never adds a tuple to a production
Compatibility Set.

Before running the suite, commit one Candidate Manifest beneath
`certification/candidates/`. The manifest names the exact tuple: the Harness,
its version and install channel, the official executable SHA-256 digest and
its official release source, the `platform.system()` value, and the
`platform.machine()` value. Review that source and digest independently; do
not derive the expected digest from the installed candidate. The suite
requires the manifest bytes to match `HEAD`, making that reviewed digest the
trust anchor that prevents a runtime-provided executable fake from becoming
certification evidence. Exactly one manifest schema exists; schema-1 Codex
manifests migrated when schema 2 landed.

```json
{
  "schema": 2,
  "harness": "codex",
  "harness_version": "0.160.0",
  "install_channel": "github-asset",
  "official_sha256": "<64-lowercase-hex-digest>",
  "official_source": "https://github.com/openai/codex/releases/tag/rust-v0.160.0",
  "operating_system": "Linux",
  "architecture": "x86_64"
}
```

The install channels of the certified first scope: Codex admits
`github-asset` and `npm`; Claude Code admits `native-installer` and `npm`.
Each channel names the Artifact Family whose official distribution surface
anchors the digest, and `official_source` must name that family — a
GitHub-asset digest cites the release, an npm digest cites the package page.
For npm channels the digest anchors the unpacked platform binary, and the
registry's `dist-integrity` field is cross-checked against it during
candidate review — that cross-check is review process, not a runtime check
of the suite.

Run the suite explicitly from the Factory checkout:

```console
UV_CACHE_DIR=/tmp/software-factory-uv-cache \
uv run --no-project --with pytest --with platformdirs --with . \
python -m pytest certification -q -s \
  --real-host-certification \
  --harness-candidate certification/candidates/<candidate>.json
```

The Harness executable resolved through `PATH` is the candidate. The suite
refuses uncommitted, modified, symlinked, misplaced, incomplete, malformed,
or unrecognized-channel candidate manifests, and each certification test
skips unless the selected manifest names its Harness. It creates a fresh
probe cache for every run, then exercises the production admission path —
executable identity, newest-stable verification, the compatibility-set
match, the negative controls, and, per Harness: strict invocation
construction, configuration inspectors, the permission profile, and the
complete Host Admission Probe for Codex; the uncached managed-policy gate,
the Linux sandbox dependency preflight, and the certified invocation for
Claude Code. No test-only permission profile or executable adapter is
involved.

The suite requires these evidence properties and production checks, recorded
per Harness in the emitted report:

- Probe-derived isolation properties — `filesystem`, `temporary-root`,
  `network`, `runtime`, `project-spec-exclusivity`, `extension`,
  `capability`, `negative-control` — established by the Codex probe.
- `inference-run`: a real Run through the production path performs the
  docstring edit inside the certified boundary — the only evidence that
  proves admission and launch together.
- The fixed refusal controls: `stale-version-refusal`,
  `wrong-digest-refusal`, `forbidden-guidance-suppression`, and, for Claude
  Code, `managed-policy-refusal`.
- Production checks: `identity`, `strict-invocation-builder`,
  `strict-configuration-inspectors`, `permission-profile`,
  `host-admission-probe`, `newest-stable-verification`, and, for Claude
  Code, `managed-policy-gate` and `sandbox-dependency-preflight`.

A Claude Code tuple runs the complete production path, including the
real-host Host Admission Probe. The certified recipe's inline settings
compute a sandbox read fence from the canonical Project Boundary at launch —
`denyRead` names the boundary's parent (parent and sibling data wherever the
boundary lives) and the home directory, `allowRead` keeps the boundary and
the installed Harness runtime readable, and `sandbox.credentials` denies
`~/.ssh` and `~/.aws` outright. The probe plants credential canaries at
those denied paths, expects every read of them to fail, and asserts from
observed evidence that no canary content reached the session. The uncached
managed-policy gate classifies each doctor-reported Managed-Policy Surface
strictly: present, configured, and uninspected surfaces refuse; a
not-fetched surface attributed to a custom `ANTHROPIC_BASE_URL` is an
unavailable surface that allows admission with a recorded residual-risk
notice; any other not-fetched wording or unrecognized shape refuses.

The result is emitted as one line prefixed with
`FACTORY_REAL_HOST_CERTIFICATION=`. Its JSON document records the Factory
version, the tuple identity — Harness, install channel, selected and
host-reported Harness versions, official digest and source, OS,
architecture — manifest and commit identity, canonical executable,
filesystem identity, kernel identity, each required evidence property and
production check, and the exact admission failure on rejection.

Ordinary `pytest` runs are restricted to `tests/` by `pyproject.toml`. Even when
the certification file is named directly, it skips unless
`--real-host-certification` is present. A successful result is evidence for a
separate support decision; only that reviewed decision may add the exact tuple
to Factory's production Compatibility Set.

Each certification run — certified or rejected — gets one recorded
Certification Record under `certification/results/`, naming the candidate
manifest, the exact failure or established evidence, and the decision's
exact-tuple scope. New records disambiguate the channel in the filename, for
example `2026-09-24-linux-x86_64-codex-npm-0.156.1.md`; existing records keep
their historical names. A named candidate without a recorded decision fails
the deterministic suite.
