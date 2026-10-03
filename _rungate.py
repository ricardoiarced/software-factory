"""The Shared Run Gate: deterministic identity and Git admission stages.

After Run Request composition and its size gate, and before Run Admission,
the gate re-establishes — inside the already-open Run Record — that the
selected Checkout still proves membership in its pinned Repository, that the
Repository is associated with exactly one Client through its referencing
Checkouts, and that the identity-store records needed to establish those
facts are internally consistent. A fresh Checkout Git Assessment then reads
the whole worktree. Every stage lands in the record: passing stages as gate
evidence, a failing stage as a structured refusal naming it. A Git common
directory bound to Checkouts of more than one Client never admits a Run, and
dirtiness warns without refusing (ADR 0003).
"""

import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

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
from _runrecord import RunRecord

_GIT_TIMEOUT_SECONDS = 60.0
_UNMERGED = frozenset({"DD", "AU", "UD", "UA", "DU", "AA", "UU"})
_STORE_GRAPH = "store-graph"
_SCOPE = "scope"
_ASSOCIATION = "association"
_PROOF = "proof"
_DRIFT = "drift"
_GIT = "git"


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
    """The identity and Git evidence one passed Shared Run Gate carries."""

    head: str
    object_format: str
    pins: Mapping[str, Mapping[str, object]]
    assessment: Mapping[str, object]


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


def run_gate(record: RunRecord, selection: RunSelection) -> None:
    """Run the ordered gate stages for one selected Checkout, recording them.

    A passing gate commits its identity and Git evidence into the record; a
    failing stage commits a structured refusal naming its stage and then
    refuses the Run. No gate stage lands unrecorded.
    """

    try:
        observation = _gate_stages(selection)
    except _StageRefusal as refusal:
        record.refuse(refusal.stage, refusal.detail, facts=refusal.facts)
        raise RunGateError(refusal.detail) from None
    record.gated(observation)
    _warn_dirty(selection, observation.head, observation.assessment)


def _gate_stages(selection: RunSelection) -> GateObservation:
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
    )


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
