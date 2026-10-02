---
name: handoff
description: Compact the current conversation into a handoff document for another agent to pick up.
argument-hint: "What will the next session be used for?"
disable-model-invocation: true
---

Write a handoff document summarising the current conversation so a fresh agent can continue the work. Save to the temporary directory of the user's OS - not the current workspace.

Include a "suggested skills" section in the document, naming which skills the next agent should call the Skill tool for.

Do not duplicate content already captured in other artifacts (specs, plans, ADRs, issues, commits, diffs, AGENTS.md, the session's memory files). Reference them by path or URL instead - and name AGENTS.md plus the active memory map file explicitly, so the next session reads them as its briefing instead of re-deriving state from GitHub.

If continuing the work can launch long-running or paid runs, include a single-resumer warning: exactly one session may resume the handoff; any other session picking it up must stop before relaunching those runs.

Redact any sensitive information, such as API keys, passwords, or personally identifiable information.

If the user passed arguments, treat them as a description of what the next session will focus on and tailor the doc accordingly.
