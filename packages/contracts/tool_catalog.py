"""Pure catalog primitives for progressive tool discovery."""

import json
import math
from dataclasses import dataclass

CATALOG_KINDS = ("native", "mcp", "skill", "plugin")
PAGE_SIZE = 4
MAX_ITEM_BYTES = 2048
MAX_PAGE_BYTES = 16384
MAX_CATALOG_BYTES = 32768
MAX_CATALOG_ITEMS = 8192

_ERROR_MESSAGES = {
    "invalid_query": "invalid catalog query",
    "invalid_metadata": "invalid catalog metadata",
    "metadata_too_large": "catalog metadata too large",
    "catalog_too_large": "catalog too large",
}

_KIND_ORDER = {"native": 0, "mcp": 1, "skill": 2, "plugin": 3}
_CATALOG_STATES = ("configured", "not_configured", "metadata_unavailable")
_ALLOWED_QUERY_KEYS = ("kind", "name", "server_id", "query", "after")
_SCHEMA_PAYLOAD_KEYS = frozenset(
    {"schema", "input_schema", "output_schema", "argument_schema", "arguments_schema"}
)


def _is_valid_name(value, bound):
    if type(value) is not str:
        return False
    if len(value) == 0 or len(value) > bound:
        return False
    if "\x00" in value:
        return False
    try:
        value.encode("utf-8")
    except Exception:
        return False
    return True


def _is_valid_search(value):
    if type(value) is not str:
        return False
    if len(value) > 80:
        return False
    try:
        value.encode("utf-8")
    except Exception:
        return False
    return True


def _is_valid_after(value):
    if type(value) is not int:
        return False
    return 0 <= value <= 1000000


def _is_valid_description(value):
    if value is None:
        return True
    if type(value) is not str:
        return False
    try:
        value.encode("utf-8")
    except Exception:
        return False
    return True


class CatalogError(ValueError):
    def __init__(self, code):
        if type(code) is not str or code not in _ERROR_MESSAGES:
            raise ValueError("invalid catalog error code")
        self.code = code
        super().__init__(_ERROR_MESSAGES[code])


@dataclass(frozen=True, slots=True)
class CatalogQuery:
    kind: object = None
    name: object = None
    server_id: object = None
    query: object = None
    after: object = 0

    @property
    def selected(self) -> bool:
        return self.name is not None

    def __post_init__(self):
        kind = self.kind
        name = self.name
        server_id = self.server_id
        query = self.query
        after = self.after
        if kind is not None:
            if type(kind) is not str or kind not in CATALOG_KINDS:
                raise CatalogError("invalid_query")
        if name is not None:
            if not _is_valid_name(name, 512):
                raise CatalogError("invalid_query")
        if server_id is not None:
            if not _is_valid_name(server_id, 128):
                raise CatalogError("invalid_query")
        if query is not None:
            if not _is_valid_search(query):
                raise CatalogError("invalid_query")
        if not _is_valid_after(after):
            raise CatalogError("invalid_query")
        if name is not None and kind is None:
            raise CatalogError("invalid_query")
        if server_id is not None and kind != "mcp":
            raise CatalogError("invalid_query")
        if kind == "mcp" and name is not None and server_id is None:
            raise CatalogError("invalid_query")
        if name is not None:
            if query is not None and query != "":
                raise CatalogError("invalid_query")
            if after != 0:
                raise CatalogError("invalid_query")


def parse_query(arguments):
    if type(arguments) is not dict:
        raise CatalogError("invalid_query")
    for k in arguments:
        if type(k) is not str:
            raise CatalogError("invalid_query")
        if k not in _ALLOWED_QUERY_KEYS:
            raise CatalogError("invalid_query")
    kind = arguments["kind"] if "kind" in arguments else None
    name = arguments["name"] if "name" in arguments else None
    server_id = arguments["server_id"] if "server_id" in arguments else None
    query = arguments["query"] if "query" in arguments else None
    after = arguments["after"] if "after" in arguments else 0
    return CatalogQuery(kind=kind, name=name, server_id=server_id, query=query, after=after)


def qualify(kind, name, server_id=None):
    if type(kind) is not str or kind not in CATALOG_KINDS:
        raise CatalogError("invalid_metadata")
    if not _is_valid_name(name, 512):
        raise CatalogError("invalid_metadata")
    if kind == "mcp":
        if not _is_valid_name(server_id, 128):
            raise CatalogError("invalid_metadata")
    else:
        if server_id is not None:
            raise CatalogError("invalid_metadata")
    if kind == "native":
        return "native:" + name
    if kind == "mcp":
        try:
            inner = json.dumps(
                [server_id, name],
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            )
        except Exception:
            raise CatalogError("invalid_metadata") from None
        return "mcp:" + inner
    if kind == "skill":
        return "skill:" + name
    return "plugin:" + name


def _validate_json_payload(
    payload, *, max_bytes=MAX_CATALOG_BYTES, size_error="catalog_too_large", reject_schemas=False
):
    """Bound traversal and exact canonical size before allocating serialized JSON."""
    nodes = 0
    size = 0
    active = set()

    def charge(amount):
        nonlocal size
        size += amount
        if size > max_bytes:
            raise CatalogError(size_error)

    def string(value):
        charge(2)  # JSON quotes.
        for character in value:
            point = ord(character)
            if 0xD800 <= point <= 0xDFFF:
                raise CatalogError("invalid_metadata")
            if character in '"\\\b\f\n\r\t':
                charge(2)
            elif point < 0x20 or 0x7F <= point <= 0xFFFF:
                charge(6)
            elif point > 0xFFFF:
                charge(12)
            else:
                charge(1)

    def visit(value, depth):
        nonlocal nodes
        nodes += 1
        if depth > 24 or nodes > 16384:
            raise CatalogError("invalid_metadata")
        kind = type(value)
        if kind is dict or kind is list:
            minimum_children = len(value) * (2 if kind is dict else 1)
            if nodes + minimum_children > 16384 or id(value) in active:
                raise CatalogError("invalid_metadata")
            # Separators have the same byte count regardless of key order.
            charge(2 + max(0, len(value) - 1))
            active.add(id(value))
            try:
                if kind is dict:
                    for key, child in value.items():
                        if type(key) is not str or (reject_schemas and key in _SCHEMA_PAYLOAD_KEYS):
                            raise CatalogError("invalid_metadata")
                        nodes += 1
                        if nodes > 16384:
                            raise CatalogError("invalid_metadata")
                        string(key)
                        charge(1)  # Key/value colon.
                        visit(child, depth + 1)
                else:
                    for child in value:
                        visit(child, depth + 1)
            finally:
                active.remove(id(value))
        elif kind is str:
            string(value)
        elif kind is int or kind is bool or kind is type(None) or kind is float:
            if kind is float and not math.isfinite(value):
                raise CatalogError("invalid_metadata")
            try:
                scalar_size = len(json.dumps(value, allow_nan=False))
            except (TypeError, ValueError):
                raise CatalogError("invalid_metadata") from None
            charge(scalar_size)
        else:
            raise CatalogError("invalid_metadata")

    visit(payload, 0)


def encode_catalog(payload, *, max_bytes=MAX_CATALOG_BYTES):
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_CATALOG_BYTES:
        raise CatalogError("invalid_query")
    _validate_json_payload(payload, max_bytes=max_bytes)
    try:
        text = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except Exception:
        raise CatalogError("invalid_metadata") from None
    try:
        size = len(text.encode("ascii"))
    except Exception:
        raise CatalogError("invalid_metadata") from None
    if size > max_bytes:
        raise CatalogError("catalog_too_large")
    return text


def _validate_item_structure(item):
    if type(item) is not dict:
        raise CatalogError("invalid_metadata")
    _validate_json_payload(
        item, max_bytes=MAX_ITEM_BYTES, size_error="metadata_too_large", reject_schemas=True
    )


def _deep_copy_json(obj):
    t = type(obj)
    if t is dict:
        return {k: _deep_copy_json(v) for k, v in obj.items()}
    if t is list:
        return [_deep_copy_json(v) for v in obj]
    return obj


def catalog_page(items, query, *, catalogs=()):
    if type(items) is not list and type(items) is not tuple:
        raise CatalogError("invalid_metadata")
    if len(items) > MAX_CATALOG_ITEMS:
        raise CatalogError("catalog_too_large")
    if type(query) is not CatalogQuery:
        raise CatalogError("invalid_query")
    if query.selected:
        raise CatalogError("invalid_query")
    if type(catalogs) is not list and type(catalogs) is not tuple:
        raise CatalogError("invalid_metadata")
    if len(catalogs) > 4:
        raise CatalogError("invalid_metadata")
    seen_kinds = set()
    for entry in catalogs:
        if type(entry) is not dict:
            raise CatalogError("invalid_metadata")
        for k in entry:
            if type(k) is not str:
                raise CatalogError("invalid_metadata")
        if len(entry) != 2:
            raise CatalogError("invalid_metadata")
        if "kind" not in entry or "state" not in entry:
            raise CatalogError("invalid_metadata")
        k = entry["kind"]
        s = entry["state"]
        if type(k) is not str or k not in CATALOG_KINDS:
            raise CatalogError("invalid_metadata")
        if type(s) is not str or s not in _CATALOG_STATES:
            raise CatalogError("invalid_metadata")
        if k in seen_kinds:
            raise CatalogError("invalid_metadata")
        seen_kinds.add(k)
    seen_ids = set()
    validated = []
    for item in items:
        if type(item) is not dict:
            raise CatalogError("invalid_metadata")
        _validate_item_structure(item)
        if "kind" not in item or "name" not in item or "qualified_id" not in item:
            raise CatalogError("invalid_metadata")
        kind = item["kind"]
        name = item["name"]
        qid = item["qualified_id"]
        if type(kind) is not str or kind not in CATALOG_KINDS:
            raise CatalogError("invalid_metadata")
        if not _is_valid_name(name, 512):
            raise CatalogError("invalid_metadata")
        has_server = "server_id" in item
        if kind == "mcp":
            if not has_server:
                raise CatalogError("invalid_metadata")
            sid = item["server_id"]
            if not _is_valid_name(sid, 128):
                raise CatalogError("invalid_metadata")
        else:
            if has_server:
                raise CatalogError("invalid_metadata")
            sid = None
        if type(qid) is not str:
            raise CatalogError("invalid_metadata")
        if kind == "mcp":
            expected = qualify(kind, name, sid)
        else:
            expected = qualify(kind, name)
        if qid != expected:
            raise CatalogError("invalid_metadata")
        if "description" in item:
            if not _is_valid_description(item["description"]):
                raise CatalogError("invalid_metadata")
        try:
            row_text = json.dumps(
                item,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            )
        except Exception:
            raise CatalogError("invalid_metadata") from None
        try:
            row_size = len(row_text.encode("ascii"))
        except Exception:
            raise CatalogError("invalid_metadata") from None
        if row_size > MAX_ITEM_BYTES:
            raise CatalogError("metadata_too_large")
        if qid in seen_ids:
            raise CatalogError("invalid_metadata")
        seen_ids.add(qid)
        validated.append(item)
    qkind = query.kind
    qsid = query.server_id
    qsearch = query.query
    if qsearch is not None and qsearch == "":
        qsearch_eff = None
    else:
        qsearch_eff = qsearch
    needle = None
    if qsearch_eff is not None:
        try:
            needle = qsearch_eff.casefold()
        except Exception:
            raise CatalogError("invalid_query") from None
    filtered = []
    for item in validated:
        if qkind is not None and item["kind"] != qkind:
            continue
        if qsid is not None:
            try:
                s = item.get("server_id")
            except Exception:
                raise CatalogError("invalid_metadata") from None
            if s != qsid:
                continue
        if needle is not None:
            qid = item["qualified_id"]
            nm = item["name"]
            try:
                qid_fold = qid.casefold()
                nm_fold = nm.casefold()
            except Exception:
                raise CatalogError("invalid_metadata") from None
            if needle in qid_fold or needle in nm_fold:
                pass
            else:
                desc = item.get("description") if "description" in item else None
                if type(desc) is not str:
                    continue
                try:
                    desc_fold = desc.casefold()
                except Exception:
                    raise CatalogError("invalid_metadata") from None
                if needle not in desc_fold:
                    continue
        filtered.append(item)

    def _sort_key(it):
        k = it["kind"]
        idx = _KIND_ORDER[k]
        if k == "mcp":
            sid = it["server_id"]
        else:
            sid = ""
        nm = it["name"]
        return (idx, sid, nm)

    filtered_sorted = sorted(filtered, key=_sort_key)
    after = query.after
    page_slice = filtered_sorted[after : after + PAGE_SIZE]
    end = after + len(page_slice)
    next_after = end if end < len(filtered_sorted) else None
    fresh_items = [_deep_copy_json(v) for v in page_slice]
    cats = list(catalogs)
    cats_sorted = sorted(cats, key=lambda e: _KIND_ORDER[e["kind"]])
    fresh_cats = [{"kind": e["kind"], "state": e["state"]} for e in cats_sorted]
    page = {
        "schema_version": "1.0",
        "items": fresh_items,
        "next_after": next_after,
        "catalogs": fresh_cats,
    }
    try:
        page_text = json.dumps(
            page,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except Exception:
        raise CatalogError("invalid_metadata") from None
    try:
        page_size = len(page_text.encode("ascii"))
    except Exception:
        raise CatalogError("invalid_metadata") from None
    if page_size > MAX_PAGE_BYTES:
        raise CatalogError("catalog_too_large")
    return page
