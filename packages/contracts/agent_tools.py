"""Native Agent advertisement, not external target readiness or permission.

The supervisor supplies these flags from trusted configuration and task pins.
This module never discovers, installs, executes or authorizes a foreign tool.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Literal, get_args

from packages.contracts.models import SCHEMA_VERSION, ToolAction

NATIVE_TOOL_ORDER: tuple[str, ...] = get_args(ToolAction.model_fields["tool"].annotation)
MAX_NATIVE_TOOLS = 16
# Unbound provider calls retain the pre-discovery protocol selection. Only a
# trusted runtime snapshot can advertise the new operation to an inference.
LEGACY_NATIVE_TOOL_ORDER = tuple(
    name for name in NATIVE_TOOL_ORDER if name not in {"tool_catalog", "consult_model"}
)

_BASE_TOOLS = ("read_file", "write_file", "search", "shell", "read_transcript", "finish")
_OPTIONAL_GROUPS = (
    ("select_context",),
    ("mcp_list", "mcp_call"),
    ("skills_list", "skill_load"),
    ("plugins_list", "plugin_call"),
    ("publish_ui",),
    ("consult_model",),
    ("tool_catalog",),
)


@dataclass(frozen=True, slots=True)
class NativeToolDescriptor:
    """Identity of a native protocol operation, including optional gateways."""

    name: str
    qualified_id: str = field(init=False)
    protocol_version: str = field(default=SCHEMA_VERSION, init=False)
    kind: Literal["native"] = field(default="native", init=False)

    def __post_init__(self) -> None:
        if type(self.name) is not str or self.name not in NATIVE_TOOL_ORDER:
            raise ValueError("Native tool name must be a known protocol action")
        object.__setattr__(self, "qualified_id", f"native:{self.name}")


@dataclass(frozen=True, slots=True)
class NativeToolSnapshot:
    """Immutable, ordered advertisement for one supervised inference request.

    An advertised operation may still need approval or fail at execution.
    Its descriptor makes no claim about foreign packages, targets or health.
    """

    available_tools: tuple[str, ...]
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    owner_disabled: tuple[str, ...] | None = None
    owner_revision: int | None = None
    task_subset: Literal["native-files-v1"] | None = None
    descriptors: tuple[NativeToolDescriptor, ...] = field(init=False)

    def __post_init__(self) -> None:
        if type(self.schema_version) is not str or self.schema_version != SCHEMA_VERSION:
            raise ValueError("Native tool snapshot requires schema version 1.0")
        names = self.available_tools
        if self.task_subset is not None and (
            type(self.task_subset) is not str or self.task_subset != "native-files-v1"
        ):
            raise ValueError("Unknown native task subset")
        base = (
            ("read_file", "write_file", "finish") if self.task_subset is not None else _BASE_TOOLS
        )
        if (
            self.task_subset is not None
            and type(names) is tuple
            and any(name not in base for name in names)
        ):
            raise ValueError("Native task subset contains an unavailable operation")
        masked = self.owner_disabled is not None
        if (not masked and self.owner_revision is not None) or (
            masked
            and (
                type(self.owner_revision) is not int
                or not 0 <= self.owner_revision <= 9_007_199_254_740_990
            )
        ):
            raise ValueError("Explicit native owner mask requires its revision")
        if masked and (
            type(self.owner_disabled) is not tuple
            or len(set(self.owner_disabled)) != len(self.owner_disabled)
            or any(
                name not in NATIVE_TOOL_ORDER or name == "finish" for name in self.owner_disabled
            )
            or self.owner_disabled
            != tuple(name for name in NATIVE_TOOL_ORDER if name in self.owner_disabled)
            or any(name in names for name in self.owner_disabled)
        ):
            raise ValueError("Invalid explicit native owner mask")
        if (
            type(names) is not tuple
            or not (1 if masked else len(base)) <= len(names) <= MAX_NATIVE_TOOLS
        ):
            raise ValueError("Native tool snapshot requires a bounded tuple of protocol actions")
        if any(type(name) is not str or name not in NATIVE_TOOL_ORDER for name in names):
            raise ValueError("Native tool snapshot contains an unknown protocol action")
        if len(set(names)) != len(names):
            raise ValueError("Native tool snapshot cannot contain duplicate actions")
        if names != tuple(name for name in NATIVE_TOOL_ORDER if name in names):
            raise ValueError("Native tool snapshot must follow protocol order")
        if not all(name in names for name in base if not masked or name not in self.owner_disabled):
            raise ValueError("Native tool snapshot must retain every base operation")
        for group in _OPTIONAL_GROUPS:
            required = tuple(
                name for name in group if not masked or name not in self.owner_disabled
            )
            if any(name in names for name in group) and not all(name in names for name in required):
                raise ValueError("Native tool snapshot must retain complete optional gateways")
        object.__setattr__(self, "descriptors", tuple(NativeToolDescriptor(name) for name in names))

    def supports(self, name: str) -> bool:
        """Unknown or malformed queries never match a native operation."""
        return type(name) is str and name in self.available_tools


def native_tool_snapshot(
    *,
    selector_available: bool = False,
    mcp_available: bool = False,
    skills_available: bool = False,
    plugins_available: bool = False,
    artifacts_available: bool = False,
    auxiliary_available: bool = False,
    catalog_available: bool = False,
) -> NativeToolSnapshot:
    """Project trusted strict booleans; grants never remove base operations."""
    flags = (
        ("selector_available", selector_available),
        ("mcp_available", mcp_available),
        ("skills_available", skills_available),
        ("plugins_available", plugins_available),
        ("artifacts_available", artifacts_available),
        ("auxiliary_available", auxiliary_available),
        ("catalog_available", catalog_available),
    )
    enabled = set(_BASE_TOOLS)
    for (flag, value), group in zip(flags, _OPTIONAL_GROUPS, strict=True):
        if type(value) is not bool:
            raise ValueError(f"{flag} requires a strict boolean")
        if value:
            enabled.update(group)
    return NativeToolSnapshot(tuple(name for name in NATIVE_TOOL_ORDER if name in enabled))


_agent_tools: ContextVar[NativeToolSnapshot | None] = ContextVar("agent_native_tools", default=None)
_agent_export_guard: ContextVar[object] = ContextVar("agent_tool_export_guard", default=None)
_agent_export_admission: ContextVar[object] = ContextVar("agent_export_admission", default=None)


@contextmanager
def agent_tool_scope(
    snapshot: NativeToolSnapshot, *, export_guard=None, export_admission=None
) -> Iterator[NativeToolSnapshot]:
    """Bind one request without modifying a provider or another task's scope."""
    if type(snapshot) is not NativeToolSnapshot:
        raise ValueError("Agent tool scope requires a native tool snapshot")
    if export_guard is not None and not callable(export_guard):
        raise ValueError("Agent tool export guard requires a native callable")
    if export_admission is not None and not callable(export_admission):
        raise ValueError("Agent export admission requires a native callable")
    token = _agent_tools.set(snapshot)
    export_token = _agent_export_guard.set(export_guard)
    admission_token = _agent_export_admission.set(export_admission)
    try:
        yield snapshot
    finally:
        _agent_export_admission.reset(admission_token)
        _agent_export_guard.reset(export_token)
        _agent_tools.reset(token)


def current_agent_tools() -> NativeToolSnapshot | None:
    """Return native request context, or None when no Agent request is bound."""
    return _agent_tools.get()


def agent_export_permitted() -> bool:
    """Recheck native-only metadata disclosure immediately before export."""
    guard = _agent_export_guard.get()
    if guard is None:
        return True
    try:
        return guard() is True
    except Exception:
        return False


def capture_agent_export_check() -> Callable[[], bool]:
    """Retain a native disclosure predicate for a deferred request callback."""
    guard = _agent_export_guard.get()

    def permitted() -> bool:
        if guard is None:
            return True
        try:
            return guard() is True
        except Exception:
            return False

    return permitted


def capture_agent_export_admission():
    """Capture the native async source admission for each final HTTP dispatch.

    The callback accepts purpose and the actual uploaded body SHA256. It is
    never supplied by a model/tool result and is not a reusable permission.
    Fixture/standalone provider calls may have no source authority attached.
    """
    return _agent_export_admission.get()


@contextmanager
def agent_export_scope(check: Callable[[], bool]) -> Iterator[None]:
    """Add an export condition without replacing the caller's native guard."""
    if not callable(check):
        raise ValueError("Agent tool export guard requires a native callable")
    previous = capture_agent_export_check()

    def permitted() -> bool:
        return previous() and check() is True

    token = _agent_export_guard.set(permitted)
    try:
        yield
    finally:
        _agent_export_guard.reset(token)
