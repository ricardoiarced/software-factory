# Real-host certification

This suite certifies or rejects one exact Codex executable and host tuple by
running Factory's complete production pre-launch path. It never launches an
inference Run and never adds a tuple to the production Compatibility Set.

Record the intended Codex version, official executable SHA-256 digest,
`platform.system()` value, and `platform.machine()` value independently before
running the suite. Do not derive the expected digest from the installed
candidate: the independently obtained official digest is the trust anchor that
prevents an executable fake from becoming certification evidence.

Run the suite explicitly from the Factory checkout:

```console
UV_CACHE_DIR=/tmp/software-factory-uv-cache \
uv run --no-project --with pytest --with platformdirs --with . \
python -m pytest certification -q -s \
  --real-host-certification \
  --codex-version 0.155.1 \
  --codex-official-sha256 <64-lowercase-hex-digest> \
  --codex-operating-system Linux \
  --codex-architecture x86_64
```

The `codex` resolved through `PATH` is the candidate. The suite requires all
four tuple fields and refuses malformed digests. It creates a fresh probe cache
for every run, then exercises production executable identity, strict invocation
construction, configuration inspectors, the permission profile, the complete
Host Admission Probe, and the final artifact recheck. No test-only permission
profile or executable adapter is involved.

The result is emitted as one line prefixed with
`FACTORY_REAL_HOST_CERTIFICATION=`. Its JSON document records the Factory and
Codex versions, official digest, OS, architecture, canonical executable,
filesystem identity, kernel identity, each required evidence property, and the
exact admission failure on rejection.

Ordinary `pytest` runs are restricted to `tests/` by `pyproject.toml`. Even when
the certification file is named directly, it skips unless
`--real-host-certification` is present. A successful result is evidence for a
separate support decision; only that reviewed decision may add the exact tuple
to Factory's production Compatibility Set.
