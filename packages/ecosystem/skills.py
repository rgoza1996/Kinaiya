"""Portable skill lifecycle: install, inspect, load, enable/disable, rollback.

A skill is a directory containing a ``SKILL.md`` file with YAML frontmatter
(``name`` and ``description`` are mandatory) plus optional instructions,
references, scripts, and assets. Skill *content* is untrusted advice: it can
never grant policy, authorize writes, or execute on install, inspection, or
load. Discovery returns bounded metadata only; full instructions load on
explicit invocation, and supporting resources load only when explicitly
requested through :meth:`SkillManager.load_resource`.

Identity is namespaced: every record id is ``<namespace>/<name>`` where the
namespace derives from a validated source (``github:<owner>``,
``clawhub:<scope>``, ``local``). Two skills with the same display name
coexist under different qualified ids. Display names colliding with
:data:`RESERVED_COMMAND_NAMES` are installable but never bare-invocable; they
stay reachable through their qualified id. Built-in command reservation
itself is owned by the lead's command registry; this module only enforces
the conservative default.

Lifecycle mutations (install, update, enable, disable, uninstall, rollback)
require explicit ``authority="owner"``. Discovery (:meth:`SkillManager.list`,
:meth:`SkillManager.inspect`, :meth:`SkillManager.versions`) and invocation
loads (:meth:`SkillManager.load`, :meth:`SkillManager.load_resource`) take no
authority argument, and content can never confer authority. Command exposure
is metadata only (``bare_invocable``); skills are non-authorizing.

Implemented: local directory import, local ``.zip``/``.tar.gz`` archive
import with all-member validation and manual bounded extraction, and opt-in
pinned GitHub fetching restricted to public ``codeload.github.com`` tarballs
at an immutable 40-hex commit with no redirects. NOT implemented and
explicitly refused: ClawHub live API resolution, private GitHub sources with
embedded credentials, unpinned remote fetches, and any marketplace install
mode that would execute code. Those paths raise :class:`SkillError` naming
the limitation; pass an already-fetched local directory instead.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import secrets
import shutil
import stat as _stat
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Literal

import yaml

# -- limits -------------------------------------------------------------------
SKILL_MD_MAX_BYTES = 262_144
SINGLE_FILE_MAX_BYTES = 1_048_576
TOTAL_SKILL_MAX_BYTES = 8_048_576
MAX_FILES_PER_SKILL = 200
FRONTMATTER_MAX_BYTES = 65_536
INSTRUCTIONS_MAX_CHARS = 200_000
RESOURCE_MAX_BYTES = 262_144
REGISTRY_MAX_BYTES = 1_048_576
# Total filesystem entries (files + dirs, root excluded) enumerated before
# sorting. Bounds allocation for hostile trees with many tiny entries.
MAX_TREE_ENTRIES = MAX_FILES_PER_SKILL + 1 + 256
# Decompressed tar stream cap (file bytes + headers). Physical download caps
# alone do not bound gzip expansion, so the shared tar parser enforces this
# on the decompressed stream before unbounded parsing/allocation.
MAX_TAR_STREAM_BYTES = TOTAL_SKILL_MAX_BYTES + 1_048_576
MAX_ARCHIVE_MEMBERS = MAX_FILES_PER_SKILL + 1
_DIGEST_RE = re.compile(r"\Asha256:[0-9a-f]{64}\Z")

OWNER_AUTHORITY = "owner"
_CODELOAD_BASE = "https://codeload.github.com"

# -- errors -------------------------------------------------------------------


class SkillError(Exception):
    """A skill operation failed safely: bad format, unsafe content, tamper."""


# Built-in command names a skill display name must not shadow. The lead's
# command registry owns the authoritative reservation; this conservative set
# keeps skills qualified-only until the lead wires dynamic commands.
RESERVED_COMMAND_NAMES = frozenset(
    {
        "help",
        "commands",
        "new",
        "sessions",
        "resume",
        "history",
        "save",
        "clear",
        "status",
        "tasks",
        "stop",
        "pause",
        "model",
        "plan",
        "compress",
        "skills",
        "skill",
        "mcp",
        "plugins",
        "permissions",
        "yolo",
        "harness",
        "alias",
    }
)

_COMPAT_READY = "ready"
_COMPAT_NEEDS_SETUP = "needs_setup"
_COMPAT_NEEDS_ADAPTER = "needs_adapter"
_COMPAT_UNSUPPORTED = "unsupported"
_COMPAT_DISABLED = "disabled"

_GITHUB_SHA_RE = re.compile(r"\A[0-9a-f]{40}\Z")
_SAFE_SEGMENT_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_VERSION_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._+\-]{0,63}\Z")


# -- pinned-descriptor IO ------------------------------------------------------
# All skill-byte reads go through directory descriptors pinned with O_NOFOLLOW
# so a symlink/special swap raced between validation and use is rejected
# (ELOOP / non-regular fstat) without ever reading outside the tree.


def _needs_no_follow() -> None:
    if not hasattr(os, "O_NOFOLLOW"):
        raise SkillError("platform lacks O_NOFOLLOW; refusing unsafe skill IO")


def _check_digest_format(digest: str) -> str:
    if not isinstance(digest, str) or not _DIGEST_RE.match(digest):
        raise SkillError(f"skill digest is corrupt: {digest!r}")
    return digest


def _lstat_path(path: Path) -> os.stat_result:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        raise SkillError(f"missing path: {path}") from None
    except OSError as exc:
        raise SkillError(f"unreadable path {path}: {exc}") from None


def _require_real_dir(path: Path, what: str) -> None:
    st = _lstat_path(path)
    if _stat.S_ISLNK(st.st_mode):
        raise SkillError(f"{what} must be a real directory, not a symlink: {path}")
    if not _stat.S_ISDIR(st.st_mode):
        raise SkillError(f"{what} is not a directory: {path}")


def _open_dir_fd(path: Path, what: str = "directory") -> int:
    """Pin every ancestor as well as the leaf; never follow a swapped parent."""
    _needs_no_follow()
    _require_real_dir(path, what)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open("/", flags)
    try:
        for part in Path(os.path.abspath(path)).parts[1:]:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except OSError as exc:
        os.close(fd)
        raise SkillError(f"cannot open {what} without following symlinks: {path}: {exc}") from None


def _make_dirs_no_follow(path: Path) -> None:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open("/", flags)
    try:
        for part in Path(os.path.abspath(path)).parts[1:]:
            try:
                child = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
    except OSError as exc:
        raise SkillError(f"control directory must be real, not a symlink: {path}: {exc}") from None
    finally:
        os.close(fd)


def _bounded_read_fd(fd: int, limit: int, what: str) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        try:
            chunk = os.read(fd, 65536)
        except OSError as exc:
            raise SkillError(f"cannot read {what}: {exc}") from None
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise SkillError(f"{what} exceeds size limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _open_child_reg(fd: int, name: str, what: str) -> int:
    _needs_no_follow()
    try:
        child = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=fd,
        )
    except FileNotFoundError:
        raise SkillError(f"{what} is not a regular file: {name!r}") from None
    except OSError as exc:
        raise SkillError(f"{what} rejected (symlink or unreadable): {name!r}: {exc}") from None
    try:
        if not _stat.S_ISREG(os.fstat(child).st_mode):
            raise SkillError(f"{what} is not a regular file: {name!r}")
    except SkillError:
        os.close(child)
        raise
    except OSError as exc:
        os.close(child)
        raise SkillError(f"cannot stat {what} {name!r}: {exc}") from None
    return child


def _capture_pinned(
    root: Path,
    *,
    per_file_limit: int = SINGLE_FILE_MAX_BYTES,
    total_limit: int = TOTAL_SKILL_MAX_BYTES,
    require_skill_md: bool = True,
) -> list[tuple[str, bytes]]:
    """Snapshot a skill tree through pinned descriptors (O_NOFOLLOW, bounded).

    Returns ``(relative-posix-path, bytes)`` pairs read from exactly the
    bytes that were validated. Symlink/special swaps raced between listing
    and opening are rejected without reading outside. Enumeration is bounded
    (files and directories) before any sort/count allocation.
    """
    _needs_no_follow()
    root_fd = _open_dir_fd(root, "skill tree")
    blobs: list[tuple[str, bytes]] = []
    total = 0
    files = 0
    entries = 0
    try:
        # Stack of (dir fd, relative prefix). Root fd is owned here.
        stack: list[tuple[int, str]] = [(root_fd, "")]
        owned: list[int] = [root_fd]
        while stack:
            dir_fd, prefix = stack.pop()
            try:
                with os.scandir(dir_fd) as it:
                    names: list[str] = []
                    for entry in it:
                        entries += 1
                        if entries > MAX_TREE_ENTRIES:
                            raise SkillError("skill exceeds entry count limit")
                        names.append(entry.name)
                    names.sort()
                    for name in names:
                        if name in ("", ".", ".."):
                            raise SkillError(f"unsafe entry: {name!r}")
                        try:
                            st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                        except FileNotFoundError:
                            raise SkillError(f"entry vanished during scan: {name!r}") from None
                        except OSError as exc:
                            raise SkillError(f"unreadable entry {name!r}: {exc}") from None
                        if _stat.S_ISLNK(st.st_mode):
                            rel = f"{prefix}{name}" if prefix else name
                            raise SkillError(f"symlinks are not installed: {rel}")
                        if _stat.S_ISDIR(st.st_mode):
                            rel = f"{prefix}{name}" if prefix else name
                            try:
                                sub = os.open(
                                    name,
                                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                    dir_fd=dir_fd,
                                )
                            except OSError as exc:
                                raise SkillError(
                                    f"directory rejected (symlink race?): {rel}: {exc}"
                                ) from None
                            try:
                                if not _stat.S_ISDIR(os.fstat(sub).st_mode):
                                    raise SkillError(f"not a directory: {rel}")
                            except SkillError:
                                os.close(sub)
                                raise
                            owned.append(sub)
                            stack.append((sub, rel + "/"))
                            continue
                        if not _stat.S_ISREG(st.st_mode):
                            rel = f"{prefix}{name}" if prefix else name
                            raise SkillError(f"special files are not installed: {rel}")
                        rel = f"{prefix}{name}" if prefix else name
                        if ".." in PurePosixPath(rel).parts:
                            raise SkillError(f"unsafe entry: {rel}")
                        child = _open_child_reg(dir_fd, name, "skill file")
                        try:
                            blob = _bounded_read_fd(child, per_file_limit, f"file {rel!r}")
                        finally:
                            os.close(child)
                        total += len(blob)
                        if total > total_limit:
                            raise SkillError("skill exceeds total size limit")
                        files += 1
                        if files > MAX_FILES_PER_SKILL:
                            raise SkillError("skill exceeds file count limit")
                        blobs.append((rel, blob))
            finally:
                if dir_fd != root_fd:
                    os.close(dir_fd)
                    owned.remove(dir_fd)
    finally:
        for fd in list(owned):
            try:
                os.close(fd)
            except OSError:
                pass
    if require_skill_md and "SKILL.md" not in {rel for rel, _ in blobs}:
        raise SkillError("skill source has no SKILL.md")
    blobs.sort(key=lambda item: item[0])
    return blobs


def _read_pinned_file(root: Path, relpath: str, limit: int, what: str = "resource") -> bytes:
    """Read one relative file through pinned descriptors, never following races."""
    _needs_no_follow()
    if not relpath or PurePosixPath(relpath).is_absolute():
        raise SkillError(f"{what} path must be relative: {relpath!r}")
    pure = PurePosixPath(relpath)
    if not pure.parts or any(part in ("", ".", "..") for part in pure.parts):
        raise SkillError(f"{what} path escapes skill dir: {relpath!r}")
    if len(relpath) > 1024:
        raise SkillError(f"{what} path too long: {relpath!r}")
    root_fd = _open_dir_fd(root, "skill tree")
    try:
        dir_fd = root_fd
        owned: list[int] = []
        try:
            for part in pure.parts[:-1]:
                try:
                    st = os.stat(part, dir_fd=dir_fd, follow_symlinks=False)
                except FileNotFoundError:
                    raise SkillError(f"{what} is not a regular file: {relpath!r}") from None
                except OSError as exc:
                    raise SkillError(f"{what} unreadable: {relpath!r}: {exc}") from None
                if _stat.S_ISLNK(st.st_mode) or not _stat.S_ISDIR(st.st_mode):
                    raise SkillError(f"{what} is not a regular file: {relpath!r}")
                try:
                    sub = os.open(
                        part,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                        dir_fd=dir_fd,
                    )
                except OSError as exc:
                    raise SkillError(
                        f"{what} rejected (symlink race?): {relpath!r}: {exc}"
                    ) from None
                owned.append(sub)
                dir_fd = sub
            leaf = pure.parts[-1]
            child = _open_child_reg(dir_fd, leaf, what)
            try:
                return _bounded_read_fd(child, limit, f"{what} {relpath!r}")
            finally:
                os.close(child)
        finally:
            for fd in owned:
                try:
                    os.close(fd)
                except OSError:
                    pass
    finally:
        try:
            os.close(root_fd)
        except OSError:
            pass


def _digest_blobs(blobs: list[tuple[str, bytes]]) -> tuple[str, list[str], int]:
    hasher = hashlib.sha256()
    names: list[str] = []
    total = 0
    for rel, blob in sorted(blobs, key=lambda item: item[0]):
        names.append(rel)
        total += len(blob)
        hasher.update(len(rel).to_bytes(4, "little"))
        hasher.update(rel.encode("utf-8"))
        hasher.update(len(blob).to_bytes(8, "little"))
        hasher.update(blob)
    return "sha256:" + hasher.hexdigest(), names, total


# -- source references -------------------------------------------------------


@dataclass(frozen=True)
class SourceRef:
    """Canonical, credential-free reference to a skill source.

    ``namespace`` always includes the source class (``github:<owner>``,
    ``clawhub:<scope>``, ``local``) so ids from different origins cannot
    silently substitute for one another. Never carries credential values.
    Non-local references must be pinned (a full commit SHA for GitHub, an
    explicit version for ClawHub); moving refs are refused.
    """

    kind: str  # "local" | "github" | "clawhub"
    namespace: str
    name: str
    subdir: str | None = None
    ref: str | None = None  # pinned commit SHA (github) or version (clawhub)
    raw: str = ""
    repository: str | None = None

    @property
    def qualified_prefix(self) -> str:
        return f"{self.namespace}/{self.name}"

    @property
    def is_pinned(self) -> bool:
        if self.kind == "github":
            return self.ref is not None and bool(_GITHUB_SHA_RE.match(self.ref))
        if self.kind == "clawhub":
            return self.ref is not None and bool(_VERSION_RE.match(self.ref))
        return True  # local bytes are already exact

    def validate(self) -> SourceRef:
        """Reject malformed namespaces, names, subdirs, and unpinned refs."""
        if self.raw and "://" in self.raw:
            parsed = urllib.parse.urlsplit(self.raw)
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise SkillError("source URLs must not contain credentials, queries or fragments")
            if self.kind == "github" and (
                parsed.scheme != "https" or parsed.hostname != "github.com"
            ):
                raise SkillError("GitHub sources must use the canonical HTTPS host")
        if self.repository is not None:
            if self.kind != "github":
                raise SkillError("repository applies only to GitHub sources")
            SourceRef._check_segment(self.repository, "github repository")
        if self.kind == "local":
            if self.namespace != "local":
                raise SkillError(f"local source must use namespace 'local': {self.namespace!r}")
            if self.ref is not None or self.subdir is not None:
                raise SkillError("local source carries no ref or subdir")
        elif self.kind == "github":
            if not self.namespace.startswith("github:"):
                raise SkillError(f"bad github namespace: {self.namespace!r}")
            SourceRef._check_segment(self.namespace[len("github:") :], "github owner")
            if not self.is_pinned:
                raise SkillError(f"github source is not pinned to a 40-hex commit: {self.raw!r}")
        elif self.kind == "clawhub":
            if not self.namespace.startswith("clawhub:"):
                raise SkillError(f"bad clawhub namespace: {self.namespace!r}")
            SourceRef._check_segment(self.namespace[len("clawhub:") :], "clawhub scope")
            if not self.is_pinned:
                raise SkillError(f"clawhub source needs an explicit version: {self.raw!r}")
        else:
            raise SkillError(f"unknown source kind: {self.kind!r}")
        SourceRef._check_segment(self.name, "skill name")
        if self.subdir is not None:
            for part in self.subdir.split("/"):
                SourceRef._check_segment(part, "source subdir segment")
        if "@" in self.raw.split("/")[0] and "://" in self.raw:
            raise SkillError("source URL must not embed credentials")
        return self

    @staticmethod
    def parse(text: str) -> SourceRef:
        """Parse a GitHub URL/identifier, ClawHub reference, or local hint.

        Raises :class:`SkillError` when the text names no usable namespace,
        embeds credentials, or is ambiguous. Dicts are never accepted: pass
        a typed :class:`SourceRef`.
        """
        raw = text.strip()
        if not raw:
            raise SkillError("empty source reference")
        lowered = raw.lower()
        if "://" in raw:
            scheme, _, rest = raw.partition("://")
            if scheme not in ("https", "clawhub"):
                raise SkillError(f"unsupported source scheme: {scheme!r}")
            if scheme == "https" and "@" in rest.split("/")[0]:
                raise SkillError("source URL must not embed credentials")
            if scheme == "clawhub":
                return SourceRef._parse_clawhub(rest, raw)
            if "github.com/" in lowered:
                return SourceRef._parse_github_url(raw)
            raise SkillError(f"unsupported registry host in {raw!r}")
        if lowered.startswith("clawhub:"):
            return SourceRef._parse_clawhub(raw[len("clawhub:") :], raw)
        if lowered.startswith("github:"):
            return SourceRef._parse_github_shorthand(raw[len("github:") :], raw)
        if "/" in raw and " " not in raw:
            # Bare "owner/repo[@ref]" defaults to GitHub shorthand.
            return SourceRef._parse_github_shorthand(raw, raw)
        # Otherwise treat as a local path hint (install() takes the Path).
        name = Path(raw).name
        SourceRef._check_segment(name, "skill name")
        return SourceRef(kind="local", namespace="local", name=name, raw=raw)

    @staticmethod
    def _check_segment(value: str, what: str) -> None:
        if not _SAFE_SEGMENT_RE.match(value or ""):
            raise SkillError(f"invalid {what}: {value!r}")

    @staticmethod
    def _check_version(value: str, what: str) -> str:
        if not _VERSION_RE.match(value or "") or ".." in value:
            raise SkillError(f"invalid {what}: {value!r}")
        return value

    @staticmethod
    def _split_ref(tail: str) -> tuple[str, str | None]:
        base, sep, ref = tail.partition("@")
        return base, (ref or None) if sep else None

    @staticmethod
    def _parse_github_shorthand(body: str, raw: str) -> SourceRef:
        base, ref = SourceRef._split_ref(body.strip().strip("/"))
        parts = [p for p in base.split("/") if p]
        if len(parts) < 2:
            raise SkillError(f"github source needs owner/repo[/subdir]: {raw!r}")
        owner, repo = parts[0], parts[1]
        SourceRef._check_segment(owner, "github owner")
        SourceRef._check_segment(repo, "github repo")
        subdir = "/".join(parts[2:]) or None
        if subdir:
            for part in subdir.split("/"):
                SourceRef._check_segment(part, "github subdir segment")
        return SourceRef(
            kind="github",
            namespace=f"github:{owner}",
            name=repo,
            repository=repo,
            subdir=subdir,
            ref=ref,
            raw=raw,
        )

    @staticmethod
    def _parse_github_url(raw: str) -> SourceRef:
        if "?" in raw or "#" in raw:
            raise SkillError(f"github URL must not carry query or fragment: {raw!r}")
        parsed = urllib.parse.urlsplit(raw)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "github.com"
            or parsed.username
            or parsed.password
        ):
            raise SkillError("GitHub sources must use the canonical HTTPS host")
        path = parsed.path.strip("/")
        parts = [p for p in path.split("/") if p]
        if len(parts) < 2:
            raise SkillError(f"github URL needs owner/repo: {raw!r}")
        owner, repo = parts[0], parts[1].removesuffix(".git")
        SourceRef._check_segment(owner, "github owner")
        SourceRef._check_segment(repo, "github repo")
        ref: str | None = None
        subdir: str | None = None
        if len(parts) >= 4 and parts[2] in ("tree", "blob"):
            ref = parts[3]
            subdir = "/".join(parts[4:]) or None
        elif len(parts) == 3:
            ref = parts[2]  # may be a branch, tag, or SHA; pinning checked later
        if subdir:
            for part in subdir.split("/"):
                SourceRef._check_segment(part, "github subdir segment")
        return SourceRef(
            kind="github",
            namespace=f"github:{owner}",
            name=repo,
            repository=repo,
            subdir=subdir,
            ref=ref,
            raw=raw,
        )

    @staticmethod
    def _parse_clawhub(body: str, raw: str) -> SourceRef:
        base, version = SourceRef._split_ref(body.strip().strip("/"))
        parts = [p for p in base.split("/") if p]
        if len(parts) != 2:
            raise SkillError(f"clawhub reference needs scope/name[@version]: {raw!r}")
        scope, name = parts
        SourceRef._check_segment(scope, "clawhub scope")
        SourceRef._check_segment(name, "clawhub skill")
        return SourceRef(
            kind="clawhub",
            namespace=f"clawhub:{scope}",
            name=name,
            ref=version,
            raw=raw,
        )


# -- records -----------------------------------------------------------------


@dataclass
class SkillRecord:
    """Durable install record. ``source`` holds provenance, never secrets."""

    id: str
    name: str
    namespace: str
    version: str
    description: str
    source: dict[str, Any]
    digest: str
    status: str
    compatibility_notes: list[str] = field(default_factory=list)
    enabled: bool = False
    bare_invocable: bool = True
    installed_at: float = 0.0
    allowed_tools: list[str] = field(default_factory=list)
    user_invocable: bool = True
    model_invocable: bool = True
    previous_digest: str = ""
    files: list[str] = field(default_factory=list)

    def metadata(self) -> dict[str, Any]:
        """Bounded discovery view: no instruction body, no file contents."""
        data = asdict(self)
        data.pop("files")
        return data


@dataclass(frozen=True)
class SkillInstructions:
    """Bounded instruction payload for one explicit skill invocation."""

    id: str
    name: str
    description: str
    body: str
    allowed_tools: tuple[str, ...] = ()
    user_invocable: bool = True
    # Advisory only: content cannot grant policy or authorize execution.
    untrusted: bool = True


def _check_record_shapes(record: SkillRecord) -> SkillRecord:
    """Reject tampered records whose identity fields would escape ownership."""
    namespace, sep, name = record.id.partition("/")
    if not sep or "/" in name or namespace != record.namespace or name != record.name:
        raise SkillError(f"skill record id is corrupt: {record.id!r}")
    if record.namespace == "local":
        if record.namespace != "local":
            raise SkillError(f"skill record namespace is corrupt: {record.namespace!r}")
    elif record.namespace.startswith("github:"):
        SourceRef._check_segment(record.namespace[len("github:") :], "github owner")
    elif record.namespace.startswith("clawhub:"):
        SourceRef._check_segment(record.namespace[len("clawhub:") :], "clawhub scope")
    else:
        raise SkillError(f"skill record namespace is corrupt: {record.namespace!r}")
    SourceRef._check_segment(record.name, "skill name")
    SourceRef._check_version(record.version, "skill version")
    return record


class _StrictLoader(yaml.SafeLoader):
    """YAML loader rejecting aliases and duplicate mapping keys."""

    def compose_node(self, parent, index):  # noqa: ANN001, ANN202
        self._nodes = getattr(self, "_nodes", 0) + 1
        self._depth = getattr(self, "_depth", 0) + 1
        if self._nodes > 2048 or self._depth > 64:
            raise SkillError("SKILL.md metadata exceeds structural bounds")
        if self.check_event(yaml.events.AliasEvent):
            raise SkillError("SKILL.md frontmatter must not use YAML aliases")
        try:
            return super().compose_node(parent, index)
        finally:
            self._depth -= 1

    def construct_mapping(self, node, deep=False):  # noqa: ANN001, ANN202
        seen: list[Any] = []
        for key_node, _value_node in node.value:
            key = self.construct_object(key_node, deep=True)
            if key in seen:
                raise SkillError(f"SKILL.md frontmatter has a duplicate key: {key!r}")
            seen.append(key)
        return super().construct_mapping(node, deep)


def _parse_skill_md_bytes(blob: bytes) -> tuple[dict[str, Any], str]:
    """Parse already-captured SKILL.md bytes (no filesystem re-read)."""
    if len(blob) > SKILL_MD_MAX_BYTES:
        raise SkillError("SKILL.md exceeds size limit")
    try:
        text = blob.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillError(f"SKILL.md is not valid UTF-8: {exc}") from None
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        raise SkillError("SKILL.md needs YAML frontmatter delimited by ---")
    end = next((i for i, line in enumerate(lines[1:], 1) if line.rstrip("\r\n") == "---"), None)
    if end is None:
        raise SkillError("SKILL.md has unterminated frontmatter")
    raw_front = "".join(lines[1:end])
    body = "".join(lines[end + 1 :])
    if len(raw_front.encode("utf-8")) > FRONTMATTER_MAX_BYTES:
        raise SkillError("SKILL.md frontmatter exceeds size limit")
    try:
        front = yaml.load(raw_front, Loader=_StrictLoader) or {}
    except SkillError:
        raise
    except yaml.YAMLError as exc:
        raise SkillError(f"SKILL.md frontmatter is not valid YAML: {exc}") from None
    if not isinstance(front, dict):
        raise SkillError("SKILL.md frontmatter must be a mapping")
    for key in front:
        if not isinstance(key, str):
            raise SkillError(f"SKILL.md frontmatter keys must be strings: {key!r}")
    name = front.get("name")
    description = front.get("description")
    if not isinstance(name, str) or not name.strip():
        raise SkillError("SKILL.md frontmatter needs a non-empty 'name'")
    if not isinstance(description, str) or not description.strip():
        raise SkillError("SKILL.md frontmatter needs a non-empty 'description'")
    SourceRef._check_segment(name.strip(), "skill name")
    version = front.get("version", "0.0.0")
    if not isinstance(version, str):
        raise SkillError("SKILL.md 'version' must be a string when present")
    SourceRef._check_version(version.strip(), "skill version")
    return front, body.strip() + "\n"


def _assess_compatibility(front: dict[str, Any]) -> tuple[str, list[str], dict[str, Any]]:
    """Map frontmatter to a compatibility state plus human-readable notes.

    Parsing success never implies executability: unknown requirements that
    affect authorization or execution resolve to ``needs_setup`` or worse,
    never silently to ``ready``.
    """
    notes: list[str] = []
    allowed = front.get("allowed-tools", [])
    if allowed is not None and not isinstance(allowed, list):
        raise SkillError("'allowed-tools' must be a list when present")
    if allowed is not None:
        for item in allowed:
            if not isinstance(item, str):
                raise SkillError("'allowed-tools' entries must be strings")
    user_invocable = front.get("user-invocable", True)
    if not isinstance(user_invocable, bool):
        raise SkillError("'user-invocable' must be boolean when present")
    model_blocked_raw = front.get("disable-model-invocation", False)
    if not isinstance(model_blocked_raw, bool):
        raise SkillError("'disable-model-invocation' must be boolean when present")
    model_blocked = model_blocked_raw
    manual_only = front.get("manual-only", False)
    if not isinstance(manual_only, bool):
        raise SkillError("'manual-only' must be boolean when present")
    runtime = front.get("runtime") or front.get("compatibility", {})
    if isinstance(runtime, str):
        runtime = {"profile": runtime}
    if not isinstance(runtime, dict):
        raise SkillError("'compatibility' must be a mapping when present")
    profile = str(runtime.get("profile", "portable")).lower()
    requires = front.get("requires", {}) or {}
    if not isinstance(requires, dict):
        raise SkillError("'requires' must be a mapping when present")
    for key in ("exec", "pip"):
        value = requires.get(key, None)
        if value is None:
            continue
        if not isinstance(value, list):
            raise SkillError(f"'requires.{key}' must be a list when present")
        for item in value:
            if not isinstance(item, str):
                raise SkillError(f"'requires.{key}' entries must be strings")

    missing: list[str] = []
    for binary in requires.get("exec", []) or []:
        if shutil.which(binary) is None:
            missing.append(binary)
    if missing:
        notes.append("missing executables: " + ", ".join(missing))
    for package in requires.get("pip", []) or []:
        notes.append(f"pip prerequisite (not auto-installed): {package}")
    unknown_reqs = sorted(set(requires) - {"exec", "pip"})
    status = _COMPAT_READY
    if profile in ("openclaw", "hermes"):
        status = _COMPAT_NEEDS_ADAPTER
        notes.append(
            f"{profile} profile needs an adapter for base-dir/invocation controls "
            "(no adapter implemented; skill stays inactive)"
        )
    elif profile != "portable":
        status = _COMPAT_NEEDS_SETUP
        notes.append(f"unknown runtime profile needs review: {profile!r}")
    if unknown_reqs:
        status = _COMPAT_NEEDS_SETUP
        notes.append("unknown requirements need review: " + ", ".join(unknown_reqs))
    if missing or requires.get("pip"):
        status = _COMPAT_NEEDS_SETUP
    platform = runtime.get("os") or front.get("os")
    if platform and platform not in ("any", "linux", "posix"):
        status = _COMPAT_UNSUPPORTED
        notes.append(f"unsupported platform marker: {platform!r}")
    if manual_only and model_blocked:
        notes.append("manual-only: model invocation disabled by skill author")
    return (
        status,
        notes,
        {
            "allowed_tools": [str(t) for t in allowed or []],
            "user_invocable": bool(user_invocable),
            "model_invocable": not (model_blocked or manual_only),
        },
    )


# -- archives (local import and pinned fetch) ---------------------------------


def _check_archive_name(name: str, what: str) -> PurePosixPath:
    """Reject empty, absolute, or traversing member names before any write."""
    if not name or name.startswith("/") or name.startswith("\\"):
        raise SkillError(f"{what} member has an unsafe name: {name!r}")
    pure = PurePosixPath(name)
    if not pure.parts or any(part in ("", ".", "..") for part in pure.parts):
        raise SkillError(f"{what} member has an unsafe name: {name!r}")
    if len(name) > 1024:
        raise SkillError(f"{what} member name too long: {name!r}")
    return pure


class _CappedStream:
    """Cap decompressed tar-stream bytes before unbounded parsing/allocation.

    Wraps a decompressed (or plain) tar byte stream so a gzip bomb cannot
    force unbounded allocation inside :mod:`tarfile`: every ``read(n)`` is
    clipped to the remaining budget and raises once the budget is exceeded,
    without ever allocating the requested huge ``n``.
    """

    def __init__(self, raw: Any, limit: int, what: str) -> None:
        self._raw = raw
        self._limit = limit
        self._total = 0
        self._what = what

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            n = self._limit - self._total + 1
        remaining = self._limit - self._total + 1
        if remaining <= 0:
            raise SkillError(f"{self._what} archive contents exceed size limit")
        # Never allocate the caller's huge n: clip to the remaining budget.
        want = min(n, remaining)
        try:
            chunk = self._raw.read(want)
        except SkillError:
            raise
        except Exception as exc:
            raise SkillError(f"{self._what} archive unreadable: {exc}") from None
        if not chunk:
            return b""
        self._total += len(chunk)
        if self._total > self._limit:
            raise SkillError(f"{self._what} archive contents exceed size limit")
        return chunk

    def readable(self) -> bool:
        return True


def _read_zip_member_bounded(archive: zipfile.ZipFile, info: zipfile.ZipInfo, what: str) -> bytes:
    """Defensively read one zip member: declared sizes are untrusted."""
    chunks: list[bytes] = []
    total = 0
    try:
        with archive.open(info, "r") as member:
            while True:
                chunk = member.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > SINGLE_FILE_MAX_BYTES:
                    raise SkillError(f"{what} archive file too large: {info.filename!r}")
                chunks.append(chunk)
    except SkillError:
        raise
    except Exception as exc:
        raise SkillError(f"{what} unreadable entry: {info.filename!r}: {exc}") from None
    return b"".join(chunks)


def _collect_tar_blobs_streaming(
    fileobj: Any, what: str, *, strip_top: str | None | Literal["auto"] = None
) -> list[tuple[str, bytes]]:
    """Shared bounded tar parser for local and pinned-GitHub paths.

    Streams members (never ``getmembers`` over an unlimited stream),
    enforcing member-count, per-file, total-decompressed, and metadata bounds
    before unbounded parsing/allocation. All member validation happens before
    any filesystem write; duplicates, links, specials, and traversal are
    rejected. Declared ``member.size`` values are treated as untrusted hints:
    actual read bytes are bounded.
    """
    blobs: list[tuple[str, bytes]] = []
    seen: set[str] = set()
    total = 0
    count = 0
    top: str | None = strip_top  # type: ignore[assignment]
    try:
        archive = tarfile.open(fileobj=fileobj, mode="r|*")
    except Exception as exc:
        raise SkillError(f"{what} archive unreadable: {exc}") from None
    with archive:
        for member in archive:
            count += 1
            if count > MAX_ARCHIVE_MEMBERS:
                raise SkillError(f"{what} archive exceeds file count limit")
            _check_archive_name(member.name, what)
            rel = member.name
            effective_top = top if top not in (None, "auto") else None
            if strip_top == "auto" and top == "auto":
                # Derive the codeload top directory from the first member.
                first = member.name
                if member.isdir() and "/" not in first:
                    top = first.rstrip("/") + "/"
                    continue
                if "/" not in first:
                    raise SkillError(f"{what} member outside top directory: {first!r}")
                top = first.split("/")[0] + "/"
                effective_top = top
            if effective_top is not None:
                if member.name == effective_top:
                    continue
                if not member.name.startswith(effective_top):
                    raise SkillError(f"{what} member outside top directory: {member.name!r}")
                rel = member.name[len(effective_top) :]
                _check_archive_name(rel, what)
            if member.isdir():
                continue
            if not member.isreg():
                raise SkillError(f"{what} non-regular entry rejected: {member.name!r}")
            if member.size > SINGLE_FILE_MAX_BYTES:
                raise SkillError(f"{what} archive file too large: {rel!r}")
            if rel in seen:
                raise SkillError(f"{what} archive duplicate entry rejected: {rel!r}")
            seen.add(rel)
            extracted = archive.extractfile(member)
            if extracted is None:
                raise SkillError(f"{what} unreadable entry: {member.name!r}")
            # Bounded actual read: declared size is untrusted.
            chunks: list[bytes] = []
            actual = 0
            while True:
                chunk = extracted.read(65536)
                if not chunk:
                    break
                actual += len(chunk)
                if actual > SINGLE_FILE_MAX_BYTES:
                    raise SkillError(f"{what} archive file too large: {rel!r}")
                chunks.append(chunk)
            blob = b"".join(chunks)
            total += len(blob)
            if total > TOTAL_SKILL_MAX_BYTES:
                raise SkillError(f"{what} archive contents exceed size limit")
            blobs.append((rel, blob))
    if not blobs:
        raise SkillError(f"{what} archive holds no files")
    blobs.sort(key=lambda item: item[0])
    return blobs


def _validate_zip_members(archive: zipfile.ZipFile, what: str) -> list[zipfile.ZipInfo]:
    """Validate EVERY zip member before extraction. No extractall, no links."""
    infos = archive.infolist()
    if len(infos) > MAX_FILES_PER_SKILL + 1:
        raise SkillError(f"{what} archive exceeds file count limit")
    total = 0
    regular: list[zipfile.ZipInfo] = []
    for info in infos:
        pure = _check_archive_name(info.filename, what)
        if info.is_dir():
            continue
        if info.flag_bits & 0x1:
            raise SkillError(f"{what} encrypted entries are not supported: {info.filename!r}")
        mode = (info.external_attr >> 16) & 0o170000
        if mode == 0o120000:
            raise SkillError(f"{what} archive link rejected: {info.filename!r}")
        if mode != 0 and mode != 0o100000:
            raise SkillError(f"{what} non-regular entry rejected: {info.filename!r}")
        if info.file_size > SINGLE_FILE_MAX_BYTES:
            raise SkillError(f"{what} archive file too large: {pure.as_posix()!r}")
        total += info.file_size
        if total > TOTAL_SKILL_MAX_BYTES:
            raise SkillError(f"{what} archive contents exceed size limit")
        regular.append(info)
    if not regular:
        raise SkillError(f"{what} archive holds no files")
    return regular


def _capped_tar_stream(raw: Any, what: str, *, is_gzip: bool) -> _CappedStream:
    """Wrap a raw archive stream with a decompressed-byte cap.

    Gzip bombs expand far beyond their physical download cap, so the shared
    tar parser reads through this cap: every decompressed ``read`` is clipped
    to the remaining budget before allocation.
    """
    if is_gzip:
        try:
            decompressed: Any = gzip.GzipFile(fileobj=raw, mode="rb")
        except Exception as exc:
            raise SkillError(f"{what} archive unreadable: {exc}") from None
    else:
        decompressed = raw
    return _CappedStream(decompressed, MAX_TAR_STREAM_BYTES, what)


def _write_planned(root: Path, planned: list[tuple[str, bytes]]) -> None:
    """Write validated (relative-path, bytes) pairs (pinned, exclusive)."""
    _needs_no_follow()
    _require_real_dir(root, "staging")
    seen: set[str] = set()
    for rel, blob in planned:
        _check_archive_name(rel, "archive")
        if rel in seen:
            raise SkillError(f"archive duplicate entry rejected: {rel!r}")
        seen.add(rel)
        if len(blob) > SINGLE_FILE_MAX_BYTES:
            raise SkillError(f"archive file too large: {rel!r}")
    root_fd = _open_dir_fd(root, "staging")
    try:
        for rel, blob in planned:
            pure = PurePosixPath(rel)
            dir_fd = root_fd
            owned: list[int] = []
            try:
                for part in pure.parts[:-1]:
                    try:
                        st = os.stat(part, dir_fd=dir_fd, follow_symlinks=False)
                    except FileNotFoundError:
                        try:
                            os.mkdir(part, 0o755, dir_fd=dir_fd)
                        except FileExistsError:
                            pass
                        except OSError as exc:
                            raise SkillError(f"archive escape rejected: {rel!r}: {exc}") from None
                        try:
                            st = os.stat(part, dir_fd=dir_fd, follow_symlinks=False)
                        except OSError as exc:
                            raise SkillError(f"archive escape rejected: {rel!r}: {exc}") from None
                        if _stat.S_ISLNK(st.st_mode) or not _stat.S_ISDIR(st.st_mode):
                            raise SkillError(f"archive escape rejected: {rel!r}") from None
                        try:
                            sub = os.open(
                                part,
                                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                dir_fd=dir_fd,
                            )
                        except OSError as exc:
                            raise SkillError(f"archive escape rejected: {rel!r}: {exc}") from None
                        owned.append(sub)
                        dir_fd = sub
                        continue
                    except SkillError:
                        raise
                    except OSError as exc:
                        raise SkillError(f"archive escape rejected: {rel!r}: {exc}") from None
                    if _stat.S_ISLNK(st.st_mode) or not _stat.S_ISDIR(st.st_mode):
                        raise SkillError(f"archive escape rejected: {rel!r}") from None
                    try:
                        sub = os.open(
                            part,
                            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                            dir_fd=dir_fd,
                        )
                    except OSError as exc:
                        raise SkillError(f"archive escape rejected: {rel!r}: {exc}") from None
                    owned.append(sub)
                    dir_fd = sub
                leaf = pure.parts[-1]
                try:
                    os.stat(leaf, dir_fd=dir_fd, follow_symlinks=False)
                    raise SkillError(f"archive duplicate entry rejected: {rel!r}")
                except FileNotFoundError:
                    pass
                except SkillError:
                    raise
                except OSError as exc:
                    raise SkillError(f"archive escape rejected: {rel!r}: {exc}") from None
                try:
                    fd = os.open(
                        leaf,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                        0o644,
                        dir_fd=dir_fd,
                    )
                except FileExistsError:
                    raise SkillError(f"archive duplicate entry rejected: {rel!r}") from None
                except OSError as exc:
                    raise SkillError(f"archive escape rejected: {rel!r}: {exc}") from None
                try:
                    view = memoryview(blob)
                    while view:
                        try:
                            written = os.write(fd, view)
                        except OSError as exc:
                            raise SkillError(f"archive write failed: {rel!r}: {exc}") from None
                        view = view[written:]
                finally:
                    os.close(fd)
            finally:
                for fd in owned:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
    finally:
        try:
            os.close(root_fd)
        except OSError:
            pass


def _stage_local_archive(path: Path, staging: Path, what: str) -> Path:
    """Validate and manually extract a local .zip/.tar.gz into staging.

    Returns the extracted skill directory: either the staging root (when it
    holds SKILL.md) or its single subdirectory (when that holds SKILL.md).
    Raises for anything ambiguous.
    """
    st = _lstat_path(path)
    if _stat.S_ISLNK(st.st_mode) or not _stat.S_ISREG(st.st_mode):
        raise SkillError(f"{what} archive is not a regular file: {path}")
    if st.st_size > TOTAL_SKILL_MAX_BYTES:
        raise SkillError(f"{what} archive exceeds size limit")
    _require_real_dir(staging, "staging")
    suffixes = path.suffixes
    if path.suffix == ".zip":
        # Physical cap preserved; members defensively read with size bounds
        # (declared sizes treated as untrusted) before any write. The archive
        # itself is opened O_NOFOLLOW so a raced symlink swap is rejected.
        _needs_no_follow()
        try:
            archive_fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError as exc:
            raise SkillError(f"{what} archive is not a regular file: {path}: {exc}") from None
        try:
            if not _stat.S_ISREG(os.fstat(archive_fd).st_mode):
                raise SkillError(f"{what} archive is not a regular file: {path}")
            with os.fdopen(archive_fd, "rb") as raw:
                with zipfile.ZipFile(raw) as archive:
                    regular = _validate_zip_members(archive, what)
                    seen: set[str] = set()
                    blobs: list[tuple[str, bytes]] = []
                    total = 0
                    for info in regular:
                        if info.filename in seen:
                            raise SkillError(
                                f"{what} archive duplicate entry rejected: {info.filename!r}"
                            )
                        seen.add(info.filename)
                        blob = _read_zip_member_bounded(archive, info, what)
                        total += len(blob)
                        if total > TOTAL_SKILL_MAX_BYTES:
                            raise SkillError(f"{what} archive contents exceed size limit")
                        blobs.append((info.filename, blob))
        except SkillError:
            try:
                os.close(archive_fd)
            except OSError:
                pass
            raise
        _write_planned(staging, blobs)
    elif suffixes[-2:] == [".tar", ".gz"] or path.suffix in (".tgz", ".tar"):
        # Shared bounded streaming parser: decompressed-byte and member caps
        # enforced before unbounded parsing/allocation, never getmembers.
        _needs_no_follow()
        is_gzip = path.suffix in (".tgz", ".gz") or suffixes[-2:] == [".tar", ".gz"]
        try:
            archive_fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError as exc:
            raise SkillError(f"{what} archive is not a regular file: {path}: {exc}") from None
        try:
            if not _stat.S_ISREG(os.fstat(archive_fd).st_mode):
                raise SkillError(f"{what} archive is not a regular file: {path}")
            with os.fdopen(archive_fd, "rb") as raw:
                capped = _capped_tar_stream(raw, what, is_gzip=is_gzip)
                blobs = _collect_tar_blobs_streaming(capped, what, strip_top=None)
        except SkillError:
            try:
                os.close(archive_fd)
            except OSError:
                pass
            raise
        _write_planned(staging, blobs)
    else:
        raise SkillError(f"{what} archive type unsupported (need .zip or .tar.gz): {path.name!r}")
    if (staging / "SKILL.md").is_file():
        return staging
    subdirs = sorted(p for p in staging.iterdir() if p.is_dir() and not p.is_symlink())
    if len(subdirs) == 1 and (subdirs[0] / "SKILL.md").is_file():
        return subdirs[0]
    raise SkillError(f"{what} archive must hold SKILL.md at its root or in one folder")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: the pinned bytes must come from the pinned URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN202
        raise SkillError(f"redirects are not followed for skill fetches (got {code})")


def _check_fetch_base(base_url: str) -> str:
    """Allow only the public codeload host or an explicit loopback test seam."""
    parsed = urllib.parse.urlparse(base_url)
    if parsed.hostname == "codeload.github.com":
        if parsed.scheme != "https" or parsed.path not in ("", "/"):
            raise SkillError(f"invalid fetch base: {base_url!r}")
        return base_url.rstrip("/")
    if parsed.hostname == "127.0.0.1" and parsed.scheme in ("http", "https"):
        return base_url.rstrip("/")
    raise SkillError(f"unsupported fetch base (need {_CODELOAD_BASE}): {base_url!r}")


def fetch_github_pinned(
    source_ref: SourceRef,
    dest: Path,
    *,
    timeout_s: float = 30.0,
    max_bytes: int = TOTAL_SKILL_MAX_BYTES,
    base_url: str = _CODELOAD_BASE,
) -> Path:
    """Fetch one pinned public GitHub repo tarball into ``dest`` (opt-in).

    Only HTTPS ``codeload.github.com`` (or an explicit ``http(s)://127.0.0.1``
    loopback base for offline tests), only at a full 40-hex commit SHA, with
    no redirects, a bounded download, and all-member validation with manual
    bounded extraction (never ``extractall``). Raises :class:`SkillError`
    for anything else, including ClawHub refs, unpinned refs, private hosts,
    and oversized archives. No credentials are ever attached; private
    sources must be fetched by the caller.
    """
    if not isinstance(source_ref, SourceRef):
        raise SkillError("fetch needs a typed SourceRef, not a dict or string")
    if source_ref.kind != "github":
        raise SkillError(
            f"remote fetch supports pinned GitHub only, not {source_ref.kind}: "
            "ClawHub live API is unresolved; pass a local directory instead"
        )
    source_ref.validate()  # pinned SHA enforced here
    base = _check_fetch_base(base_url)
    owner = source_ref.namespace.split(":", 1)[1]
    url = f"{base}/{owner}/{source_ref.repository or source_ref.name}/tar.gz/{source_ref.ref}"
    request = urllib.request.Request(url, headers={"User-Agent": "kinaiya-ecosystem/0.1"})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout_s) as response:  # noqa: S310
            chunks: list[bytes] = []
            received = 0
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                received += len(chunk)
                if received > max_bytes:
                    raise SkillError("github archive exceeds download limit")
                chunks.append(chunk)
            payload = b"".join(chunks)
    except SkillError:
        raise
    except Exception as exc:
        raise SkillError(f"github fetch failed: {exc}") from None
    dest = Path(dest)
    # Do not follow a raced destination symlink, and never write into a
    # non-empty destination.
    try:
        st_dest = os.lstat(dest)
    except FileNotFoundError:
        pass
    else:
        if _stat.S_ISLNK(st_dest.st_mode):
            raise SkillError(f"fetch destination must not be a symlink: {dest}")
        if _stat.S_ISDIR(st_dest.st_mode) and any(dest.iterdir()):
            raise SkillError(f"fetch destination is not empty: {dest}")
        if not _stat.S_ISDIR(st_dest.st_mode):
            raise SkillError(f"fetch destination is not empty: {dest}")
    # Same bounded streaming parser as local tar: decompressed-byte and
    # member caps enforced before unbounded parsing/allocation (never
    # getmembers). The top directory is derived streaming ("auto").
    import io as _io

    raw = _io.BytesIO(payload)
    capped = _capped_tar_stream(raw, "github", is_gzip=True)
    blobs = _collect_tar_blobs_streaming(capped, "github", strip_top="auto")
    _make_dirs_no_follow(dest)
    _require_real_dir(dest, "fetch destination")
    _write_planned(dest, blobs)
    selected = dest / source_ref.subdir if source_ref.subdir else dest
    fd = _open_dir_fd(selected, "selected GitHub skill directory")
    os.close(fd)
    return selected


def _require_owner(authority: str | None, operation: str) -> None:
    """Lifecycle mutations need explicit owner authority; nothing else grants it."""
    if authority != OWNER_AUTHORITY:
        raise SkillError(
            f"owner authority required for skill {operation} (got {authority!r}); "
            "discovery and model invocation cannot grant lifecycle authority"
        )


class SkillManager:
    """Owns the skill registry under ``root`` with workspace-scoped activation.

    ``root`` is the install registry (profile/user level). ``workspace`` is
    an optional narrower activation scope; loads resolve resources only
    inside the installed package. Installs land inactive and disabled skills
    stay inert: :meth:`list` exposes metadata only, :meth:`load` requires
    an enabled, installed skill. Nothing here executes skill content: there
    is no import, subprocess, or dependency-install path.
    """

    def __init__(self, root: Path, workspace: Path | None = None) -> None:
        self.root = Path(root)
        self.workspace = Path(workspace) if workspace is not None else None
        # Reject pre-existing root symlink/special nodes before creating.
        try:
            st_root = os.lstat(self.root)
        except FileNotFoundError:
            pass
        else:
            if _stat.S_ISLNK(st_root.st_mode):
                raise SkillError(f"skill root must be a real directory, not a symlink: {self.root}")
            if not _stat.S_ISDIR(st_root.st_mode):
                raise SkillError(f"skill root is not a directory: {self.root}")
        if self.workspace is not None:
            self._check_workspace_disjoint()
        _make_dirs_no_follow(self.root)
        _require_real_dir(self.root, "skill root")
        self._registry_path = self.root / "registry.json"
        self._versions_dir = self.root / ".versions"
        self._installed_dir = self.root / "installed"
        for control, what in (
            (self._registry_path, "registry"),
            (self._versions_dir, "versions store"),
            (self._installed_dir, "installed store"),
        ):
            try:
                st = os.lstat(control)
            except FileNotFoundError:
                continue
            if _stat.S_ISLNK(st.st_mode):
                raise SkillError(f"skill {what} must not be a symlink: {control}")
            if what == "registry":
                if not _stat.S_ISREG(st.st_mode):
                    raise SkillError(f"skill registry is not a regular file: {control}")
            elif not _stat.S_ISDIR(st.st_mode):
                raise SkillError(f"skill {what} is not a directory: {control}")
        _make_dirs_no_follow(self._versions_dir)
        _make_dirs_no_follow(self._installed_dir)
        _require_real_dir(self._versions_dir, "versions store")
        _require_real_dir(self._installed_dir, "installed store")
        if self.workspace is not None:
            self._check_workspace_disjoint()
        self._registry: dict[str, dict[str, Any]] = self._read_registry()

    def _check_workspace_disjoint(self) -> None:
        """Keep manager control state outside/disjoint from Agent workspace."""
        assert self.workspace is not None
        try:
            root_real = os.path.realpath(self.root)
            ws_real = os.path.realpath(self.workspace)
        except OSError as exc:
            raise SkillError(f"workspace check failed: {exc}") from None
        if (
            root_real == ws_real
            or root_real.startswith(ws_real + os.sep)
            or ws_real.startswith(root_real + os.sep)
        ):
            raise SkillError(
                f"skill root {self.root} must stay outside/disjoint from workspace {self.workspace}"
            )

    # -- registry persistence -------------------------------------------
    def _read_registry(self) -> dict[str, dict[str, Any]]:
        try:
            st = os.lstat(self._registry_path)
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise SkillError(f"skill registry unreadable: {exc}") from None
        if _stat.S_ISLNK(st.st_mode):
            raise SkillError("skill registry must not be a symlink")
        if not _stat.S_ISREG(st.st_mode):
            raise SkillError("skill registry is corrupt")
        if st.st_size > REGISTRY_MAX_BYTES:
            raise SkillError("skill registry exceeds size limit")
        _needs_no_follow()
        try:
            fd = os.open(self._registry_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError as exc:
            raise SkillError(f"skill registry unreadable: {exc}") from None
        try:
            if not _stat.S_ISREG(os.fstat(fd).st_mode):
                raise SkillError("skill registry is corrupt")
            raw = _bounded_read_fd(fd, REGISTRY_MAX_BYTES, "skill registry")
        finally:
            os.close(fd)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SkillError(f"skill registry unreadable: {exc}") from None
        if not isinstance(data, dict):
            raise SkillError("skill registry is corrupt")
        return {str(k): v for k, v in data.items() if isinstance(v, dict)}

    def _write_registry(self) -> None:
        _needs_no_follow()
        _require_real_dir(self.root, "skill root")
        payload = json.dumps(self._registry, indent=2, sort_keys=True).encode("utf-8")
        if len(payload) > REGISTRY_MAX_BYTES:
            raise SkillError("skill registry exceeds size limit")
        root_fd = _open_dir_fd(self.root, "skill root")
        try:
            name = f".registry-{secrets.token_hex(16)}.tmp"
            try:
                fd = os.open(
                    name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600,
                    dir_fd=root_fd,
                )
            except OSError as exc:
                raise SkillError(f"skill registry write failed: {exc}") from None
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
                raise SkillError(f"skill registry write failed: {exc}") from None
            try:
                os.close(fd)
            except OSError as exc:
                raise SkillError(f"skill registry write failed: {exc}") from None
            try:
                os.replace(name, "registry.json", src_dir_fd=root_fd, dst_dir_fd=root_fd)
            except OSError as exc:
                try:
                    os.unlink(name, dir_fd=root_fd)
                except OSError:
                    pass
                raise SkillError(f"skill registry write failed: {exc}") from None
        finally:
            try:
                os.close(root_fd)
            except OSError:
                pass

    def _record(self, skill_id: str) -> SkillRecord:
        try:
            data = self._registry[skill_id]
            record = SkillRecord(**data)
        except KeyError:
            raise SkillError(f"unknown skill: {skill_id}") from None
        except TypeError as exc:
            raise SkillError(f"skill registry entry is corrupt: {skill_id}: {exc}") from None
        return _check_record_shapes(record)

    def _install_dir(self, record: SkillRecord) -> Path:
        _check_record_shapes(record)
        digest = hashlib.sha256(record.id.encode()).hexdigest()[:16]
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", record.name)
        target = self._installed_dir / record.namespace.replace(":", "_") / safe_name / digest
        self._ensure_owned(target)
        return target

    def _ensure_owned(self, target: Path) -> Path:
        """Confine every destructive path to manager-owned state (no follow)."""
        _require_real_dir(self.root, "skill root")
        _require_real_dir(self._installed_dir, "installed store")
        _require_real_dir(self._versions_dir, "versions store")
        target = Path(target)
        installed = Path(self._installed_dir)
        try:
            rel = target.relative_to(installed)
        except ValueError:
            raise SkillError(f"refusing to operate outside installed state: {target}") from None
        if not rel.parts or any(part in ("", ".", "..") for part in rel.parts):
            raise SkillError(f"refusing to operate outside installed state: {target}") from None
        if target == self.root or target == self._registry_path or target == self._versions_dir:
            raise SkillError(f"refusing to operate on manager control state: {target}")
        # Every existing prefix from installed/ down must be a real dir.
        probe = Path(installed)
        for part in rel.parts[:-1]:
            probe = probe / part
            try:
                st = os.lstat(probe)
            except FileNotFoundError:
                break
            except OSError as exc:
                raise SkillError(f"refusing unsafe state path {target}: {exc}") from None
            if _stat.S_ISLNK(st.st_mode) or not _stat.S_ISDIR(st.st_mode):
                raise SkillError(f"refusing unsafe state path: {target}")
        try:
            st = os.lstat(target)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise SkillError(f"refusing unsafe state path {target}: {exc}") from None
        else:
            # Existing targets must be real directories; symlinks/specials are
            # tamper, never silently replaced. Callers remove dirs explicitly.
            if _stat.S_ISLNK(st.st_mode):
                raise SkillError(f"installed state is not a directory: {target}")
        return target

    def _slot(self, skill_id: str) -> Path:
        """Return the versions slot, creating it without following symlinks."""
        _needs_no_follow()
        _require_real_dir(self._versions_dir, "versions store")
        name = hashlib.sha256(skill_id.encode()).hexdigest()[:16]
        versions_fd = _open_dir_fd(self._versions_dir, "versions store")
        try:
            try:
                st = os.stat(name, dir_fd=versions_fd, follow_symlinks=False)
            except FileNotFoundError:
                try:
                    os.mkdir(name, 0o755, dir_fd=versions_fd)
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise SkillError(f"cannot create version slot: {exc}") from None
                try:
                    st = os.stat(name, dir_fd=versions_fd, follow_symlinks=False)
                except OSError as exc:
                    raise SkillError(f"cannot create version slot: {exc}") from None
            except OSError as exc:
                raise SkillError(f"unreadable version slot: {exc}") from None
            if _stat.S_ISLNK(st.st_mode) or not _stat.S_ISDIR(st.st_mode):
                raise SkillError(f"version slot is unsafe: {skill_id}")
        finally:
            try:
                os.close(versions_fd)
            except OSError:
                pass
        return self._versions_dir / name

    # -- lifecycle -------------------------------------------------------
    @staticmethod
    def _coerce_ref(source_ref: SourceRef | None) -> SourceRef | None:
        if source_ref is None:
            return None
        if not isinstance(source_ref, SourceRef):
            raise SkillError("source_ref must be a typed SourceRef, never a dict")
        return source_ref.validate()

    def _build_record_from_blobs(
        self,
        blobs: list[tuple[str, bytes]],
        source_ref: SourceRef | None,
        *,
        previous_digest: str = "",
        enabled: bool = False,
        provenance_raw: str = "",
    ) -> tuple[SkillRecord, list[tuple[str, bytes]]]:
        """Derive metadata from exactly the captured snapshot bytes."""
        if previous_digest:
            _check_digest_format(previous_digest)
        by_name = dict(blobs)
        if "SKILL.md" not in by_name:
            raise SkillError("skill source has no SKILL.md")
        front, _ = _parse_skill_md_bytes(by_name["SKILL.md"])
        name = str(front["name"]).strip()
        if source_ref is not None and source_ref.kind != "local":
            if source_ref.name != name and source_ref.repository is None:
                raise SkillError(
                    f"source ref name {source_ref.name!r} does not match SKILL.md name {name!r}"
                )
            namespace = source_ref.namespace
        else:
            namespace = "local"
        skill_id = f"{namespace}/{name}"
        status, notes, flags = _assess_compatibility(front)
        raw_version = str(front.get("version") or "").strip()
        if not raw_version:
            if source_ref is not None and source_ref.ref is not None:
                raw_version = source_ref.ref
            else:
                raw_version = "0.0.0"
        version = SourceRef._check_version(raw_version, "skill version")
        provenance: dict[str, Any] = {
            "kind": source_ref.kind if source_ref else "local",
            "namespace": namespace,
            "name": name,
            "subdir": source_ref.subdir if source_ref else None,
            "ref": source_ref.ref if source_ref else None,
            "raw": (
                f"{source_ref.namespace}/{source_ref.repository or source_ref.name}"
                + (f"/{source_ref.subdir}" if source_ref.subdir else "")
                + (f"@{source_ref.ref}" if source_ref.ref else "")
            )
            if source_ref
            else provenance_raw,
            "repository": source_ref.repository if source_ref else None,
        }
        # Only fully ready skills can land enabled. needs_adapter stays
        # inactive: no adapter is implemented, so enabling would mislead.
        record = SkillRecord(
            id=skill_id,
            name=name,
            namespace=namespace,
            version=version,
            description=str(front["description"]).strip(),
            source=provenance,
            digest="",
            status=status,
            compatibility_notes=notes,
            enabled=enabled and status == _COMPAT_READY,
            bare_invocable=name not in RESERVED_COMMAND_NAMES,
            installed_at=time.time(),
            allowed_tools=flags["allowed_tools"],
            user_invocable=flags["user_invocable"],
            model_invocable=flags["model_invocable"],
            previous_digest=previous_digest,
        )
        return _check_record_shapes(record), blobs

    def _build_record(
        self,
        skill_dir: Path,
        source_ref: SourceRef | None,
        *,
        previous_digest: str = "",
        enabled: bool = False,
    ) -> tuple[SkillRecord, list[tuple[str, bytes]]]:
        """Legacy entry: snapshots once, then derives from captured bytes."""
        blobs = _capture_pinned(Path(skill_dir))
        return self._build_record_from_blobs(
            blobs,
            source_ref,
            previous_digest=previous_digest,
            enabled=enabled,
            provenance_raw=str(skill_dir),
        )

    def _replace_bytes_blobs(self, target: Path, blobs: list[tuple[str, bytes]]) -> None:
        """Stage exactly the captured snapshot bytes beside target, swap in."""
        self._ensure_owned(target)
        parent = Path(target).parent
        parent.mkdir(parents=True, exist_ok=True)
        _require_real_dir(parent, "install parent")
        staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=parent))
        try:
            _write_planned(staging, blobs)
            try:
                st = os.lstat(target)
            except FileNotFoundError:
                pass
            else:
                if _stat.S_ISLNK(st.st_mode) or not _stat.S_ISDIR(st.st_mode):
                    raise SkillError(f"installed state is not a directory: {target}")
                shutil.rmtree(target, ignore_errors=False)
            staging.replace(target)
            staging = None  # type: ignore[assignment]
        finally:
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)

    def _finalize_record(
        self, record: SkillRecord, files: list[tuple[str, bytes]], source: str = ""
    ) -> SkillRecord:
        # Install from exactly the captured bytes that produced the metadata,
        # then re-verify the installed tree through pinned descriptors.
        _ = source
        target = self._install_dir(record)
        self._replace_bytes_blobs(target, files)
        installed = _capture_pinned(target, require_skill_md=False)
        digest, names, _ = _digest_blobs(installed)
        expect, _, _ = _digest_blobs(files)
        if digest != expect:
            raise SkillError("install verification failed; refusing to record")
        record.digest = digest
        record.files = names
        self._registry[record.id] = asdict(record)
        self._write_registry()
        return record

    def install(
        self, source: Path, source_ref: SourceRef | None = None, *, authority: str
    ) -> SkillRecord:
        """Validate and install a local skill tree. Never executes content."""
        _require_owner(authority, "install")
        source_ref = self._coerce_ref(source_ref)
        record, files = self._build_record(Path(source), source_ref)
        if record.id in self._registry:
            existing = self._record(record.id)
            raise SkillError(
                f"skill already installed: {record.id} "
                f"(version {existing.version}, digest {existing.digest}); "
                "use update() or uninstall() first"
            )
        return self._finalize_record(record, files, Path(source))

    def install_archive(
        self, archive: Path, source_ref: SourceRef | None = None, *, authority: str
    ) -> SkillRecord:
        """Validate and install a local .zip/.tar.gz skill archive."""
        _require_owner(authority, "install")
        source_ref = self._coerce_ref(source_ref)
        staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=self.root))
        try:
            skill_dir = _stage_local_archive(Path(archive), staging, "local")
            record, files = self._build_record(skill_dir, source_ref)
            if record.id in self._registry:
                existing = self._record(record.id)
                raise SkillError(
                    f"skill already installed: {record.id} "
                    f"(version {existing.version}, digest {existing.digest}); "
                    "use update() or uninstall() first"
                )
            return self._finalize_record(record, files, skill_dir)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def update(
        self, skill_id: str, source: Path, source_ref: SourceRef | None = None, *, authority: str
    ) -> SkillRecord:
        """Replace the bytes of an installed skill, preserving the prior snapshot."""
        _require_owner(authority, "update")
        source_ref = self._coerce_ref(source_ref)
        current = self._record(skill_id)
        self.verify(current)  # refuse to pave over tampered bytes silently
        self._stash_snapshot(current)
        record, files = self._build_record(
            Path(source),
            source_ref,
            previous_digest=current.digest,
            enabled=current.enabled,
        )
        if record.id != skill_id:
            raise SkillError(
                f"update identity mismatch: {record.id!r} is not the installed {skill_id!r}"
            )
        return self._finalize_record(record, files, Path(source))

    def update_archive(
        self, skill_id: str, archive: Path, source_ref: SourceRef | None = None, *, authority: str
    ) -> SkillRecord:
        """Replace the bytes of an installed skill from a local archive."""
        _require_owner(authority, "update")
        source_ref = self._coerce_ref(source_ref)
        current = self._record(skill_id)
        self.verify(current)
        self._stash_snapshot(current)
        staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=self.root))
        try:
            skill_dir = _stage_local_archive(Path(archive), staging, "local")
            record, files = self._build_record(
                skill_dir, source_ref, previous_digest=current.digest, enabled=current.enabled
            )
            if record.id != skill_id:
                raise SkillError(
                    f"update identity mismatch: {record.id!r} is not the installed {skill_id!r}"
                )
            return self._finalize_record(record, files, skill_dir)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _stash_snapshot(self, record: SkillRecord) -> None:
        """Persist content-addressed bytes + record for rollback (pinned)."""
        _check_digest_format(record.digest)
        target = self._install_dir(record)
        _require_real_dir(target, "installed skill")
        blobs = _capture_pinned(target, require_skill_md=False)
        digest, _, _ = _digest_blobs(blobs)
        if digest != record.digest:
            raise SkillError(f"skill bytes changed since install: {record.id}")
        slot = self._slot(record.id)
        snap = slot / record.digest.replace(":", "_")
        try:
            os.lstat(snap)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise SkillError(f"unreadable snapshot slot: {exc}") from None
        else:
            _require_real_dir(snap, "snapshot")
            existing = _capture_pinned(snap / "files")
            if _digest_blobs(existing)[0] != record.digest:
                raise SkillError("existing snapshot failed revalidation")
            return
        try:
            os.mkdir(snap, 0o755)
        except FileExistsError:
            return
        except OSError as exc:
            raise SkillError(f"cannot create snapshot: {exc}") from None
        try:
            files_dir = snap / "files"
            files_dir.mkdir(parents=True)
            _write_planned(files_dir, blobs)
            _needs_no_follow()
            try:
                fd = os.open(
                    snap / "record.json",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o644,
                )
            except OSError as exc:
                raise SkillError(f"cannot write snapshot record: {exc}") from None
            try:
                payload = json.dumps(asdict(record), indent=2, sort_keys=True).encode("utf-8")
                view = memoryview(payload)
                while view:
                    written = os.write(fd, view)
                    view = view[written:]
            finally:
                os.close(fd)
        except Exception:
            shutil.rmtree(snap, ignore_errors=True)
            raise

    def versions(self, skill_id: str) -> list[dict[str, Any]]:
        """List preserved content-addressed snapshots, oldest first."""
        self._record(skill_id)  # unknown skills stay unknown here too
        slot = self._slot(skill_id)
        found: list[dict[str, Any]] = []
        if slot.is_dir():
            for snap in sorted(slot.iterdir()):
                _require_real_dir(snap, "snapshot")
                try:
                    data = json.loads(
                        _read_pinned_file(
                            snap, "record.json", REGISTRY_MAX_BYTES, "snapshot record"
                        )
                    )
                except (json.JSONDecodeError, OSError):
                    continue
                if not isinstance(data, dict) or data.get("id") != skill_id:
                    continue
                found.append(
                    {
                        "version": data.get("version"),
                        "digest": data.get("digest"),
                        "installed_at": data.get("installed_at"),
                        "status": data.get("status"),
                    }
                )
        return sorted(found, key=lambda item: item.get("installed_at") or 0)

    def inspect(self, skill_id: str) -> dict[str, Any]:
        """Full record plus installed file list. Reads no file contents."""
        record = self._record(skill_id)
        data = asdict(record)
        data["installed"] = self._install_dir(record).is_dir()
        data["snapshots"] = len(self.versions(skill_id))
        return data

    def list(self) -> list[dict[str, Any]]:
        """Bounded discovery metadata for every installed skill, no bodies."""
        return [self._record(skill_id).metadata() for skill_id in sorted(self._registry)]

    def _reassess(self, record: SkillRecord) -> SkillRecord:
        """Refresh compatibility from the installed SKILL.md (missing setup drifts)."""
        front, _ = _parse_skill_md_bytes(dict(self._verified_blobs(record))["SKILL.md"])
        status, notes, flags = _assess_compatibility(front)
        record.status = status
        record.compatibility_notes = notes
        record.allowed_tools = flags["allowed_tools"]
        record.user_invocable = flags["user_invocable"]
        record.model_invocable = flags["model_invocable"]
        return record

    def enable(self, skill_id: str, *, authority: str) -> SkillRecord:
        """Activate an installed skill. Only fully ready skills enable."""
        _require_owner(authority, "enable")
        record = self._record(skill_id)
        self.verify(record)
        self._reassess(record)
        if record.status == _COMPAT_UNSUPPORTED:
            self._registry[skill_id] = asdict(record)
            self._write_registry()
            raise SkillError(f"cannot enable unsupported skill: {skill_id}")
        if record.status == _COMPAT_NEEDS_SETUP:
            self._registry[skill_id] = asdict(record)
            self._write_registry()
            raise SkillError(
                f"cannot enable skill needing setup: {skill_id} "
                f"({'; '.join(record.compatibility_notes)})"
            )
        if record.status == _COMPAT_NEEDS_ADAPTER:
            self._registry[skill_id] = asdict(record)
            self._write_registry()
            raise SkillError(
                f"cannot enable skill needing an adapter (none implemented): {skill_id} "
                f"({'; '.join(record.compatibility_notes)})"
            )
        record.enabled = True
        if record.status == _COMPAT_DISABLED:
            record.status = _COMPAT_READY
        self._registry[skill_id] = asdict(record)
        self._write_registry()
        return record

    def disable(self, skill_id: str, *, authority: str) -> SkillRecord:
        _require_owner(authority, "disable")
        record = self._record(skill_id)
        record.enabled = False
        record.status = _COMPAT_DISABLED
        self._registry[skill_id] = asdict(record)
        self._write_registry()
        return record

    def uninstall(self, skill_id: str, *, authority: str) -> None:
        """Remove an installed skill. Only manager-owned bytes are deleted."""
        _require_owner(authority, "uninstall")
        record = self._record(skill_id)
        target = self._install_dir(record)
        self._ensure_owned(target)
        if target.exists() or target.is_symlink():
            if target.is_symlink() or not target.is_dir():
                raise SkillError(f"installed state is not a directory: {skill_id}")
            shutil.rmtree(target)
        del self._registry[skill_id]
        self._write_registry()

    def remove(self, skill_id: str, *, authority: str) -> None:
        """Alias of :meth:`uninstall` for API compatibility."""
        self.uninstall(skill_id, authority=authority)

    def rollback(self, skill_id: str, *, authority: str) -> SkillRecord:
        """Restore the most recent snapshot that differs from the live install."""
        _require_owner(authority, "rollback")
        record = self._record(skill_id)
        slot = self._slot(skill_id)
        candidates: list[tuple[float, Path, dict[str, Any]]] = []
        if slot.is_dir():
            for snap in sorted(slot.iterdir()):
                _require_real_dir(snap, "snapshot")
                try:
                    data = json.loads(
                        _read_pinned_file(
                            snap, "record.json", REGISTRY_MAX_BYTES, "snapshot record"
                        )
                    )
                except (json.JSONDecodeError, OSError):
                    continue
                if not isinstance(data, dict) or data.get("id") != skill_id:
                    continue
                digest = str(data.get("digest", ""))
                if not digest or digest == record.digest:
                    continue
                if snap.name != digest.replace(":", "_"):
                    continue  # snapshot directory was renamed: refuse it
                candidates.append((float(data.get("installed_at") or 0), snap, data))
        if not candidates:
            raise SkillError(f"no previous version stashed for {skill_id}")
        _, snap, data = max(candidates, key=lambda item: item[0])
        # Capture once: the digest and restored files must use the same
        # descriptor-pinned bytes, including if the source changes afterward.
        snap_files = snap / "files"
        blobs = _capture_pinned(snap_files)
        digest, _, _ = _digest_blobs(blobs)
        if digest != data.get("digest"):
            raise SkillError(f"snapshot for {skill_id} failed revalidation; refusing rollback")
        restored = _check_record_shapes(SkillRecord(**data))
        if record.enabled and restored.status == _COMPAT_READY:
            restored.enabled = True
        else:
            restored.enabled = False
            restored.status = _COMPAT_DISABLED
        target = self._install_dir(restored)
        self._ensure_owned(target)
        self._replace_bytes_blobs(target, blobs)
        self._registry[skill_id] = asdict(restored)
        self._write_registry()
        return self.verify(restored)

    def _verified_blobs(self, record: SkillRecord) -> list[tuple[str, bytes]]:
        target = self._install_dir(record)
        blobs = _capture_pinned(target)
        digest, _, _ = _digest_blobs(blobs)
        if digest != record.digest:
            raise SkillError(
                f"skill bytes changed since install: {record.id}; reinstall or roll back"
            )
        front, _ = _parse_skill_md_bytes(dict(blobs)["SKILL.md"])
        _, _, flags = _assess_compatibility(front)
        if any(getattr(record, key) != value for key, value in flags.items()):
            raise SkillError("skill invocation metadata differs from its pinned instructions")
        return blobs

    def verify(self, record: SkillRecord | str) -> SkillRecord:
        if isinstance(record, str):
            record = self._record(record)
        else:
            _check_record_shapes(record)
        _check_digest_format(record.digest)
        self._verified_blobs(record)
        return record

    def load(self, skill_id: str, *, invocation: str = "model") -> SkillInstructions:
        """Load one enabled skill's instruction body (bounded, untrusted)."""
        if invocation not in ("user", "model"):
            raise SkillError(f"unknown invocation: {invocation!r}")
        record = self._record(skill_id)
        if not record.enabled or record.status in (_COMPAT_DISABLED, _COMPAT_NEEDS_ADAPTER):
            raise SkillError(f"skill is not enabled: {skill_id}")
        if invocation == "user" and not record.user_invocable:
            raise SkillError(f"skill is not user-invocable: {skill_id}")
        if invocation == "model" and not record.model_invocable:
            raise SkillError(f"skill model invocation is disabled: {skill_id}")
        blobs = self._verified_blobs(record)
        _, body = _parse_skill_md_bytes(dict(blobs)["SKILL.md"])
        if len(body) > INSTRUCTIONS_MAX_CHARS:
            body = body[:INSTRUCTIONS_MAX_CHARS] + "\n…[truncated]\n"
        return SkillInstructions(
            id=record.id,
            name=record.name,
            description=record.description,
            body=body,
            allowed_tools=tuple(record.allowed_tools),
            user_invocable=record.user_invocable,
        )

    def load_resource(self, skill_id: str, relpath: str, *, invocation: str = "model") -> bytes:
        record = self._record(skill_id)
        if not record.enabled or record.status in (_COMPAT_DISABLED, _COMPAT_NEEDS_ADAPTER):
            raise SkillError(f"skill is not enabled: {skill_id}")
        if invocation not in {"user", "model"}:
            raise SkillError("unknown skill invocation")
        if (invocation == "user" and not record.user_invocable) or (
            invocation == "model" and not record.model_invocable
        ):
            raise SkillError("skill invocation is disabled")
        pure = PurePosixPath(relpath)
        if pure.is_absolute():
            raise SkillError("resource path must be relative")
        if ".." in pure.parts:
            raise SkillError("resource escapes skill dir")
        blobs = dict(self._verified_blobs(record))
        data = blobs.get(pure.as_posix())
        if data is None:
            raise SkillError("resource is not a regular file")
        if len(data) > RESOURCE_MAX_BYTES:
            raise SkillError("resource exceeds size limit")
        return data
