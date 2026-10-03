"""The durable Run Record envelope and launch atomicity.

Every Run opens one crash-durable audit record after successful CLI parsing
and before any operational selection, scope resolution, composition, or
admission work. The record holds only facts the pre-gate Run path actually
knows; unset evidence stays absent, never synthesized. The Launch Commit is
written crash-durably immediately after the final pre-spawn check and before
process creation, so from this envelope onward process creation is never
attempted without a preceding durable authorization.
"""

import errno
import json
import os
import secrets
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from _project_registry import RegistryError, _open_config_directory

_RUNS_DIRECTORY = "runs"
_RECORD_SCHEMA = "factory-run-v1"
_ARGV_SCHEMA = "factory-argv-v1"
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


class LaunchAuthorization(Protocol):
    """The admitted launch plan a Launch Commit records as authorization."""

    @property
    def harness(self) -> str: ...

    @property
    def executable(self) -> Path: ...

    @property
    def invocation(self) -> tuple[str, ...]: ...

    @property
    def environment(self) -> tuple[tuple[str, str], ...]: ...

    @property
    def unset_environment(self) -> tuple[str, ...]: ...


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
        }
        if authorization.environment:
            launch["environment"] = {
                name: value for name, value in authorization.environment
            }
        if authorization.unset_environment:
            launch["unset_environment"] = list(authorization.unset_environment)
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
