"""Bounded native artifact storage for Kinaiya issue165.

Native SQLite store, synchronous, RLock guarded, BEGIN IMMEDIATE + CAS.
No model text is executed. No URL fetching. Immutable deepcopies.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import sqlite3
import threading
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0"
DB_SCHEMA_VERSION = "1"

MAX_TITLE_LEN = 160
MAX_SUMMARY_LEN = 2000
MAX_COMPONENTS = 24
MAX_PUBLICATION_BYTES = 48 * 1024
MAX_PER_TASK = 100
MAX_DB_ARTIFACTS = 2500
MAX_DB_BYTES = 64 * 1024 * 1024
MAX_ACTION_KEYS = 128
MAX_SAFE_INTEGER = 9_007_199_254_740_991

_ID_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")
_HEX64_RE = re.compile(r"[0-9a-fA-F]{64}")

_VALID_TYPES = {"text", "status", "table", "chart", "diagram", "source", "number", "select", "ask"}
_VALID_TONES = {"neutral", "success", "warning", "error"}
_VALID_FRESH = {"static", "simulated", "delayed", "stale"}
_VALID_STYLE = {"bar", "line"}
_VALID_LAYOUT = {"stack", "grid"}
_VALID_SCOPE = {"local_only", "public_synthetic", "private_cloud_authorized"}


class ArtifactInvalid(ValueError):
    pass


class ArtifactConflict(ValueError):
    pass


class ArtifactNotFound(KeyError):
    pass


def _reject_unknown(d: dict, allowed: set[str], ctx: str) -> None:
    if not isinstance(d, dict):
        raise ArtifactInvalid(f"{ctx} must be an object")
    extra = set(d.keys()) - allowed
    if extra:
        raise ArtifactInvalid(f"{ctx} forbids unknown fields")


def _req_str(v: Any, lo: int, hi: int, ctx: str) -> str:
    if type(v) is not str:
        raise ArtifactInvalid(f"{ctx} must be str")
    if not (lo <= len(v) <= hi):
        raise ArtifactInvalid(f"{ctx} length {len(v)} out of [{lo},{hi}]")
    if any(
        (ord(c) < 32 or 127 <= ord(c) <= 159) and c not in "\t\n" or 0xD800 <= ord(c) <= 0xDFFF
        for c in v
    ):
        raise ArtifactInvalid(f"{ctx} contains unsupported control or surrogate characters")
    return v


def _req_number(v: Any, ctx: str, max_abs: float = 1e12) -> Any:
    if isinstance(v, bool):
        raise ArtifactInvalid(f"{ctx} bool is not a number")
    if not isinstance(v, (int, float)):
        raise ArtifactInvalid(f"{ctx} must be a number")
    if isinstance(v, float) and not math.isfinite(v):
        raise ArtifactInvalid(f"{ctx} must be finite (no NaN/Inf)")
    try:
        av = abs(v)
    except Exception as exc:
        raise ArtifactInvalid(f"{ctx} bad number") from exc
    if av > max_abs:
        raise ArtifactInvalid(f"{ctx} abs exceeds {max_abs}")
    return v


def _req_int(v: Any, ctx: str, lo: int | None = None, hi: int | None = None) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise ArtifactInvalid(f"{ctx} must be integer (no bool/float)")
    if lo is not None and v < lo:
        raise ArtifactInvalid(f"{ctx} {v} < {lo}")
    if hi is not None and v > hi:
        raise ArtifactInvalid(f"{ctx} {v} > {hi}")
    return v


def _req_bool(v: Any, ctx: str) -> bool:
    if type(v) is not bool:
        raise ArtifactInvalid(f"{ctx} must be boolean")
    return v


def _validate_url(url: Any, ctx: str = "source.url") -> str:
    _req_str(url, 1, 2000, ctx)
    for c in url:
        o = ord(c)
        if o < 32 or o == 127:
            raise ArtifactInvalid(f"{ctx} control character forbidden")
    if any(c.isspace() for c in url):
        raise ArtifactInvalid(f"{ctx} whitespace forbidden")
    try:
        p = urllib.parse.urlparse(url)
    except Exception as exc:
        raise ArtifactInvalid(f"{ctx} unparseable") from exc
    if p.scheme != "https":
        raise ArtifactInvalid(f"{ctx} scheme must be https")
    if not p.hostname:
        raise ArtifactInvalid(f"{ctx} missing host")
    try:
        _ = p.port
    except ValueError as exc:
        raise ArtifactInvalid(f"{ctx} invalid port") from exc
    if "@" in p.netloc:
        raise ArtifactInvalid(f"{ctx} userinfo forbidden")
    if p.username is not None or p.password is not None:
        raise ArtifactInvalid(f"{ctx} credentials forbidden")
    try:
        qsl = urllib.parse.parse_qsl(p.query, keep_blank_values=True)
    except Exception as exc:
        raise ArtifactInvalid(f"{ctx} bad query") from exc
    for k, _vv in qsl:
        kl = k.lower()
        if (
            "password" in kl
            or "passwd" in kl
            or "token" in kl
            or "secret" in kl
            or "credential" in kl
            or kl
            in (
                "api_key",
                "apikey",
                "api-key",
                "auth",
                "authorization",
                "access_key",
                "sessionid",
                "session_key",
                "access_token",
                "refresh_token",
                "client_secret",
            )
        ):
            raise ArtifactInvalid(f"{ctx} query secret forbidden: {k!r}")
    return url


def _validate_scalar(cell: Any, ctx: str) -> Any:
    if cell is None:
        return None
    if type(cell) is bool:
        return cell
    if isinstance(cell, int):
        if abs(cell) > 1e12:
            raise ArtifactInvalid(f"{ctx} number abs exceeds 1e12")
        return cell
    if isinstance(cell, float):
        if not math.isfinite(cell):
            raise ArtifactInvalid(f"{ctx} non-finite number")
        if abs(cell) > 1e12:
            raise ArtifactInvalid(f"{ctx} number abs exceeds 1e12")
        return cell
    if isinstance(cell, str):
        _req_str(cell, 0, 1000, ctx)
        return cell
    raise ArtifactInvalid(f"{ctx} invalid scalar type {type(cell).__name__}")


def _validate_text_data(data: dict, ctx: str) -> dict:
    _reject_unknown(data, {"text"}, ctx)
    if "text" not in data:
        raise ArtifactInvalid(f"{ctx} missing text")
    _req_str(data["text"], 1, 8000, ctx + ".text")
    return {"text": data["text"]}


def _validate_status_data(data: dict, ctx: str) -> dict:
    _reject_unknown(data, {"text", "tone"}, ctx)
    if "text" not in data:
        raise ArtifactInvalid(f"{ctx} missing text")
    _req_str(data["text"], 1, 1000, ctx + ".text")
    out: dict[str, Any] = {"text": data["text"]}
    if "tone" in data:
        t = data["tone"]
        if type(t) is not str or t not in _VALID_TONES:
            raise ArtifactInvalid(f"{ctx}.tone must be one of {sorted(_VALID_TONES)}")
        out["tone"] = t
    return out


def _validate_table_data(data: dict, ctx: str) -> dict:
    _reject_unknown(data, {"columns", "rows", "selectable"}, ctx)
    if "columns" not in data or "rows" not in data:
        raise ArtifactInvalid(f"{ctx} requires columns and rows")
    cols = data["columns"]
    rows = data["rows"]
    if not isinstance(cols, list) or not (1 <= len(cols) <= 12):
        raise ArtifactInvalid(f"{ctx}.columns must be 1..12")
    for c in cols:
        _req_str(c, 1, 80, ctx + ".columns[]")
    if not isinstance(rows, list) or len(rows) > 100:
        raise ArtifactInvalid(f"{ctx}.rows must be 0..100")
    for r in rows:
        if not isinstance(r, list) or len(r) != len(cols):
            raise ArtifactInvalid(f"{ctx}.rows[] must match column count")
        for cell in r:
            _validate_scalar(cell, ctx + ".rows[][]")
    if "selectable" in data:
        _req_bool(data["selectable"], ctx + ".selectable")
        sel = data["selectable"]
    else:
        sel = True
    return {"columns": copy.deepcopy(cols), "rows": copy.deepcopy(rows), "selectable": sel}


def _validate_chart_data(data: dict, ctx: str) -> dict:
    _reject_unknown(data, {"labels", "values", "unit", "style"}, ctx)
    if "labels" not in data or "values" not in data:
        raise ArtifactInvalid(f"{ctx} requires labels and values")
    labels = data["labels"]
    values = data["values"]
    if not isinstance(labels, list) or not (1 <= len(labels) <= 100):
        raise ArtifactInvalid(f"{ctx}.labels must be 1..100")
    for lb in labels:
        _req_str(lb, 0, 80, ctx + ".labels[]")
    if not isinstance(values, list) or len(values) != len(labels):
        raise ArtifactInvalid(f"{ctx}.values must match labels length")
    for vv in values:
        _req_number(vv, ctx + ".values[]")
    out: dict[str, Any] = {"labels": copy.deepcopy(labels), "values": copy.deepcopy(values)}
    if "unit" in data:
        u = data["unit"]
        _req_str(u, 0, 40, ctx + ".unit")
        out["unit"] = u
    if "style" in data:
        s = data["style"]
        if type(s) is not str or s not in _VALID_STYLE:
            raise ArtifactInvalid(f"{ctx}.style must be bar/line")
        out["style"] = s
    else:
        out["style"] = "bar"
    return out


def _validate_diagram_data(data: dict, ctx: str) -> dict:
    _reject_unknown(data, {"nodes", "edges"}, ctx)
    if "nodes" not in data or "edges" not in data:
        raise ArtifactInvalid(f"{ctx} requires nodes and edges")
    nodes = data["nodes"]
    edges = data["edges"]
    if not isinstance(nodes, list) or not (1 <= len(nodes) <= 30):
        raise ArtifactInvalid(f"{ctx}.nodes must be 1..30")
    if not isinstance(edges, list) or len(edges) > 60:
        raise ArtifactInvalid(f"{ctx}.edges must be 0..60")
    seen: set[str] = set()
    norm_nodes = []
    for i, n in enumerate(nodes):
        nctx = f"{ctx}.nodes[{i}]"
        if not isinstance(n, dict):
            raise ArtifactInvalid(f"{nctx} must be object")
        _reject_unknown(n, {"id", "label", "detail"}, nctx)
        if "id" not in n or "label" not in n:
            raise ArtifactInvalid(f"{nctx} requires id and label")
        nid = n["id"]
        _req_str(nid, 1, 128, nctx + ".id")
        if nid in seen:
            raise ArtifactInvalid(f"{ctx} duplicate node id {nid!r}")
        seen.add(nid)
        lb = n["label"]
        _req_str(lb, 0, 160, nctx + ".label")
        nn: dict[str, Any] = {"id": nid, "label": lb}
        if "detail" in n:
            d = n["detail"]
            _req_str(d, 0, 1000, nctx + ".detail")
            nn["detail"] = d
        norm_nodes.append(nn)
    norm_edges = []
    for i, e in enumerate(edges):
        ectx = f"{ctx}.edges[{i}]"
        if not isinstance(e, dict):
            raise ArtifactInvalid(f"{ectx} must be object")
        _reject_unknown(e, {"from", "to", "label"}, ectx)
        if "from" not in e or "to" not in e:
            raise ArtifactInvalid(f"{ectx} requires from/to")
        f = e["from"]
        t = e["to"]
        if type(f) is not str or type(t) is not str:
            raise ArtifactInvalid(f"{ectx}.from/to must be str")
        if f not in seen or t not in seen:
            raise ArtifactInvalid(f"{ectx} unknown node reference")
        ne: dict[str, Any] = {"from": f, "to": t}
        if "label" in e:
            lb = e["label"]
            _req_str(lb, 0, 80, ectx + ".label")
            ne["label"] = lb
        norm_edges.append(ne)
    return {"nodes": norm_nodes, "edges": norm_edges}


def _validate_source_data(data: dict, ctx: str) -> dict:
    _reject_unknown(data, {"label", "url", "observed_at", "freshness"}, ctx)
    if "label" not in data or "url" not in data or "freshness" not in data:
        raise ArtifactInvalid(f"{ctx} requires label, url, freshness")
    _req_str(data["label"], 1, 160, ctx + ".label")
    _validate_url(data["url"], ctx + ".url")
    out: dict[str, Any] = {"label": data["label"], "url": data["url"]}
    if "observed_at" in data:
        oa = data["observed_at"]
        _req_number(oa, ctx + ".observed_at", max_abs=MAX_SAFE_INTEGER)
        if oa < 0:
            raise ArtifactInvalid(f"{ctx}.observed_at must be >=0")
        out["observed_at"] = oa
    fr = data["freshness"]
    if type(fr) is not str or fr not in _VALID_FRESH:
        raise ArtifactInvalid(f"{ctx}.freshness must be one of {sorted(_VALID_FRESH)}")
    out["freshness"] = fr
    return out


def _validate_number_data(data: dict, ctx: str) -> dict:
    _reject_unknown(data, {"label", "value", "min", "max", "step"}, ctx)
    for k in ("label", "value", "min", "max"):
        if k not in data:
            raise ArtifactInvalid(f"{ctx} requires label,value,min,max")
    _req_str(data["label"], 1, 160, ctx + ".label")
    vv = _req_number(data["value"], ctx + ".value")
    mn = _req_number(data["min"], ctx + ".min")
    mx = _req_number(data["max"], ctx + ".max")
    if not (mn <= vv <= mx):
        raise ArtifactInvalid(f"{ctx} require min <= value <= max")
    out: dict[str, Any] = {"label": data["label"], "value": vv, "min": mn, "max": mx}
    if "step" in data:
        st = data["step"]
        if isinstance(st, bool) or not isinstance(st, (int, float)):
            raise ArtifactInvalid(f"{ctx}.step must be number")
        if isinstance(st, float) and not math.isfinite(st):
            raise ArtifactInvalid(f"{ctx}.step non-finite")
        if not (st > 0):
            raise ArtifactInvalid(f"{ctx}.step must be positive")
        if abs(st) > 1e12:
            raise ArtifactInvalid(f"{ctx}.step abs exceeds 1e12")
        out["step"] = st
    return out


def _validate_select_data(data: dict, ctx: str) -> dict:
    _reject_unknown(data, {"label", "options", "value"}, ctx)
    if "label" not in data or "options" not in data or "value" not in data:
        raise ArtifactInvalid(f"{ctx} requires label,options,value")
    _req_str(data["label"], 1, 160, ctx + ".label")
    opts = data["options"]
    if not isinstance(opts, list) or not (1 <= len(opts) <= 30):
        raise ArtifactInvalid(f"{ctx}.options must be 1..30")
    for o in opts:
        _req_str(o, 1, 160, ctx + ".options[]")
    if len(set(opts)) != len(opts):
        raise ArtifactInvalid(f"{ctx}.options must be unique")
    v = data["value"]
    if type(v) is not str:
        raise ArtifactInvalid(f"{ctx}.value must be str")
    if v not in opts:
        raise ArtifactInvalid(f"{ctx}.value unknown option")
    return {"label": data["label"], "options": copy.deepcopy(opts), "value": v}


def _validate_ask_data(data: dict, ctx: str) -> dict:
    _reject_unknown(data, {"label", "message"}, ctx)
    if "label" not in data or "message" not in data:
        raise ArtifactInvalid(f"{ctx} requires label,message")
    _req_str(data["label"], 1, 160, ctx + ".label")
    _req_str(data["message"], 1, 2000, ctx + ".message")
    return {"label": data["label"], "message": data["message"]}


def _validate_data_for_type(ctype: str, data: Any, ctx: str) -> dict:
    if not isinstance(data, dict):
        raise ArtifactInvalid(f"{ctx} data must be object")
    if ctype == "text":
        return _validate_text_data(data, ctx)
    if ctype == "status":
        return _validate_status_data(data, ctx)
    if ctype == "table":
        return _validate_table_data(data, ctx)
    if ctype == "chart":
        return _validate_chart_data(data, ctx)
    if ctype == "diagram":
        return _validate_diagram_data(data, ctx)
    if ctype == "source":
        return _validate_source_data(data, ctx)
    if ctype == "number":
        return _validate_number_data(data, ctx)
    if ctype == "select":
        return _validate_select_data(data, ctx)
    if ctype == "ask":
        return _validate_ask_data(data, ctx)
    raise ArtifactInvalid(f"{ctx} unknown type {ctype!r}")


def _validate_single_component(comp: Any, ctx: str) -> dict:
    if not isinstance(comp, dict):
        raise ArtifactInvalid(f"{ctx} must be object")
    _reject_unknown(comp, {"id", "type", "title", "data"}, ctx)
    if "id" not in comp or "type" not in comp or "data" not in comp:
        raise ArtifactInvalid(f"{ctx} requires id,type,data")
    cid = comp["id"]
    if type(cid) is not str or not (1 <= len(cid) <= 64) or not _ID_RE.fullmatch(cid):
        raise ArtifactInvalid(f"{ctx}.id must match [A-Za-z][A-Za-z0-9_-]* 1..64")
    ctype = comp["type"]
    if type(ctype) is not str or ctype not in _VALID_TYPES:
        raise ArtifactInvalid(f"{ctx}.type unknown {ctype!r}")
    if "title" in comp:
        t = comp["title"]
        _req_str(t, 0, 160, ctx + ".title")
        title = t
    else:
        title = ""
    data = _validate_data_for_type(ctype, comp["data"], ctx + ".data")
    return {"id": cid, "type": ctype, "title": title, "data": data}


def validate_components(components: Any) -> list:
    """Validate and normalize 1..24 components; return deep copied list."""
    if not isinstance(components, list) or not (1 <= len(components) <= 24):
        raise ArtifactInvalid("components must be list 1..24")
    seen: set[str] = set()
    out: list[dict] = []
    for i, c in enumerate(components):
        norm = _validate_single_component(c, f"components[{i}]")
        if norm["id"] in seen:
            raise ArtifactInvalid(f"duplicate component id {norm['id']!r}")
        seen.add(norm["id"])
        out.append(norm)
    return copy.deepcopy(out)


def _canonical_json(obj: Any) -> str:
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _bounded_publication_json(arguments: dict, *, reserve_api_order: bool = False) -> None:
    """Bound parsing/serialization work before entering a write transaction."""
    # The owner API adds one key/value pair outside stored content. Reserve
    # both visits so every accepted envelope fits the current renderer walk.
    visited = 2 if reserve_api_order else 0
    ancestors: set[int] = set()

    def visit(value: Any, depth: int) -> None:
        nonlocal visited
        visited += 1
        if visited > 8192 or depth > 12:
            raise ArtifactInvalid("publication exceeds structural bounds")
        if value is None or type(value) is bool:
            return
        if type(value) in {int, float}:
            _req_number(value, "JSON number", max_abs=MAX_SAFE_INTEGER)
            return
        if type(value) is str:
            _req_str(value, 0, MAX_PUBLICATION_BYTES, "JSON text")
            return
        if type(value) not in {dict, list}:
            raise ArtifactInvalid("publication must contain only JSON values")
        maximum = 128 if type(value) is dict else 1200
        if len(value) > maximum or id(value) in ancestors:
            raise ArtifactInvalid("publication exceeds collection bounds or contains a cycle")
        ancestors.add(id(value))
        if type(value) is dict:
            for key, child in value.items():
                _req_str(key, 1, 128, "JSON key")
                visit(key, depth + 1)
                visit(child, depth + 1)
        else:
            for child in value:
                visit(child, depth + 1)
        ancestors.remove(id(value))

    visit(arguments, 0)
    if len(_canonical_json(arguments).encode("utf-8")) > MAX_PUBLICATION_BYTES:
        raise ArtifactInvalid("publication exceeds 48 KiB")


def _compute_content_sha256(
    artifact_id: str,
    session_id: str,
    thread_id: str,
    epoch: int,
    task_id: str,
    objective_revision: int,
    revision: int,
    title: str,
    summary: str,
    components: Any,
    layout: str,
    content_scope: str,
) -> str:
    payload = {
        "artifact_id": artifact_id,
        "content_scope": content_scope,
        "components": components,
        "epoch": epoch,
        "layout": layout,
        "objective_revision": objective_revision,
        "revision": revision,
        "schema_version": SCHEMA_VERSION,
        "session_id": session_id,
        "summary": summary,
        "task_id": task_id,
        "thread_id": thread_id,
        "title": title,
    }
    canon = _canonical_json(payload)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _task_attrs(task: Any) -> tuple[str, str, int, str, int, str]:
    try:
        sid = task.session_id
        tid = task.thread_id
        ep = task.epoch
        task_id = task.task_id
        rev = task.objective_revision
        scope = task.content_scope
    except Exception as exc:
        raise ArtifactInvalid("malformed task") from exc
    _check_scope(sid, tid, ep)
    _req_str(task_id, 1, 128, "task.task_id")
    _req_int(rev, "task.objective_revision", 1, MAX_SAFE_INTEGER)
    if type(scope) is not str or scope not in _VALID_SCOPE:
        raise ArtifactInvalid("task.content_scope invalid")
    return sid, tid, ep, task_id, rev, scope


def _check_scope(session_id: Any, thread_id: Any, epoch: Any) -> None:
    _req_str(session_id, 1, 128, "session_id")
    _req_str(thread_id, 1, 128, "thread_id")
    _req_int(epoch, "epoch", 1, MAX_SAFE_INTEGER)


def _is_view_value_valid(comp: dict, value: Any) -> bool:
    try:
        ctype = comp.get("type")
        data = comp.get("data", {})
        if ctype == "table":
            if not data.get("selectable", True):
                return False
            if value is None:
                return True
            if isinstance(value, bool) or not isinstance(value, int):
                return False
            rows = data.get("rows", [])
            return 0 <= value < len(rows)
        if ctype == "diagram":
            if type(value) is not str:
                return False
            ids = {n.get("id") for n in data.get("nodes", [])}
            return value in ids
        if ctype == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return False
            if isinstance(value, float) and not math.isfinite(value):
                return False
            if abs(value) > 1e12:
                return False
            return data.get("min", float("-inf")) <= value <= data.get("max", float("inf"))
        if ctype == "select":
            if type(value) is not str:
                return False
            return value in data.get("options", [])
        return False
    except Exception:
        return False


class ArtifactStore:
    def __init__(self, path: Path) -> None:
        self._lock = threading.RLock()
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self._path), check_same_thread=False, isolation_level=None, timeout=30.0
        )
        self._conn.row_factory = sqlite3.Row
        self._closed = False
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL;").fetchall()
            self._conn.execute("PRAGMA synchronous=FULL;")
            self._conn.execute("PRAGMA foreign_keys=ON;")
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS artifacts(
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                artifact_id TEXT UNIQUE NOT NULL,
                session_id TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                epoch INTEGER NOT NULL,
                task_id TEXT NOT NULL,
                objective_revision INTEGER NOT NULL,
                revision INTEGER NOT NULL,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                components_json TEXT NOT NULL,
                layout TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                content_sha256 TEXT NOT NULL,
                content_scope TEXT NOT NULL,
                view_state_json TEXT NOT NULL,
                view_revision INTEGER NOT NULL
                )"""
            )
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS actions(
                artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id) ON DELETE CASCADE,
                idempotency_key TEXT NOT NULL,
                session_id TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                epoch INTEGER NOT NULL,
                expected_revision INTEGER NOT NULL,
                component_id TEXT NOT NULL,
                value_json TEXT NOT NULL,
                response_json TEXT NOT NULL,
                PRIMARY KEY (artifact_id, idempotency_key)
                )"""
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_artifacts_scope ON artifacts(session_id, thread_id, epoch, seq)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_artifacts_task ON artifacts(task_id)"
            )
            cur = self._conn.execute("SELECT value FROM metadata WHERE key='schema_version'")
            row = cur.fetchone()
            if row is None:
                self._conn.execute("INSERT INTO metadata(key,value) VALUES('schema_version','1')")
            elif row[0] != DB_SCHEMA_VERSION:
                raise ArtifactInvalid(f"unsupported database schema_version {row[0]!r}")

    def _ensure_open(self) -> None:
        if self._closed:
            raise ArtifactInvalid("store closed")

    def _usage_locked(self) -> tuple[int, int]:
        cur = self._conn.execute("SELECT COUNT(*) FROM artifacts")
        count = cur.fetchone()[0]
        cur = self._conn.execute(
            "SELECT COALESCE(SUM(LENGTH(CAST(components_json AS BLOB))"
            "+LENGTH(CAST(view_state_json AS BLOB))"
            "+LENGTH(CAST(title AS BLOB))+LENGTH(CAST(summary AS BLOB))),0) FROM artifacts"
        )
        a = cur.fetchone()[0] or 0
        cur = self._conn.execute(
            "SELECT COALESCE(SUM(LENGTH(CAST(response_json AS BLOB))"
            "+LENGTH(CAST(value_json AS BLOB))),0) FROM actions"
        )
        b = cur.fetchone()[0] or 0
        return int(count), int(a + b)

    def _row_to_envelope(self, row: Any) -> dict:
        comps = json.loads(row["components_json"])
        vs = json.loads(row["view_state_json"])
        return {
            "schema_version": SCHEMA_VERSION,
            "artifact_id": row["artifact_id"],
            "session_id": row["session_id"],
            "thread_id": row["thread_id"],
            "epoch": row["epoch"],
            "task_id": row["task_id"],
            "objective_revision": row["objective_revision"],
            "revision": row["revision"],
            "title": row["title"],
            "summary": row["summary"],
            "components": comps,
            "layout": row["layout"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "content_sha256": row["content_sha256"],
            "content_scope": row["content_scope"],
            "view_state": vs,
            "view_revision": row["view_revision"],
        }

    def publish(self, task: Any, arguments: dict) -> dict:
        if not isinstance(arguments, dict):
            raise ArtifactInvalid("arguments must be object")
        _bounded_publication_json(arguments)
        sid, tid, ep, task_id, rev, scope = _task_attrs(task)
        has_create = any(k in arguments for k in ("title", "summary", "components", "layout"))
        has_update = any(k in arguments for k in ("artifact_id", "expected_revision", "updates"))
        if has_create and has_update:
            raise ArtifactInvalid("mixed creation/update arguments")
        if has_create:
            return self._publish_create(sid, tid, ep, task_id, rev, scope, arguments)
        if has_update:
            return self._publish_update(sid, tid, ep, task_id, rev, scope, arguments)
        raise ArtifactInvalid("arguments must be creation or update shape")

    def _publish_create(
        self, sid: str, tid: str, ep: int, task_id: str, rev: int, scope: str, arguments: dict
    ) -> dict:
        _reject_unknown(arguments, {"title", "summary", "components", "layout"}, "publish")
        if "title" not in arguments or "summary" not in arguments or "components" not in arguments:
            raise ArtifactInvalid("creation requires title,summary,components")
        _req_str(arguments["title"], 1, MAX_TITLE_LEN, "title")
        _req_str(arguments["summary"], 1, MAX_SUMMARY_LEN, "summary")
        layout = arguments.get("layout", "stack")
        if type(layout) is not str or layout not in _VALID_LAYOUT:
            raise ArtifactInvalid("layout must be stack/grid")
        norm = validate_components(arguments["components"])
        with self._lock:
            self._ensure_open()
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self._conn.execute(
                    "SELECT COUNT(*) FROM artifacts WHERE task_id=?", (task_id,)
                )
                if cur.fetchone()[0] >= MAX_PER_TASK:
                    raise ArtifactConflict("quota: max 100 artifacts per task")
                cur = self._conn.execute("SELECT COUNT(*) FROM artifacts")
                if cur.fetchone()[0] >= MAX_DB_ARTIFACTS:
                    raise ArtifactConflict("quota: max 2500 artifacts")
                aid = str(uuid.uuid4())
                now = time.time()
                ch = _compute_content_sha256(
                    aid,
                    sid,
                    tid,
                    ep,
                    task_id,
                    rev,
                    1,
                    arguments["title"],
                    arguments["summary"],
                    norm,
                    layout,
                    scope,
                )
                comps_json = _canonical_json(norm)
                vs_json = _canonical_json({})
                env = {
                    "schema_version": SCHEMA_VERSION,
                    "artifact_id": aid,
                    "session_id": sid,
                    "thread_id": tid,
                    "epoch": ep,
                    "task_id": task_id,
                    "objective_revision": rev,
                    "revision": 1,
                    "title": arguments["title"],
                    "summary": arguments["summary"],
                    "components": copy.deepcopy(norm),
                    "layout": layout,
                    "created_at": now,
                    "updated_at": now,
                    "content_sha256": ch,
                    "content_scope": scope,
                    "view_state": {},
                    "view_revision": 0,
                }
                _bounded_publication_json(env, reserve_api_order=True)
                _, total = self._usage_locked()
                new_bytes = sum(
                    len(value.encode("utf-8"))
                    for value in (
                        comps_json,
                        vs_json,
                        arguments["title"],
                        arguments["summary"],
                    )
                )
                if total + new_bytes > MAX_DB_BYTES:
                    raise ArtifactConflict("quota: database 64MiB exceeded")
                self._conn.execute(
                    "INSERT INTO artifacts(artifact_id,session_id,thread_id,epoch,task_id,objective_revision,revision,title,summary,components_json,layout,created_at,updated_at,content_sha256,content_scope,view_state_json,view_revision) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        aid,
                        sid,
                        tid,
                        ep,
                        task_id,
                        rev,
                        1,
                        arguments["title"],
                        arguments["summary"],
                        comps_json,
                        layout,
                        now,
                        now,
                        ch,
                        scope,
                        vs_json,
                        0,
                    ),
                )
                self._conn.execute("COMMIT")
                return copy.deepcopy(env)
            except BaseException:
                try:
                    self._conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise

    def _publish_update(
        self, sid: str, tid: str, ep: int, task_id: str, rev: int, scope: str, arguments: dict
    ) -> dict:
        _reject_unknown(arguments, {"artifact_id", "expected_revision", "updates"}, "update")
        if (
            "artifact_id" not in arguments
            or "expected_revision" not in arguments
            or "updates" not in arguments
        ):
            raise ArtifactInvalid("update requires artifact_id,expected_revision,updates")
        aid = arguments["artifact_id"]
        exp = arguments["expected_revision"]
        updates = arguments["updates"]
        _req_str(aid, 1, 128, "artifact_id")
        _req_int(exp, "expected_revision", 1, MAX_SAFE_INTEGER)
        if not isinstance(updates, list) or not (1 <= len(updates) <= 24):
            raise ArtifactInvalid("updates must be list 1..24")
        seen_upd: set[str] = set()
        for i, u in enumerate(updates):
            if not isinstance(u, dict):
                raise ArtifactInvalid(f"updates[{i}] must be object")
            _reject_unknown(u, {"id", "data"}, f"updates[{i}]")
            if "id" not in u or "data" not in u:
                raise ArtifactInvalid(f"updates[{i}] requires id and data")
            uid = u["id"]
            if type(uid) is not str or not (1 <= len(uid) <= 64) or not _ID_RE.fullmatch(uid):
                raise ArtifactInvalid(f"updates[{i}].id invalid")
            if uid in seen_upd:
                raise ArtifactInvalid(f"duplicate update id {uid!r}")
            seen_upd.add(uid)
            if not isinstance(u["data"], dict):
                raise ArtifactInvalid(f"updates[{i}].data must be object")
        with self._lock:
            self._ensure_open()
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self._conn.execute("SELECT * FROM artifacts WHERE artifact_id=?", (aid,))
                row = cur.fetchone()
                if row is None:
                    raise ArtifactNotFound(aid)
                if (
                    row["task_id"] != task_id
                    or row["session_id"] != sid
                    or row["thread_id"] != tid
                    or row["epoch"] != ep
                ):
                    raise ArtifactConflict("only original task/session/thread/epoch may update")
                if row["objective_revision"] != rev:
                    raise ArtifactConflict(
                        "stale objective_revision: artifact from earlier revision cannot mutate"
                    )
                if row["content_scope"] != scope:
                    # A task can narrow its disclosure scope without changing
                    # the objective. Reusing a publicly labeled artifact for
                    # newly private content would misstate provenance.
                    raise ArtifactConflict("content scope changed; publish a new artifact")
                if row["revision"] != exp:
                    raise ArtifactConflict(f"stale expected_revision {exp} != {row['revision']}")
                old_comps: list = json.loads(row["components_json"])
                cmap = {c["id"]: c for c in old_comps}
                for uid in seen_upd:
                    if uid not in cmap:
                        raise ArtifactInvalid(f"unknown component id {uid!r}")
                new_comps = copy.deepcopy(old_comps)
                by_id = {c["id"]: c for c in new_comps}
                for u in updates:
                    uid = u["id"]
                    orig = by_id[uid]
                    norm_data = _validate_data_for_type(
                        orig["type"], copy.deepcopy(u["data"]), f"updates[{uid}].data"
                    )
                    orig["data"] = norm_data
                # full re-validate (type/title fixed, data replaced)
                revalidated: list = []
                seen2: set[str] = set()
                for i, c in enumerate(new_comps):
                    n = _validate_single_component(c, f"components[{i}]")
                    if n["id"] in seen2:
                        raise ArtifactInvalid("duplicate component id after patch")
                    seen2.add(n["id"])
                    # type/title must be unchanged
                    if n["type"] != cmap[n["id"]]["type"] or n["title"] != cmap[n["id"]]["title"]:
                        raise ArtifactInvalid("type/title are immutable")
                    revalidated.append(n)
                old_view: dict = json.loads(row["view_state_json"])
                pruned: dict = {}
                by_new = {c["id"]: c for c in revalidated}
                for k, v in old_view.items():
                    comp = by_new.get(k)
                    if comp is None:
                        continue
                    if _is_view_value_valid(comp, copy.deepcopy(v)):
                        pruned[k] = copy.deepcopy(v)
                new_rev = row["revision"] + 1
                now = time.time()
                ch = _compute_content_sha256(
                    aid,
                    sid,
                    tid,
                    ep,
                    task_id,
                    rev,
                    new_rev,
                    row["title"],
                    row["summary"],
                    revalidated,
                    row["layout"],
                    row["content_scope"],
                )
                comps_json = _canonical_json(revalidated)
                vs_json = _canonical_json(pruned)
                env = {
                    "schema_version": SCHEMA_VERSION,
                    "artifact_id": aid,
                    "session_id": sid,
                    "thread_id": tid,
                    "epoch": ep,
                    "task_id": task_id,
                    "objective_revision": rev,
                    "revision": new_rev,
                    "title": row["title"],
                    "summary": row["summary"],
                    "components": copy.deepcopy(revalidated),
                    "layout": row["layout"],
                    "created_at": row["created_at"],
                    "updated_at": now,
                    "content_sha256": ch,
                    "content_scope": row["content_scope"],
                    "view_state": copy.deepcopy(pruned),
                    "view_revision": row["view_revision"],
                }
                _bounded_publication_json(env, reserve_api_order=True)
                _, total = self._usage_locked()
                old_bytes = len(row["components_json"].encode("utf-8")) + len(
                    row["view_state_json"].encode("utf-8")
                )
                new_bytes = len(comps_json.encode("utf-8")) + len(vs_json.encode("utf-8"))
                if total - old_bytes + new_bytes > MAX_DB_BYTES:
                    raise ArtifactConflict("quota: database 64MiB exceeded")
                cur = self._conn.execute(
                    "UPDATE artifacts SET revision=?,components_json=?,updated_at=?,content_sha256=?,view_state_json=? WHERE artifact_id=? AND revision=?",
                    (new_rev, comps_json, now, ch, vs_json, aid, exp),
                )
                if cur.rowcount != 1:
                    raise ArtifactConflict("concurrent revision change")
                self._conn.execute("COMMIT")
                return copy.deepcopy(env)
            except BaseException:
                try:
                    self._conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise

    def get(self, artifact_id: str, *, session_id: str, thread_id: str, epoch: int) -> dict:
        _req_str(artifact_id, 1, 128, "artifact_id")
        _check_scope(session_id, thread_id, epoch)
        with self._lock:
            self._ensure_open()
            cur = self._conn.execute("SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,))
            row = cur.fetchone()
            if row is None:
                raise ArtifactNotFound(artifact_id)
            if (
                row["session_id"] != session_id
                or row["thread_id"] != thread_id
                or row["epoch"] != epoch
            ):
                raise ArtifactNotFound(artifact_id)
            return copy.deepcopy(self._row_to_envelope(row))

    def page(
        self, *, session_id: str, thread_id: str, epoch: int, after: int = 0, limit: int = 20
    ) -> dict:
        _check_scope(session_id, thread_id, epoch)
        _req_int(after, "after", 0, MAX_SAFE_INTEGER)
        if isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= 20):
            raise ArtifactInvalid("limit must be int 1..20")
        with self._lock:
            self._ensure_open()
            cur = self._conn.execute(
                "SELECT * FROM artifacts WHERE session_id=? AND thread_id=? AND epoch=? AND seq>? ORDER BY seq ASC LIMIT ?",
                (session_id, thread_id, epoch, after, limit),
            )
            rows = cur.fetchall()
            items = [copy.deepcopy(self._row_to_envelope(r)) for r in rows[:limit]]
            nxt = rows[-1]["seq"] if rows else after
            return {"items": items, "next_after": nxt}

    def action(self, artifact_id: str, body: dict) -> dict:
        _req_str(artifact_id, 1, 128, "artifact_id")
        if not isinstance(body, dict):
            raise ArtifactInvalid("action body must be object")
        _reject_unknown(
            body,
            {
                "session_id",
                "thread_id",
                "epoch",
                "expected_revision",
                "component_id",
                "value",
                "idempotency_key",
            },
            "action",
        )
        for k in (
            "session_id",
            "thread_id",
            "epoch",
            "expected_revision",
            "component_id",
            "idempotency_key",
        ):
            if k not in body:
                raise ArtifactInvalid(f"action missing {k}")
        if "value" not in body:
            raise ArtifactInvalid("action missing value")
        sid = body["session_id"]
        tid = body["thread_id"]
        ep = body["epoch"]
        exp = body["expected_revision"]
        cid = body["component_id"]
        val = body["value"]
        key = body["idempotency_key"]
        _check_scope(sid, tid, ep)
        _req_int(exp, "expected_revision", 1, MAX_SAFE_INTEGER)
        _req_str(cid, 1, 64, "component_id")
        _req_str(key, 1, 128, "idempotency_key")
        _validate_scalar(val, "action.value")
        try:
            val_json = _canonical_json(val)
        except (TypeError, ValueError) as exc:
            raise ArtifactInvalid("value is not finite JSON") from exc
        with self._lock:
            self._ensure_open()
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self._conn.execute(
                    "SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,)
                )
                row = cur.fetchone()
                if row is None:
                    raise ArtifactNotFound(artifact_id)
                if row["session_id"] != sid or row["thread_id"] != tid or row["epoch"] != ep:
                    raise ArtifactNotFound(artifact_id)
                cur = self._conn.execute(
                    "SELECT session_id,thread_id,epoch,expected_revision,component_id,value_json,response_json FROM actions WHERE artifact_id=? AND idempotency_key=?",
                    (artifact_id, key),
                )
                ex = cur.fetchone()
                if ex is not None:
                    if (
                        ex["session_id"] == sid
                        and ex["thread_id"] == tid
                        and ex["epoch"] == ep
                        and ex["expected_revision"] == exp
                        and ex["component_id"] == cid
                        and ex["value_json"] == val_json
                    ):
                        resp = json.loads(ex["response_json"])
                        self._conn.execute("COMMIT")
                        return copy.deepcopy(resp)
                    raise ArtifactConflict("conflicting idempotency_key reuse")
                if row["revision"] != exp:
                    raise ArtifactConflict(f"stale expected_revision {exp} != {row['revision']}")
                cur = self._conn.execute(
                    "SELECT COUNT(*) FROM actions WHERE artifact_id=?", (artifact_id,)
                )
                if cur.fetchone()[0] >= MAX_ACTION_KEYS:
                    raise ArtifactConflict("quota: max 128 action keys per artifact")
                comps: list = json.loads(row["components_json"])
                comp = None
                for c in comps:
                    if c.get("id") == cid:
                        comp = c
                        break
                if comp is None:
                    raise ArtifactInvalid(f"unknown component {cid!r}")
                ctype = comp.get("type")
                data = comp.get("data", {})
                if ctype == "table":
                    if not data.get("selectable", True):
                        raise ArtifactInvalid("table is not selectable")
                    if val is None:
                        pass
                    elif isinstance(val, bool) or not isinstance(val, int):
                        raise ArtifactInvalid("table action must be row index int or null")
                    elif not (0 <= val < len(data.get("rows", []))):
                        raise ArtifactInvalid("table row index out of range")
                elif ctype == "diagram":
                    if type(val) is not str:
                        raise ArtifactInvalid("diagram action must be node id str")
                    if val not in {n.get("id") for n in data.get("nodes", [])}:
                        raise ArtifactInvalid("diagram unknown node")
                elif ctype == "number":
                    if isinstance(val, bool) or not isinstance(val, (int, float)):
                        raise ArtifactInvalid("number action must be number")
                    if isinstance(val, float) and not math.isfinite(val):
                        raise ArtifactInvalid("number action non-finite")
                    if abs(val) > 1e12:
                        raise ArtifactInvalid("number action abs exceeds 1e12")
                    if not (data.get("min", float("-inf")) <= val <= data.get("max", float("inf"))):
                        raise ArtifactInvalid("number action out of range")
                elif ctype == "select":
                    if type(val) is not str:
                        raise ArtifactInvalid("select action must be str")
                    if val not in data.get("options", []):
                        raise ArtifactInvalid("select unknown option")
                else:
                    raise ArtifactInvalid(f"component type {ctype!r} rejects actions")
                old_view: dict = json.loads(row["view_state_json"])
                new_view = copy.deepcopy(old_view)
                new_view[cid] = copy.deepcopy(val)
                new_view_json = _canonical_json(new_view)
                new_vrev = row["view_revision"] + 1
                now = time.time()
                env = {
                    "schema_version": SCHEMA_VERSION,
                    "artifact_id": row["artifact_id"],
                    "session_id": row["session_id"],
                    "thread_id": row["thread_id"],
                    "epoch": row["epoch"],
                    "task_id": row["task_id"],
                    "objective_revision": row["objective_revision"],
                    "revision": row["revision"],
                    "title": row["title"],
                    "summary": row["summary"],
                    "components": copy.deepcopy(comps),
                    "layout": row["layout"],
                    "created_at": row["created_at"],
                    "updated_at": now,
                    "content_sha256": row["content_sha256"],
                    "content_scope": row["content_scope"],
                    "view_state": copy.deepcopy(new_view),
                    "view_revision": new_vrev,
                }
                _bounded_publication_json(env, reserve_api_order=True)
                _, total = self._usage_locked()
                old_ab = len(row["view_state_json"].encode("utf-8"))
                new_ab = len(new_view_json.encode("utf-8"))
                env_json = _canonical_json(env)
                delta = (
                    (new_ab - old_ab)
                    + len(val_json.encode("utf-8"))
                    + len(env_json.encode("utf-8"))
                )
                if total + delta > MAX_DB_BYTES:
                    raise ArtifactConflict("quota: database 64MiB exceeded")
                cur = self._conn.execute(
                    "UPDATE artifacts SET view_state_json=?,view_revision=?,updated_at=? WHERE artifact_id=? AND revision=?",
                    (new_view_json, new_vrev, now, artifact_id, exp),
                )
                if cur.rowcount != 1:
                    raise ArtifactConflict("concurrent revision change")
                self._conn.execute(
                    "INSERT INTO actions(artifact_id,idempotency_key,session_id,thread_id,epoch,expected_revision,component_id,value_json,response_json) VALUES(?,?,?,?,?,?,?,?,?)",
                    (artifact_id, key, sid, tid, ep, exp, cid, val_json, env_json),
                )
                self._conn.execute("COMMIT")
                return copy.deepcopy(env)
            except BaseException:
                try:
                    self._conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise

    def verify(self, evidence: Any, task: Any) -> bool:
        try:
            with self._lock:
                if self._closed:
                    return False
                try:
                    kind = evidence.kind
                    path = evidence.path
                    sha = evidence.sha256
                    erev = evidence.objective_revision
                except Exception:
                    return False
                if kind != "artifact":
                    return False
                if type(path) is not str or not path.startswith("artifact:"):
                    return False
                aid = path[len("artifact:") :]
                if not aid or any(c in aid for c in " \t\n\r"):
                    return False
                if type(sha) is not str or not _HEX64_RE.fullmatch(sha):
                    return False
                if isinstance(erev, bool) or not isinstance(erev, int) or erev < 1:
                    return False
                try:
                    t_sid = task.session_id
                    t_tid = task.thread_id
                    t_ep = task.epoch
                    t_id = task.task_id
                    t_rev = task.objective_revision
                except Exception:
                    return False
                if type(t_sid) is not str or type(t_tid) is not str or type(t_id) is not str:
                    return False
                if isinstance(t_ep, bool) or not isinstance(t_ep, int) or t_ep < 1:
                    return False
                if isinstance(t_rev, bool) or not isinstance(t_rev, int) or t_rev < 1:
                    return False
                if erev != t_rev:
                    return False
                cur = self._conn.execute("SELECT * FROM artifacts WHERE artifact_id=?", (aid,))
                row = cur.fetchone()
                if row is None:
                    return False
                if (
                    row["task_id"] != t_id
                    or row["session_id"] != t_sid
                    or row["thread_id"] != t_tid
                    or row["epoch"] != t_ep
                ):
                    return False
                if row["objective_revision"] != t_rev or row["objective_revision"] != erev:
                    return False
                if row["content_sha256"].lower() != sha.lower():
                    return False
                try:
                    comps = json.loads(row["components_json"])
                    recomputed = _compute_content_sha256(
                        row["artifact_id"],
                        row["session_id"],
                        row["thread_id"],
                        row["epoch"],
                        row["task_id"],
                        row["objective_revision"],
                        row["revision"],
                        row["title"],
                        row["summary"],
                        comps,
                        row["layout"],
                        row["content_scope"],
                    )
                    if recomputed.lower() != row["content_sha256"].lower():
                        return False
                    if recomputed.lower() != sha.lower():
                        return False
                except Exception:
                    return False
                return True
        except Exception:
            return False

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._closed = True
