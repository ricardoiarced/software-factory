"""The Shared Run Gate: deterministic identity, Git, runtime, and policy admission stages.

After Run Request composition and its size gate, and before Run Admission,
the gate re-establishes — inside the already-open Run Record — that the
selected Checkout still proves membership in its pinned Repository, that the
Repository is associated with exactly one Client through its referencing
Checkouts, and that the identity-store records needed to establish those
facts are internally consistent. A fresh Checkout Git Assessment then reads
the whole worktree, and the runtime, instruction, sandbox, network, and
environment evidence stages derive the selected Harness's certified policy
declaration from the certified recipe builders themselves, refusing the Run
on security-relevant UNKNOWN. Every stage lands in the record: passing
stages as gate evidence, a failing stage as a structured refusal naming it.
A Git common directory bound to Checkouts of more than one Client never
admits a Run, and dirtiness warns without refusing (ADR 0003).
"""

import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from _harness import factory_version
from _identity import (
    Checkout,
    Client,
    IdentityError,
    IdentityStore,
    Pin,
    ProjectRecord,
    Repository,
    RunSelection,
    _git_environment,
    _git_proof,
    _quoted,
    _validate_references,
    run_store,
)
from _project_registry import RegistryError
from _runrecord import RunRecord, _timestamp

_GIT_TIMEOUT_SECONDS = 60.0
_UNMERGED = frozenset({"DD", "AU", "UD", "UA", "DU", "AA", "UU"})
_STORE_GRAPH = "store-graph"
_SCOPE = "scope"
_ASSOCIATION = "association"
_PROOF = "proof"
_DRIFT = "drift"
_GIT = "git"
_RUNTIME = "runtime"
_INSTRUCTIONS = "instructions"
_SANDBOX = "sandbox"
_NETWORK = "network"
_ENVIRONMENT = "environment"
ENVIRONMENT_DIGEST_SCHEMA = "factory-env-digest-v1"
_PROJECT_SPEC_PATH = ".agent/spec.md"
_REQUIRED_ENVIRONMENT_EDITS: Mapping[str, tuple[tuple[str, str], ...]] = {
    "codex": tuple(
        ("removal", marker)
        for marker in (
            "CODEX_MANAGED_PACKAGE_ROOT",
            "CODEX_MANAGED_BY_NPM",
            "CODEX_MANAGED_BY_PNPM",
            "CODEX_MANAGED_BY_BUN",
            "CODEX_MANAGED_BY_VITE_PLUS",
        )
    ),
    "claude": (
        ("pin", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"),
        ("pin", "DISABLE_TELEMETRY"),
        ("pin", "DISABLE_ERROR_REPORTING"),
    ),
}


class RunGateError(Exception):
    """The Shared Run Gate refuses this Run after recording the refusal."""


class _GitAssessmentError(Exception):
    """One required Git inspection of the Checkout could not be completed."""


@dataclass(frozen=True)
class _StageRefusal(Exception):
    """One classified gate failure awaiting its structured record refusal."""

    stage: str
    detail: str
    facts: Mapping[str, str] | None = None


@dataclass(frozen=True)
class GateObservation:
    """The identity, Git, and policy evidence one passed Shared Run Gate carries."""

    head: str
    object_format: str
    pins: Mapping[str, Mapping[str, object]]
    assessment: Mapping[str, object]
    policy: Mapping[str, object]


@dataclass(frozen=True)
class CertifiedRuntime:
    """One declared certified Harness runtime identity."""

    version: str
    channel: str
    operating_system: str
    architecture: str
    sha256: str


@dataclass(frozen=True)
class HarnessPolicy:
    """The certified policy declaration one selected Harness exposes.

    Every control is derived by calling the certified recipe builders of the
    declared policy itself, so the gate's configured evidence cannot drift
    from the certified recipe it declares. The recorded fragments come from
    the declaration's first entry; binding the evidence to the exact matched
    artifact is the plan seal's contract, not this stage's. The gate records
    this evidence as configured or observed and never as proven, which stays
    owned by exact-tuple certification and the Host Admission Probe.
    """

    harness: str
    runtimes: tuple[CertifiedRuntime, ...]
    instructions: Mapping[str, object]
    sandbox: Mapping[str, object]
    network: Mapping[str, object]
    pins: tuple[tuple[str, str], ...]
    removals: tuple[str, ...]
    config_variable: str
    shell_environment_policy: Mapping[str, str]


@dataclass(frozen=True)
class _Proof:
    """The fresh Git proof of the selected Checkout's identity."""

    boundary: Path
    worktree_root: Pin
    git_dir: Pin
    common_dir: Pin
    head: str
    object_format: str


@dataclass(frozen=True)
class _Gitlink:
    """One required submodule recorded in a tree's index."""

    path: str
    index_gitlink: str


@dataclass(frozen=True)
class _StatusEntry:
    """One porcelain status entry of one inspected tree."""

    index_status: str
    worktree_status: str
    path: str


@dataclass(frozen=True)
class _TreeStatus:
    """The categorized fresh status of one inspected tree."""

    staged: tuple[str, ...]
    unstaged: tuple[str, ...]
    untracked: tuple[str, ...]
    conflicts: tuple[str, ...]
    submodules: tuple[dict[str, object], ...]

    @property
    def changed_paths(self) -> tuple[str, ...]:
        return self.staged + self.unstaged + self.untracked + self.conflicts

    @property
    def dirty(self) -> bool:
        return bool(self.changed_paths) or any(
            _submodule_dirty(entry) for entry in self.submodules
        )


def run_gate(record: RunRecord, selection: RunSelection, harness: str) -> None:
    """Run the ordered gate stages for one selected Checkout, recording them.

    A passing gate commits its identity, Git, and policy evidence into the
    record; a failing stage commits a structured refusal naming its stage
    and then refuses the Run. No gate stage lands unrecorded.
    """

    try:
        observation = _gate_stages(selection, harness)
    except _StageRefusal as refusal:
        record.refuse(refusal.stage, refusal.detail, facts=refusal.facts)
        raise RunGateError(refusal.detail) from None
    record.gated(observation)
    _warn_dirty(selection, observation.head, observation.assessment)


def _gate_stages(selection: RunSelection, harness: str) -> GateObservation:
    store = _validated_store()
    checkout = _selected_checkout(store, selection)
    _scan_association(store, checkout)
    proof = _fresh_proof(selection.boundary)
    _check_drift(store, checkout, selection, proof)
    status = _assess_with_recheck(checkout, proof)
    return GateObservation(
        head=proof.head,
        object_format=proof.object_format,
        pins={
            "worktree_root": _pin_evidence(proof.worktree_root),
            "git_dir": _pin_evidence(proof.git_dir),
            "common_dir": _pin_evidence(proof.common_dir),
        },
        assessment=_assessment_document(status),
        policy=_policy_stages(harness, selection.boundary),
    )


def _policy_stages(harness: str, boundary: Path) -> dict[str, object]:
    """Run the runtime and policy evidence stages in their fixed order.

    Each stage derives its document from the selected Harness's certified
    policy declaration and refuses as UNKNOWN when a required control cannot
    be established as version-bound; nothing here labels evidence proven.
    """

    policy = _harness_policy(harness, boundary)
    return {
        "at": _timestamp(),
        "runtime": _runtime_document(policy),
        "instructions": _stage_document(policy, policy.instructions),
        "sandbox": _stage_document(policy, policy.sandbox),
        "network": _stage_document(policy, policy.network),
        "environment": _environment_document(policy),
    }


def _harness_policy(harness: str, boundary: Path) -> HarnessPolicy:
    if harness == "codex":
        return _codex_policy(boundary)
    if harness == "claude":
        return _claude_policy(boundary)
    raise _StageRefusal(
        _RUNTIME,
        f"the selected Harness {_quoted(harness)} is not a registered Runner; "
        "no certified runtime or policy can be established for it, so the "
        "Run refuses as UNKNOWN.",
    )


def _runtime_document(policy: HarnessPolicy) -> dict[str, object]:
    return {
        "harness": policy.harness,
        "factory_version": factory_version(),
        "certified": [
            {
                "version": runtime.version,
                "channel": runtime.channel,
                "os": runtime.operating_system,
                "arch": runtime.architecture,
                "sha256": runtime.sha256,
            }
            for runtime in policy.runtimes
        ],
        "provenance": "configured",
    }


def _stage_document(
    policy: HarnessPolicy, controls: Mapping[str, object]
) -> dict[str, object]:
    return {
        "harness": policy.harness,
        "provenance": "configured",
        "controls": dict(controls),
    }


def _require_controls(
    stage: str, established: str, controls: tuple[tuple[str, bool], ...]
) -> None:
    """Refuse as UNKNOWN when a required certified control is not established."""

    missing = ", ".join(name for name, holds in controls if not holds)
    if not missing:
        return
    raise _StageRefusal(
        stage,
        f"the certified controls establishing {established} cannot be "
        f"established as version-bound: {missing} is missing or contradicted "
        "in the certified recipe; the Run refuses as UNKNOWN rather than "
        "launch with an unestablished required property.",
    )


def _certified_runtimes(entries: Sequence[Any]) -> tuple[CertifiedRuntime, ...]:
    """Map one declared Compatibility Set to its certified runtime identities.

    Every entry is a certified policy dataclass carrying ``version``,
    ``install_channel``, ``operating_system``, ``architecture``, and
    ``sha256``; Codex and Claude declare those fields with different
    variances, so the shared shape stays documented here rather than
    forced into one protocol.
    """

    return tuple(
        CertifiedRuntime(
            version=entry.version,
            channel=entry.install_channel,
            operating_system=entry.operating_system,
            architecture=entry.architecture,
            sha256=entry.sha256,
        )
        for entry in entries
    )


def _codex_policy(boundary: Path) -> HarnessPolicy:
    """Derive the Codex declaration from its first declared certified policy.

    Every tuple of a Compatibility Set carries the identical certified
    recipe for its release, so the declaration is derived once from the
    first entry while the runtime stage records the whole declared set;
    which exact artifact will run stays observed and refused by Run
    Admission, and the plan seal binds it.
    """

    from _codex import _CODEX_MANAGED_MARKERS, _COMPATIBILITY_SET

    if not _COMPATIBILITY_SET:
        raise _StageRefusal(
            _RUNTIME,
            "Codex declares no certified runtime; Factory cannot establish "
            "the version-bound controls this Run requires and refuses as "
            "UNKNOWN.",
        )
    policy = _COMPATIBILITY_SET[0]
    configuration = _configuration_pairs(policy._configuration(boundary))
    flags, _ = _flagged(policy._invocation(boundary, boundary))
    profile = configuration.get("permissions.factory-isolated")
    return HarnessPolicy(
        harness="codex",
        runtimes=_certified_runtimes(_COMPATIBILITY_SET),
        instructions=_codex_instructions(configuration, flags),
        sandbox=_codex_sandbox(configuration, profile),
        network=_codex_network(profile),
        pins=(),
        removals=_CODEX_MANAGED_MARKERS,
        config_variable="CODEX_HOME",
        shell_environment_policy=_codex_shell_policy(configuration),
    )


def _configuration_pairs(items: Sequence[str]) -> Mapping[str, str]:
    """Read the certified Codex override pairs, refusing malformed output."""

    malformed = len(items) % 2 != 0 or any(
        items[index] != "-c" for index in range(0, len(items), 2)
    )
    if malformed:
        raise _StageRefusal(
            _INSTRUCTIONS,
            "the certified Codex configuration cannot be read as override "
            "pairs; the Run refuses as UNKNOWN rather than record "
            "unestablished policy evidence.",
        )
    pairs: dict[str, str] = {}
    for override in items[1::2]:
        key, separator, value = override.partition("=")
        if not separator or not key:
            raise _StageRefusal(
                _INSTRUCTIONS,
                "the certified Codex configuration contains a malformed "
                "override; the Run refuses as UNKNOWN rather than record "
                "unestablished policy evidence.",
            )
        pairs[key] = value
    return pairs


def _flagged(invocation: Sequence[str]) -> tuple[tuple[str, ...], Mapping[str, str]]:
    """Split one certified invocation into flag names and valued flags."""

    names: list[str] = []
    values: dict[str, str] = {}
    arguments = invocation[1:]
    index = 0
    while index < len(arguments):
        element = arguments[index]
        if element.startswith("-"):
            names.append(element)
            following = index + 1
            if following < len(arguments) and not arguments[following].startswith("-"):
                values[element] = arguments[following]
                index = following + 1
                continue
        index += 1
    return tuple(names), values


def _codex_instructions(
    configuration: Mapping[str, str], flags: tuple[str, ...]
) -> Mapping[str, object]:
    _require_controls(
        _INSTRUCTIONS,
        "Project Spec Exclusivity and discovered-guidance suppression",
        (
            (
                "project_doc_max_bytes=0",
                configuration.get("project_doc_max_bytes") == "0",
            ),
            (
                "project_root_markers=[]",
                configuration.get("project_root_markers") == "[]",
            ),
            (
                "skills.include_instructions=false",
                configuration.get("skills.include_instructions") == "false",
            ),
            ("--ignore-user-config", "--ignore-user-config" in flags),
            ("--ignore-rules", "--ignore-rules" in flags),
        ),
    )
    return {
        "flags": [
            flag
            for flag in (
                "--ephemeral",
                "--strict-config",
                "--ignore-user-config",
                "--ignore-rules",
            )
            if flag in flags
        ],
        "discovered_guidance": {
            "project_doc_max_bytes": configuration["project_doc_max_bytes"],
            "project_root_markers": configuration["project_root_markers"],
            "skills.include_instructions": configuration["skills.include_instructions"],
        },
        "project_spec": {"path": _PROJECT_SPEC_PATH, "scope": "boundary-only"},
    }


def _codex_sandbox(
    configuration: Mapping[str, str], profile: str | None
) -> Mapping[str, object]:
    trust = {
        name: value
        for name, value in configuration.items()
        if name.startswith("projects.") and name.endswith(".trust_level")
    }
    _require_controls(
        _SANDBOX,
        "fail-closed filesystem and command approval isolation",
        (
            (
                'approval_policy="never"',
                configuration.get("approval_policy") == '"never"',
            ),
            (
                "allow_login_shell=false",
                configuration.get("allow_login_shell") == "false",
            ),
            (
                'default_permissions="factory-isolated"',
                configuration.get("default_permissions") == '"factory-isolated"',
            ),
            ("the factory-isolated permission profile", profile is not None),
            (
                'one projects...trust_level="untrusted" declaration',
                len(trust) == 1 and next(iter(trust.values())) == '"untrusted"',
            ),
        ),
    )
    return {
        "approval_policy": configuration["approval_policy"],
        "allow_login_shell": configuration["allow_login_shell"],
        "default_permissions": configuration["default_permissions"],
        "permission_profile": profile,
        "workspace_trust_level": next(iter(trust.values())),
    }


def _codex_network(profile: str | None) -> Mapping[str, object]:
    _require_controls(
        _NETWORK,
        "the certified sandbox command-network denial",
        (
            (
                "network = {enabled = false} in the permission profile",
                profile is not None and "network = {enabled = false}" in profile,
            ),
        ),
    )
    return {"sandbox_command_network": "disabled"}


def _codex_shell_policy(configuration: Mapping[str, str]) -> Mapping[str, str]:
    _require_controls(
        _ENVIRONMENT,
        "the Codex shell environment policy",
        (
            (
                'shell_environment_policy.inherit="core"',
                configuration.get("shell_environment_policy.inherit") == '"core"',
            ),
            (
                "shell_environment_policy.ignore_default_excludes=false",
                configuration.get("shell_environment_policy.ignore_default_excludes")
                == "false",
            ),
        ),
    )
    return {
        "inherit": configuration["shell_environment_policy.inherit"],
        "ignore_default_excludes": configuration[
            "shell_environment_policy.ignore_default_excludes"
        ],
    }


def _claude_policy(boundary: Path) -> HarnessPolicy:
    """Derive the Claude Code declaration from its first declared policy.

    Every tuple of the Compatibility Set carries the identical certified
    recipe for its release, so the declaration is derived once from the
    first entry while the runtime stage records the whole declared set;
    which exact artifact will run stays observed and refused by Run
    Admission, and the plan seal binds it.
    """

    from _claude import _CLAUDE_COMPATIBILITY_SET, _ENVIRONMENT_PINS

    if not _CLAUDE_COMPATIBILITY_SET:
        raise _StageRefusal(
            _RUNTIME,
            "Claude Code declares no certified runtime; Factory cannot "
            "establish the version-bound controls this Run requires and "
            "refuses as UNKNOWN.",
        )
    policy = _CLAUDE_COMPATIBILITY_SET[0]
    settings = _claude_settings(policy._settings_document(boundary))
    flags, flag_values = _flagged(policy._invocation(boundary))
    return HarnessPolicy(
        harness="claude",
        runtimes=_certified_runtimes(_CLAUDE_COMPATIBILITY_SET),
        instructions=_claude_instructions(flags, flag_values),
        sandbox=_claude_sandbox(settings, flag_values),
        network=_claude_network(settings, _ENVIRONMENT_PINS),
        pins=_ENVIRONMENT_PINS,
        removals=(),
        config_variable="CLAUDE_CONFIG_DIR",
        shell_environment_policy={},
    )


def _claude_settings(document: str) -> Mapping[str, object]:
    """Read the certified inline settings, refusing malformed output."""

    try:
        decoded: object = json.loads(document)
    except json.JSONDecodeError as error:
        raise _StageRefusal(
            _SANDBOX,
            "the certified Claude inline settings cannot be read; the Run "
            "refuses as UNKNOWN rather than record unestablished policy "
            "evidence.",
        ) from error
    if not isinstance(decoded, dict):
        raise _StageRefusal(
            _SANDBOX,
            "the certified Claude inline settings are not a settings object; "
            "the Run refuses as UNKNOWN rather than record unestablished "
            "policy evidence.",
        )
    return {str(name): value for name, value in decoded.items()}


def _settings_object(section: object) -> Mapping[str, object] | None:
    return section if isinstance(section, dict) else None


def _claude_instructions(
    flags: tuple[str, ...], flag_values: Mapping[str, str]
) -> Mapping[str, object]:
    _require_controls(
        _INSTRUCTIONS,
        "the effective-settings scoping of the certified recipe",
        (
            ("--restricted", "--restricted" in flags),
            ("--strict-mcp-config", "--strict-mcp-config" in flags),
            ("--settings", "--settings" in flag_values),
        ),
    )
    return {
        "flags": [
            flag
            for flag in ("-p", "--restricted", "--safe-mode", "--strict-mcp-config")
            if flag in flags
        ],
        "project_spec": {"path": _PROJECT_SPEC_PATH, "scope": "boundary-only"},
        "settings_source": "inline",
    }


def _claude_sandbox(
    settings: Mapping[str, object], flag_values: Mapping[str, str]
) -> Mapping[str, object]:
    sandbox = _settings_object(settings.get("sandbox"))
    permissions = _settings_object(settings.get("permissions"))
    filesystem = (
        None if sandbox is None else _settings_object(sandbox.get("filesystem"))
    )
    credentials = (
        None if sandbox is None else _settings_object(sandbox.get("credentials"))
    )
    tools = flag_values.get("--tools")
    _require_controls(
        _SANDBOX,
        "fail-closed sandboxing and the boundary read fence",
        (
            (
                "permissions.blockReadsOutsideWorkingDirectories=true",
                permissions is not None
                and permissions.get("blockReadsOutsideWorkingDirectories") is True,
            ),
            (
                "sandbox.enabled=true",
                sandbox is not None and sandbox.get("enabled") is True,
            ),
            (
                "sandbox.failIfUnavailable=true",
                sandbox is not None and sandbox.get("failIfUnavailable") is True,
            ),
            (
                "sandbox.allowUnsandboxedCommands=false",
                sandbox is not None
                and sandbox.get("allowUnsandboxedCommands") is False,
            ),
            (
                "sandbox.excludedCommands=[]",
                sandbox is not None and sandbox.get("excludedCommands") == [],
            ),
            (
                "--permission-mode dontAsk",
                flag_values.get("--permission-mode") == "dontAsk",
            ),
            (
                "--tools matching --allowedTools",
                tools is not None and flag_values.get("--allowedTools") == tools,
            ),
            (
                "the sandbox filesystem read fence",
                filesystem is not None
                and isinstance(filesystem.get("denyRead"), list)
                and isinstance(filesystem.get("allowRead"), list),
            ),
            (
                "the sandbox credential denials",
                credentials is not None and isinstance(credentials.get("deny"), list),
            ),
        ),
    )
    assert sandbox is not None and filesystem is not None and credentials is not None, (
        "the sandbox controls check established every section above"
    )
    return {
        "tools": tools,
        "permission_mode": flag_values["--permission-mode"],
        "block_reads_outside_working_directories": True,
        "sandbox_enabled": True,
        "fail_if_unavailable": True,
        "allow_unsandboxed_commands": False,
        "excluded_commands": [],
        "read_fence": {
            "deny_read": filesystem["denyRead"],
            "allow_read": filesystem["allowRead"],
        },
        "credential_denials": credentials["deny"],
    }


def _claude_network(
    settings: Mapping[str, object], pins: tuple[tuple[str, str], ...]
) -> Mapping[str, object]:
    sandbox = _settings_object(settings.get("sandbox"))
    network = None if sandbox is None else _settings_object(sandbox.get("network"))
    _require_controls(
        _NETWORK,
        "the certified sandbox network allowlist",
        (
            (
                "sandbox.network strictAllowlist=true with an empty allow list",
                network is not None
                and network.get("strictAllowlist") is True
                and network.get("allow") == [],
            ),
        ),
    )
    return {
        "sandbox_allowlist": {"strict": True, "allow": []},
        "nonessential_traffic_pins": [name for name, _ in pins],
    }


def _require_environment_edits(policy: HarnessPolicy) -> None:
    """Refuse a missing or ambiguous required pin or removal as UNKNOWN."""

    pins = {name for name, _ in policy.pins}
    removals = set(policy.removals)
    missing = [
        f"the required {kind} {name}"
        for kind, name in _REQUIRED_ENVIRONMENT_EDITS.get(policy.harness, ())
        if name not in (removals if kind == "removal" else pins)
    ]
    ambiguous = [
        f"{name} is both pinned and removed" for name in sorted(pins & removals)
    ]
    if not missing and not ambiguous:
        return
    raise _StageRefusal(
        _ENVIRONMENT,
        "the certified environment policy cannot be established as "
        f"version-bound: {', '.join(missing + ambiguous)}; the Run refuses "
        "as UNKNOWN rather than launch with an unestablished environment "
        "policy.",
    )


def _environment_document(policy: HarnessPolicy) -> dict[str, object]:
    _require_environment_edits(policy)
    inherited = tuple(os.environ.items())
    effective = effective_environment(policy.pins, policy.removals, inherited)
    overridden = {name for name, _ in policy.pins} | set(policy.removals)
    document: dict[str, object] = {
        "harness": policy.harness,
        "provenance": "observed",
        "digest_scheme": ENVIRONMENT_DIGEST_SCHEMA,
        "digest": environment_digest(effective),
        "variable_count": len(effective),
        "pins": [name for name, _ in policy.pins],
        "removals": list(policy.removals),
        "inherited": sorted(name for name, _ in inherited if name not in overridden),
        "config_location": {
            "variable": policy.config_variable,
            "path": dict(effective).get(policy.config_variable),
        },
    }
    if policy.shell_environment_policy:
        document["shell_environment_policy"] = dict(policy.shell_environment_policy)
    return document


def effective_environment(
    pins: Sequence[tuple[str, str]],
    removals: Sequence[str],
    inherited: Sequence[tuple[str, str]],
) -> tuple[tuple[str, str], ...]:
    """Derive the effective child environment from one observed parent.

    The certified removals strip inherited names first and the certified
    pins override what remains; the parent environment is observed exactly
    once per Run and never consulted again for this fact.
    """

    removed = set(removals)
    effective = {name: value for name, value in inherited if name not in removed}
    effective.update(dict(pins))
    return tuple(sorted(effective.items()))


def environment_digest(variables: Sequence[tuple[str, str]]) -> str:
    """Return the pinned ``factory-env-digest-v1`` SHA-256 over one mapping.

    The canonical serialization is the versioned schema document with the
    variables as name/value pairs sorted by name, JSON-escaped to ASCII so
    every host environment byte is bound losslessly; it carries the values,
    while the record itself carries only names.
    """

    canonical = json.dumps(
        {
            "schema": ENVIRONMENT_DIGEST_SCHEMA,
            "variables": [[name, value] for name, value in sorted(variables)],
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def _validated_store() -> IdentityStore:
    """Re-read the identity store and refuse a corrupt identity graph."""

    try:
        store = run_store()
        _validate_references(store)
    except (IdentityError, RegistryError) as error:
        raise _StageRefusal(
            _STORE_GRAPH, f"the identity store is corrupt: {error}"
        ) from error
    _refuse_duplicate_pins(store)
    return store


def _pins_collide(left: Pin, right: Pin) -> bool:
    return left.path == right.path or (
        left.st_dev,
        left.st_ino,
    ) == (right.st_dev, right.st_ino)


def _refuse_duplicate_pins(store: IdentityStore) -> None:
    for first in range(len(store.repositories)):
        for second in range(first + 1, len(store.repositories)):
            left, right = store.repositories[first], store.repositories[second]
            if _pins_collide(left.common_dir, right.common_dir):
                raise _StageRefusal(
                    _STORE_GRAPH, _repository_corruption(store, left, right)
                )
    for first in range(len(store.checkouts)):
        for second in range(first + 1, len(store.checkouts)):
            _refuse_checkout_pin_collision(
                store.checkouts[first], store.checkouts[second]
            )


def _refuse_checkout_pin_collision(left: Checkout, right: Checkout) -> None:
    if _pins_collide(left.boundary, right.boundary):
        raise _StageRefusal(
            _STORE_GRAPH,
            f"Checkouts {left.id} and {right.id} pin the same worktree "
            "boundary; a single worktree cannot be bound to two Projects; the "
            "identity store is corrupt and never admits this Run",
        )
    if _pins_collide(left.git_dir, right.git_dir):
        raise _StageRefusal(
            _STORE_GRAPH,
            f"Checkouts {left.id} and {right.id} pin the same worktree Git "
            "directory; the identity store is corrupt and never admits this Run",
        )


def _client_keys_of(store: IdentityStore, repository_id: str) -> set[str]:
    """Return the Client keys bound to one Repository through its Checkouts."""

    keys: set[str] = set()
    for checkout in store.checkouts:
        if checkout.repository_id != repository_id:
            continue
        project = _find_project(store, checkout.project_id)
        if project is None:
            raise _StageRefusal(
                _STORE_GRAPH,
                f"the identity store is corrupt: Checkout {checkout.id} "
                "references a missing Project",
            )
        client = _find_client(store, project.client_id)
        if client is None:
            raise _StageRefusal(
                _STORE_GRAPH,
                f"the identity store is corrupt: Project {project.id} "
                "references a missing Client",
            )
        keys.add(client.key)
    return keys


def _repository_corruption(
    store: IdentityStore, left: Repository, right: Repository
) -> str:
    shared = _client_keys_of(store, left.id) | _client_keys_of(store, right.id)
    if len(shared) > 1:
        names = ", ".join(sorted(shared))
        return (
            f"Repositories {left.id} and {right.id} pin one Git common directory "
            f"while Checkouts of more than one Client ({names}) reference them, "
            "so cross-Client Project Data Isolation cannot be established; the "
            "identity store is corrupt and never admits this Run"
        )
    return (
        f"Repositories {left.id} and {right.id} pin one Git common directory; "
        "the identity store is corrupt and never admits this Run; repair it "
        "with 'factory project repository re-register'"
    )


def _selected_checkout(store: IdentityStore, selection: RunSelection) -> Checkout:
    checkout = _find_checkout(store, selection.checkout_id)
    if checkout is None:
        raise _StageRefusal(
            _SCOPE,
            f"the selected Checkout {_quoted(selection.checkout_id)} is absent "
            "from the identity store",
        )
    project = _find_project(store, checkout.project_id)
    if project is None:
        raise _StageRefusal(
            _SCOPE,
            f"Checkout {_quoted(checkout.id)} references a missing Project in "
            "the identity store",
        )
    if (
        project.id != selection.project_id
        or project.client_id != selection.client_id
        or checkout.repository_id != selection.repository_id
        or checkout.boundary.path != str(selection.boundary)
    ):
        raise _StageRefusal(
            _SCOPE,
            f"the recorded selection of Checkout {_quoted(checkout.id)} no "
            "longer matches the identity store; select the Run again",
        )
    return checkout


def _scan_association(store: IdentityStore, checkout: Checkout) -> None:
    """Refuse a Repository whose referencing Checkouts span two Clients."""

    keys = _client_keys_of(store, checkout.repository_id)
    if len(keys) > 1:
        names = ", ".join(sorted(keys))
        raise _StageRefusal(
            _ASSOCIATION,
            f"Repository {checkout.repository_id} is bound to Checkouts of "
            f"more than one Client ({names}); a Git common directory is not "
            "partitioned by Client, so Project Data Isolation cannot be "
            "proven and cross-Client shared Repositories never admit Runs.",
            facts={"repository_id": checkout.repository_id, "clients": names},
        )


def _run_git(
    boundary: Path, arguments: list[str]
) -> subprocess.CompletedProcess[bytes]:
    """Run one read-only Git inspection, bounded and without overrides."""

    try:
        return subprocess.run(
            ["git", "--no-optional-locks", "-C", os.fspath(boundary), *arguments],
            capture_output=True,
            check=False,
            env=_bounded_environment(boundary),
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise _GitAssessmentError(
            f"git {arguments[0]} timed out after {_GIT_TIMEOUT_SECONDS:g} seconds"
        ) from error
    except OSError as error:
        raise _GitAssessmentError(f"Git is unavailable: {error}") from error


def _bounded_environment(boundary: Path) -> dict[str, str]:
    """Return the identity environment with Git discovery bounded to the tree.

    A required submodule that Git cannot open as a repository must refuse as
    UNKNOWN, not silently answer with the enclosing repository's identity, so
    discovery is forbidden from walking above the inspected boundary.
    """

    environment = _git_environment()
    environment["GIT_CEILING_DIRECTORIES"] = os.fspath(boundary.parent)
    return environment


def _object_format(boundary: Path) -> str:
    try:
        result = _run_git(boundary, ["rev-parse", "--show-object-format"])
    except _GitAssessmentError as error:
        raise _StageRefusal(_PROOF, f"Git inspection failed: {error}") from error
    text = os.fsdecode(result.stdout).strip()
    if result.returncode or not text:
        raise _StageRefusal(
            _PROOF, "Git inspection failed: the object format is unreadable"
        )
    return text


def _fresh_proof(boundary: Path) -> _Proof:
    """Gather the full fresh proof of the selected Checkout."""

    try:
        root, git_dir, common, head = _git_proof(boundary, str(boundary))
    except IdentityError as error:
        raise _StageRefusal(_PROOF, str(error)) from error
    return _Proof(boundary, root, git_dir, common, head, _object_format(boundary))


def _check_drift(
    store: IdentityStore,
    checkout: Checkout,
    selection: RunSelection,
    proof: _Proof,
) -> None:
    """Refuse a Checkout or Repository whose pinned identity drifted."""

    components = (
        ("boundary", checkout.boundary, proof.worktree_root),
        ("worktree root", checkout.worktree_root, proof.worktree_root),
        ("Git directory", checkout.git_dir, proof.git_dir),
        ("common directory", checkout.common_dir, proof.common_dir),
    )
    for name, stored, fresh in components:
        if stored == fresh:
            continue
        raise _StageRefusal(
            _DRIFT,
            f"Checkout {checkout.id} is stale: its registered {name} "
            f"{_quoted(stored.path)} no longer matches the checkout at "
            f"{_quoted(fresh.path)}; the checkout identity changed and "
            "requires explicit re-registration with 'factory project checkout "
            f"re-register {checkout.id} {_quoted(fresh.path)}'",
            facts={"checkout_id": checkout.id},
        )
    repository = _find_repository(store, checkout.repository_id)
    if repository is None:
        raise _StageRefusal(
            _STORE_GRAPH,
            f"the identity store is corrupt: Checkout {checkout.id} references "
            "a missing Repository",
        )
    if repository.common_dir != proof.common_dir:
        _refuse_repository_drift(store, checkout, repository, selection, proof)


def _refuse_repository_drift(
    store: IdentityStore,
    checkout: Checkout,
    repository: Repository,
    selection: RunSelection,
    proof: _Proof,
) -> None:
    foreign = _foreign_repository(store, proof.common_dir, selection)
    if foreign is not None:
        raise _StageRefusal(
            _DRIFT,
            f"Checkout {checkout.id} now resolves to the Git common directory "
            f"of Repository {foreign.id} bound to another Client; a shared "
            "common directory never admits Runs; restore the Checkout to its "
            "expected Repository or explicitly repair its registration.",
            facts={"checkout_id": checkout.id, "repository_id": foreign.id},
        )
    raise _StageRefusal(
        _DRIFT,
        f"Checkout {checkout.id} resolves to the Git common directory "
        f"{_quoted(proof.common_dir.path)} but its Repository {repository.id} "
        f"is pinned at {_quoted(repository.common_dir.path)}; repair the "
        "Repository with 'factory project repository re-register "
        f"{repository.id} {_quoted(proof.common_dir.path)}'",
        facts={"checkout_id": checkout.id, "repository_id": repository.id},
    )


def _foreign_repository(
    store: IdentityStore, fresh: Pin, selection: RunSelection
) -> Repository | None:
    """Return the foreign Repository whose pin the fresh common dir matches."""

    for repository in store.repositories:
        if not _pins_collide(repository.common_dir, fresh):
            continue
        for checkout in store.checkouts:
            if checkout.repository_id != repository.id:
                continue
            project = _find_project(store, checkout.project_id)
            if project is not None and project.client_id != selection.client_id:
                return repository
    return None


def _find_checkout(store: IdentityStore, checkout_id: str) -> Checkout | None:
    return next((c for c in store.checkouts if c.id == checkout_id), None)


def _find_project(store: IdentityStore, project_id: str) -> ProjectRecord | None:
    return next((p for p in store.projects if p.id == project_id), None)


def _find_client(store: IdentityStore, client_id: str) -> Client | None:
    return next((c for c in store.clients if c.id == client_id), None)


def _find_repository(store: IdentityStore, repository_id: str) -> Repository | None:
    return next((r for r in store.repositories if r.id == repository_id), None)


def _pin_evidence(pin: Pin) -> dict[str, object]:
    return {"path": pin.path, "st_dev": pin.st_dev, "st_ino": pin.st_ino}


def _assessment_document(status: _TreeStatus) -> dict[str, object]:
    return {
        "outcome": "dirty" if status.dirty else "clean",
        "staged": list(status.staged),
        "unstaged": list(status.unstaged),
        "untracked": list(status.untracked),
        "conflicts": list(status.conflicts),
        "submodules": list(status.submodules),
    }


def _assess_with_recheck(checkout: Checkout, proof: _Proof) -> _TreeStatus:
    """Assess the whole worktree, then recheck the proof the observation is
    bound to; a change during the interval refuses the Run."""

    try:
        status = _assess_tree(proof.boundary)
    except _GitAssessmentError as error:
        raise _StageRefusal(
            _GIT, f"the Checkout Git Assessment cannot establish the state: {error}"
        ) from error
    except RecursionError as error:
        raise _StageRefusal(
            _GIT,
            "the Checkout Git Assessment cannot establish the state: the nested "
            "submodule structure is too deeply nested to inspect",
        ) from error
    _recheck_during_assessment(checkout, proof)
    return status


def _recheck_during_assessment(checkout: Checkout, proof: _Proof) -> None:
    try:
        root, git_dir, common, head = _git_proof(proof.boundary, str(proof.boundary))
    except IdentityError as error:
        raise _StageRefusal(
            _GIT,
            f"Checkout {checkout.id} changed during its Git Assessment: {error}",
        ) from error
    if (root, git_dir, common, head) != (
        proof.worktree_root,
        proof.git_dir,
        proof.common_dir,
        proof.head,
    ):
        raise _StageRefusal(
            _GIT,
            f"Checkout {checkout.id} changed during its Git Assessment; the "
            "observation cannot be bound to one checkout identity",
        )


def _assess_tree(worktree: Path) -> _TreeStatus:
    """Assess one tree's whole worktree, recursing into required submodules."""

    _require_assessable_index(worktree)
    entries = _status_entries(worktree)
    staged: list[str] = []
    unstaged: list[str] = []
    untracked: list[str] = []
    conflicts: list[str] = []
    for entry in entries:
        if entry.index_status + entry.worktree_status in _UNMERGED:
            conflicts.append(entry.path)
        elif entry.index_status == "?":
            untracked.append(entry.path)
        else:
            if entry.index_status != " ":
                staged.append(entry.path)
            if entry.worktree_status != " ":
                unstaged.append(entry.path)
    submodules = tuple(
        _submodule_evidence(worktree, link, entries) for link in _gitlinks(worktree)
    )
    return _TreeStatus(
        staged=tuple(staged),
        unstaged=tuple(unstaged),
        untracked=tuple(untracked),
        conflicts=tuple(conflicts),
        submodules=submodules,
    )


def _require_assessable_index(worktree: Path) -> None:
    """Refuse index entries that can hide required changes from inspection."""

    result = _run_git(worktree, ["ls-files", "-v", "-z"])
    if result.returncode:
        raise _GitAssessmentError(
            f"git ls-files failed with exit status {result.returncode}"
        )
    marked = []
    for field in result.stdout.split(b"\0"):
        if not field:
            continue
        text = os.fsdecode(field)
        tag, separator, path = text[:1], text[1:2], text[2:]
        if separator != " ":
            raise _GitAssessmentError("git ls-files produced malformed output")
        if tag == "S" or (tag and tag.islower()):
            marked.append(path)
    if marked:
        listed = ", ".join(_quoted(path) for path in marked)
        raise _GitAssessmentError(
            "the index cannot support a complete assessment: "
            f"{listed} {'is' if len(marked) == 1 else 'are'} marked skip-worktree "
            "or assume-unchanged; unset those flags before running"
        )


def _status_entries(worktree: Path) -> list[_StatusEntry]:
    result = _run_git(
        worktree,
        [
            "status",
            "--porcelain",
            "--no-renames",
            "--untracked-files=all",
            "--ignore-submodules=none",
            "-z",
        ],
    )
    if result.returncode:
        raise _GitAssessmentError(
            f"git status failed with exit status {result.returncode}"
        )
    entries = []
    for field in result.stdout.split(b"\0"):
        if not field:
            continue
        text = os.fsdecode(field)
        if len(text) < 4 or text[2] != " ":
            raise _GitAssessmentError("git status produced malformed output")
        entries.append(_StatusEntry(text[0], text[1], text[3:]))
    return entries


def _gitlinks(worktree: Path) -> tuple[_Gitlink, ...]:
    """Return every required submodule the index records for one tree."""

    result = _run_git(worktree, ["ls-files", "--stage", "-z"])
    if result.returncode:
        raise _GitAssessmentError(
            f"git ls-files failed with exit status {result.returncode}"
        )
    links = []
    for field in result.stdout.split(b"\0"):
        if not field:
            continue
        text = os.fsdecode(field)
        meta, separator, path = text.partition("\t")
        parts = meta.split(" ")
        if separator != "\t" or len(parts) != 3:
            raise _GitAssessmentError("git ls-files produced malformed output")
        if parts[0] == "160000":
            links.append(_Gitlink(path, parts[1]))
    return tuple(links)


def _submodule_evidence(
    worktree: Path, link: _Gitlink, entries: list[_StatusEntry]
) -> dict[str, object]:
    """Retain and compare one required submodule's three commit identities."""

    entry: dict[str, object] = {"path": link.path, "index_gitlink": link.index_gitlink}
    status = next((e for e in entries if e.path == link.path), None)
    if status is not None and "D" in (status.index_status, status.worktree_status):
        entry["deleted"] = True
        return entry
    head_gitlink = _head_gitlink(worktree, link.path)
    if head_gitlink is not None:
        entry["head_gitlink"] = head_gitlink
        entry["staged_gitlink_change"] = head_gitlink != link.index_gitlink
    checkout_head = _checkout_head(worktree / link.path, link.path)
    entry["checkout_head"] = checkout_head
    entry["checkout_commit_mismatch"] = checkout_head != link.index_gitlink
    nested = _assess_tree(worktree / link.path)
    entry["changes"] = list(nested.changed_paths)
    entry["submodules"] = list(nested.submodules)
    return entry


def _submodule_dirty(entry: Mapping[str, object]) -> bool:
    return bool(
        entry.get("deleted")
        or entry.get("staged_gitlink_change")
        or entry.get("checkout_commit_mismatch")
        or entry.get("changes")
    )


def _head_gitlink(worktree: Path, path: str) -> str | None:
    """Return the gitlink commit HEAD records for one path, or None."""

    result = _run_git(worktree, ["ls-tree", "HEAD", "--", path])
    if result.returncode:
        return None
    meta, separator, _path = os.fsdecode(result.stdout).partition("\t")
    fields = meta.split(" ")
    if (
        separator == "\t"
        and len(fields) == 3
        and fields[0] == "160000"
        and fields[1] == "commit"
    ):
        return fields[2]
    return None


def _checkout_head(inner: Path, path: str) -> str:
    result = _run_git(inner, ["rev-parse", "--verify", "HEAD^{commit}"])
    if result.returncode:
        if b"not a git repository" in result.stderr:
            cause = "it is not an initialized Git repository"
        else:
            cause = "it has no readable commit HEAD"
        raise _GitAssessmentError(
            f"the required submodule {_quoted(path)} cannot be inspected: {cause}"
        )
    return os.fsdecode(result.stdout.strip())


def _warn_dirty(
    selection: RunSelection, head: str, assessment: Mapping[str, object]
) -> None:
    """Warn on stderr that the observed working tree is dirty; never refuse."""

    if assessment.get("outcome") != "dirty":
        return
    parts: list[str] = []
    for name in ("staged", "unstaged", "untracked", "conflicts"):
        paths = assessment.get(name)
        if isinstance(paths, list) and paths:
            parts.append(f"{name}: {', '.join(_quoted(path) for path in paths)}")
    submodules = assessment.get("submodules")
    if isinstance(submodules, list):
        parts.extend(
            summary for entry in submodules if (summary := _submodule_summary(entry))
        )
    detail = "; ".join(parts)
    sys.stderr.write(
        f"factory: warning: Checkout {selection.checkout_id} (client "
        f"{selection.client_id}, project {selection.project_id}, repository "
        f"{selection.repository_id}) at {_quoted(str(selection.boundary))} is "
        f"dirty at HEAD {head}: {detail}. Ignored paths are excluded, and the "
        "observed working tree is not fully represented by HEAD.\n"
    )


def _submodule_summary(entry: Mapping[str, object]) -> str:
    """Summarize one submodule's dirtiness, or "" when it is clean."""

    path = _quoted(str(entry.get("path")))
    flags = []
    if entry.get("staged_gitlink_change"):
        flags.append("staged gitlink change")
    if entry.get("checkout_commit_mismatch"):
        flags.append("checkout commit mismatch")
    if entry.get("deleted"):
        flags.append("deleted")
    changes = entry.get("changes")
    if isinstance(changes, list) and changes:
        flags.append(
            "changes: " + ", ".join(_quoted(str(change)) for change in changes)
        )
    nested = entry.get("submodules")
    if isinstance(nested, list):
        flags.extend(
            summary for child in nested if (summary := _submodule_summary(child))
        )
    if not flags:
        return ""
    return f"submodule {path}: " + ", ".join(flags)
