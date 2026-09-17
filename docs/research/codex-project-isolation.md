# Codex project isolation for Factory

_Research date: 2026-09-16. Tested with Codex CLI 0.154.0 on Linux. macOS conclusions come from current official documentation and source, not a local macOS run._

## Decision

Current Codex can enforce a strong **project-data boundary** for model-generated shell commands, but it cannot support the literal claim that the Codex process reads and writes only the selected Project Boundary.

Factory can safely promise this narrower contract:

> Codex shell commands may read and write the selected Project Boundary. Reads and writes to parent and sibling project data are denied. A small Codex-defined runtime set and the installed Codex executable remain readable so commands can run. Factory suppresses project `AGENTS.md` discovery and injects `.agent/spec.md` itself.

The literal contract is unavailable for three reasons:

1. Named permission profiles are beta and require `:minimal = "read"` for common tools. OpenAI's own workspace-only example describes those as limited exceptions outside the workspace.[^permissions]
2. The command sandbox governs model-generated local commands. Codex host features such as instruction discovery, skills, apps, plugins, MCP servers, Browser Use, and Computer Use have separate loading and policy paths. For example, the configuration reference says app traffic is not controlled by the sandboxed-command network proxy.[^config]
3. There is no supported `codex exec` flag that disables every instruction and extension source while retaining arbitrary user configuration. `--ignore-user-config` ignores `$CODEX_HOME/config.toml` and retains authentication, while `--ignore-rules` only suppresses user and project exec-policy rules.[^cli]

If the destination requires the literal contract, Factory needs an outer process sandbox or isolated runner that encloses the whole Codex process. That is a different, larger architecture than a roughly 150-line portable Python wrapper.

## Supported invocation design

Factory should generate the run's security settings as runtime `-c` overrides and invoke the resolved `codex` executable directly:

```text
codex exec
  --strict-config
  --ignore-user-config
  --ignore-rules
  -C <Project Boundary>
  -c approval_policy="never"
  -c project_doc_max_bytes=0
  -c project_root_markers=[]
  -c allow_login_shell=false
  -c shell_environment_policy.inherit="core"
  -c shell_environment_policy.ignore_default_excludes=false
  -c features.apps=false
  -c features.plugins=false
  -c features.remote_plugin=false
  -c features.browser_use=false
  -c features.browser_use_external=false
  -c features.computer_use=false
  -c features.multi_agent=false
  -c features.memories=false
  -c features.skip_host_skill_discovery=true
  -c default_permissions="factory-isolated"
  -c 'permissions.factory-isolated={...}'
  -
```

The generated profile is:

```toml
[permissions.factory-isolated]
extends = ":workspace"

[permissions.factory-isolated.filesystem]
":root" = "deny"
":minimal" = "read"
":tmpdir" = "deny"
":slash_tmp" = "deny"
"<resolved directory containing the running Codex binary>" = "read"

[permissions.factory-isolated.filesystem.":workspace_roots"]
"." = "write"

[permissions.factory-isolated.network]
enabled = false
```

OpenAI documents `deny` as denying both reads and writes, with more specific entries overriding broader ones. Its documented workspace-only example uses the same `:root` deny, `:minimal` read, and workspace reopening pattern.[^permissions] `approval_policy="never"` is part of the boundary: the model must not be able to request an approved escape. Denying both temporary-directory tokens prevents them from becoming additional writable roots through `:workspace`.

The Codex executable directory is a runtime exception rather than project access. The installed Linux helper re-executes the Codex binary inside the sandbox; the local probe failed until that directory was readable. Factory must resolve the actual executable path instead of assuming a location under `~/.codex`.

Factory should also pass a runtime `projects.<git-root>.trust_level="untrusted"` override for the enclosing Git root. Untrusted projects skip project-scoped `.codex/` config, hooks, and rules.[^config] This prevents a parent repository configuration from reintroducing extensions before the command sandbox starts. The custom runtime permission profile remains explicit and higher precedence.

The composed stdin prompt starts with the contents of `<Project Boundary>/.agent/spec.md`, when present. Factory reads that file before launching Codex and labels it `## Project Instructions`. Codex does not discover `spec.md` itself.

## Instruction isolation

Codex normally starts at the project root and concatenates `AGENTS.override.md`, `AGENTS.md`, or configured fallback files down to the working directory.[^agents] Set `project_doc_max_bytes=0`; do not rely only on changing `project_root_markers`.

This setting suppresses all **project** instruction files, including files in the Git root, parent directories, and the selected boundary. In current source, `load_project_instructions` exits before obtaining the environment filesystem or probing paths when the remaining byte budget is zero.[^source-agents] A local prompt-input experiment confirmed the behavior:

| Run | Parent sentinel | Boundary sentinel |
| --- | --- | --- |
| Default discovery | Present | Present |
| `project_doc_max_bytes=0` | Absent | Absent |
| `project_root_markers=[".agent"]` only | Absent | Present |

`project_root_markers=[]` remains useful defense in depth because the source documents an empty marker list as disabling parent traversal. It is not the suppression mechanism.

Global `$CODEX_HOME/AGENTS.md` is a separate user-instruction source and remains model-visible even when `project_doc_max_bytes=0`. The same local experiment confirmed that sentinel remained. `--ignore-user-config` does not suppress it because it only replaces the user `config.toml` layer with an empty layer.[^source-config] Therefore Factory can guarantee `.agent/spec.md` is the sole **project-specific** instruction source, but cannot claim it is the sole instruction source while sharing the user's normal `CODEX_HOME`.

Using a separate Factory `CODEX_HOME` would suppress the user's global `AGENTS.md`, but `CODEX_HOME` also owns auth, logs, sessions, skills, and package metadata.[^env] Reusing authentication would then require credential migration or indirection, including both file and OS-keyring storage modes. That is not a supported auth-only switch and does not fit the lightweight wrapper without a separate design decision.

## User configuration and extensions

For a fail-closed Factory run:

- Keep authentication through the normal `CODEX_HOME`; official CLI behavior explicitly retains it with `--ignore-user-config`.[^cli]
- Ignore user `config.toml` and exec-policy rules. This deliberately gives up inherited model, profile, MCP, hook, and similar user settings for Factory runs.
- Mark the enclosing repository untrusted at runtime so parent or boundary `.codex/` layers are skipped.
- Disable apps, plugins, remote plugins, Browser Use, Computer Use, multi-agent, and memories with runtime feature overrides.
- Disable command network access in the permission profile. Inference traffic from the Codex host necessarily remains.
- Use non-login shells and core-only environment inheritance so shell startup files and ambient secret variables do not create side channels.[^config]
- Refuse startup if the effective CLI does not recognize every required field under `--strict-config`, cannot compile the named profile, or cannot enforce it on the current host.

`features.skip_host_skill_discovery` is under development in 0.154.0. Even with it enabled and plugins/apps disabled, the local prompt-input report still contained OpenAI-provided system skills. The source only skips host skill discovery when no registered extension requires it.[^source-skills] Factory must not promise that `.agent/spec.md` is the only model-visible context. It may promise only that it is the only Factory-selected project instruction file.

Managed system or organization policy can also remain above runtime configuration. Factory should fail closed if current Codex reports active MCP servers, hooks, or capabilities that the runtime overrides cannot disable. A wrapper cannot safely override administrator-enforced requirements.

## Platform behavior

The permission-profile design is documented for both target platforms:

- On macOS, Codex uses Seatbelt and refuses to run a command when the selected policy cannot be enforced.[^permissions]
- On Linux, Codex uses bubblewrap plus seccomp, with compatibility paths depending on user namespaces and kernel support; unsupported split policies are refused.[^permissions]

Factory should treat refusal as a runtime error and never retry with `workspace-write` or full access. Named permission profiles are beta, so Factory also needs a pinned minimum/compatible Codex version and an isolation acceptance probe for every supported upgrade.

## Local validation

The Linux probe used a nested `service` boundary with a readable sibling sentinel. Under the documented profile:

```text
inside-boundary
inside-read=0
cat: ../sibling/outside.txt: No such file or directory
outside-read=1
inside-write=0
cannot create ../sibling/outside.txt: Directory nonexistent
outside-write=2
minimal-runtime-read=0
```

This validates the command boundary on the installed host. It does not validate macOS or turn a beta interface into a stable compatibility guarantee.

## Follow-up decision

The architecture must now choose between:

1. adopting the project-data contract above, with explicit runtime read exceptions and disabled optional extensions; or
2. expanding scope to an external runner that encloses the entire Codex process and separately solves credentials, network, sessions, and cross-platform packaging.

That is a product architecture decision, not a remaining research question.

[^permissions]: [OpenAI Codex permissions](https://learn.chatgpt.com/docs/permissions) documents beta named profiles, filesystem precedence, the workspace-only pattern, and macOS/Linux enforcement.
[^config]: [OpenAI Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference) documents permission profiles, project trust, feature flags, shell environment policy, and extension boundaries.
[^cli]: [OpenAI Codex developer commands](https://learn.chatgpt.com/docs/developer-commands?surface=cli) documents `codex exec`, `--ignore-user-config`, `--ignore-rules`, and sandbox flags.
[^agents]: [OpenAI AGENTS.md guidance](https://learn.chatgpt.com/docs/agent-configuration/agents-md) documents root-to-working-directory instruction discovery and merge order.
[^env]: [OpenAI Codex environment variables](https://learn.chatgpt.com/docs/config-file/environment-variables) documents that `CODEX_HOME` owns config, auth, logs, sessions, skills, and package metadata.
[^source-agents]: [Official `openai/codex` source at `787823c`](https://github.com/openai/codex/blob/787823cf957709b314276646024ec54e1761c089/codex-rs/core/src/agents_md.rs#L58-L87) shows the zero-byte early exit before project filesystem access.
[^source-config]: [Official `openai/codex` config loader at `787823c`](https://github.com/openai/codex/blob/787823cf957709b314276646024ec54e1761c089/codex-rs/config/src/loader/mod.rs#L278-L328) shows that `--ignore-user-config` empties the user config layer while project loading is a separate path.
[^source-skills]: [Official `openai/codex` session source at `787823c`](https://github.com/openai/codex/blob/787823cf957709b314276646024ec54e1761c089/codex-rs/core/src/session/session.rs#L610-L629) shows the conditional nature of `skip_host_skill_discovery`.
