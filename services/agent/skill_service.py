"""Single-controller owner service over :class:`SkillManager` with task pins.

This module is the controller/pinning portion of the skills track. The
reviewed :mod:`packages.ecosystem.skills` foundation owns install bytes,
digests, and invocation flags; this service owns *who may run what, where*:

- one live :class:`SkillService` per control root (OS file lock),
- owner-managed content scopes persisted beside the registry,
- immutable per-task package pins served across updates,
- model-facing disclosure filtered by provider, scope, and author flags.

Trust notes: skill text, resources, and ``allowed-tools`` metadata are
untrusted input. They never grant policy, provider access, or proof of
completed work. Lifecycle authority (``authority="owner"``) is an internal
interface for the trusted launcher/command integration, never a model tool
argument, and ``invocation="user"`` is set only by that same trusted
integration. Model tool schemas must not expose ``authority``,
``invocation``, or ``content_scope`` choices.

Only the configured launcher imports this service. Base Agent paths must
not import it, and this module imports nothing from ``services.*``,
``packages.providers.*``, or any optional dependency: stdlib plus the
reviewed ``packages.ecosystem.skills`` helpers only.
"""

from __future__ import annotations

import asyncio
import copy
import fcntl
import hashlib
import json
import os
import re
import stat as _stat
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from packages.ecosystem.skills import (
    OWNER_AUTHORITY,
    RESOURCE_MAX_BYTES,
    SkillError,
    SkillManager,
    SourceRef,
    _make_dirs_no_follow,
    _open_dir_fd,
    _parse_skill_md_bytes,
    _require_real_dir,
)

# -- public constants (bounds are part of the contract) -------------------------

#: Owner-managed disclosure scopes. Skills without an explicit entry behave
#: as ``local_only`` (fail closed: unknown scope never discloses).
VALID_SCOPES = ("local_only", "public_synthetic", "private_cloud_authorized")
DEFAULT_SCOPE = "local_only"

#: Owner paging bound for :meth:`SkillService.list_owner`.
OWNER_PAGE_LIMIT = 8
#: Model catalog page bound for :meth:`SkillService.catalog`.
CATALOG_PAGE_ROWS = 4
#: Per-row description bound for the model catalog.
CATALOG_DESC_CHARS = 80
#: Whole-payload bound for the model catalog JSON.
CATALOG_JSON_CHARS = 1600
#: Whole-payload bound for every :meth:`SkillService.load` response JSON.
RESULT_JSON_CHARS = 12000

#: Pin memory bounds: at most this many task+skill pins holding at most
#: this many captured bytes in total. Pin creation fails explicitly first.
MAX_PINS = 32
MAX_PIN_BYTES = 32 * 1024 * 1024

#: Task id and cursor bounds.
TASK_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
SKILL_ID_RE = re.compile(
    r"\A(?:local|github:[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
    r"|clawhub:[A-Za-z0-9][A-Za-z0-9._-]{0,127})"
    r"/[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z"
)
MAX_AFTER = 1_000_000
MAX_RESOURCE_PATH = 1024

_LOCK_FILENAME = "controller.lock"
_SCOPES_FILENAME = "scopes.json"
_SCOPE_PENDING_FILENAME = "scope-change.pending"
_SCOPES_MAX_BYTES = 65_536
_SCOPES_MAX_ENTRIES = 4096

_MUTATE_OPS = (
    "install",
    "install_archive",
    "enable",
    "disable",
    "update",
    "update_archive",
    "rollback",
    "remove",
    "scope",
)


class SkillServiceError(SkillError):
    """A controller operation failed safely: denied, malformed, or bounded."""


@dataclass
class _Pin:
    """Immutable per-task snapshot of one verified skill package."""

    task_id: str
    skill_id: str
    digest: str
    source: dict[str, Any]
    name: str
    description: str
    version: str
    allowed_tools: tuple[str, ...] = ()
    model_invocable: bool = True
    user_invocable: bool = True
    blobs: dict[str, bytes] = field(default_factory=dict)
    total_bytes: int = 0


def _fail(message: str) -> SkillServiceError:
    return SkillServiceError(message)


def _is_meta_contributor(provider: str, model: str) -> bool:
    # Every Meta provider runs in Contributor mode, including aliases whose
    # configured model name does not contain "contributor". Mirrors the
    # ModelScopedMCP rule so skills and MCP servers disclose identically.
    return provider == "meta" or "contributor" in model.lower()


def _provider_flags(provider_info: Any) -> tuple[bool, bool]:
    """Return ``(local, contributor)`` from a ProviderInfo-like object.

    Accepts attribute or mapping access; anything else (or wrongly typed
    fields) is rejected instead of guessed.
    """
    if isinstance(provider_info, dict):
        local = provider_info.get("local")
        provider = provider_info.get("provider")
        model = provider_info.get("model")
    else:
        try:
            local = provider_info.local
            provider = provider_info.provider
            model = provider_info.model
        except AttributeError:
            raise _fail(
                "provider_info needs local/provider/model fields, "
                f"got {type(provider_info).__name__}"
            ) from None
    if not isinstance(local, bool):
        raise _fail("provider_info.local must be a boolean")
    if not isinstance(provider, str) or not provider:
        raise _fail("provider_info.provider must be a non-empty string")
    if not isinstance(model, str) or not model:
        raise _fail("provider_info.model must be a non-empty string")
    return local, _is_meta_contributor(provider, model)


def _scope_allows(scope: str, *, local: bool, contributor: bool) -> bool:
    if scope not in VALID_SCOPES:
        return False
    if local:
        return True
    if contributor:
        return scope == "public_synthetic"
    return scope in ("public_synthetic", "private_cloud_authorized")


def _check_task_id(task_id: Any) -> str:
    if not isinstance(task_id, str) or not TASK_ID_RE.match(task_id):
        raise _fail(f"task_id must match [A-Za-z0-9][A-Za-z0-9._-]{{0,127}}, got {task_id!r}")
    return task_id


def _check_skill_id(skill_id: Any) -> str:
    if not isinstance(skill_id, str) or not SKILL_ID_RE.match(skill_id):
        raise _fail(f"skill_id must look like <namespace>/<name>, got {skill_id!r}")
    return skill_id


def _check_after(after: Any) -> int:
    if isinstance(after, bool) or not isinstance(after, int):
        raise _fail(f"after must be an integer cursor, got {after!r}")
    if not 0 <= after <= MAX_AFTER:
        raise _fail(f"after must be within 0..{MAX_AFTER}, got {after!r}")
    return after


def _check_invocation(invocation: Any) -> str:
    if invocation not in ("user", "model"):
        raise _fail(f"unknown invocation: {invocation!r}")
    return invocation


def _check_content_scope(content_scope: Any) -> str:
    if not isinstance(content_scope, str) or content_scope not in VALID_SCOPES:
        raise _fail(f"content_scope must be one of {list(VALID_SCOPES)}, got {content_scope!r}")
    return content_scope


def _check_resource_path(resource: Any) -> str:
    if not isinstance(resource, str) or not resource:
        raise _fail(f"resource path must be a non-empty string, got {resource!r}")
    if len(resource) > MAX_RESOURCE_PATH:
        raise _fail("resource path too long")
    pure = PurePosixPath(resource)
    if pure.is_absolute():
        raise _fail(f"resource path must be relative: {resource!r}")
    if not pure.parts or any(part in ("", ".", "..") for part in pure.parts):
        raise _fail(f"resource path escapes skill dir: {resource!r}")
    return pure.as_posix()


def _check_source_path(source: Any) -> Path:
    if isinstance(source, Path):
        text = str(source)
    elif isinstance(source, str):
        text = source
    else:
        raise _fail(f"source must be a local path string, got {type(source).__name__}")
    if "://" in text or text.strip().lower().startswith("clawhub:"):
        # Network fetch is the lead's future responsibility. A URL-looking
        # source is rejected, never fetched and never opened as a path.
        raise _fail(
            "source must be an already-fetched local directory or archive path; "
            "network fetch is not performed here"
        )
    if not text or len(text) > 4096:
        raise _fail(f"source path is malformed: {text!r}")
    return Path(text)


def _check_source_ref(source_ref: Any) -> SourceRef | None:
    if source_ref is None:
        return None
    if isinstance(source_ref, dict):
        raise _fail("source_ref must be a typed SourceRef, never a dict")
    if not isinstance(source_ref, SourceRef):
        raise _fail(f"source_ref must be a typed SourceRef, got {type(source_ref).__name__}")
    return source_ref


class SkillService:
    """One live controller over a skill root, with task pins and scopes.

    ``root`` is the private manager directory; ``workspace`` is the granted
    Agent workspace, which must stay disjoint from ``root`` (enforced by the
    manager) and is never written by this service. Construction creates
    ``root`` without following links, takes a non-blocking exclusive
    :func:`fcntl.flock` on a no-follow regular ``0600`` lockfile (a second
    live controller for the same root is rejected), then builds the manager.
    There is no import, subprocess, or dependency-install path anywhere here.
    """

    def __init__(self, root: Path, workspace: Path) -> None:
        if isinstance(root, str):
            root = Path(root)
        if isinstance(workspace, str):
            workspace = Path(workspace)
        if not isinstance(root, Path) or not isinstance(workspace, Path):
            raise _fail("root and workspace must be paths")
        root_real, workspace_real = Path(os.path.realpath(root)), Path(os.path.realpath(workspace))
        if root_real.is_relative_to(workspace_real) or workspace_real.is_relative_to(root_real):
            raise _fail("Skill control root must be disjoint from the Agent workspace")
        self._closed = False
        self._lock = asyncio.Lock()
        # Create the control root without following ancestor links, then pin
        # the lifetime lock before the manager touches shared state.
        _make_dirs_no_follow(Path(root))
        _require_real_dir(Path(root), "skill root")
        self._root = Path(root)
        self._workspace = Path(workspace)
        self._scopes_path = self._root / _SCOPES_FILENAME
        self._lock_path = self._root / _LOCK_FILENAME
        self._lock_fd = self._take_lock(self._lock_path)
        try:
            if os.path.lexists(self._root / _SCOPE_PENDING_FILENAME):
                raise _fail(
                    "Unfinished skill scope change; owner must inspect persisted scopes before clearing scope-change.pending"
                )
            self._manager = SkillManager(self._root, workspace=self._workspace)
        except Exception:
            self._release_lock()
            raise
        try:
            self._scopes: dict[str, dict[str, str]] = self._load_scopes()
        except Exception:
            self._release_lock()
            raise
        self._pins: dict[tuple[str, str], _Pin] = {}
        self._pinned_bytes = 0

    # -- lifetime lock -----------------------------------------------------
    @staticmethod
    def _take_lock(path: Path) -> int:
        try:
            fd = os.open(
                path,
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
            )
        except OSError as exc:
            raise _fail(
                f"controller lock unavailable (must be a real file): {path}: {exc}"
            ) from None
        try:
            st = os.fstat(fd)
            if not _stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.geteuid():
                raise _fail(f"controller lock is not a regular file: {path}")
            # Enforce owner-only access on our own control file.
            try:
                os.fchmod(fd, 0o600)
            except OSError as exc:
                raise _fail(f"cannot secure controller lock: {exc}") from None
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise _fail(
                    "another skill controller already owns this root; refusing a second instance"
                ) from None
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        return fd

    def _release_lock(self) -> None:
        fd, self._lock_fd = getattr(self, "_lock_fd", -1), -1
        if fd is not None and fd >= 0:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass

    def close(self) -> None:
        """Release the lifetime OS lock. Synchronous; idempotent.

        Callers must still :meth:`release_task` finished, cancelled, or
        failed tasks; close only frees the lock (and drops retained pin
        bytes as defense in depth). Operations after close are refused.
        """
        if self._closed:
            return
        self._closed = True
        self._pins.clear()
        self._pinned_bytes = 0
        self._release_lock()

    @property
    def closed(self) -> bool:
        return self._closed

    def _ensure_open(self) -> None:
        if self._closed:
            raise _fail("skill controller is closed")

    # -- owner scopes (protected root, no-follow, temp+fsync+replace) ------
    def _load_scopes(self) -> dict[str, dict[str, str]]:
        try:
            st = os.lstat(self._scopes_path)
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise _fail(f"skill scopes unreadable: {exc}") from None
        if _stat.S_ISLNK(st.st_mode):
            raise _fail("skill scopes must not be a symlink")
        if not _stat.S_ISREG(st.st_mode):
            raise _fail("skill scopes is not a regular file")
        if st.st_size > _SCOPES_MAX_BYTES:
            raise _fail("skill scopes exceeds size limit")
        try:
            fd = os.open(self._scopes_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError as exc:
            raise _fail(f"skill scopes unreadable: {exc}") from None
        try:
            if not _stat.S_ISREG(os.fstat(fd).st_mode):
                raise _fail("skill scopes is not a regular file")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > _SCOPES_MAX_BYTES:
                    raise _fail("skill scopes exceeds size limit")
                chunks.append(chunk)
            raw = b"".join(chunks)
        except OSError as exc:
            raise _fail(f"skill scopes unreadable: {exc}") from None
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise _fail(f"skill scopes unreadable: {exc}") from None
        if not isinstance(data, dict):
            raise _fail("skill scopes is corrupt")
        if len(data) > _SCOPES_MAX_ENTRIES:
            raise _fail("skill scopes holds too many entries")
        scopes: dict[str, dict[str, str]] = {}
        for key, value in data.items():
            if not isinstance(key, str) or not SKILL_ID_RE.match(key):
                raise _fail(f"skill scopes entry is corrupt: {key!r}")
            if (
                not isinstance(value, dict)
                or len(value) > MAX_PINS + 1
                or any(
                    not isinstance(digest, str)
                    or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
                    or not isinstance(scope, str)
                    or scope not in VALID_SCOPES
                    for digest, scope in value.items()
                )
            ):
                raise _fail(f"skill scopes entry is corrupt: {key!r}")
            scopes[key] = value
        return scopes

    def _write_scopes(self) -> None:
        _require_real_dir(self._root, "skill root")
        payload = json.dumps(self._scopes, indent=2, sort_keys=True).encode("utf-8")
        if len(payload) > _SCOPES_MAX_BYTES:
            raise _fail("skill scopes exceeds size limit")
        try:
            root_fd = os.open(
                self._root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            )
        except OSError as exc:
            raise _fail(f"skill scopes write failed: {exc}") from None
        try:
            import secrets

            name = f".scopes-{secrets.token_hex(16)}.tmp"
            try:
                fd = os.open(
                    name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600,
                    dir_fd=root_fd,
                )
            except OSError as exc:
                raise _fail(f"skill scopes write failed: {exc}") from None
            try:
                view = memoryview(payload)
                while view:
                    written = os.write(fd, view)
                    view = view[written:]
                os.fsync(fd)
            except OSError as exc:
                try:
                    os.close(fd)
                except OSError:
                    pass
                try:
                    os.unlink(name, dir_fd=root_fd)
                except OSError:
                    pass
                raise _fail(f"skill scopes write failed: {exc}") from None
            try:
                os.close(fd)
            except OSError as exc:
                raise _fail(f"skill scopes write failed: {exc}") from None
            try:
                os.replace(name, _SCOPES_FILENAME, src_dir_fd=root_fd, dst_dir_fd=root_fd)
            except OSError as exc:
                try:
                    os.unlink(name, dir_fd=root_fd)
                except OSError:
                    pass
                raise _fail(f"skill scopes write failed: {exc}") from None
        finally:
            try:
                os.close(root_fd)
            except OSError:
                pass

    def _effective_scope(self, skill_id: str, digest: str | None = None) -> str:
        if digest is None:
            digest = self._manager._record(skill_id).digest
        scope = self._scopes.get(skill_id, {}).get(digest, DEFAULT_SCOPE)
        return scope if scope in VALID_SCOPES else DEFAULT_SCOPE

    def _persist_scopes(self) -> None:
        """Record durable intent before replacing grants; unfinished intent
        refuses restart instead of resurrecting an older permissive grant.
        """
        root_fd = _open_dir_fd(self._root, "skill root")
        try:
            fd = os.open(
                _SCOPE_PENDING_FILENAME,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=root_fd,
            )
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            os.fsync(root_fd)
            self._write_scopes()
            os.fsync(root_fd)
            os.unlink(_SCOPE_PENDING_FILENAME, dir_fd=root_fd)
            os.fsync(root_fd)
        except OSError as exc:
            raise _fail(f"Skill scope persistence failed: {exc}") from None
        finally:
            os.close(root_fd)

    def _set_scope(self, skill_id: str, scope: str, *, preserve_pins: bool = False) -> None:
        """Persist one owner scope; fail closed to ``local_only`` on error.

        A persistence failure never leaves a broader scope in force for this
        controller: the in-memory entry drops to ``local_only`` and the
        caller gets an explicit error instead of silent disclosure.
        """
        previous = self._scopes.get(skill_id)
        # An update is new content. Prior review of a name cannot authorize
        # changed bytes, and review of a new public version cannot authorize
        # an old private task pin. Retain only active, separately reviewed pins.
        grants = {}
        if preserve_pins and previous:
            active = {pin.digest for pin in self._pins.values() if pin.skill_id == skill_id}
            grants = {digest: value for digest, value in previous.items() if digest in active}
        grants[self._manager._record(skill_id).digest] = scope
        self._scopes[skill_id] = grants
        try:
            self._persist_scopes()
        except SkillServiceError:
            self._scopes[skill_id] = {}
            if previous is None:
                del self._scopes[skill_id]
            raise
        except SkillError as exc:
            self._scopes[skill_id] = {}
            if previous is None:
                del self._scopes[skill_id]
            raise _fail(f"skill scope persistence failed: {exc}") from None

    # -- shared guards ------------------------------------------------------
    def _wrap(self, exc: SkillError) -> SkillServiceError:
        if isinstance(exc, SkillServiceError):
            return exc
        return _fail(str(exc))

    def _current_record(self, skill_id: str) -> Any:
        try:
            return self._manager._record(skill_id)
        except SkillError as exc:
            raise self._wrap(exc) from None

    def _deny_unless_disclosable(
        self,
        record: Any,
        scope: str,
        provider_info: Any,
        *,
        invocation: str,
    ) -> tuple[bool, bool]:
        """Recheck live state before any metadata/content return.

        Returns ``(local, contributor)`` for logging-free policy use. Raises
        on disabled skills, author invocation refusals, and scope/provider
        denials. Both the current record and (for pins) the pinned author
        flags must allow the requested invocation.
        """
        local, contributor = _provider_flags(provider_info)
        if not record.enabled or record.status != "ready":
            raise _fail(f"skill is not enabled: {record.id}")
        if invocation == "user" and not record.user_invocable:
            raise _fail(f"skill is not user-invocable: {record.id}")
        if invocation == "model" and not record.model_invocable:
            raise _fail(f"skill model invocation is disabled: {record.id}")
        if not _scope_allows(scope, local=local, contributor=contributor):
            raise _fail(
                f"skill {record.id} is not disclosable to this provider under scope {scope!r}"
            )
        return local, contributor

    def _eligible_for_model(self, record: Any, scope: str, flags: tuple[bool, bool]) -> bool:
        local, contributor = flags
        if not record.enabled or record.status != "ready":
            return False
        if not record.model_invocable:
            return False
        return _scope_allows(scope, local=local, contributor=contributor)

    # -- lifecycle -----------------------------------------------------------
    async def mutate(
        self,
        operation: str,
        *,
        authority: str,
        skill_id: str | None = None,
        source: str | Path | None = None,
        source_ref: SourceRef | None = None,
        content_scope: str | None = None,
    ) -> dict[str, Any]:
        """Run one owner lifecycle operation; return bounded metadata.

        ``authority`` must be exactly ``"owner"``. Each operation accepts
        only its own arguments; unknown operations and extra combinations
        are rejected. ``install``/``update`` variants may set an explicit
        owner ``content_scope``; scope persistence failure fails closed to
        ``local_only``. Network fetch is never performed: ``source`` must be
        an already-fetched local path, never a URL.
        """
        if operation not in _MUTATE_OPS:
            raise _fail(f"unknown operation {operation!r}; expected one of {list(_MUTATE_OPS)}")
        if authority != OWNER_AUTHORITY:
            raise _fail(f"operation {operation!r} requires explicit owner authority")
        checked_ref = _check_source_ref(source_ref)
        checked_scope: str | None = None
        if content_scope is not None:
            checked_scope = _check_content_scope(content_scope)

        needs_id = operation in (
            "enable",
            "disable",
            "update",
            "update_archive",
            "rollback",
            "remove",
            "scope",
        )
        needs_source = operation in (
            "install",
            "install_archive",
            "update",
            "update_archive",
        )
        takes_scope = operation in (
            "install",
            "install_archive",
            "update",
            "update_archive",
            "scope",
        )

        checked_id: str | None = None
        if needs_id:
            if skill_id is None:
                raise _fail(f"operation {operation!r} needs skill_id")
            checked_id = _check_skill_id(skill_id)
        elif skill_id is not None:
            raise _fail(f"operation {operation!r} takes no skill_id")

        checked_source: Path | None = None
        if needs_source:
            if source is None:
                raise _fail(f"operation {operation!r} needs a local source path")
            checked_source = _check_source_path(source)
        elif source is not None:
            raise _fail(f"operation {operation!r} takes no source")

        if operation in ("install", "install_archive", "update", "update_archive"):
            if checked_ref is not None:
                try:
                    checked_ref.validate()
                except SkillError as exc:
                    raise self._wrap(exc) from None
        elif checked_ref is not None:
            raise _fail(f"operation {operation!r} takes no source_ref")

        if content_scope is not None and not takes_scope:
            raise _fail(f"operation {operation!r} takes no content_scope")
        if operation == "scope" and checked_scope is None:
            raise _fail("operation 'scope' needs content_scope")

        async with self._lock:
            self._ensure_open()
            try:
                if operation == "install":
                    assert checked_source is not None
                    record = self._manager.install(checked_source, checked_ref, authority=authority)
                elif operation == "install_archive":
                    assert checked_source is not None
                    record = self._manager.install_archive(
                        checked_source, checked_ref, authority=authority
                    )
                elif operation == "update":
                    assert checked_id is not None and checked_source is not None
                    record = self._manager.update(
                        checked_id, checked_source, checked_ref, authority=authority
                    )
                elif operation == "update_archive":
                    assert checked_id is not None and checked_source is not None
                    record = self._manager.update_archive(
                        checked_id, checked_source, checked_ref, authority=authority
                    )
                elif operation == "enable":
                    assert checked_id is not None
                    record = self._manager.enable(checked_id, authority=authority)
                elif operation == "disable":
                    assert checked_id is not None
                    record = self._manager.disable(checked_id, authority=authority)
                elif operation == "rollback":
                    assert checked_id is not None
                    record = self._manager.rollback(checked_id, authority=authority)
                elif operation == "remove":
                    assert checked_id is not None
                    dependents = sorted(
                        task for (task, pinned) in self._pins if pinned == checked_id
                    )
                    if dependents:
                        raise _fail(
                            f"cannot remove {checked_id}: active tasks hold pins: {dependents}"
                        )
                    # Persist scope cleanup before touching installed bytes so
                    # a scopes failure refuses removal with the skill intact.
                    if checked_id in self._scopes:
                        del self._scopes[checked_id]
                        try:
                            self._persist_scopes()
                        except SkillError as exc:
                            raise self._wrap(exc) from None
                    self._manager.remove(checked_id, authority=authority)
                    return {"id": checked_id, "removed": True}
                else:  # operation == "scope"
                    assert checked_id is not None and checked_scope is not None
                    self._current_record(checked_id)  # unknown skills stay unknown
                    try:
                        self._set_scope(checked_id, checked_scope)
                    except SkillError as exc:
                        raise self._wrap(exc) from None
                    record = self._manager._record(checked_id)
            except SkillError as exc:
                raise self._wrap(exc) from None

            if checked_scope is not None and operation != "scope":
                # Install/update with an explicit owner scope. A persistence
                # failure fails closed: the skill stays installed but its
                # effective scope is local_only, reported as an error.
                try:
                    self._set_scope(record.id, checked_scope, preserve_pins=True)
                except SkillError as exc:
                    raise self._wrap(exc) from None
            result = copy.deepcopy(record.metadata())
            result["scope"] = self._effective_scope(record.id)
            return result

    # -- owner reads ----------------------------------------------------------
    async def list_owner(self, after: int = 0, limit: int = OWNER_PAGE_LIMIT) -> dict[str, Any]:
        """Return one bounded owner page over all installed skills."""
        checked_after = _check_after(after)
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= OWNER_PAGE_LIMIT
        ):
            raise _fail(f"limit must be an integer in 1..{OWNER_PAGE_LIMIT}")
        async with self._lock:
            self._ensure_open()
            try:
                ids = sorted(self._manager._registry.keys())
            except SkillError as exc:
                raise self._wrap(exc) from None
            page = ids[checked_after : checked_after + limit]
            skills: list[dict[str, Any]] = []
            for skill_id in page:
                try:
                    record = self._manager._record(skill_id)
                except SkillError as exc:
                    raise self._wrap(exc) from None
                entry = copy.deepcopy(record.metadata())
                entry["scope"] = self._effective_scope(skill_id)
                skills.append(entry)
            position = checked_after + len(page)
            return {
                "skills": skills,
                "next_after": position if position < len(ids) else None,
            }

    async def inspect_owner(self, skill_id: str) -> dict[str, Any]:
        """Return full record metadata plus scope and dependent task ids.

        Never returns instruction or resource bodies.
        """
        checked_id = _check_skill_id(skill_id)
        async with self._lock:
            self._ensure_open()
            try:
                data = copy.deepcopy(self._manager.inspect(checked_id))
            except SkillError as exc:
                raise self._wrap(exc) from None
            data["scope"] = self._effective_scope(checked_id)
            data["dependent_task_ids"] = sorted(
                task for (task, pinned) in self._pins if pinned == checked_id
            )
            return data

    # -- model-facing reads ----------------------------------------------------
    async def catalog(self, provider_info: Any, after: int = 0) -> dict[str, Any]:
        """Return one bounded model catalog page (at most 4 rows).

        Filtering happens before any id/name/description is exported: only
        enabled, model-invocable skills whose owner scope permits this
        provider appear. Disabled, manual-only, and model-disabled skills are
        absent. Descriptions are capped at 80 chars; the whole payload is
        valid JSON of at most 1600 chars.
        """
        checked_after = _check_after(after)
        flags = _provider_flags(provider_info)
        async with self._lock:
            self._ensure_open()
            try:
                ids = sorted(self._manager._registry.keys())
            except SkillError as exc:
                raise self._wrap(exc) from None
            eligible: list[dict[str, Any]] = []
            for skill_id in ids:
                try:
                    record = self._manager._record(skill_id)
                except SkillError as exc:
                    raise self._wrap(exc) from None
                if not self._eligible_for_model(record, self._effective_scope(skill_id), flags):
                    continue
                description = record.description
                if len(description) > CATALOG_DESC_CHARS:
                    description = description[:CATALOG_DESC_CHARS]
                eligible.append(
                    {
                        "id": record.id,
                        "name": record.name,
                        "description": description,
                        "version": record.version,
                    }
                )
            rows = eligible[checked_after : checked_after + CATALOG_PAGE_ROWS]
            position = checked_after + len(rows)
            payload: dict[str, Any] = {
                "skills": rows,
                "next_after": position if position < len(eligible) else None,
            }
            # Guarantee the JSON bound even for adversarially long ids by
            # shedding trailing rows (never by emitting invalid JSON).
            while len(json.dumps(payload, sort_keys=True)) > CATALOG_JSON_CHARS and rows:
                rows = rows[:-1]
                position = checked_after + len(rows)
                payload = {
                    "skills": rows,
                    "next_after": position if position < len(eligible) else None,
                }
            return copy.deepcopy(payload)

    # -- task pins --------------------------------------------------------------
    def _pin_key(self, task_id: str, skill_id: str) -> tuple[str, str]:
        return (task_id, skill_id)

    def _store_pin(self, pin: _Pin) -> None:
        key = self._pin_key(pin.task_id, pin.skill_id)
        if key in self._pins:
            return
        if len(self._pins) >= MAX_PINS:
            raise _fail(
                f"too many pinned skill packages (max {MAX_PINS}); release finished tasks first"
            )
        if self._pinned_bytes + pin.total_bytes > MAX_PIN_BYTES:
            raise _fail(
                f"pinned skill bytes would exceed {MAX_PIN_BYTES} bytes; "
                "release finished tasks first"
            )
        self._pins[key] = pin
        self._pinned_bytes += pin.total_bytes

    def _capture_pin(self, task_id: str, record: Any) -> _Pin:
        """Capture exactly one verified package snapshot for a task+skill."""
        try:
            blobs = self._manager._verified_blobs(record)
        except SkillError as exc:
            raise self._wrap(exc) from None
        by_name = dict(blobs)
        total = sum(len(blob) for _, blob in blobs)
        source = copy.deepcopy(dict(record.source))
        return _Pin(
            task_id=task_id,
            skill_id=record.id,
            digest=record.digest,
            source=source,
            name=record.name,
            description=record.description,
            version=record.version,
            allowed_tools=tuple(record.allowed_tools),
            model_invocable=record.model_invocable,
            user_invocable=record.user_invocable,
            blobs=by_name,
            total_bytes=total,
        )

    async def load(
        self,
        task_id: str,
        skill_id: str,
        provider_info: Any,
        *,
        invocation: str = "model",
        resource: str | None = None,
    ) -> dict[str, Any]:
        """Load pinned instructions (or one pinned resource) for a task.

        The first load captures one verified package snapshot; later loads
        for the same task+skill reuse those pinned bytes even after update
        or rollback, with no file overwrite or rebinding. Every load
        rechecks the *current* enabled state, author invocation flags, and
        owner scope/provider policy before returning anything, so disabling
        or revoking scope blocks subsequent disclosure of existing pins; the
        pinned author flags must allow the invocation as well. Re-enabling
        under an explicitly permitting policy can admit an old pin again.

        Returns ``untrusted`` content plus the exact whole-package digest
        and source provenance. Nothing here materializes or executes
        scripts: the Agent may separately copy selected code into its
        workspace under normal approved tools. The whole response JSON is
        bounded to 12000 chars; oversized bodies fail with an explicit
        needs-smaller-resource error instead of silent truncation.
        """
        checked_task = _check_task_id(task_id)
        checked_id = _check_skill_id(skill_id)
        checked_invocation = _check_invocation(invocation)
        checked_resource = _check_resource_path(resource) if resource is not None else None
        async with self._lock:
            self._ensure_open()
            record = self._current_record(checked_id)
            scope = self._effective_scope(checked_id)
            self._deny_unless_disclosable(
                record, scope, provider_info, invocation=checked_invocation
            )
            key = self._pin_key(checked_task, checked_id)
            pin = self._pins.get(key)
            if pin is None:
                pin = self._capture_pin(checked_task, record)
                self._store_pin(pin)
            local, contributor = _provider_flags(provider_info)
            if not _scope_allows(
                self._effective_scope(checked_id, pin.digest), local=local, contributor=contributor
            ):
                raise _fail(
                    "Pinned skill version is unavailable under this model disclosure policy"
                )
            # Pinned author controls still apply on top of the current ones.
            if checked_invocation == "user" and not pin.user_invocable:
                raise _fail(f"skill is not user-invocable: {checked_id} (pinned version)")
            if checked_invocation == "model" and not pin.model_invocable:
                raise _fail(f"skill model invocation is disabled: {checked_id} (pinned version)")
            try:
                if checked_resource is None:
                    _, body = _parse_skill_md_bytes(pin.blobs["SKILL.md"])
                    content: dict[str, Any] = {
                        "body": body,
                        "instruction_path": "SKILL.md",
                        "instruction_sha256": "sha256:"
                        + hashlib.sha256(pin.blobs["SKILL.md"]).hexdigest(),
                    }
                else:
                    data = pin.blobs.get(checked_resource)
                    if data is None:
                        raise _fail(f"resource is not a regular file: {resource!r}")
                    if len(data) > RESOURCE_MAX_BYTES:
                        raise _fail("resource exceeds size limit")
                    try:
                        text = data.decode("utf-8")
                    except UnicodeDecodeError:
                        raise _fail(
                            "resource is not UTF-8 text; binary dumps are never returned"
                        ) from None
                    content = {
                        "resource_path": checked_resource,
                        "resource_text": text,
                        "resource_sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
                    }
            except SkillError as exc:
                raise self._wrap(exc) from None
            response: dict[str, Any] = {
                "id": pin.skill_id,
                "name": pin.name,
                "description": pin.description,
                "version": pin.version,
                "digest": pin.digest,
                "source": copy.deepcopy(pin.source),
                "untrusted": True,
                "invocation": checked_invocation,
                "allowed_tools": list(pin.allowed_tools),
            }
            response.update(content)
            encoded = json.dumps(response, sort_keys=True)
            if len(encoded) > RESULT_JSON_CHARS:
                raise _fail(
                    f"skill result is {len(encoded)} chars (max {RESULT_JSON_CHARS}); "
                    "request a smaller resource via resource= instead of "
                    "the full instructions (needs-smaller-resource)"
                )
            return json.loads(encoded)

    async def release_task(self, task_id: str) -> None:
        """Drop every pin for a task. Idempotent; safe for unknown tasks.

        Required on task finish, cancellation, failure, and controller
        close. A restarted task incarnation must use a fresh task id (or
        release first): pins never silently resume across restarts.
        """
        checked_task = _check_task_id(task_id)
        async with self._lock:
            self._ensure_open()
            drops = [key for key in self._pins if key[0] == checked_task]
            for key in drops:
                pin = self._pins.pop(key)
                self._pinned_bytes -= pin.total_bytes
            if self._pinned_bytes < 0:  # pragma: no cover - internal invariant
                self._pinned_bytes = 0

    async def validate_task(
        self, task_id: str, provider_info: Any, *, user_skill_id: str | None = None
    ) -> None:
        """Recheck retained context before inference without loading bodies.

        A task may deliberately select a small resource from a package whose
        instructions exceed the context profile; checking revocation must not
        require serializing those oversized instructions again.
        """
        checked_task = _check_task_id(task_id)
        async with self._lock:
            self._ensure_open()
            local, contributor = _provider_flags(provider_info)
            for (task, skill_id), pin in self._pins.items():
                if task != checked_task:
                    continue
                invocation = "user" if skill_id == user_skill_id else "model"
                record = self._current_record(skill_id)
                self._deny_unless_disclosable(
                    record, self._effective_scope(skill_id), provider_info, invocation=invocation
                )
                if not _scope_allows(
                    self._effective_scope(skill_id, pin.digest),
                    local=local,
                    contributor=contributor,
                ):
                    raise _fail(
                        "Pinned skill version is unavailable under this model disclosure policy"
                    )
                if (invocation == "model" and not pin.model_invocable) or (
                    invocation == "user" and not pin.user_invocable
                ):
                    raise _fail("Pinned skill invocation is disabled")
