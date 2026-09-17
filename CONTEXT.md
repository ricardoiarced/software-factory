# Terminal Agent Dispatch

This context describes sending work from a terminal to Codex in a chosen local project.

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
A Git-backed local workspace selected by exactly one project name or explicit path, or by automatic discovery when neither selector is supplied.
_Avoid_: Repository, working directory

**Project Boundary**:
The canonical directory passed to Codex as its workspace root. Selection expands `~` and environment variables, rejects referenced variables that are unset or empty, makes relative paths absolute from the invocation directory, and resolves symlinks. Registrations store the resulting machine-local path, not an environment expression. Explicit and registered paths preserve that exact directory rather than widening to the Git root; automatic discovery searches only within the containing Git worktree for the nearest ancestor containing a Project Marker, falling back to that worktree's root.
_Avoid_: Project root, working directory

**Project Marker**:
An `.agent/` directory that marks its containing directory as a Project Boundary during automatic discovery. A symlinked marker is valid only when its resolved target remains inside that boundary. Encountering a malformed marker stops discovery rather than widening the boundary. Explicit and registered selection do not require a marker.
_Avoid_: Project Spec directory

**Project Data Isolation**:
The Run contract that permits model-generated shell commands to read and write within the selected Project Boundary while denying access to parent and sibling project data. A small Codex runtime set may remain readable, and Codex host behavior is outside this command boundary.
_Avoid_: Process isolation, unqualified strict isolation

**Fail-Closed Isolation**:
The Run admission policy that refuses execution when Project Data Isolation cannot be enforced as specified. Factory never falls back to a weaker isolation mode.
_Avoid_: Best-effort isolation, sandbox fallback

**Codex Compatibility Set**:
The exact Codex executable artifacts and platform combinations certified by a Factory release to enforce Project Data Isolation.
_Avoid_: Compatibility range, minimum Codex version

**Run Admission**:
The fail-closed checks that establish a Run can use a member of the Codex Compatibility Set and enforce the required isolation before Factory submits the Run Request.
_Avoid_: Compatibility mode

**Host Admission Probe**:
A cacheable synthetic sandbox check that proves the current host enforces the certified permission boundary required for Run Admission.
_Avoid_: Sandbox detection

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

**Run**:
One non-interactive Codex execution started for an instruction in a project.
_Avoid_: Job, task, session
