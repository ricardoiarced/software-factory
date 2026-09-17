# Terminal Agent Dispatch

This context describes sending work from a terminal to Codex in a chosen local project.

## Language

**Instruction**:
User-authored positional text that tells Codex what work to perform in one project.
_Avoid_: Prompt, message, command

**Context**:
Supporting text read from piped standard input or files and labeled separately from the instruction before submission to Codex.
_Avoid_: Input, attachment, background information

**Project**:
A Git-backed local workspace selected by a project name, an explicit path, or automatic discovery.
_Avoid_: Repository, working directory

**Project Boundary**:
The directory passed to Codex as its workspace root. Explicit and registered paths preserve their exact directory; automatic discovery uses the nearest ancestor containing `.agent/`, falling back to the Git root.
_Avoid_: Project root, working directory

**Project Spec**:
Optional guidance stored at `<Project Boundary>/.agent/spec.md` and prepended to each instruction sent from that project.
_Avoid_: Instructions file, context file

**Project Name**:
A user-defined name that resolves to one project through the local project registry.
_Avoid_: Alias, shortcut

**Project Registry**:
The local collection of project names and the project paths they resolve to.
_Avoid_: Project list, catalog

**Run**:
One non-interactive Codex execution started for an instruction in a project.
_Avoid_: Job, task, session
