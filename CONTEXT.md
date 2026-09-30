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
A logical body of work owned by exactly one Client. A Project may use multiple Repositories and Checkouts while retaining one immutable Project ID.
_Avoid_: Repository, Checkout, working directory

**Client**:
The customer or owning organization to which a Project belongs. A Client may own multiple Projects.
_Avoid_: Git owner, Harness account

**Client ID**:
Factory-generated immutable identity of one Client, distinct from its lookup key and display name.
_Avoid_: Client key, Client name

**Client Key**:
A stable human-readable lookup key unique among Clients. Changing the key does not change the Client ID.
_Avoid_: Client ID, Client name

**Project ID**:
Factory-generated immutable identity of one logical Project, independent of its Repository, Checkout, key, or display name.
_Avoid_: Project Name, Project Key, Checkout ID

**Project Key**:
A stable human-readable lookup key unique within one Client. Different Clients may use the same Project Key.
_Avoid_: Project ID, Project Name

**Repository**:
The source-control identity that one or more Projects may use. A Project may eventually use more than one Repository.
_Avoid_: Project, Checkout

**Shared Repository**:
A Repository explicitly bound to Checkouts of more than one Project of the same Client. Its repository-wide Git state is authorized shared data for those Projects; binding across Clients is refused, and a cross-Client shared Repository is never Run-admissible.
_Avoid_: Shared Git directory, cross-client repository

**Checkout**:
One registered local Git worktree assigned to exactly one Project and one Repository at a time. Each Checkout has its own immutable Factory-generated Checkout ID and local Git identity proof.
_Avoid_: Project, Repository, working directory

**Checkout Git Assessment**:
A fresh observation of a selected Checkout's Git state before a Run. CLEAN means the required inspection completed and found no relevant changes; DIRTY means it completed and found relevant changes; UNKNOWN means the required state could not be established and the Run is refused. The assessment describes the observed state, not an immutable snapshot of the later execution.
_Avoid_: Cleanliness boolean, snapshot, execution-state proof

**Default Checkout**:
The Checkout a Project selects for a Run when no explicit Checkout selector is supplied: the Project's stored default reference if valid, otherwise its sole Checkout. Ambiguity refuses rather than guesses, and convenience selection never changes the stored default.
_Avoid_: Active checkout, implicit checkout

**Project Boundary**:
The canonical directory passed to Codex as its workspace root. Selection expands `~` and environment variables, rejects referenced variables that are unset or empty, makes relative paths absolute from the invocation directory, and resolves symlinks. Registrations store the resulting machine-local path, not an environment expression. Explicit and registered paths preserve that exact directory rather than widening to the Git root; automatic discovery searches only within the containing Git worktree for the nearest ancestor containing a Project Marker, falling back to that worktree's root.
_Avoid_: Project root, working directory

**Project Marker**:
An `.agent/` directory that marks its containing directory as a Project Boundary during automatic discovery. A symlinked marker is valid only when its resolved target remains inside that boundary. Encountering a malformed marker stops discovery rather than widening the boundary. Explicit and registered selection do not require a marker.
_Avoid_: Project Spec directory

**Project Data Isolation**:
The Run contract that permits model-generated shell commands to read and write within the selected Project Boundary while denying access to Project-exclusive data of other Projects and to anything outside the selected Project's Client boundary; a Repository explicitly bound to multiple Projects of the same Client is authorized shared data for those Projects, not sibling-private data. A small Codex runtime set may remain readable, and Codex host behavior is outside this command boundary.
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
The packaging of one Harness release that a Compatibility Set tuple is certified against — a publisher release asset, a native installer, or an npm package whose digest anchors the unpacked platform binary. Different families of the same Harness version certify separately and never stand in for one another.
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
A case-sensitive, 1–64 character ASCII selector in the path-only Project Registry. It is a local registration handle, not the immutable Project ID or the Client-scoped Project Key.
_Avoid_: Project ID, Project Key

**Project Registry**:
The pre-identity, path-only local collection of Project Names and canonical paths they resolve to. Explicit migration converts it to Factory's authoritative Client, Project, Repository, and Checkout identity store; Factory never changes the registry schema implicitly.
_Avoid_: Project list, catalog, Project ID

**Legacy Registration**:
A Project Registry entry that records only a Project Name and a canonical path, without any Client, Project, Repository, or Checkout identity. A Legacy Registration is inspectable data, never selectable for a Run, and leaves the registry only through explicit conversion or removal.
_Avoid_: Legacy project, stale project, orphan

**Re-registration**:
An explicit continuity claim that preserves an immutable Checkout or Repository identity while replacing its pinned local proof with freshly verified evidence. Re-registration never infers continuity from paths, names, or Git state.
_Avoid_: Re-add, automatic repair, silent rebinding

**Run**:
One non-interactive execution started for an instruction in a project by the selected Harness.
_Avoid_: Job, task, session
