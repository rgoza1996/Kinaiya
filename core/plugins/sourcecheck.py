"""Static source checks applied at registration time.

Defense in depth: the bubblewrap sandbox contains behavior at runtime, but
sources that even *ask* for filesystem, network, process, or interpreter
escape hatches are rejected before they are stored. Runtime containment is
still enforced and tested independently of these static checks (tests
bypass them to prove the kernel filter).

Allowed imports are the selector's safe set plus a fixed declared set for
conventional JSON computation: csv, io, statistics, decimal, datetime,
hashlib. Everything else (os, sys, subprocess, socket, pathlib,
importlib, inspect, ctypes, asyncio, threading, multiprocessing, ...) is
refused. This module never weakens ``context_selector.v1``; it only adds
a separate allowlist for the new ``python_json.v1`` runtime.
"""

from __future__ import annotations

import ast

MAX_SOURCE_BYTES = 65_536

# Importable top-level modules for python_json.v1. Selector set plus the
# fixed declared compute set. Deliberately excludes os, sys, subprocess,
# socket, pathlib, importlib, inspect, ctypes, asyncio, threading,
# multiprocessing, shutil, signal, and everything outside this list.
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
        # Fixed declared compute additions.
        "csv",
        "io",
        "statistics",
        "decimal",
        "datetime",
        "hashlib",
    }
)

_PRELOAD_MODULES = (
    "collections dataclasses difflib enum functools heapq itertools json math "
    "operator re string typing unicodedata csv io statistics decimal datetime hashlib"
).split()

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


def preload_modules() -> list[str]:
    """Ordered stdlib modules the trusted runner preloads before the filter."""
    return list(_PRELOAD_MODULES)


def check_source(text: str) -> None:
    """Raise PluginRegistrationError if source violates the static contract."""
    from core.plugins.errors import PluginRegistrationError

    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        raise PluginRegistrationError(f"source is not valid Python: {exc}") from exc
    found = False
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run":
            if isinstance(node, ast.AsyncFunctionDef):
                raise PluginRegistrationError("run must be a sync function")
            if len(node.args.args) != 1 or node.args.vararg or node.args.kwarg:
                raise PluginRegistrationError("run(arguments) must take exactly one argument")
            found = True
    if not found:
        raise PluginRegistrationError("source must define a top-level sync run(arguments)")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root not in ALLOWED_IMPORTS:
                    raise PluginRegistrationError(f"unsupported import: {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root not in ALLOWED_IMPORTS:
                raise PluginRegistrationError(f"unsupported import: {node.module}")
        elif isinstance(node, ast.Name) and node.id in _FORBIDDEN_NAMES:
            raise PluginRegistrationError(f"forbidden builtin use: {node.id}")


def decode_source(data: bytes) -> str:
    """Size-limit, decode, and statically check raw source bytes."""
    from core.plugins.errors import PluginRegistrationError

    if len(data) > MAX_SOURCE_BYTES:
        raise PluginRegistrationError(f"source exceeds {MAX_SOURCE_BYTES} bytes ({len(data)})")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PluginRegistrationError(f"source is not UTF-8: {exc}") from exc
    check_source(text)
    return text
