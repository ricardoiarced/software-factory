"""The Project Doctor: read-only observation of the Shared Run Gate.

``factory doctor project`` reports, as observation, the state the next Run
of one selected Project would meet: the resolved identity, the Checkout
inventory, the shared-Repository topology, and the Shared Run Gate over one
target Checkout, plus optional per-Harness admission observations under
``--harness``. The gate stages are the gate's own functions, so the report
reads top-down exactly as the Run would refuse, with byte-identical
diagnostics and the same precedence. Doctor never mints or mutates a Run
Record, never mutates identity state, never runs the Host Admission Probe,
never refreshes or invalidates its cache, and never submits a Run Request.
Configuration is presented classified by launch effectiveness (ADR 0006):
effective launch inputs, gated managed inputs, and ignored ambient
configuration, the last explicitly not a launch input.
"""

import json
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from _harness import RunAdmissionError
from _identity import (
    Checkout,
    IdentityError,
    IdentityStore,
    ProjectRecord,
    Repository,
    RunSelection,
    _client_by_id,
    _fresh_boundary,
    _project_by_id,
    _project_repositories,
    _quoted,
    _resolve_project,
    _selector,
    resolve_inspection_target,
    run_store,
)
from _rungate import (
    _ASSOCIATION,
    _DRIFT,
    _GIT,
    _PROOF,
    _SCOPE,
    _STORE_GRAPH,
    _assess_with_recheck,
    _assessment_document,
    _association_diagnostic,
    _check_drift,
    _client_keys_of,
    _dirty_message,
    _fresh_proof,
    _Proof,
    _scan_association,
    _selected_checkout,
    _StageRefusal,
    _validated_store,
)

STANDING_LABEL = (
    "Doctor observations are not Run admission; a Run re-validates every "
    "check before launch."
)
_NO_CHECKOUTS = (
    "No Run is admissible: Project {selector} has no Checkouts; register one "
    "with 'factory checkout register {selector} PATH'."
)
_STORED = "stored"
_OBSERVED = "observed"
_CACHED = "cached"
_NOT_REACHED = "not reached"
_CACHE_STEP = "host admission probe cache"
_CONFIGURED = "configured"
_GATE_STAGES = (_STORE_GRAPH, _SCOPE, _ASSOCIATION, _PROOF, _DRIFT, _GIT)
_LAUNCH_EFFECTIVENESS: Mapping[str, tuple[str, str, str]] = {
    "codex": (
        (
            "the certified -c overrides Factory sends are the effective launch "
            "inputs, validated as a whole by --strict-config"
        ),
        "Codex declares no managed-policy surface, so nothing is a gated managed input",
        (
            "the ambient Codex config and project rules that --ignore-user-config "
            "and --ignore-rules exclude are ignored ambient configuration; they "
            "are not a launch input, and their presence, absence, or validity "
            "never affects admission"
        ),
    ),
    "claude": (
        "the inline --settings document Factory authors is the effective launch input",
        (
            "the managed-policy surfaces observed by the managed-policy "
            "inspection are the gated managed inputs"
        ),
        (
            "the ambient Claude Code settings files that --restricted excludes, "
            "and the MCP sources that --strict-mcp-config confines, are ignored "
            "ambient configuration; they are not a launch input, and their "
            "presence, absence, or validity never affects admission"
        ),
    ),
}


class _MatchedPolicy(Protocol):
    """The matched Compatibility Set entry's surface the doctor observes."""

    version: str
    install_channel: str

    def _inspect_strict_configuration(
        self, boundary: Path, enclosing: Path
    ) -> None: ...

    def _host_probe_cache_payload(self) -> bytes: ...

    def _host_probe_cache_path(self, payload: bytes) -> Path: ...


@dataclass(frozen=True)
class _Observation:
    """One reported observation: subject, status, provenance, and detail."""

    subject: str
    status: str
    provenance: str
    detail: str | None = None


def doctor_project(
    selector: str,
    checkout: str | None,
    harness: str | None,
    invocation_dir: Path,
) -> int:
    """Report one Project's state as read-only observation; findings keep 0."""

    store = run_store()
    project = _resolve_project(store, selector)
    target = _inspection_target(store, project, checkout, invocation_dir)
    lines = [f"Project Doctor report for {selector}"]
    lines += _resolution_lines(store, project, target)
    lines += _inventory_lines(store, project, target)
    lines += _topology_lines(store, project)
    gate_lines, stopped, dirty = _gate_lines(store, project, target)
    lines += gate_lines
    refused = None
    if harness is not None:
        harness_lines, refused = _harness_lines(harness, _boundary_of(target))
        lines += harness_lines
    lines += _summary_lines(stopped, dirty, target is None, harness, refused)
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


def _inspection_target(
    store: IdentityStore,
    project: ProjectRecord,
    supplied: str | None,
    invocation_dir: Path,
) -> Checkout | None:
    """Resolve the target Checkout, or None when the Project has no Checkouts."""

    owned = [c for c in store.checkouts if c.project_id == project.id]
    if not owned and supplied is None:
        return None
    return resolve_inspection_target(store, project, supplied, invocation_dir)


def _boundary_of(target: Checkout | None) -> Path | None:
    return None if target is None else Path(target.boundary.path)


def _selection_of(store: IdentityStore, checkout: Checkout) -> RunSelection:
    """Return the recorded selection of one Checkout, as a Run carries it."""

    project = _project_by_id(store, checkout.project_id)
    return RunSelection(
        client_id=project.client_id,
        project_id=project.id,
        repository_id=checkout.repository_id,
        checkout_id=checkout.id,
        boundary=Path(checkout.boundary.path),
    )


def _repository_of(store: IdentityStore, checkout: Checkout) -> Repository:
    for repository in store.repositories:
        if repository.id == checkout.repository_id:
            return repository
    raise _StageRefusal(
        _STORE_GRAPH, f"Checkout {checkout.id} references a missing Repository"
    )


def _resolution_lines(
    store: IdentityStore, project: ProjectRecord, target: Checkout | None
) -> list[str]:
    client = _client_by_id(store, project.client_id)
    default = (
        "none" if project.default_checkout_id is None else project.default_checkout_id
    )
    lines = [
        "Resolution",
        f"  Client: {client.key} {client.id} ({_STORED})",
        f"  Project: {_selector(store, project)} {project.id} ({_STORED})",
        f"  Default checkout: {default} ({_STORED})",
    ]
    if target is not None:
        repository = _repository_of(store, target)
        lines.append(
            f"  Repository: {repository.id} at "
            f"{_quoted(repository.common_dir.path)} ({_STORED})"
        )
        lines.append(
            f"  Checkout: {target.id} at {_quoted(target.boundary.path)} ({_STORED})"
        )
    return lines


def _inventory_lines(
    store: IdentityStore, project: ProjectRecord, target: Checkout | None
) -> list[str]:
    client = _client_by_id(store, project.client_id)
    lines = [
        "Inventory",
        f"  Client: {client.key} {client.id} ({_STORED})",
    ]
    owned = sorted(
        (c for c in store.checkouts if c.project_id == project.id),
        key=lambda c: c.id,
    )
    for checkout in owned:
        status, cause = _checkout_status(store, checkout)
        marker = "" if checkout is not target else " (target)"
        lines.append(
            f"  Checkout: {checkout.id} at {_quoted(checkout.boundary.path)}"
            f"{marker} {status} ({_OBSERVED})"
        )
        if cause is not None:
            lines.append(f"    {cause}")
    for repository in _project_repositories(store, list(owned)):
        lines.append(
            f"  Repository: {repository.id} at "
            f"{_quoted(repository.common_dir.path)} ({_STORED})"
        )
    return lines


def _checkout_status(
    store: IdentityStore, checkout: Checkout
) -> tuple[str, str | None]:
    """Classify one Checkout as fresh, stale, or unknown, with its cause."""

    try:
        _fresh_boundary(checkout)
    except IdentityError as error:
        return "stale", f"{error} {_stale_remedy(checkout)}"
    except (OSError, RuntimeError) as error:
        return "unknown", f"the Checkout cannot be inspected: {error}"
    try:
        proof = _fresh_proof(Path(checkout.boundary.path))
        _check_drift(store, checkout, _selection_of(store, checkout), proof)
    except _StageRefusal as refusal:
        if refusal.stage == _PROOF:
            return "unknown", refusal.detail
        return "stale", refusal.detail
    return "fresh", None


def _stale_remedy(checkout: Checkout) -> str:
    return (
        "The gate refuses Runs until explicit re-registration with "
        "'factory project checkout re-register "
        f"{checkout.id} {_quoted(checkout.boundary.path)}'."
    )


def _topology_lines(store: IdentityStore, project: ProjectRecord) -> list[str]:
    lines = ["Shared Repository topology"]
    findings: list[str] = []
    shared = False
    for checkout in store.checkouts:
        if checkout.project_id != project.id or _reported(lines, checkout):
            continue
        selectors = sorted(
            {
                _selector(store, _project_by_id(store, sharing.project_id))
                for sharing in store.checkouts
                if sharing.repository_id == checkout.repository_id
            }
        )
        if len(selectors) > 1:
            shared = True
            lines.append(
                f"  Repository {checkout.repository_id} is bound to Projects "
                f"{', '.join(selectors)}. ({_STORED})"
            )
        keys = _client_keys_of(store, checkout.repository_id)
        if len(keys) > 1:
            findings.append(
                f"  fail: "
                f"{_association_diagnostic(checkout.repository_id, ', '.join(sorted(keys)))} "
                f"({_STORED})"
            )
    if not shared:
        lines.append(
            f"  No Repository of this Project is bound to Checkouts of more "
            f"than one Project. ({_STORED})"
        )
    return lines + findings


def _reported(lines: list[str], checkout: Checkout) -> bool:
    """Return whether one Repository line was already added, marking it then."""

    identifier = f"  Repository {checkout.repository_id} "
    return any(line.startswith(identifier) for line in lines)


def _gate_lines(
    store: IdentityStore, project: ProjectRecord, target: Checkout | None
) -> tuple[list[str], str | None, bool]:
    lines = ["Shared Run Gate observations"]
    if target is None:
        lines.append(
            f"  {_NO_CHECKOUTS.format(selector=_selector(store, project))} ({_STORED})"
        )
        return lines, None, False
    selection = _selection_of(store, target)
    observations, stopped = _observe_gate(store, selection, target)
    dirty = False
    for observation in observations:
        lines.append(
            f"  {observation.subject}: {observation.status} ({observation.provenance})"
        )
        if observation.detail is not None:
            lines.append(f"    {observation.detail}")
        dirty = dirty or observation.status == "warn"
    return lines, stopped, dirty


def _observe_gate(
    store: IdentityStore, selection: RunSelection, checkout: Checkout
) -> tuple[list[_Observation], str | None]:
    """Observe the Harness-independent gate stages in their fixed order."""

    observations: list[_Observation] = []
    fresh: _Proof | None = None
    head = ""

    def store_graph() -> None:
        nonlocal store
        store = _validated_store()

    def proof_stage() -> None:
        nonlocal fresh, head
        fresh = _fresh_proof(Path(checkout.boundary.path))
        head = fresh.head

    stopped = _observed_stage(observations, _STORE_GRAPH, store_graph)
    if stopped is None:
        stopped = _observed_stage(
            observations, _SCOPE, lambda: _selected_checkout(store, selection)
        )
    if stopped is None:
        stopped = _observed_stage(
            observations, _ASSOCIATION, lambda: _scan_association(store, checkout)
        )
    if stopped is None:
        stopped = _observed_stage(observations, _PROOF, proof_stage)
    if stopped is None:
        proven = fresh
        assert proven is not None, "the proof stage established the fresh proof"

        def drift() -> None:
            _check_drift(store, checkout, selection, proven)

        stopped = _observed_stage(observations, _DRIFT, drift)
        if stopped is None:
            stopped = _assess_stage(observations, selection, checkout, proven, head)
    if stopped is not None:
        recorded = {observation.subject for observation in observations}
        observations.extend(
            _Observation(
                stage,
                _NOT_REACHED,
                _NOT_REACHED,
                f"the gate stops at the {stopped.stage} stage",
            )
            for stage in _GATE_STAGES
            if stage not in recorded
        )
    return observations, None if stopped is None else stopped.stage


def _observed_stage(
    observations: list[_Observation], stage: str, action: Callable[[], object]
) -> _StageRefusal | None:
    """Record one gate stage's observation; return its refusal when it fails."""

    try:
        action()
    except _StageRefusal as refusal:
        observations.append(_Observation(stage, "fail", _OBSERVED, refusal.detail))
        return refusal
    observations.append(_Observation(stage, "ok", _OBSERVED))
    return None


def _assess_stage(
    observations: list[_Observation],
    selection: RunSelection,
    checkout: Checkout,
    proof: _Proof,
    head: str,
) -> _StageRefusal | None:
    """Record the Git Assessment stage; DIRTY warns while UNKNOWN refuses."""

    try:
        status = _assess_with_recheck(checkout, proof)
    except _StageRefusal as refusal:
        observations.append(_Observation(_GIT, "fail", _OBSERVED, refusal.detail))
        return refusal
    document = _assessment_document(status)
    if document["outcome"] == "dirty":
        observations.append(
            _Observation(
                _GIT, "warn", _OBSERVED, _dirty_message(selection, head, document)
            )
        )
        return None
    observations.append(_Observation(_GIT, "ok", _OBSERVED))
    return None


def _harness_lines(harness: str, boundary: Path | None) -> tuple[list[str], str | None]:
    lines = [f"Harness observations ({harness})"]
    observations = _observe_harness(harness, boundary)
    refused = None
    for observation in observations:
        lines.append(
            f"  {observation.subject}: {observation.status} ({observation.provenance})"
        )
        if observation.detail is not None:
            lines.append(f"    {observation.detail}")
        if observation.status == "fail":
            refused = observation.subject
    lines.append("  Configuration by launch effectiveness")
    names = (
        "Effective launch inputs",
        "Gated managed inputs",
        "Ignored ambient configuration",
    )
    for name, classification in zip(names, _LAUNCH_EFFECTIVENESS[harness], strict=True):
        lines.append(f"    {name}: {classification} ({_CONFIGURED})")
    return lines, refused


def _sequencer(
    observations: list[_Observation],
) -> Callable[[str, Callable[[], str | None]], None]:
    """Record admission observations in order; a failure stops the sequence."""

    failure: list[str] = []

    def attempt(subject: str, action: Callable[[], str | None]) -> None:
        if failure:
            observations.append(
                _Observation(
                    subject,
                    _NOT_REACHED,
                    _NOT_REACHED,
                    f"the admission stops at the {failure[0]} step",
                )
            )
            return
        provenance = _CACHED if subject == _CACHE_STEP else _OBSERVED
        try:
            detail = action()
        except RunAdmissionError as error:
            failure.append(subject)
            observations.append(_Observation(subject, "fail", provenance, str(error)))
        else:
            if detail is None:
                observations.append(
                    _Observation(
                        subject, _NOT_REACHED, _NOT_REACHED, "no target Checkout"
                    )
                )
            else:
                observations.append(_Observation(subject, "ok", provenance, detail))

    return attempt


def _observe_harness(harness: str, boundary: Path | None) -> list[_Observation]:
    observations: list[_Observation] = []
    attempt = _sequencer(observations)
    if harness == "codex":
        _codex_sequence(attempt, boundary)
    elif harness == "claude":
        _claude_sequence(attempt)
    else:
        raise RunAdmissionError(
            f"the Harness {_quoted(harness)} is not a registered Runner; "
            "no admission can be observed for it."
        )
    return observations


def _codex_sequence(
    attempt: Callable[[str, Callable[[], str | None]], None], boundary: Path | None
) -> None:
    from _codex import (
        _match_compatibility,
        _reported_version,
        _resolve_executable,
        verify_codex_newest_stable,
    )

    state: dict[str, object] = {}

    def resolve() -> str:
        executable = _resolve_executable()
        state["executable"] = executable
        return str(executable)

    def version() -> str:
        installed = _reported_version(cast("Path", state["executable"]))
        state["installed"] = installed
        return installed

    def newest() -> str:
        return verify_codex_newest_stable(
            cast("Path", state["executable"]), cast("str", state["installed"])
        )

    def compatibility() -> str:
        policy = _match_compatibility(
            cast("Path", state["executable"]), cast("str", state["installed"])
        )
        state["policy"] = policy
        return f"{policy.version} {policy.install_channel}"

    def inspection() -> str | None:
        if boundary is None:
            return None
        policy = cast("_MatchedPolicy", state["policy"])
        policy._inspect_strict_configuration(boundary, boundary)
        return "the certified inspectors accepted the strict configuration"

    def cache() -> str:
        return _probe_cache_state(cast("_MatchedPolicy", state["policy"]))

    attempt("executable resolution", resolve)
    attempt("local version", version)
    attempt("newest stable", newest)
    attempt("compatibility", compatibility)
    attempt("strict configuration", inspection)
    attempt("host admission probe cache", cache)


def _claude_sequence(
    attempt: Callable[[str, Callable[[], str | None]], None],
) -> None:
    from _claude import (
        _match_compatibility,
        _reported_version,
        _require_no_managed_policy,
        _resolve_executable,
        verify_claude_code_newest_stable,
    )

    state: dict[str, object] = {}

    def resolve() -> str:
        executable = _resolve_executable()
        state["executable"] = executable
        return str(executable)

    def version() -> str:
        installed = _reported_version(cast("Path", state["executable"]))
        state["installed"] = installed
        return installed

    def newest() -> str:
        return verify_claude_code_newest_stable(
            cast("Path", state["executable"]), cast("str", state["installed"])
        )

    def compatibility() -> str:
        policy = _match_compatibility(
            cast("Path", state["executable"]), cast("str", state["installed"])
        )
        state["policy"] = policy
        return f"{policy.version} {policy.install_channel}"

    def managed_policy() -> str:
        _require_no_managed_policy(cast("Path", state["executable"]))
        return "no managed-policy source is present or configured"

    def cache() -> str:
        return _probe_cache_state(cast("_MatchedPolicy", state["policy"]))

    attempt("executable resolution", resolve)
    attempt("local version", version)
    attempt("newest stable", newest)
    attempt("compatibility", compatibility)
    attempt("managed policy", managed_policy)
    attempt("host admission probe cache", cache)


def _probe_cache_state(policy: _MatchedPolicy) -> str:
    """Report the probe cache state without executing the probe or writing."""

    payload = policy._host_probe_cache_payload()
    path = policy._host_probe_cache_path(payload)
    try:
        recorded = path.read_bytes()
        age = max(0.0, time.time() - path.stat().st_mtime)
    except FileNotFoundError:
        return (
            "no Host Admission Probe cache entry exists for this Factory "
            "version, artifact, and host"
        )
    except OSError as error:
        return f"the Host Admission Probe cache entry cannot be inspected: {error}"
    if recorded != payload:
        return (
            "the Host Admission Probe cache entry does not match this Factory "
            "version, artifact, and host"
        )
    document = json.loads(recorded.decode("utf-8"))
    versions = ", ".join(
        f"{name.removesuffix('_version').replace('_', ' ')} {document[name]}"
        for name in sorted(document)
        if name.endswith("_version") and isinstance(document[name], str)
    )
    return (
        f"a Host Admission Probe success is cached ({versions}), recorded "
        f"{int(age)} seconds ago"
    )


def _summary_lines(
    stopped: str | None,
    dirty: bool,
    without_target: bool,
    harness: str | None,
    refused: str | None,
) -> list[str]:
    lines = ["Summary"]
    if without_target:
        lines.append(
            "  A Run would refuse before the Shared Run Gate: the Project has "
            "no Checkouts."
        )
    elif stopped is not None:
        lines.append(f"  A Run would stop at the {stopped} stage.")
    elif dirty:
        lines.append("  A Run would pass the Shared Run Gate with the dirty warning.")
    else:
        lines.append("  A Run would pass the Shared Run Gate.")
    if harness is not None and refused is not None:
        lines.append(f"  A {harness} Run would refuse admission at the {refused} step.")
    lines.append(STANDING_LABEL)
    return lines
