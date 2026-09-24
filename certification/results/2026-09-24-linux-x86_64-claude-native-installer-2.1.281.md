# Support decision: Claude Code 2.1.281 on Linux x86_64 — unsupported

Recorded 2026-09-24 (issue #105). The newest stable Claude Code artifact ran
the complete Host Admission Probe on this host and **failed Project Data
Isolation**: sandboxed Bash reads outside the Project Boundary succeed. The
Claude Compatibility Set remains empty and no Run is supported. Certification
stays blocked by the two decisions raised from this probe (#113: whether the
sandbox read model can satisfy Project Data Isolation at all; #114: how to
classify the doctor's not-fetched managed-policy notices).

## Artifact and real host

- Harness: Claude Code, install channel `native-installer`, version 2.1.281 —
  verified newest stable live (npm `latest` = GitHub `v2.1.281` = the
  `downloads.claude.ai` pointer, all three agreed)
- Official SHA-256:
  `56fe3da88458465fb27d7e9299dddb3fead55750fb9c2de795f233b5eea6dce1`
- Canonical executable: `~/.local/share/claude/versions/2.1.281`
  (device 2096, inode 224631)
- Host: Linux x86_64, kernel `6.18.33.2-microsoft-standard-WSL2`
  (`#1 SMP PREEMPT_DYNAMIC Thu Jun 18 21:54:43 UTC 2026`), Ubuntu 24.04.3
  (WSL2); bubblewrap 0.9.0 with working user namespaces; `socat` installed
- Factory 0.1.0; probe launched with the exact production invocation of the
  #102 candidate recipe and its pinned nonessential-traffic environment

No candidate manifest was committed for this tuple: the probe failed before
any candidate could be reviewed for certification, so this record predates
the manifest a future candidate would carry.

## What the probe found

Ground-truth diagnostic Run (Claude Code 2.1.281, sandbox fail-closed
settings from `_claude._ClaudePolicy._settings_document`):

| Command | Result |
|---|---|
| `cat ../outside-read.txt` (parent of boundary) | **read succeeded** → `outside-boundary` |
| `printf 'probe-write' > ../confirm-outside-write.txt` | denied — `Read-only file system` |
| `printf 'probe-write' > /tmp/.factory-groundtruth-tmp-write.txt` | denied — `Read-only file system` |
| `cat /tmp/.factory-groundtruth-outside-read.txt` | **read succeeded** → `outside-boundary` |

Claude Code's sandbox enforces a read-only host filesystem plus a writable
workspace: writes outside are denied, but reads outside the boundary through
model-generated shell commands are permitted. The native tools honor
`blockReadsOutsideWorkingDirectories`; shell commands do not. The probe
refused: `step bash-read-parent was recorded 'succeeded' instead of
'failed'`. The probe fixture itself (`_build_probe_fixture`) worked; the
candidate recipe is what failed.

`claude doctor` (exit 0) also prints
`Managed settings (remote): not fetched — not available with a custom
ANTHROPIC_BASE_URL` and `Organization policy: not fetched with a custom
ANTHROPIC_BASE_URL`, both of which match the #104 gate's markers, so the
gate refuses this host as classified while the custom endpoint is set.
Whether those notices are a detected source or an unavailable surface is
#114's decision. One probe Run also refused fail-closed without writing its
report; failures are correctly never cached
(`~/.cache/factory/host-admission-probe/` stayed empty).

Full evidence: the [#105 resolution](https://github.com/ricardoiarced/software-factory/issues/105#issuecomment-5819503451).
