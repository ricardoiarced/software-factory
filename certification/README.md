# Real-host certification

This suite certifies or rejects one exact Codex executable and host tuple by
running Factory's complete production pre-launch path. It never launches an
inference Run and never adds a tuple to the production Compatibility Set.

Before running the suite, commit one candidate manifest beneath
`certification/candidates/`. The manifest records the intended Codex version,
official executable SHA-256 digest and its official release source,
`platform.system()` value, and `platform.machine()` value. Review that source
and digest independently; do not derive the expected digest from the installed
candidate. The suite requires the manifest bytes to match `HEAD`, making that
reviewed digest the trust anchor that prevents a runtime-provided executable
fake from becoming certification evidence.

```json
{
  "schema": 1,
  "codex_version": "0.155.1",
  "official_sha256": "<64-lowercase-hex-digest>",
  "official_source": "https://github.com/openai/codex/releases/...",
  "operating_system": "Linux",
  "architecture": "x86_64"
}
```

Run the suite explicitly from the Factory checkout:

```console
UV_CACHE_DIR=/tmp/software-factory-uv-cache \
uv run --no-project --with pytest --with platformdirs --with . \
python -m pytest certification -q -s \
  --real-host-certification \
  --codex-candidate certification/candidates/<candidate>.json
```

The `codex` resolved through `PATH` is the executable candidate. The suite
refuses uncommitted, modified, symlinked, misplaced, incomplete, or malformed
candidate manifests. It creates a fresh probe cache for every run, then
exercises production executable identity, strict invocation
construction, configuration inspectors, the permission profile, the complete
Host Admission Probe, and the final artifact recheck. No test-only permission
profile or executable adapter is involved.

The result is emitted as one line prefixed with
`FACTORY_REAL_HOST_CERTIFICATION=`. Its JSON document records the Factory
version, the selected and host-reported Codex versions, official digest and
source, manifest and commit identity, OS, architecture, canonical executable,
filesystem identity, kernel identity, each required evidence property, and the
exact admission failure on rejection.

Ordinary `pytest` runs are restricted to `tests/` by `pyproject.toml`. Even when
the certification file is named directly, it skips unless
`--real-host-certification` is present. A successful result is evidence for a
separate support decision; only that reviewed decision may add the exact tuple
to Factory's production Compatibility Set.

Each certification run — certified or rejected — gets one recorded decision
under `certification/results/`, naming the candidate manifest, the exact
failure or established evidence, and the decision's exact-tuple scope. A
named candidate without a recorded decision fails the deterministic suite.
