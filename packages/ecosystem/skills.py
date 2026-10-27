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

import hashlib
import json
import re
import shutil
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

# -- limits -------------------------------------------------------------------
SKILL_MD_MAX_BYTES = 262_144
SINGLE_FILE_MAX_BYTES = 1_048_576
TOTAL_SKILL_MAX_BYTES = 8_048_576
MAX_FILES_PER_SKILL = 200
FRONTMATTER_MAX_BYTES = 65_536
INSTRUCTIONS_MAX_CHARS = 200_000
RESOURCE_MAX_BYTES = 262_144

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
            subdir=subdir,
            ref=ref,
            raw=raw,
        )

    @staticmethod
    def _parse_github_url(raw: str) -> SourceRef:
        if "?" in raw or "#" in raw:
            raise SkillError(f"github URL must not carry query or fragment: {raw!r}")
        path = raw.split("github.com/", 1)[1].strip("/")
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
        if self.check_event(yaml.events.AliasEvent):
            raise SkillError("SKILL.md frontmatter must not use YAML aliases")
        return super().compose_node(parent, index)

    def construct_mapping(self, node, deep=False):  # noqa: ANN001, ANN202
        seen: list[Any] = []
        for key_node, _value_node in node.value:
            key = self.construct_object(key_node, deep=True)
            if key in seen:
                raise SkillError(f"SKILL.md frontmatter has a duplicate key: {key!r}")
            seen.append(key)
        return super().construct_mapping(node, deep)


def _read_skill_md(skill_dir: Path) -> tuple[dict[str, Any], str]:
    """Parse SKILL.md frontmatter and body with hard bounds.

    Returns ``(frontmatter, body)``. Raises :class:`SkillError` for a
    missing file, oversized input, malformed YAML, aliases, duplicate or
    non-string keys, or missing/invalid ``name``/``description``/``version``.
    """
    candidate = skill_dir / "SKILL.md"
    if candidate.is_symlink():
        raise SkillError("SKILL.md must be a regular file, not a symlink")
    if not candidate.is_file():
        raise SkillError(f"no SKILL.md in {skill_dir}")
    if candidate.stat().st_size > SKILL_MD_MAX_BYTES:
        raise SkillError("SKILL.md exceeds size limit")
    text = candidate.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise SkillError("SKILL.md needs YAML frontmatter delimited by ---")
    try:
        _, raw_front, body = text.split("---", 2)
    except ValueError:
        raise SkillError("SKILL.md has unterminated frontmatter") from None
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


def _validate_tree(source: Path) -> list[Path]:
    """Collect regular files, rejecting traversal, escapes, and bombs.

    Works with absolute or relative roots: only the path *relative* to the
    root is judged. Symlinks, FIFOs, sockets, and device nodes are rejected
    outright, never installed or followed.
    """
    if source.is_symlink():
        raise SkillError("skill source must be a real directory, not a symlink")
    if not source.is_dir():
        raise SkillError(f"skill source is not a directory: {source}")
    files: list[Path] = []
    total = 0
    for path in sorted(source.rglob("*")):
        try:
            rel = path.relative_to(source)
        except ValueError:
            raise SkillError(f"entry escapes skill source: {path}") from None
        if not rel.parts or ".." in rel.parts or rel.is_absolute():
            raise SkillError(f"unsafe entry: {rel}")
        if path.is_symlink():
            raise SkillError(f"symlinks are not installed: {rel}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise SkillError(f"special files are not installed: {rel}")
        size = path.stat().st_size
        if size > SINGLE_FILE_MAX_BYTES:
            raise SkillError(f"file too large: {rel} ({size} bytes)")
        total += size
        if total > TOTAL_SKILL_MAX_BYTES:
            raise SkillError("skill exceeds total size limit")
        files.append(path)
    if len(files) > MAX_FILES_PER_SKILL:
        raise SkillError("skill exceeds file count limit")
    if (source / "SKILL.md") not in files:
        raise SkillError("skill source has no SKILL.md")
    return files


def _assert_no_special(root: Path) -> None:
    """Reject symlinks and special files hiding in an installed tree."""
    for path in sorted(root.rglob("*")):
        try:
            rel = path.relative_to(root)
        except ValueError:
            raise SkillError(f"entry escapes skill tree: {path}") from None
        if path.is_symlink():
            raise SkillError(f"installed skill must not contain symlinks: {rel}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise SkillError(f"installed skill must not contain special files: {rel}")


def _digest_tree(root: Path) -> tuple[str, list[str], int]:
    """Hash sorted relative-path + bytes pairs. Caller checks for symlinks."""
    names: list[str] = []
    total = 0
    hasher = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        names.append(rel)
        blob = path.read_bytes()
        total += len(blob)
        hasher.update(len(rel).to_bytes(4, "little"))
        hasher.update(rel.encode("utf-8"))
        hasher.update(len(blob).to_bytes(8, "little"))
        hasher.update(blob)
    return "sha256:" + hasher.hexdigest(), names, total


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
    user_invocable = front.get("user-invocable", True)
    model_blocked = bool(front.get("disable-model-invocation", False))
    if not isinstance(user_invocable, bool):
        raise SkillError("'user-invocable' must be boolean when present")
    runtime = front.get("runtime") or front.get("compatibility", {})
    if isinstance(runtime, str):
        runtime = {"profile": runtime}
    if not isinstance(runtime, dict):
        raise SkillError("'compatibility' must be a mapping when present")
    profile = str(runtime.get("profile", "portable")).lower()
    requires = front.get("requires", {}) or {}
    if not isinstance(requires, dict):
        raise SkillError("'requires' must be a mapping when present")

    missing: list[str] = []
    for binary in requires.get("exec", []) or []:
        if shutil.which(str(binary)) is None:
            missing.append(str(binary))
    if missing:
        notes.append("missing executables: " + ", ".join(missing))
    for package in requires.get("pip", []) or []:
        notes.append(f"pip prerequisite (not auto-installed): {package}")
    status = _COMPAT_READY
    if profile in ("openclaw", "hermes"):
        status = _COMPAT_NEEDS_ADAPTER
        notes.append(f"{profile} profile may need an adapter for base-dir/invocation controls")
    if missing or requires.get("pip"):
        status = _COMPAT_NEEDS_SETUP
    platform = runtime.get("os") or front.get("os")
    if platform and platform not in ("any", "linux", "posix"):
        status = _COMPAT_UNSUPPORTED
        notes.append(f"unsupported platform marker: {platform!r}")
    if front.get("manual-only", False) and model_blocked:
        notes.append("manual-only: model invocation disabled by skill author")
    return (
        status,
        notes,
        {
            "allowed_tools": [str(t) for t in allowed or []],
            "user_invocable": bool(user_invocable),
            "model_invocable": not model_blocked,
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


def _validate_tar_members(
    members: list[tarfile.TarInfo], what: str, *, strip_top: str | None = None
) -> list[tuple[tarfile.TarInfo, str]]:
    """Validate EVERY tar member before extraction. No extractall, no links.

    Returns ``(member, relative-path)`` pairs with the optional top-level
    directory prefix stripped. Members outside the top directory are
    rejected, not skipped: a partial allowlist is a traversal bug.
    """
    if len(members) > MAX_FILES_PER_SKILL + 1:
        raise SkillError(f"{what} archive exceeds file count limit")
    total = 0
    planned: list[tuple[tarfile.TarInfo, str]] = []
    for member in members:
        _check_archive_name(member.name, what)
        rel = member.name
        if strip_top is not None:
            if member.name == strip_top:
                continue
            if not member.name.startswith(strip_top):
                raise SkillError(f"{what} member outside top directory: {member.name!r}")
            rel = member.name[len(strip_top) :]
            _check_archive_name(rel, what)
        if member.isdir():
            continue
        if not member.isreg():
            raise SkillError(f"{what} non-regular entry rejected: {member.name!r}")
        if member.size > SINGLE_FILE_MAX_BYTES:
            raise SkillError(f"{what} archive file too large: {rel!r}")
        total += member.size
        if total > TOTAL_SKILL_MAX_BYTES:
            raise SkillError(f"{what} archive contents exceed size limit")
        planned.append((member, rel))
    if not planned:
        raise SkillError(f"{what} archive holds no files")
    return planned


def _write_planned(root: Path, planned: list[tuple[str, bytes]]) -> None:
    """Write validated (relative-path, bytes) pairs, containment-checked."""
    base = root.resolve()
    for rel, blob in planned:
        candidate = (base / rel).resolve()
        try:
            candidate.relative_to(base)
        except ValueError:
            raise SkillError(f"archive escape rejected: {rel!r}") from None
        if candidate.exists() or candidate.is_symlink():
            raise SkillError(f"archive duplicate entry rejected: {rel!r}")
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_bytes(blob)


def _stage_local_archive(path: Path, staging: Path, what: str) -> Path:
    """Validate and manually extract a local .zip/.tar.gz into staging.

    Returns the extracted skill directory: either the staging root (when it
    holds SKILL.md) or its single subdirectory (when that holds SKILL.md).
    Raises for anything ambiguous.
    """
    if path.is_symlink() or not path.is_file():
        raise SkillError(f"{what} archive is not a regular file: {path}")
    if path.stat().st_size > TOTAL_SKILL_MAX_BYTES:
        raise SkillError(f"{what} archive exceeds size limit")
    suffixes = path.suffixes
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as archive:
            regular = _validate_zip_members(archive, what)
            blobs = [(info.filename, archive.read(info.filename)) for info in regular]
        _write_planned(staging, blobs)
    elif suffixes[-2:] == [".tar", ".gz"] or path.suffix in (".tgz", ".tar"):
        mode = "r:gz" if path.suffix in (".tgz", ".gz") else "r:"
        with tarfile.open(path, mode) as archive:
            planned = _validate_tar_members(archive.getmembers(), what)
            blobs = []
            for member, rel in planned:
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise SkillError(f"{what} unreadable entry: {member.name!r}")
                blobs.append((rel, extracted.read()))
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
    url = f"{base}/{owner}/{source_ref.name}/tar.gz/{source_ref.ref}"
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
    if dest.exists() and any(dest.iterdir()):
        raise SkillError(f"fetch destination is not empty: {dest}")
    dest.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
        tmp.write(payload)
        tmp_path = tmp.name
    try:
        with tarfile.open(tmp_path, "r:gz") as archive:
            members = archive.getmembers()
            top = (members[0].name.split("/")[0] + "/") if members else ""
            planned = _validate_tar_members(members, "github", strip_top=top or None)
            blobs = []
            for member, rel in planned:
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise SkillError(f"github unreadable entry: {member.name!r}")
                blobs.append((rel, extracted.read()))
        _write_planned(dest, blobs)
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    return dest


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
        self.root.mkdir(parents=True, exist_ok=True)
        self._registry_path = self.root / "registry.json"
        self._versions_dir = self.root / ".versions"
        self._installed_dir = self.root / "installed"
        self._versions_dir.mkdir(exist_ok=True)
        self._installed_dir.mkdir(exist_ok=True)
        self._registry: dict[str, dict[str, Any]] = self._read_registry()

    # -- registry persistence -------------------------------------------
    def _read_registry(self) -> dict[str, dict[str, Any]]:
        if not self._registry_path.exists():
            return {}
        try:
            data = json.loads(self._registry_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise SkillError(f"skill registry unreadable: {exc}") from None
        if not isinstance(data, dict):
            raise SkillError("skill registry is corrupt")
        return {str(k): v for k, v in data.items() if isinstance(v, dict)}

    def _write_registry(self) -> None:
        tmp = self._registry_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._registry, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self._registry_path)

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
        """Confine every destructive path to manager-owned state."""
        base = self.root.resolve()
        candidate = target.resolve() if target.exists() else (target.parent.resolve() / target.name)
        # Never operate outside the registry root, and never on the registry
        # file, the versions store, or the root itself.
        try:
            candidate.relative_to(base)
        except ValueError:
            raise SkillError(f"refusing to operate outside skill root: {target}") from None
        if candidate in (base, self._registry_path.resolve(), self._versions_dir.resolve()):
            raise SkillError(f"refusing to operate on manager control state: {target}")
        try:
            candidate.relative_to(self._installed_dir.resolve())
        except ValueError:
            raise SkillError(f"refusing to operate outside installed state: {target}") from None
        return target

    def _slot(self, skill_id: str) -> Path:
        slot = self._versions_dir / hashlib.sha256(skill_id.encode()).hexdigest()[:16]
        slot.mkdir(parents=True, exist_ok=True)
        return slot

    # -- lifecycle -------------------------------------------------------
    @staticmethod
    def _coerce_ref(source_ref: SourceRef | None) -> SourceRef | None:
        if source_ref is None:
            return None
        if not isinstance(source_ref, SourceRef):
            raise SkillError("source_ref must be a typed SourceRef, never a dict")
        return source_ref.validate()

    def _build_record(
        self,
        skill_dir: Path,
        source_ref: SourceRef | None,
        *,
        previous_digest: str = "",
        enabled: bool = False,
    ) -> tuple[SkillRecord, list[Path]]:
        files = _validate_tree(skill_dir)
        front, _ = _read_skill_md(skill_dir)
        name = str(front["name"]).strip()
        if source_ref is not None and source_ref.kind != "local":
            if source_ref.name != name:
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
            "raw": source_ref.raw if source_ref else str(skill_dir),
        }
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
            enabled=enabled and status in (_COMPAT_READY, _COMPAT_NEEDS_ADAPTER),
            bare_invocable=name not in RESERVED_COMMAND_NAMES,
            installed_at=time.time(),
            allowed_tools=flags["allowed_tools"],
            user_invocable=flags["user_invocable"],
            model_invocable=flags["model_invocable"],
            previous_digest=previous_digest,
        )
        return _check_record_shapes(record), files

    def _replace_bytes(self, target: Path, files: list[Path], source: Path) -> None:
        """Stage validated files beside the target, then swap them in."""
        self._ensure_owned(target)
        parent = target.resolve().parent if target.exists() else target.parent.resolve()
        parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=parent))
        try:
            for path in files:
                rel = path.relative_to(source)
                dest = (staging / rel).resolve()
                try:
                    dest.relative_to(staging.resolve())
                except ValueError:
                    raise SkillError(f"install escape rejected: {rel}") from None
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dest)
            if target.exists() or target.is_symlink():
                shutil.rmtree(target, ignore_errors=False)
            staging.replace(target)
            staging = None  # type: ignore[assignment]
        finally:
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)

    def _finalize_record(self, record: SkillRecord, files: list[Path], source: Path) -> SkillRecord:
        target = self._install_dir(record)
        self._replace_bytes(target, files, source)
        _assert_no_special(target)
        digest, names, _ = _digest_tree(target)
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
        """Persist content-addressed bytes + record for rollback."""
        target = self._install_dir(record)
        if not target.is_dir():
            raise SkillError(f"skill bytes missing, cannot snapshot: {record.id}")
        slot = self._slot(record.id)
        snap = slot / record.digest.replace(":", "_")
        if snap.is_dir():
            return  # immutable: this exact content is already preserved
        files_dir = snap / "files"
        files_dir.mkdir(parents=True)
        try:
            for path in sorted(target.rglob("*")):
                rel = path.relative_to(target)
                if path.is_symlink():
                    raise SkillError(f"cannot snapshot symlinked tree: {rel}")
                if path.is_dir():
                    (files_dir / rel).mkdir(parents=True, exist_ok=True)
                elif path.is_file():
                    dest = files_dir / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, dest)
                else:
                    raise SkillError(f"cannot snapshot special file: {rel}")
            (snap / "record.json").write_text(
                json.dumps(asdict(record), indent=2, sort_keys=True), encoding="utf-8"
            )
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
                meta = snap / "record.json"
                if not snap.is_dir() or not meta.is_file():
                    continue
                try:
                    data = json.loads(meta.read_text(encoding="utf-8"))
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
        front, _ = _read_skill_md(self._install_dir(record))
        status, notes, flags = _assess_compatibility(front)
        record.status = status
        record.compatibility_notes = notes
        record.allowed_tools = flags["allowed_tools"]
        record.user_invocable = flags["user_invocable"]
        record.model_invocable = flags["model_invocable"]
        return record

    def enable(self, skill_id: str, *, authority: str) -> SkillRecord:
        """Activate an installed skill. Refuses missing setup and unsupported skills."""
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
                meta = snap / "record.json"
                if not snap.is_dir() or not meta.is_file():
                    continue
                try:
                    data = json.loads(meta.read_text(encoding="utf-8"))
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
        # Re-verify the snapshot bytes before trusting them.
        snap_files = snap / "files"
        digest, _, _ = _digest_tree(snap_files)
        if digest != data.get("digest"):
            raise SkillError(f"snapshot for {skill_id} failed revalidation; refusing rollback")
        restored = _check_record_shapes(SkillRecord(**data))
        if record.enabled and restored.status in (_COMPAT_READY, _COMPAT_NEEDS_ADAPTER):
            restored.enabled = True
        else:
            restored.enabled = False
            restored.status = _COMPAT_DISABLED
        target = self._install_dir(restored)
        self._ensure_owned(target)
        parent = target.resolve().parent if target.exists() else target.parent.resolve()
        parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=parent))
        try:
            for path in sorted(snap_files.rglob("*")):
                rel = path.relative_to(snap_files)
                if path.is_symlink():
                    raise SkillError(f"snapshot holds a symlink, refusing rollback: {rel}")
                dest = staging / rel
                if path.is_dir():
                    dest.mkdir(parents=True, exist_ok=True)
                elif path.is_file():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, dest)
                else:
                    raise SkillError(f"snapshot holds a special file: {rel}")
            _assert_no_special(staging)
            live_digest, _, _ = _digest_tree(staging)
            if live_digest != restored.digest:
                raise SkillError(f"snapshot for {skill_id} failed revalidation")
            if target.exists() or target.is_symlink():
                if target.is_symlink() or not target.is_dir():
                    raise SkillError(f"installed state is not a directory: {skill_id}")
                shutil.rmtree(target)
            staging.replace(target)
            staging = None  # type: ignore[assignment]
        finally:
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)
        self._registry[skill_id] = asdict(restored)
        self._write_registry()
        return self.verify(restored)

    def verify(self, record: SkillRecord | str) -> SkillRecord:
        """Recompute the install digest; raise on missing dir or tamper."""
        if isinstance(record, str):
            record = self._record(record)
        else:
            _check_record_shapes(record)
        target = self._install_dir(record)
        if target.is_symlink() or not target.is_dir():
            raise SkillError(f"skill bytes missing: {record.id}")
        _assert_no_special(target)  # added symlinks/special files are tamper too
        digest, _, _ = _digest_tree(target)
        if digest != record.digest:
            raise SkillError(
                f"skill bytes changed since install (expected {record.digest}, "
                f"found {digest}): reinstall or roll back {record.id}"
            )
        return record

    def load(self, skill_id: str) -> SkillInstructions:
        """Load one enabled skill's instruction body (bounded, untrusted)."""
        record = self._record(skill_id)
        if not record.enabled or record.status == _COMPAT_DISABLED:
            raise SkillError(f"skill is not enabled: {skill_id}")
        self.verify(record)
        _, body = _read_skill_md(self._install_dir(record))
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

    def load_resource(self, skill_id: str, relpath: str) -> bytes:
        """Read one explicitly requested referenced file (bounded, untrusted)."""
        record = self._record(skill_id)
        if not record.enabled or record.status == _COMPAT_DISABLED:
            raise SkillError(f"skill is not enabled: {skill_id}")
        if not relpath or PurePosixPath(relpath).is_absolute():
            raise SkillError(f"resource path must be relative: {relpath!r}")
        self.verify(record)
        base = self._install_dir(record).resolve()
        candidate = (base / relpath).resolve()
        try:
            candidate.relative_to(base)
        except ValueError:
            raise SkillError(f"resource escapes skill dir: {relpath!r}") from None
        if candidate.is_symlink() or not candidate.is_file():
            raise SkillError(f"resource is not a regular file: {relpath!r}")
        data = candidate.read_bytes()
        if len(data) > RESOURCE_MAX_BYTES:
            raise SkillError(f"resource exceeds size limit: {relpath!r}")
        return data
