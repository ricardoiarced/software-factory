# Terminal Agent Dispatch

This context describes sending work from a terminal to a selected Harness in a chosen local project.

## Language

**Instruction**:
Required, non-whitespace user-authored positional text that tells Codex what work to perform in one project. Factory preserves whitespace within each shell argument and joins multiple positional arguments with one ASCII space.
_Avoid_: Prompt, message, command

**Context**:
Optional supporting UTF-8 text read from explicitly supplied files or piped standard input and labeled by source separately from the Instruction before submission to Codex. Empty sources contribute no Context.
_Avoid_: Input, attachment, background information

**Context Source**:
A user-authorized source of Context: one file explicitly named for a Run or piped standard input. File sources retain their supplied order, followed by standard input.
_Avoid_: Attachment, input source

**Harness**:
The selected external agent CLI that performs one Run: the Codex CLI by default, or Claude Code by explicit selection. Each Harness is admitted only against its own compatibility set, and an unavailable or uncertified Harness fails closed without falling back to another.
_Avoid_: Agent, backend, engine, provider

**Three-Layer Run Model**:
The separation of each Run into the Codex Foundation, Project Guidance, and Run Request layers.
_Avoid_: Three-Layer Context Model

**Codex Foundation**:
Global rules, built-in skills, and system behavior supplied by Codex outside Factory's project-specific control.

**Project Guidance**:
Project-specific direction supplied exclusively through the Project Spec selected and injected by Factory.

**Run Request**:
The Instruction and optional Context submitted for one Run.

**Project**:
A Git-backed local workspace registered in the identity store and selected through one registered Checkout: by Project selector, by Checkout path, or by automatic discovery of the enclosing worktree root.
_Avoid_: Repository, working directory

**Project Boundary**:
The canonical directory passed to Codex as its workspace root: exactly the verified Git worktree root of one registered Checkout. Registration expands `~` and environment variables, rejects referenced variables that are unset or empty, makes relative paths absolute from the invocation directory, resolves symlinks, and stores the resulting machine-local path — never an environment expression. A Run boundary is never widened to a directory other than the selected Checkout's registered root, and discovery resolves the nearest enclosing Git worktree root without searching past it.
_Avoid_: Project root, working directory, Run boundary

**Project Data Isolation**:
The Run contract that permits model-generated shell commands to read and write within the selected Project Boundary while denying access to parent and sibling project data. A small Codex runtime set may remain readable, and Codex host behavior is outside this command boundary.
_Avoid_: Process isolation, unqualified strict isolation

**Fail-Closed Isolation**:
The Run admission policy that refuses execution when Project Data Isolation cannot be enforced as specified. Factory never falls back to a weaker isolation mode.
_Avoid_: Best-effort isolation, sandbox fallback

**Codex Compatibility Set**:
The exact Codex executable artifacts and platform combinations certified by a Factory release to enforce Project Data Isolation.
_Avoid_: Compatibility range, minimum Codex version

**Claude Compatibility Set**:
The exact Claude Code executable artifacts and platform combinations certified by a Factory release to enforce Project Data Isolation.
_Avoid_: Compatibility range, minimum Claude Code version

**Artifact Family**:
The packaging of one Harness release that a Compatibility Set tuple is certified against — a publisher release asset, a native installer, or an npm package whose digest anchors the unpacked platform binary or, for packages without a platform binary, the unpacked entry point. Different families of the same Harness version certify separately and never stand in for one another.
_Avoid_: Package format, release flavor

**Candidate Manifest**:
The committed reviewed file beneath `certification/candidates/` that selects exactly one Harness tuple for real-host certification: schema, Harness, Harness version, install channel, official executable SHA-256, official source, OS, and architecture. The manifest bytes must match `HEAD`, and the digest is reviewed independently of the installed binary, so a runtime-provided executable can never become certification evidence.
_Avoid_: Config file, candidate list

**Certification Record**:
The recorded result of one real-host certification run: the emitted report's identity — Harness, install channel, Harness version, official digest and source, OS, architecture — plus each established evidence property and production check, or the exact admission failure on rejection. A record authorizes, never performs, the addition of its exact tuple to a Compatibility Set, and evidence never rides over from a previous Factory version.
_Avoid_: Test log, CI output

**Publication Gate**:
The release rule that a Factory version ships only when CI is green and fresh Certification Records dated at or after the release commit exist for every certified tuple. A recertifying release replaces the affected Harness's Compatibility Set tuples wholesale rather than letting stale tuples accumulate.
_Avoid_: Release checklist, signing step

**Run Admission**:
The fail-closed checks that establish a Run can use a member of the selected Harness's compatibility set and enforce the required isolation before Factory submits the Run Request.
_Avoid_: Compatibility mode

**Host Admission Probe**:
A cacheable synthetic sandbox check that proves the current host enforces the certified permission boundary required for Run Admission.
_Avoid_: Sandbox detection

**Masked Denial**:
An in-session exit-0 write into a read-denied region that lands in the sandbox's ephemeral overlay instead of on the host. Expected sandbox behavior: the Host Admission Probe accepts a forbidden write recorded as succeeded only with Factory-verified non-landing on every monitored path, and never conflates it with a real escape.
_Avoid_: Suppressed write, silent denial, blocked write

**Newest-Stable Verification**:
The live per-Run publisher check that identifies each Harness's newest stable release and refuses a Run whose installed release is older, unstable, or unverifiable. Factory performs one unauthenticated lookup per Harness, cross-checks the authoritative publisher surfaces before trusting any of them, and treats transport faults, disagreeing surfaces, and rate limits as refusals — never as a stale allow. A release at or above the verified newest stable still requires membership in the selected Harness's Compatibility Set.
_Avoid_: Version check, update check, cached release lookup

**Managed-Policy Surface**:
Any channel through which organization-managed configuration can reach the Harness: the managed settings file, managed settings drop-ins, managed MCP configuration, remote managed settings, organization policy, and the server policy cache. Each surface is present (on disk), configured (the Harness reports it active), unavailable (the Harness reports the channel cannot deliver policy, with the reason attributed), or uninspected (inspection evidence is missing or unreadable). Run Admission refuses a Run on present, configured, and uninspected surfaces; an unavailable surface allows the Run with a recorded residual-risk notice.
_Avoid_: Managed policy source, organization configuration

**Project Spec**:
Optional guidance stored at `<Project Boundary>/.agent/spec.md` and prepended to each instruction sent from that project. Factory never inherits a Project Spec from above the selected boundary, and a symlinked spec is valid only when its resolved target remains inside the boundary.
_Avoid_: Instructions file, context file

**Project Spec Exclusivity**:
The Run contract that suppresses Codex-discovered project instruction files, ignores configuration extensions, and injects the Project Spec as Factory's sole project-specific instruction source. Global rules, system instructions, and built-in skills may remain visible to Codex.
_Avoid_: Project Spec supremacy, sole instruction source

**Project Name**:
A case-sensitive, 1–64 character ASCII identifier that resolves to one Project through the Project Registry. It starts and ends with an ASCII letter or digit and may contain letters, digits, periods, underscores, and hyphens; names that differ only by case remain distinct.
_Avoid_: Alias, shortcut

**Project Registry**:
The local collection of project names and the canonical project paths they resolve to. Paths are validated when registered and before each Run; a stale registration remains recorded so the user can inspect, replace, or remove it.
_Avoid_: Project list, catalog

**Default Checkout**:
The Checkout record a Project designates as its selection default: the first Checkout is set at registration, `project set-default-checkout` changes it, and removing the referenced Checkout clears it without selecting a replacement. Convenience selection never writes this field.
_Avoid_: Active checkout, current checkout

**Legacy Registration**:
A version-one Project Registry entry carried verbatim into the identity store's legacy section by `project migrate`. It records a name and a path, assigns no identity, is inspected and removed only through the explicit `--legacy` namespace, never resolves as a selectable Project, and its surviving claim blocks fresh registration of the same canonical path until it is consumed with `--from-legacy` or removed.
_Avoid_: Old project, migrated project

**Re-registration**:
The explicit continuity claim that repairs one immutable Checkout or Repository record by ID: Factory freshly verifies the supplied path, replaces only that record's Git pin after self-excluded uniqueness checks, preserves the record's identity and relationships, and reports the old and new proof. It never runs implicitly and never mints a replacement record.
_Avoid_: Re-add, silent repair, duplicate registration

**Run**:
One non-interactive execution started for an instruction in a project by the selected Harness.
_Avoid_: Job, task, session
