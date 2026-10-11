"""The durable Run Record envelope and launch atomicity.

Every Run opens one crash-durable audit record after successful CLI parsing
and before any operational selection, scope resolution, composition, or
admission work. The record holds only facts the pre-gate Run path actually
knows; unset evidence stays absent, never synthesized. The Launch Commit is
written crash-durably immediately after the final pre-spawn check and before
process creation, so from this envelope onward process creation is never
attempted without a preceding durable authorization. The store is audit
evidence only: strict readback exists to inspect it, and no Run-path
behavior reads recorded history to resume, replay, or admit a Run.
"""

import errno
import json
import os
import re
import secrets
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast

from _project_registry import RegistryError, _open_config_directory

_RUNS_DIRECTORY = "runs"
_RECORD_SCHEMA = "factory-run-v1"
_ARGV_SCHEMA = "factory-argv-v1"
_RUN_ID_PATTERN = re.compile(r"run_[0-9a-f]{32}")
_RECORD_FIELDS = frozenset(
    {
        "schema",
        "id",
        "argv",
        "harness",
        "opened_at",
        "status",
        "selection",
        "gate",
        "launch",
        "refusal",
        "outcome",
    }
)
_RECORD_CORE_FIELDS = frozenset(
    {"schema", "id", "argv", "harness", "opened_at", "status"}
)
_RECORD_STATUSES = ("open", "refused", "launched")
_SELECTION_FIELDS = frozenset(
    {
        "client_id",
        "project_id",
        "repository_id",
        "checkout_id",
        "boundary",
    }
)
_GATE_FIELDS = frozenset({"at", "head", "object_format", "proof", "git", "policy"})
_REFUSAL_FIELDS = frozenset({"stage", "detail", "at"})
_LAUNCH_REQUIRED_FIELDS = frozenset(
    {"committed_at", "harness", "executable", "invocation", "plan_fingerprint"}
)
_LAUNCH_FIELDS = _LAUNCH_REQUIRED_FIELDS | frozenset(
    {"environment", "unset_environment"}
)
_ID_ATTEMPTS = 5
_TEMPORARY_ATTEMPTS = 100


class RunRecordError(Exception):
    """A Run Record cannot be opened or durably updated."""


class RunSelectionFacts(Protocol):
    """The immutable identities one verified Run selection resolves."""

    @property
    def client_id(self) -> str: ...

    @property
    def project_id(self) -> str: ...

    @property
    def repository_id(self) -> str: ...

    @property
    def checkout_id(self) -> str: ...

    @property
    def boundary(self) -> Path: ...


class RunGateFacts(Protocol):
    """The identity, Git, and policy evidence one passed Shared Run Gate carries."""

    @property
    def head(self) -> str: ...

    @property
    def object_format(self) -> str: ...

    @property
    def pins(self) -> Mapping[str, Mapping[str, object]]: ...

    @property
    def assessment(self) -> Mapping[str, object]: ...

    @property
    def policy(self) -> Mapping[str, object]: ...


class LaunchAuthorization(Protocol):
    """The admitted launch plan a Launch Commit records as authorization.

    The certified environment edits ride as ``pins`` and ``removals`` — the
    plan's complete effective environment is its launch source and never
    enters the record — and ``fingerprint`` binds the commit to the sealed
    Validated Run Plan it authorizes.
    """

    @property
    def harness(self) -> str: ...

    @property
    def executable(self) -> Path: ...

    @property
    def invocation(self) -> tuple[str, ...]: ...

    @property
    def pins(self) -> tuple[tuple[str, str], ...]: ...

    @property
    def removals(self) -> tuple[str, ...]: ...

    @property
    def fingerprint(self) -> str: ...


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def argv_document(argv: Sequence[str]) -> dict[str, object]:
    """Return the pinned ``factory-argv-v1`` serialization of one command line."""

    return {"schema": _ARGV_SCHEMA, "argv": list(argv)}


def _serialize(document: dict[str, object]) -> bytes:
    return (json.dumps(document, ensure_ascii=True, indent=2) + "\n").encode("ascii")


def _open_runs_directory() -> int:
    """Open the per-Run record directory, creating it durably when absent."""

    config = _open_config_directory(create=True)
    try:
        try:
            return os.open(
                _RUNS_DIRECTORY,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=config,
            )
        except FileNotFoundError:
            pass
        try:
            os.mkdir(_RUNS_DIRECTORY, mode=0o700, dir_fd=config)
        except FileExistsError:
            pass
        os.fsync(config)
        return os.open(
            _RUNS_DIRECTORY,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=config,
        )
    finally:
        os.close(config)


def _temporary_record(runs: int) -> tuple[int, str]:
    for _ in range(_TEMPORARY_ATTEMPTS):
        name = f".run-{secrets.token_hex(8)}"
        try:
            descriptor = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=runs,
            )
        except FileExistsError:
            continue
        return descriptor, name
    raise FileExistsError(errno.EEXIST, "cannot create unique Run Record temporary")


def _replace_document(runs: int, name: str, document: dict[str, object]) -> None:
    """Replace one record file with a durable same-directory generation."""

    payload = _serialize(document)
    temporary: str | None = None
    replaced = False
    try:
        descriptor, temporary = _temporary_record(runs)
        with os.fdopen(descriptor, "wb") as target:
            target.write(payload)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, name, src_dir_fd=runs, dst_dir_fd=runs)
        replaced = True
        os.fsync(runs)
    except OSError as error:
        if temporary is not None and not replaced:
            try:
                os.unlink(temporary, dir_fd=runs)
            except FileNotFoundError:
                pass
        raise RunRecordError(f"Run Record cannot be updated: {error}") from error


class RunRecord:
    """One Run's durable audit record, rewritten whole on every update."""

    def __init__(self, identifier: str, document: dict[str, object]) -> None:
        self._identifier = identifier
        self._document = document

    @property
    def identifier(self) -> str:
        return self._identifier

    def selected(self, selection: RunSelectionFacts) -> None:
        """Durably record the immutable identities of the selected Checkout."""

        if self._document["status"] != "open":
            raise RunRecordError(
                f"Run Record {self._identifier} is not open for selection evidence"
            )
        document = {
            **self._document,
            "selection": {
                "client_id": selection.client_id,
                "project_id": selection.project_id,
                "repository_id": selection.repository_id,
                "checkout_id": selection.checkout_id,
                "boundary": str(selection.boundary),
            },
        }
        self._store(document)
        self._document = document

    def refuse(
        self,
        stage: str,
        detail: str,
        *,
        facts: Mapping[str, str] | None = None,
    ) -> None:
        """Commit one classified refusal best-effort, never masking the Run."""

        if self._document["status"] != "open":
            return
        refusal: dict[str, object] = {
            "stage": stage,
            "detail": detail,
            "at": _timestamp(),
        }
        if facts:
            refusal["facts"] = dict(sorted(facts.items()))
        document = {**self._document, "status": "refused", "refusal": refusal}
        try:
            self._store(document)
            self._document = document
        except (RunRecordError, OSError, RegistryError):
            pass

    def gated(self, facts: RunGateFacts) -> None:
        """Durably record the Shared Run Gate's identity and Git evidence.

        The gate stages run before admission, so their evidence commits
        strictly: a store that cannot record it refuses the Run rather than
        admitting a stage that landed unrecorded.
        """

        if self._document["status"] != "open":
            raise RunRecordError(
                f"Run Record {self._identifier} is not open for gate evidence"
            )
        gate: dict[str, object] = {
            "at": _timestamp(),
            "head": facts.head,
            "object_format": facts.object_format,
            "proof": {name: dict(facts.pins[name]) for name in sorted(facts.pins)},
            "git": facts.assessment,
            "policy": dict(facts.policy),
        }
        document = {**self._document, "gate": gate}
        self._store(document)
        self._document = document

    def commit_launch(self, authorization: LaunchAuthorization) -> None:
        """Write the crash-durable Launch Commit; a failure refuses the launch."""

        if self._document["status"] != "open":
            raise RunRecordError(
                f"Run Record {self._identifier} is not open for a Launch Commit"
            )
        launch: dict[str, object] = {
            "committed_at": _timestamp(),
            "harness": authorization.harness,
            "executable": str(authorization.executable),
            "invocation": list(authorization.invocation),
            "plan_fingerprint": authorization.fingerprint,
        }
        if authorization.pins:
            launch["environment"] = {name: value for name, value in authorization.pins}
        if authorization.removals:
            launch["unset_environment"] = list(authorization.removals)
        document = {**self._document, "status": "launched", "launch": launch}
        self._store(document)
        self._document = document

    def record_outcome(self, outcome: Mapping[str, object]) -> None:
        """Record one post-launch outcome best-effort, never altering the Run.

        The first durably recorded outcome is final: a later outcome is
        dropped, so the Run's outcome history is exactly one entry.
        """

        if self._document["status"] != "launched":
            return
        if "outcome" in self._document:
            return
        document = {
            **self._document,
            "outcome": {**outcome, "at": _timestamp()},
        }
        try:
            self._store(document)
            self._document = document
        except (RunRecordError, OSError, RegistryError):
            pass

    def _store(self, document: dict[str, object]) -> None:
        try:
            runs = _open_runs_directory()
        except (OSError, RegistryError) as error:
            raise RunRecordError(f"Run Record store cannot be opened: {error}") from (
                error
            )
        try:
            _replace_document(runs, f"{self._identifier}.json", document)
        finally:
            os.close(runs)


_ACTIVE_RECORD: RunRecord | None = None


@contextmanager
def run_record(argv: Sequence[str], harness: str) -> Generator[RunRecord]:
    """Open the active Run's durable record for the extent of one Run."""

    global _ACTIVE_RECORD
    record = _open_record(argv, harness)
    _ACTIVE_RECORD = record
    try:
        yield record
    finally:
        _ACTIVE_RECORD = None


def _open_record(argv: Sequence[str], harness: str) -> RunRecord:
    """Mint one Run ID and create its crash-durable record file."""

    try:
        runs = _open_runs_directory()
    except (OSError, RegistryError) as error:
        raise RunRecordError(f"Run Record store cannot be opened: {error}") from error
    try:
        for _ in range(_ID_ATTEMPTS):
            identifier = f"run_{secrets.token_hex(16)}"
            document: dict[str, object] = {
                "schema": _RECORD_SCHEMA,
                "id": identifier,
                "argv": argv_document(argv),
                "harness": harness,
                "opened_at": _timestamp(),
                "status": "open",
            }
            try:
                descriptor = os.open(
                    f"{identifier}.json",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=runs,
                )
            except FileExistsError:
                continue
            try:
                with os.fdopen(descriptor, "wb") as target:
                    target.write(_serialize(document))
                    target.flush()
                    os.fsync(target.fileno())
            except OSError as error:
                raise RunRecordError(
                    f"Run Record cannot be created: {error}"
                ) from error
            try:
                os.fsync(runs)
            except OSError as error:
                raise RunRecordError(
                    f"Run Record creation may not be durable: {error}"
                ) from error
            return RunRecord(identifier, document)
        raise RunRecordError("Run Record store cannot mint a unique Run ID")
    finally:
        os.close(runs)


def commit_pending_launch(authorization: LaunchAuthorization) -> None:
    """Write the active Run's Launch Commit; without one, write nothing."""

    if _ACTIVE_RECORD is not None:
        _ACTIVE_RECORD.commit_launch(authorization)


def record_run_outcome(outcome: Mapping[str, object]) -> None:
    """Record the active Run's post-launch outcome; without one, do nothing."""

    if _ACTIVE_RECORD is not None:
        _ACTIVE_RECORD.record_outcome(outcome)


def _unrecognized(path: Path, cause: str) -> RunRecordError:
    """Build the readback refusal naming the record and the violated schema."""

    return RunRecordError(
        f"Run Record {path.name} is not a recognized audit record: {cause}"
    )


def _require_timestamp(path: Path, value: object, label: str) -> None:
    if not isinstance(value, str):
        raise _unrecognized(path, f"the {label} is not a timestamp")
    try:
        datetime.fromisoformat(value)
    except ValueError as error:
        raise _unrecognized(path, f"the {label} is not a timestamp") from error


def _validate_selection(path: Path, value: object) -> None:
    if not isinstance(value, dict) or set(value) != _SELECTION_FIELDS:
        raise _unrecognized(path, "the selection evidence is unrecognized")
    if not all(isinstance(value[name], str) for name in _SELECTION_FIELDS):
        raise _unrecognized(path, "the selection evidence is unrecognized")


def _validate_gate(path: Path, value: object) -> None:
    if not isinstance(value, dict) or set(value) != _GATE_FIELDS:
        raise _unrecognized(path, "the gate evidence is unrecognized")
    for name in ("at", "head", "object_format"):
        if not isinstance(value[name], str):
            raise _unrecognized(path, "the gate evidence is unrecognized")
    for name in ("proof", "git", "policy"):
        if not isinstance(value[name], dict):
            raise _unrecognized(path, "the gate evidence is unrecognized")
    _require_timestamp(path, value["at"], "gate time")


def _validate_refusal(path: Path, value: object) -> None:
    shapes = (_REFUSAL_FIELDS, _REFUSAL_FIELDS | frozenset({"facts"}))
    if not isinstance(value, dict) or frozenset(value) not in shapes:
        raise _unrecognized(path, "the refusal is unrecognized")
    for name in _REFUSAL_FIELDS:
        if not isinstance(value[name], str):
            raise _unrecognized(path, "the refusal is unrecognized")
    facts = value.get("facts")
    if facts is not None and (
        not isinstance(facts, dict)
        or not all(
            isinstance(name, str) and isinstance(item, str)
            for name, item in facts.items()
        )
    ):
        raise _unrecognized(path, "the refusal is unrecognized")
    _require_timestamp(path, value["at"], "refusal time")


def _validate_launch(path: Path, value: object) -> None:
    if (
        not isinstance(value, dict)
        or not _LAUNCH_REQUIRED_FIELDS <= frozenset(value)
        or not frozenset(value) <= _LAUNCH_FIELDS
    ):
        raise _unrecognized(path, "the Launch Commit is unrecognized")
    for name in ("committed_at", "harness", "executable", "plan_fingerprint"):
        if not isinstance(value[name], str):
            raise _unrecognized(path, "the Launch Commit is unrecognized")
    invocation = value["invocation"]
    if not isinstance(invocation, list) or not all(
        isinstance(token, str) for token in invocation
    ):
        raise _unrecognized(path, "the Launch Commit is unrecognized")
    environment = value.get("environment")
    if environment is not None and (
        not isinstance(environment, dict)
        or not all(
            isinstance(name, str) and isinstance(pin, str)
            for name, pin in environment.items()
        )
    ):
        raise _unrecognized(path, "the Launch Commit is unrecognized")
    removals = value.get("unset_environment")
    if removals is not None and (
        not isinstance(removals, list)
        or not all(isinstance(name, str) for name in removals)
    ):
        raise _unrecognized(path, "the Launch Commit is unrecognized")
    _require_timestamp(path, value["committed_at"], "commit time")


def _validate_outcome(path: Path, value: object) -> None:
    payload = {"exit": "status", "signal": "signal", "spawn-failure": "detail"}
    if (
        not isinstance(value, dict)
        or "kind" not in value
        or value["kind"] not in payload
    ):
        raise _unrecognized(path, "the outcome is unrecognized")
    kind = cast("str", value["kind"])
    if frozenset(value) != frozenset({"kind", "at", payload[kind]}):
        raise _unrecognized(path, "the outcome is unrecognized")
    observed = value[payload[kind]]
    if kind == "spawn-failure":
        if not isinstance(observed, str):
            raise _unrecognized(path, "the outcome is unrecognized")
    elif type(observed) is not int:
        raise _unrecognized(path, "the outcome is unrecognized")
    _require_timestamp(path, value["at"], "outcome time")


def _validate_argv(path: Path, argv: object) -> None:
    if (
        not isinstance(argv, dict)
        or frozenset(argv) != frozenset({"schema", "argv"})
        or argv["schema"] != _ARGV_SCHEMA
        or not isinstance(argv["argv"], list)
        or not all(isinstance(token, str) for token in argv["argv"])
    ):
        raise _unrecognized(path, "the factory-argv-v1 document is unrecognized")


def _validate_status(path: Path, record: dict[str, object]) -> None:
    status = record["status"]
    if status not in _RECORD_STATUSES:
        raise _unrecognized(path, f"the status is {status!r}")
    if status == "open" and any(
        name in record for name in ("refusal", "launch", "outcome")
    ):
        raise _unrecognized(path, "an open record carries terminal evidence")
    if status == "refused":
        if "refusal" not in record:
            raise _unrecognized(path, "a refused record carries no refusal")
        if any(name in record for name in ("launch", "outcome")):
            raise _unrecognized(path, "a refused record carries launch evidence")
        _validate_refusal(path, record["refusal"])
    if status == "launched":
        if "launch" not in record:
            raise _unrecognized(path, "a launched record carries no Launch Commit")
        if "refusal" in record:
            raise _unrecognized(path, "a launched record carries a refusal")
        _validate_launch(path, record["launch"])
        if "outcome" in record:
            _validate_outcome(path, record["outcome"])


def _validate_record(path: Path, record: dict[str, object]) -> None:
    if not _RECORD_CORE_FIELDS <= frozenset(record):
        missing = sorted(_RECORD_CORE_FIELDS - frozenset(record))
        raise _unrecognized(path, f"missing fields {missing}")
    if record["schema"] != _RECORD_SCHEMA:
        raise _unrecognized(path, f"the schema is {record['schema']!r}")
    identifier = record["id"]
    if not isinstance(identifier, str) or not _RUN_ID_PATTERN.fullmatch(identifier):
        raise _unrecognized(path, "the Run ID is not a run_ token")
    if path.name != f"{identifier}.json":
        raise _unrecognized(path, "the file name does not match the Run ID")
    _validate_argv(path, record["argv"])
    for name in ("harness", "opened_at"):
        if not isinstance(record[name], str):
            raise _unrecognized(path, f"the {name} is not a string")
    _require_timestamp(path, record["opened_at"], "opening time")
    _validate_status(path, record)
    if "selection" in record:
        _validate_selection(path, record["selection"])
    if "gate" in record:
        _validate_gate(path, record["gate"])


def read_run_record(path: Path) -> dict[str, object]:
    """Read one Run Record strictly for audit; unrecognized state refuses.

    Readback is audit-only evidence inspection: it validates the frozen
    record schema and refuses every unrecognized state, and no Run-path
    behavior reads the store — recorded history never resumes, replays, or
    admits a Run.
    """

    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        raise RunRecordError(f"Run Record {path.name} cannot be read: {error}") from (
            error
        )
    try:
        with os.fdopen(descriptor, "rb") as source:
            payload = source.read()
    except OSError as error:
        raise RunRecordError(f"Run Record {path.name} cannot be read: {error}") from (
            error
        )
    try:
        document = json.loads(payload.decode("ascii"))
    except (UnicodeError, ValueError) as error:
        raise RunRecordError(f"Run Record {path.name} cannot be read: {error}") from (
            error
        )
    if not isinstance(document, dict):
        raise _unrecognized(path, "the document is not a JSON object")
    if not frozenset(document) <= _RECORD_FIELDS:
        unknown = sorted(frozenset(document) - _RECORD_FIELDS)
        raise _unrecognized(path, f"unknown fields {unknown}")
    record = cast("dict[str, object]", document)
    _validate_record(path, record)
    return record
