import copy
import json

import pytest

from packages.contracts.tool_catalog import (
    CATALOG_KINDS,
    MAX_CATALOG_BYTES,
    MAX_CATALOG_ITEMS,
    MAX_ITEM_BYTES,
    MAX_PAGE_BYTES,
    PAGE_SIZE,
    CatalogError,
    CatalogQuery,
    catalog_page,
    encode_catalog,
    parse_query,
    qualify,
)


class EvilStr(str):
    def __str__(self):
        raise AssertionError("user str called")

    def __repr__(self):
        raise AssertionError("user repr called")


class EvilDict(dict):
    def items(self):
        raise AssertionError("user items called")


class EvilList(list):
    def __iter__(self):
        raise AssertionError("user iter called")


class EvilInt(int):
    pass


def _native(name, desc="a tool"):
    qid = qualify("native", name)
    d = {"kind": "native", "name": name, "qualified_id": qid}
    if desc is not None:
        d["description"] = desc
    return d


def _mcp(server, name, desc="mcp tool"):
    qid = qualify("mcp", name, server)
    d = {"kind": "mcp", "name": name, "server_id": server, "qualified_id": qid}
    if desc is not None:
        d["description"] = desc
    return d


def _skill(name, desc="skill tool"):
    qid = qualify("skill", name)
    d = {"kind": "skill", "name": name, "qualified_id": qid}
    if desc is not None:
        d["description"] = desc
    return d


def _plugin(name, desc="plugin tool"):
    qid = qualify("plugin", name)
    d = {"kind": "plugin", "name": name, "qualified_id": qid}
    if desc is not None:
        d["description"] = desc
    return d


def test_constants():
    assert CATALOG_KINDS == ("native", "mcp", "skill", "plugin")
    assert PAGE_SIZE == 4
    assert MAX_ITEM_BYTES == 2048
    assert MAX_PAGE_BYTES == 16384
    assert MAX_CATALOG_BYTES == 32768
    assert MAX_CATALOG_ITEMS == 8192


def test_error_is_value_error_and_closed():
    err = CatalogError("invalid_query")
    assert isinstance(err, ValueError)
    assert err.code == "invalid_query"
    for bad in ["nope", "", None, 42]:
        with pytest.raises(ValueError):
            CatalogError(bad)


def test_error_messages_constant():
    with pytest.raises(CatalogError) as a:
        CatalogQuery(kind="bogus")
    with pytest.raises(CatalogError) as b:
        CatalogQuery(kind="also-bogus")
    assert str(a.value) == str(b.value)
    assert "bogus" not in str(a.value)
    secret = "SECRET-XYZ-123"
    with pytest.raises(CatalogError) as c:
        qualify("native", secret + "\x00")
    assert secret not in str(c.value)


def test_query_defaults_and_selected():
    q = CatalogQuery()
    assert q.kind is None
    assert q.after == 0
    assert q.selected is False
    assert type(q.selected) is bool
    s = CatalogQuery(kind="native", name="t")
    assert s.selected is True
    m = CatalogQuery(kind="mcp", name="t", server_id="s")
    assert m.selected is True


def test_query_empty_search_equivalent():
    items = [_native("alpha", "hello world"), _native("beta", "other")]
    p_none = catalog_page(items, CatalogQuery(query=None))
    p_empty = catalog_page(items, CatalogQuery(query=""))
    assert p_none == p_empty


def test_query_rejects_malformed():
    with pytest.raises(CatalogError) as e:
        CatalogQuery(kind="Native")
    assert e.value.code == "invalid_query"
    with pytest.raises(CatalogError):
        CatalogQuery(kind=EvilStr("native"))
    with pytest.raises(CatalogError):
        CatalogQuery(name="t")
    with pytest.raises(CatalogError):
        CatalogQuery(kind="native", name="")
    with pytest.raises(CatalogError):
        CatalogQuery(kind="native", name="a\x00b")
    with pytest.raises(CatalogError):
        CatalogQuery(kind="native", name="x" * 513)
    with pytest.raises(CatalogError):
        CatalogQuery(kind="native", name="\ud800")
    with pytest.raises(CatalogError):
        CatalogQuery(kind="native", name="t", server_id="s")
    with pytest.raises(CatalogError):
        CatalogQuery(kind="mcp", name="t")
    with pytest.raises(CatalogError):
        CatalogQuery(kind="native", server_id="s")
    with pytest.raises(CatalogError):
        CatalogQuery(kind="native", name="t", query="x")
    with pytest.raises(CatalogError):
        CatalogQuery(kind="native", name="t", after=1)
    with pytest.raises(CatalogError):
        CatalogQuery(after=True)
    with pytest.raises(CatalogError):
        CatalogQuery(after="0")
    with pytest.raises(CatalogError):
        CatalogQuery(after=-1)
    with pytest.raises(CatalogError):
        CatalogQuery(after=1000001)
    with pytest.raises(CatalogError):
        CatalogQuery(after=EvilInt(0))
    with pytest.raises(CatalogError):
        CatalogQuery(query="x" * 81)


def test_parse_query_round_trip_and_unknown():
    q = parse_query({"kind": "skill", "name": None, "after": 2})
    assert q.kind == "skill"
    assert q.after == 2
    assert parse_query({}) == CatalogQuery()
    before = {"kind": "native"}
    snapshot = dict(before)
    parse_query(before)
    assert before == snapshot
    with pytest.raises(CatalogError) as e:
        parse_query({"kind": "native", "bogus": 1})
    assert e.value.code == "invalid_query"
    with pytest.raises(CatalogError):
        parse_query(EvilDict({"kind": "native"}))
    with pytest.raises(CatalogError):
        parse_query([("kind", "native")])
    with pytest.raises(CatalogError):
        parse_query({42: "native"})
    evil_key = object()
    with pytest.raises(CatalogError):
        parse_query({evil_key: 1})


def test_qualify_identities():
    assert qualify("native", "run") == "native:run"
    assert qualify("skill", "summarize") == "skill:summarize"
    assert qualify("plugin", "calc") == "plugin:calc"
    qid = qualify("mcp", "search", "srv")
    assert qid == "mcp:" + json.dumps(
        ["srv", "search"],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    tricky = qualify("native", 'a:b,c"q')
    assert tricky == 'native:a:b,c"q'
    mcp_tricky = qualify("mcp", "a:b", "s,c")
    assert mcp_tricky.startswith("mcp:")
    with pytest.raises(CatalogError) as e:
        qualify("mcp", "t")
    assert e.value.code == "invalid_metadata"
    with pytest.raises(CatalogError):
        qualify("native", "t", "s")
    with pytest.raises(CatalogError):
        qualify("bogus", "t")
    with pytest.raises(CatalogError):
        qualify("native", EvilStr("t"))


def test_encode_canonical_and_bounds():
    payload = {"b": [1, 2.5, True, None], "a": "x"}
    text = encode_catalog(payload)
    assert text == '{"a":"x","b":[1,2.5,true,null]}'
    assert type(text) is str
    before = {"a": [1, 2]}
    snap = copy.deepcopy(before)
    encode_catalog(before)
    assert before == snap
    uni = {"v": "caf\u00e9"}
    t2 = encode_catalog(uni)
    assert "\\u00e9" in t2
    with pytest.raises(CatalogError) as e:
        encode_catalog({"a": float("nan")})
    assert e.value.code == "invalid_metadata"
    with pytest.raises(CatalogError):
        encode_catalog({"a": float("inf")})
    with pytest.raises(CatalogError):
        encode_catalog({1: "x"})
    with pytest.raises(CatalogError):
        encode_catalog({"a": "\ud800"})
    with pytest.raises(CatalogError):
        encode_catalog({"a": EvilStr("x")})
    with pytest.raises(CatalogError):
        encode_catalog(EvilDict({"a": 1}))
    with pytest.raises(CatalogError):
        encode_catalog({"a": (1, 2)})
    with pytest.raises(CatalogError):
        encode_catalog({"a": {1, 2}})
    deep = []
    cur = deep
    for _ in range(30):
        nxt = []
        cur.append(nxt)
        cur = nxt
    with pytest.raises(CatalogError):
        encode_catalog(deep)
    wide = list(range(16385))
    with pytest.raises(CatalogError):
        encode_catalog(wide)
    big = {"v": "x" * 40000}
    with pytest.raises(CatalogError) as e2:
        encode_catalog(big)
    assert e2.value.code == "catalog_too_large"
    with pytest.raises(CatalogError) as e3:
        encode_catalog({"a": 1}, max_bytes=0)
    assert e3.value.code == "invalid_query"
    with pytest.raises(CatalogError):
        encode_catalog({"a": 1}, max_bytes=True)
    small = encode_catalog({"a": 1}, max_bytes=7)
    assert small == '{"a":1}'
    with pytest.raises(CatalogError):
        encode_catalog({"a": 1}, max_bytes=6)


def test_catalog_page_rejects_bad_containers():
    good = [_native("a")]
    with pytest.raises(CatalogError):
        catalog_page(EvilList(good), CatalogQuery())
    with pytest.raises(CatalogError):
        catalog_page("notalist", CatalogQuery())
    with pytest.raises(CatalogError):
        catalog_page(good, {"kind": "native"})
    with pytest.raises(CatalogError):
        catalog_page(good, CatalogQuery(kind="native", name="a"))
    many = [_native("a")] * 8193
    with pytest.raises(CatalogError) as e:
        catalog_page(many, CatalogQuery())
    assert e.value.code == "catalog_too_large"


def test_catalog_page_identity_and_duplicates():
    a = _native("tool")
    b = _skill("tool")
    page = catalog_page([a, b], CatalogQuery())
    assert len(page["items"]) == 2
    dup1 = _native("same")
    dup2 = dict(dup1)
    with pytest.raises(CatalogError) as e:
        catalog_page([dup1, dup2], CatalogQuery())
    assert e.value.code == "invalid_metadata"
    bad = dict(a)
    bad["qualified_id"] = "native:wrong"
    with pytest.raises(CatalogError):
        catalog_page([bad], CatalogQuery())
    mcp_no_server = {"kind": "mcp", "name": "t", "qualified_id": "mcp:x"}
    with pytest.raises(CatalogError):
        catalog_page([mcp_no_server], CatalogQuery())
    native_with_server = dict(_native("t"))
    native_with_server["server_id"] = "s"
    with pytest.raises(CatalogError):
        catalog_page([native_with_server], CatalogQuery())
    schema_row = dict(_native("t"))
    schema_row["schema"] = {"type": "object"}
    with pytest.raises(CatalogError):
        catalog_page([schema_row], CatalogQuery())
    nested = dict(_native("t2"))
    nested["extra"] = {"input_schema": 1}
    with pytest.raises(CatalogError):
        catalog_page([nested], CatalogQuery())


def test_catalog_page_row_bytes_escaped():
    ok = _native("a", "x" * 100)
    catalog_page([ok], CatalogQuery())
    big = _native("a", "x" * 2000)
    with pytest.raises(CatalogError) as e:
        catalog_page([big], CatalogQuery())
    assert e.value.code == "metadata_too_large"
    escaped = _native("e", "\u00e9" * 400)
    with pytest.raises(CatalogError) as e2:
        catalog_page([escaped], CatalogQuery())
    assert e2.value.code == "metadata_too_large"


def test_catalog_page_search_literal_casefold():
    items = [_native("a.*b"), _native("axb"), _native("ToolBox", "holds tools")]
    q = CatalogQuery(query=".*")
    page = catalog_page(items, q)
    assert [r["name"] for r in page["items"]] == ["a.*b"]
    q2 = CatalogQuery(query="toolbox")
    page2 = catalog_page(items, q2)
    assert [r["name"] for r in page2["items"]] == ["ToolBox"]
    q3 = CatalogQuery(query="HOLDS")
    page3 = catalog_page(items, q3)
    assert [r["name"] for r in page3["items"]] == ["ToolBox"]
    skill = _skill("s1", "plain")
    skill["instructions"] = "needle-in-instructions"
    page4 = catalog_page([skill], CatalogQuery(query="needle-in-instructions"))
    assert page4["items"] == []


def test_catalog_page_sort_filter_paginate():
    items = [
        _plugin("b"),
        _native("b"),
        _mcp("s2", "a"),
        _mcp("s1", "b"),
        _skill("a"),
        _native("a"),
    ]
    page = catalog_page(items, CatalogQuery(after=0))
    assert len(page["items"]) == 4
    assert page["schema_version"] == "1.0"
    names = [(r["kind"], r.get("server_id", ""), r["name"]) for r in page["items"]]
    assert names[0] == ("native", "", "a")
    assert names[1] == ("native", "", "b")
    nxt = page["next_after"]
    assert nxt == 4
    page2 = catalog_page(items, CatalogQuery(after=nxt))
    assert len(page2["items"]) == 2
    assert page2["next_after"] is None
    only_mcp = catalog_page(items, CatalogQuery(kind="mcp", server_id="s1"))
    assert len(only_mcp["items"]) == 1
    assert only_mcp["items"][0]["server_id"] == "s1"
    only_native = catalog_page(items, CatalogQuery(kind="native"))
    assert all(r["kind"] == "native" for r in only_native["items"])
    empty = catalog_page(items, CatalogQuery(after=100))
    assert empty["items"] == []
    assert empty["next_after"] is None


def test_catalog_page_catalogs_and_freshness():
    items = [_native("a")]
    cats = [
        {"kind": "plugin", "state": "configured"},
        {"kind": "native", "state": "not_configured"},
    ]
    page = catalog_page(items, CatalogQuery(), catalogs=cats)
    assert [c["kind"] for c in page["catalogs"]] == ["native", "plugin"]
    assert cats[0]["kind"] == "plugin"
    with pytest.raises(CatalogError):
        catalog_page(items, CatalogQuery(), catalogs=[{"kind": "native", "state": "bogus"}])
    with pytest.raises(CatalogError):
        catalog_page(
            items,
            CatalogQuery(),
            catalogs=[
                {"kind": "native", "state": "configured"},
                {"kind": "native", "state": "configured"},
            ],
        )
    with pytest.raises(CatalogError):
        catalog_page(items, CatalogQuery(), catalogs=[{"kind": "native"}])
    before = copy.deepcopy(items)
    out = catalog_page(items, CatalogQuery())
    assert items == before
    assert out["items"][0] is not items[0]
    out["items"][0]["description"] = "mutated"
    assert items[0]["description"] != "mutated"
    out2 = catalog_page(items, CatalogQuery())
    assert out["items"][0] is not out2["items"][0]


def test_catalog_page_hostile_metadata():
    with pytest.raises(CatalogError):
        catalog_page([EvilDict({"kind": "native"})], CatalogQuery())
    bad_name = {"kind": "native", "name": EvilStr("t"), "qualified_id": "native:t"}
    with pytest.raises(CatalogError):
        catalog_page([bad_name], CatalogQuery())
    bad_list = {"kind": "native", "name": "t", "qualified_id": "native:t", "extra": EvilList([1])}
    with pytest.raises(CatalogError):
        catalog_page([bad_list], CatalogQuery())
    bad_num = {"kind": "native", "name": "t", "qualified_id": "native:t", "extra": float("nan")}
    with pytest.raises(CatalogError):
        catalog_page([bad_num], CatalogQuery())


def test_unavailable_and_wire_agreement():
    dis = _native("calc", "calculator")
    dis["unavailable"] = True
    page = catalog_page([dis], CatalogQuery())
    assert page["items"][0]["unavailable"] is True
    assert "invocation" not in page["items"][0]
    en = _native("calc2", "other")
    page2 = catalog_page([dis, en], CatalogQuery())
    assert len(page2["items"]) == 2
    wire = qualify("mcp", 'weird:name,with"q', "srv:id")
    expected = "mcp:" + json.dumps(
        ["srv:id", 'weird:name,with"q'],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    assert wire == expected
    item = {
        "kind": "mcp",
        "name": 'weird:name,with"q',
        "server_id": "srv:id",
        "qualified_id": wire,
    }
    found = catalog_page([item], CatalogQuery(kind="mcp", server_id="srv:id"))
    assert len(found["items"]) == 1


def test_selected_schema_bounded_via_encode():
    schema = {
        "type": "object",
        "properties": {"q": {"type": "string"}},
        "required": ["q"],
    }
    text = encode_catalog({"schema": schema, "state": "available"})
    assert json.loads(text)["state"] == "available"
    huge = {"schema": {"v": "x" * 40000}}
    with pytest.raises(CatalogError) as e:
        encode_catalog(huge)
    assert e.value.code == "catalog_too_large"
    unavailable = {"state": "unavailable", "reason": "not_configured"}
    assert encode_catalog(unavailable).startswith("{")


# Independent review regressions against the authoritative parent contract.
@pytest.mark.parametrize("length", [264, 265, 512])
def test_existing_and_maximum_skill_identities_survive_query_and_page(length):
    name = "a" * length
    assert CatalogQuery(kind="skill", name=name).name == name
    assert qualify("skill", name) == "skill:" + name
    assert catalog_page([_skill(name)], CatalogQuery())["items"][0]["name"] == name


def test_exhausted_cursor_is_none_for_empty_exact_and_past_end_pages():
    items = [_native(str(index)) for index in range(PAGE_SIZE)]
    for selected, after in (([], 0), (items, 0), (items, PAGE_SIZE), (items, 1000000)):
        assert catalog_page(selected, CatalogQuery(after=after))["next_after"] is None


def test_search_nul_is_literal_and_does_not_change_identity_rules():
    item = _native("a", "prefix\x00suffix")
    assert catalog_page([item], CatalogQuery(query="\x00"))["items"] == [item]
    for make in (
        lambda: CatalogQuery(kind="native", name="a\x00b"),
        lambda: qualify("mcp", "x", "a\x00b"),
    ):
        with pytest.raises(CatalogError):
            make()


def test_descriptive_schema_metadata_is_not_a_schema_payload():
    item = _native("a")
    item.update(schema_state="not_discovered", schema_sha256="a" * 64, schema_version="1.0")
    item["metadata"] = {"database_schema_title": "public synthetic label"}
    page = catalog_page([item], CatalogQuery())
    assert page["items"] == [item]


@pytest.mark.parametrize(
    "key", ["schema", "input_schema", "output_schema", "argument_schema", "arguments_schema"]
)
def test_reserved_schema_payload_keys_are_rejected_at_any_list_row_depth(key):
    item = _native("a")
    item["metadata"] = [{key: {"type": "object"}}]
    with pytest.raises(CatalogError) as caught:
        catalog_page([item], CatalogQuery(query="does not match"))
    assert caught.value.code == "invalid_metadata"


def test_node_and_depth_limits_are_inclusive_and_cycles_are_constant_errors():
    assert json.loads(encode_catalog([0] * 16383)) == [0] * 16383
    for value in ([0] * 16384,):
        with pytest.raises(CatalogError) as caught:
            encode_catalog(value)
        assert caught.value.code == "invalid_metadata"
    value = None
    for _ in range(24):
        value = [value]
    assert json.loads(encode_catalog(value)) == value
    with pytest.raises(CatalogError):
        encode_catalog([value])
    cyclic_list = []
    cyclic_list.append(cyclic_list)
    cyclic_dict = {}
    cyclic_dict["self"] = cyclic_dict
    for value in (cyclic_list, cyclic_dict):
        with pytest.raises(CatalogError) as caught:
            encode_catalog(value)
        assert caught.value.code == "invalid_metadata"


def test_exact_canonical_row_byte_limit_and_no_schema_clipping():
    item = _native("a", "")
    item["description"] = "x" * (MAX_ITEM_BYTES - len(encode_catalog(item)))
    assert len(encode_catalog(item)) == MAX_ITEM_BYTES
    assert catalog_page([item], CatalogQuery())["items"] == [item]
    item["description"] += "x"
    with pytest.raises(CatalogError) as caught:
        catalog_page([item], CatalogQuery())
    assert caught.value.code == "metadata_too_large"
    selected = {"schema": {"description": ""}}
    selected["schema"]["description"] = "x" * (MAX_CATALOG_BYTES - len(encode_catalog(selected)))
    raw = encode_catalog(selected)
    assert len(raw) == MAX_CATALOG_BYTES
    assert json.loads(raw) == selected
    selected["schema"]["description"] += "x"
    with pytest.raises(CatalogError) as caught:
        encode_catalog(selected)
    assert caught.value.code == "catalog_too_large"


def test_nested_metadata_is_fresh_and_mcp_delimiter_names_never_collide():
    items = [_mcp("a:b", "c"), _mcp("a", "b:c"), _skill("c"), _plugin("c")]
    items[0]["metadata"] = {"values": [{"value": 1}]}
    page = catalog_page(items, CatalogQuery())
    assert len({item["qualified_id"] for item in page["items"]}) == 4
    copied = next(item for item in page["items"] if item.get("server_id") == "a:b")
    copied["metadata"]["values"][0]["value"] = 2
    assert items[0]["metadata"]["values"][0]["value"] == 1


def test_hostile_protocol_methods_are_not_called_by_rejections():
    class Hostile:
        def __str__(self):
            raise AssertionError("untrusted str")

        def __repr__(self):
            raise AssertionError("untrusted repr")

        def __iter__(self):
            raise AssertionError("untrusted iteration")

    class HostileTuple(tuple):
        def __iter__(self):
            raise AssertionError("untrusted tuple iteration")

    value = Hostile()
    for make in (
        lambda: encode_catalog({"value": value}),
        lambda: parse_query({value: None}),
        lambda: qualify(value, "a"),
        lambda: catalog_page(HostileTuple(), CatalogQuery()),
    ):
        with pytest.raises(CatalogError):
            make()


def test_filters_paginate_only_matching_rows_and_unicode_search_is_casefolded():
    items = [_skill(f"{index:02d}", "Straße needle") for index in range(9)]
    items.extend(_native(f"{index:02d}") for index in range(4))
    first = catalog_page(tuple(reversed(items)), CatalogQuery(kind="skill", query="STRASSE"))
    assert [item["name"] for item in first["items"]] == ["00", "01", "02", "03"]
    second = catalog_page(
        items, CatalogQuery(kind="skill", query="STRASSE", after=first["next_after"])
    )
    assert [item["name"] for item in second["items"]] == ["04", "05", "06", "07"]
    last = catalog_page(
        items, CatalogQuery(kind="skill", query="STRASSE", after=second["next_after"])
    )
    assert [item["name"] for item in last["items"]] == ["08"]
    assert last["next_after"] is None


@pytest.mark.parametrize("value", [None, False, True, 123456, -0.0, 1e30])
def test_scalar_size_refusal_retains_the_catalog_size_code(value):
    raw = json.dumps(value, allow_nan=False)
    assert encode_catalog(value, max_bytes=len(raw)) == raw
    with pytest.raises(CatalogError) as caught:
        encode_catalog(value, max_bytes=len(raw) - 1)
    assert caught.value.code == "catalog_too_large"


def test_json_ascii_escaping_is_measured_before_serialization_without_clipping():
    value = {"text": "".join(map(chr, range(256))) + "🌱\u2028"}
    raw = json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    assert encode_catalog(value, max_bytes=len(raw)) == raw
    with pytest.raises(CatalogError) as caught:
        encode_catalog(value, max_bytes=len(raw) - 1)
    assert caught.value.code == "catalog_too_large"


def test_arbitrary_metaclass_equality_is_not_a_json_type_check():
    class HostileMeta(type):
        def __eq__(self, other):
            raise AssertionError("untrusted metaclass equality")

    class Hostile(metaclass=HostileMeta):
        pass

    with pytest.raises(CatalogError):
        encode_catalog({"value": Hostile()})


def test_maximum_catalog_count_is_bounded_and_filtering_keeps_terminal_cursor():
    items = [_native(f"tool{index:04d}", None) for index in range(MAX_CATALOG_ITEMS)]
    page = catalog_page(items, CatalogQuery(after=MAX_CATALOG_ITEMS - PAGE_SIZE))
    assert len(page["items"]) == PAGE_SIZE
    assert page["next_after"] is None
    assert catalog_page(items, CatalogQuery(query="not present"))["next_after"] is None
