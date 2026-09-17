# Adopt Project Data Isolation

Factory adopts Project Data Isolation for Runs: model-generated shell commands may access the selected Project Boundary but are denied access to parent and sibling project data. Factory also guarantees Project Spec Exclusivity within project-specific guidance and fails closed whenever the required boundary cannot be enforced.

Factory does not claim Process Isolation because Codex retains limited runtime access and may receive global or built-in guidance outside Factory's control. An external runner was rejected for version one because separately enclosing Codex, credentials, networking, sessions, and platform-specific sandboxing would violate the lightweight, portable CLI baseline.
