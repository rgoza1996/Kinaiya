"""Static source checks applied at registration time.

Defense in depth: the bubblewrap sandbox contains behavior at runtime, but
sources that even *ask* for filesystem, network, process, or interpreter
escape hatches are rejected before they are stored. Runtime containment is
still enforced and tested independently of these static checks.
"""

from __future__ import annotations

import ast

MAX_SOURCE_BYTES = 65_536

# Importable top-level modules. Deliberately excludes os, sys, subprocess,
# socket, pathlib, importlib, inspect, ctypes, asyncio, threading,
# multiprocessing, and everything outside this list.
ALLOWED_IMPORTS = frozenset(
    {
        "__future__",
        "collections",
        "dataclasses",
        "difflib",
        "enum",
        "functools",
        "heapq",
        "itertools",
        "json",
        "math",
        "operator",
        "re",
        "string",
        "typing",
        "unicodedata",
    }
)

_FORBIDDEN_NAMES = frozenset(
    {
        "__import__",
        "compile",
        "eval",
        "exec",
        "exit",
        "globals",
        "input",
        "locals",
        "open",
        "quit",
        "vars",
    }
)


def check_source(text: str) -> None:
    """Raise RegistrationError if source violates the static contract."""
    from core.extensions.errors import RegistrationError
    from core.extensions.manifest import ENTRYPOINT

    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        raise RegistrationError(f"source is not valid Python: {exc}") from exc
    found = False
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (node.name == ENTRYPOINT):
            if isinstance(node, ast.AsyncFunctionDef):
                raise RegistrationError("select_context must be a sync function")
            if len(node.args.args) != 1 or node.args.vararg or node.args.kwarg:
                raise RegistrationError("select_context(payload) must take exactly one argument")
            found = True
    if not found:
        raise RegistrationError("source must define a top-level sync select_context(payload)")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root not in ALLOWED_IMPORTS:
                    raise RegistrationError(f"unsupported import: {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root not in ALLOWED_IMPORTS:
                raise RegistrationError(f"unsupported import: {node.module}")
        elif isinstance(node, ast.Name) and node.id in _FORBIDDEN_NAMES:
            raise RegistrationError(f"forbidden builtin use: {node.id}")


def decode_source(data: bytes) -> str:
    """Size-limit, decode, and statically check raw source bytes."""
    from core.extensions.errors import RegistrationError

    if len(data) > MAX_SOURCE_BYTES:
        raise RegistrationError(f"source exceeds {MAX_SOURCE_BYTES} bytes ({len(data)})")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RegistrationError(f"source is not UTF-8: {exc}") from exc
    check_source(text)
    return text
