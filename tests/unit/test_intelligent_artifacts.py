import copy
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

import services.agent.artifacts as artmod
from services.agent.artifacts import (
    ArtifactConflict,
    ArtifactInvalid,
    ArtifactNotFound,
    ArtifactStore,
    validate_components,
)


def mk_task(sid="sess", tid="thr", epoch=1, rev=1, scope="local_only", task_id=None):
    return SimpleNamespace(
        session_id=sid,
        thread_id=tid,
        epoch=epoch,
        task_id=task_id or str(uuid.uuid4()),
        objective_revision=rev,
        content_scope=scope,
        objective="synthetic objective",
    )


def mk_ev(aid, sha, rev, kind="artifact"):
    return SimpleNamespace(
        kind=kind,
        path=f"artifact:{aid}",
        sha256=sha,
        objective_revision=rev,
        summary="receipt",
        evidence_id=str(uuid.uuid4()),
    )


def txt(cid="t1", text="hello"):
    return {"id": cid, "type": "text", "title": "T", "data": {"text": text}}


def all_types_comps():
    return [
        {"id": "tx", "type": "text", "data": {"text": "hi"}},
        {"id": "st", "type": "status", "data": {"text": "ok", "tone": "success"}},
        {
            "id": "tb",
            "type": "table",
            "data": {"columns": ["a", "b"], "rows": [[1, "x"], [None, True]]},
        },
        {"id": "ch", "type": "chart", "data": {"labels": ["a"], "values": [1.5]}},
        {
            "id": "dg",
            "type": "diagram",
            "data": {"nodes": [{"id": "n1", "label": "N"}], "edges": []},
        },
        {
            "id": "so",
            "type": "source",
            "data": {"label": "L", "url": "https://example.com/a", "freshness": "static"},
        },
        {"id": "nb", "type": "number", "data": {"label": "L", "value": 5, "min": 0, "max": 10}},
        {"id": "se", "type": "select", "data": {"label": "L", "options": ["a", "b"], "value": "a"}},
        {"id": "ak", "type": "ask", "data": {"label": "L", "message": "pick?"}},
    ]


def new_store(tmp_path, name="a.db"):
    return ArtifactStore(tmp_path / name)


def pub_basic(store, task, comps=None, **kw):
    args = {"title": "T", "summary": "S", "components": comps or [txt()]}
    args.update(kw)
    return store.publish(task, args)


def test_validate_normalizes_and_copies():
    src = [{"id": "a1", "type": "table", "data": {"columns": ["c"], "rows": []}}]
    out = validate_components(src)
    assert out[0]["title"] == ""
    assert out[0]["data"]["selectable"] is True
    out[0]["data"]["columns"].append("evil")
    assert src[0]["data"]["columns"] == ["c"]
    ch = validate_components(
        [{"id": "c1", "type": "chart", "data": {"labels": ["x"], "values": [1]}}]
    )
    assert ch[0]["data"]["style"] == "bar"


def test_all_types_and_limits():
    validate_components(all_types_comps())
    with pytest.raises(ArtifactInvalid):
        validate_components([{"id": "x", "type": "text", "data": {"text": ""}}])
    with pytest.raises(ArtifactInvalid):
        validate_components([{"id": "x", "type": "text", "data": {"text": "a" * 8001}}])
    with pytest.raises(ArtifactInvalid):
        validate_components([{"id": "x", "type": "status", "data": {"text": "a", "tone": "nope"}}])
    with pytest.raises(ArtifactInvalid):
        validate_components([{"id": "x", "type": "table", "data": {"columns": [], "rows": []}}])
    with pytest.raises(ArtifactInvalid):
        validate_components(
            [{"id": "x", "type": "chart", "data": {"labels": ["a"], "values": [1, 2]}}]
        )
    with pytest.raises(ArtifactInvalid):
        validate_components(
            [
                {
                    "id": "x",
                    "type": "number",
                    "data": {"label": "L", "value": 99, "min": 0, "max": 10},
                }
            ]
        )
    with pytest.raises(ArtifactInvalid):
        validate_components(
            [{"id": "x", "type": "select", "data": {"label": "L", "options": ["a"], "value": "z"}}]
        )
    with pytest.raises(ArtifactInvalid):
        validate_components([{"id": "x", "type": "ask", "data": {"label": "L", "message": ""}}])
    with pytest.raises(ArtifactInvalid):
        validate_components([])
    with pytest.raises(ArtifactInvalid):
        validate_components([txt("ok"), txt("ok")])


def test_unknown_fields_every_level():
    with pytest.raises(ArtifactInvalid):
        validate_components(
            [{"id": "a", "type": "text", "title": "", "data": {"text": "hi"}, "zzz": 1}]
        )
    with pytest.raises(ArtifactInvalid):
        validate_components([{"id": "a", "type": "text", "data": {"text": "hi", "zzz": 1}}])
    with pytest.raises(ArtifactInvalid):
        validate_components(
            [
                {
                    "id": "a",
                    "type": "diagram",
                    "data": {"nodes": [{"id": "n", "label": "L", "zzz": 1}], "edges": []},
                }
            ]
        )
    with pytest.raises(ArtifactInvalid):
        validate_components([{"id": "a", "type": "nope", "data": {}}])
    with pytest.raises(ArtifactInvalid):
        validate_components([{"id": "bad id!", "type": "text", "data": {"text": "hi"}}])


def test_duplicates_and_refs():
    with pytest.raises(ArtifactInvalid):
        validate_components([txt("d"), txt("d")])
    with pytest.raises(ArtifactInvalid):
        validate_components(
            [
                {
                    "id": "s",
                    "type": "select",
                    "data": {"label": "L", "options": ["a", "a"], "value": "a"},
                }
            ]
        )
    with pytest.raises(ArtifactInvalid):
        validate_components(
            [
                {
                    "id": "d",
                    "type": "diagram",
                    "data": {
                        "nodes": [{"id": "n", "label": "a"}, {"id": "n", "label": "b"}],
                        "edges": [],
                    },
                }
            ]
        )
    with pytest.raises(ArtifactInvalid):
        validate_components(
            [
                {
                    "id": "d",
                    "type": "diagram",
                    "data": {
                        "nodes": [{"id": "n", "label": "a"}],
                        "edges": [{"from": "n", "to": "missing"}],
                    },
                }
            ]
        )
    with pytest.raises(ArtifactInvalid):
        validate_components(
            [{"id": "s", "type": "select", "data": {"label": "L", "options": ["a"], "value": "b"}}]
        )


def test_inf_nan_bool_rejected():
    for bad in (float("inf"), float("-inf"), float("nan")):
        with pytest.raises(ArtifactInvalid):
            validate_components(
                [{"id": "c", "type": "chart", "data": {"labels": ["a"], "values": [bad]}}]
            )
        with pytest.raises(ArtifactInvalid):
            validate_components(
                [
                    {
                        "id": "n",
                        "type": "number",
                        "data": {"label": "L", "value": bad, "min": -1e12, "max": 1e12},
                    }
                ]
            )
    with pytest.raises(ArtifactInvalid):
        validate_components(
            [{"id": "c", "type": "chart", "data": {"labels": ["a"], "values": [True]}}]
        )
    with pytest.raises(ArtifactInvalid):
        validate_components(
            [
                {
                    "id": "n",
                    "type": "number",
                    "data": {"label": "L", "value": True, "min": 0, "max": 10},
                }
            ]
        )
    with pytest.raises(ArtifactInvalid):
        validate_components(
            [{"id": "t", "type": "table", "data": {"columns": ["c"], "rows": [[{"x": 1}]]}}]
        )
    # table bools are fine as scalars
    validate_components(
        [
            {
                "id": "t",
                "type": "table",
                "data": {"columns": ["c"], "rows": [[True], [None], [1.5], ["s"]]},
            }
        ]
    )
    with pytest.raises(ArtifactInvalid):
        validate_components(
            [
                {
                    "id": "t",
                    "type": "table",
                    "data": {"columns": ["c"], "rows": [[1.5]], "selectable": 1},
                }
            ]
        )


def test_source_urls():
    validate_components(
        [
            {
                "id": "s",
                "type": "source",
                "data": {"label": "L", "url": "https://example.com/x?q=1", "freshness": "static"},
            }
        ]
    )
    for bad in (
        "http://example.com/",
        "https://user:pass@example.com/",
        "https://example.com/\x01",
        "https://example.com/?token=abc",
        "https://example.com/?api_key=1",
    ):
        with pytest.raises(ArtifactInvalid):
            validate_components(
                [
                    {
                        "id": "s",
                        "type": "source",
                        "data": {"label": "L", "url": bad, "freshness": "static"},
                    }
                ]
            )
    with pytest.raises(ArtifactInvalid):
        validate_components(
            [
                {
                    "id": "s",
                    "type": "source",
                    "data": {"label": "L", "url": "https://example.com", "freshness": "live"},
                }
            ]
        )


def test_publish_basic(tmp_path):
    s = new_store(tmp_path)
    try:
        t = mk_task()
        env = pub_basic(s, t)
        assert env["revision"] == 1 and env["view_revision"] == 0
        assert env["session_id"] == t.session_id and env["task_id"] == t.task_id
        assert len(env["content_sha256"]) == 64
        assert env["view_state"] == {}
        got = s.get(
            env["artifact_id"], session_id=t.session_id, thread_id=t.thread_id, epoch=t.epoch
        )
        assert got == env
        # immutable: mutate caller copy must not affect store
        env["title"] = "mut"
        got2 = s.get(
            got["artifact_id"], session_id=t.session_id, thread_id=t.thread_id, epoch=t.epoch
        )
        assert got2["title"] == "T"
    finally:
        s.close()


def test_publish_validation(tmp_path):
    s = new_store(tmp_path)
    try:
        t = mk_task()
        with pytest.raises(ArtifactInvalid):
            s.publish(t, {"title": "T", "summary": "S", "components": [txt()], "extra": 1})
        with pytest.raises(ArtifactInvalid):
            s.publish(t, {"title": "", "summary": "S", "components": [txt()]})
        with pytest.raises(ArtifactInvalid):
            s.publish(t, {"title": "T", "summary": "S", "components": [txt()], "layout": "circle"})
        with pytest.raises(ArtifactInvalid):
            s.publish(t, {"title": "T", "summary": "S", "components": []})
        e = s.publish(t, {"title": "T", "summary": "S", "components": [txt()], "layout": "grid"})
        assert e["layout"] == "grid"
    finally:
        s.close()


def test_publish_48kib(tmp_path):
    s = new_store(tmp_path)
    try:
        t = mk_task()
        big = "a" * 8000
        comps = [{"id": f"t{i}", "type": "text", "data": {"text": big}} for i in range(7)]
        with pytest.raises(ArtifactInvalid):
            s.publish(t, {"title": "T", "summary": "S", "components": comps})
    finally:
        s.close()


def test_update_cas_and_identity(tmp_path):
    s = new_store(tmp_path)
    try:
        t = mk_task()
        env = pub_basic(s, t, [txt("a", "one")])
        env2 = s.publish(
            t,
            {
                "artifact_id": env["artifact_id"],
                "expected_revision": 1,
                "updates": [{"id": "a", "data": {"text": "two"}}],
            },
        )
        assert env2["revision"] == 2 and env2["content_sha256"] != env["content_sha256"]
        with pytest.raises(ArtifactConflict):
            s.publish(
                t,
                {
                    "artifact_id": env["artifact_id"],
                    "expected_revision": 1,
                    "updates": [{"id": "a", "data": {"text": "stale"}}],
                },
            )
        with pytest.raises(ArtifactInvalid):
            s.publish(
                t,
                {
                    "artifact_id": env["artifact_id"],
                    "expected_revision": 2,
                    "updates": [
                        {"id": "a", "data": {"text": "x"}},
                        {"id": "a", "data": {"text": "y"}},
                    ],
                },
            )
        with pytest.raises(ArtifactInvalid):
            s.publish(
                t,
                {
                    "artifact_id": env["artifact_id"],
                    "expected_revision": 2,
                    "updates": [{"id": "missing", "data": {"text": "x"}}],
                },
            )
        other = mk_task(sid=t.session_id, tid=t.thread_id, epoch=t.epoch, rev=t.objective_revision)
        with pytest.raises(ArtifactConflict):
            s.publish(
                other,
                {
                    "artifact_id": env["artifact_id"],
                    "expected_revision": 2,
                    "updates": [{"id": "a", "data": {"text": "cross"}}],
                },
            )
        old = mk_task(
            sid=t.session_id,
            tid=t.thread_id,
            epoch=t.epoch,
            rev=t.objective_revision + 1,
            task_id=t.task_id,
        )
        with pytest.raises(ArtifactConflict):
            s.publish(
                old,
                {
                    "artifact_id": env["artifact_id"],
                    "expected_revision": 2,
                    "updates": [{"id": "a", "data": {"text": "late"}}],
                },
            )
        changed_scope = mk_task(
            sid=t.session_id,
            tid=t.thread_id,
            epoch=t.epoch,
            rev=t.objective_revision,
            task_id=t.task_id,
            scope="public_synthetic",
        )
        with pytest.raises(ArtifactConflict, match="content scope changed"):
            s.publish(
                changed_scope,
                {
                    "artifact_id": env["artifact_id"],
                    "expected_revision": 2,
                    "updates": [{"id": "a", "data": {"text": "different disclosure scope"}}],
                },
            )
    finally:
        s.close()


def test_get_page_scope(tmp_path):
    s = new_store(tmp_path)
    try:
        t = mk_task(sid="s1", tid="t1")
        env = pub_basic(s, t)
        with pytest.raises(ArtifactNotFound):
            s.get(env["artifact_id"], session_id="wrong", thread_id="t1", epoch=1)
        with pytest.raises(ArtifactNotFound):
            s.get("nope", session_id="s1", thread_id="t1", epoch=1)
        pg = s.page(session_id="s1", thread_id="t1", epoch=1)
        assert len(pg["items"]) == 1 and pg["next_after"] > 0
        empty = s.page(session_id="s1", thread_id="t1", epoch=1, after=pg["next_after"])
        assert empty == {"items": [], "next_after": pg["next_after"]}
        with pytest.raises(ArtifactInvalid):
            s.page(session_id="s1", thread_id="t1", epoch=1, limit=21)
    finally:
        s.close()


def test_pagination_stable(tmp_path):
    s = new_store(tmp_path)
    try:
        t = mk_task()
        ids = [pub_basic(s, t, [txt(f"c{i}")])["artifact_id"] for i in range(5)]
        p1 = s.page(session_id=t.session_id, thread_id=t.thread_id, epoch=t.epoch, after=0, limit=2)
        assert len(p1["items"]) == 2 and p1["next_after"] is not None
        p2 = s.page(
            session_id=t.session_id,
            thread_id=t.thread_id,
            epoch=t.epoch,
            after=p1["next_after"],
            limit=2,
        )
        assert len(p2["items"]) == 2
        p3 = s.page(
            session_id=t.session_id,
            thread_id=t.thread_id,
            epoch=t.epoch,
            after=p2["next_after"],
            limit=2,
        )
        assert len(p3["items"]) == 1 and p3["next_after"] > p2["next_after"]
        assert [x["artifact_id"] for x in p1["items"] + p2["items"] + p3["items"]] == ids
    finally:
        s.close()


def _action_body(task, rev, cid, val, key):
    return {
        "session_id": task.session_id,
        "thread_id": task.thread_id,
        "epoch": task.epoch,
        "expected_revision": rev,
        "component_id": cid,
        "value": val,
        "idempotency_key": key,
    }


def test_actions_all_types(tmp_path):
    s = new_store(tmp_path)
    try:
        t = mk_task()
        comps = [
            {"id": "tb", "type": "table", "data": {"columns": ["a"], "rows": [["x"], ["y"]]}},
            {
                "id": "dg",
                "type": "diagram",
                "data": {
                    "nodes": [{"id": "n1", "label": "N1"}, {"id": "n2", "label": "N2"}],
                    "edges": [{"from": "n1", "to": "n2"}],
                },
            },
            {"id": "nb", "type": "number", "data": {"label": "L", "value": 5, "min": 0, "max": 10}},
            {
                "id": "se",
                "type": "select",
                "data": {"label": "L", "options": ["a", "b"], "value": "a"},
            },
            {"id": "tx", "type": "text", "data": {"text": "hi"}},
        ]
        env = s.publish(t, {"title": "T", "summary": "S", "components": comps})
        r1 = s.action(env["artifact_id"], _action_body(t, 1, "tb", 1, "k1"))
        assert r1["view_state"]["tb"] == 1 and r1["view_revision"] == 1 and r1["revision"] == 1
        r2 = s.action(env["artifact_id"], _action_body(t, 1, "dg", "n2", "k2"))
        assert r2["view_state"]["dg"] == "n2" and r2["view_revision"] == 2
        r3 = s.action(env["artifact_id"], _action_body(t, 1, "nb", 7, "k3"))
        assert r3["view_state"]["nb"] == 7
        r4 = s.action(env["artifact_id"], _action_body(t, 1, "se", "b", "k4"))
        assert r4["view_state"]["se"] == "b"
        assert r4["content_sha256"] == env["content_sha256"]
        with pytest.raises(ArtifactInvalid):
            s.action(env["artifact_id"], _action_body(t, 1, "tx", "x", "k5"))
        with pytest.raises(ArtifactInvalid):
            s.action(env["artifact_id"], _action_body(t, 1, "tb", 99, "k6"))
        with pytest.raises(ArtifactInvalid):
            s.action(env["artifact_id"], _action_body(t, 1, "dg", "nope", "k7"))
        with pytest.raises(ArtifactInvalid):
            s.action(env["artifact_id"], _action_body(t, 1, "nb", 99, "k8"))
        with pytest.raises(ArtifactInvalid):
            s.action(env["artifact_id"], _action_body(t, 1, "se", "zzz", "k9"))
        with pytest.raises(ArtifactInvalid):
            s.action(env["artifact_id"], _action_body(t, 1, "nb", True, "k10"))
    finally:
        s.close()


def test_action_replay_conflict_stale(tmp_path):
    s = new_store(tmp_path)
    try:
        t = mk_task()
        env = s.publish(
            t,
            {
                "title": "T",
                "summary": "S",
                "components": [
                    {
                        "id": "se",
                        "type": "select",
                        "data": {"label": "L", "options": ["a", "b"], "value": "a"},
                    }
                ],
            },
        )
        b1 = _action_body(t, 1, "se", "b", "same")
        r1 = s.action(env["artifact_id"], copy.deepcopy(b1))
        b2 = _action_body(t, 1, "se", "a", "other")
        r2 = s.action(env["artifact_id"], copy.deepcopy(b2))
        assert r2["view_state"]["se"] == "a"
        replay = s.action(env["artifact_id"], copy.deepcopy(b1))
        assert replay == r1
        assert replay["view_state"]["se"] == "b"
        with pytest.raises(ArtifactConflict):
            s.action(env["artifact_id"], {**copy.deepcopy(b1), "value": "a"})
        with pytest.raises(ArtifactConflict):
            s.action(env["artifact_id"], _action_body(t, 999, "se", "a", "stale"))
        with pytest.raises(ArtifactInvalid):
            bad = _action_body(t, 1, "se", "b", "x")
            bad["unknown"] = 1
            s.action(env["artifact_id"], bad)
        s.publish(
            t,
            {
                "artifact_id": env["artifact_id"],
                "expected_revision": 1,
                "updates": [
                    {"id": "se", "data": {"label": "L", "options": ["a", "c"], "value": "a"}}
                ],
            },
        )
        # The receipt is the original response even when its action is no
        # longer valid against the new content revision.
        assert s.action(env["artifact_id"], copy.deepcopy(b1)) == r1
    finally:
        s.close()


def test_viewstate_prune_and_persist(tmp_path):
    s = new_store(tmp_path)
    try:
        t = mk_task()
        env = s.publish(
            t,
            {
                "title": "T",
                "summary": "S",
                "components": [
                    {
                        "id": "se",
                        "type": "select",
                        "data": {"label": "L", "options": ["a", "b"], "value": "a"},
                    }
                ],
            },
        )
        s.action(env["artifact_id"], _action_body(t, 1, "se", "b", "k1"))
        got = s.get(
            env["artifact_id"], session_id=t.session_id, thread_id=t.thread_id, epoch=t.epoch
        )
        assert got["view_state"]["se"] == "b"
        env2 = s.publish(
            t,
            {
                "artifact_id": env["artifact_id"],
                "expected_revision": 1,
                "updates": [
                    {"id": "se", "data": {"label": "L", "options": ["a", "c"], "value": "a"}}
                ],
            },
        )
        assert env2["view_state"] == {}
        # valid selection survives
        env3 = pub_basic(s, mk_task(), [txt("z")])
        assert env3["view_state"] == {}
    finally:
        s.close()


def test_reopen(tmp_path):
    p = tmp_path / "r.db"
    s = ArtifactStore(p)
    try:
        t = mk_task()
        env = pub_basic(s, t)
        aid = env["artifact_id"]
    finally:
        s.close()
    s2 = ArtifactStore(p)
    try:
        got = s2.get(aid, session_id=t.session_id, thread_id=t.thread_id, epoch=t.epoch)
        assert got["title"] == "T"
    finally:
        s2.close()


def test_verify(tmp_path):
    s = new_store(tmp_path)
    try:
        t = mk_task()
        env = pub_basic(s, t)
        good = mk_ev(env["artifact_id"], env["content_sha256"], t.objective_revision)
        assert s.verify(good, t) is True
        assert s.verify(mk_ev(env["artifact_id"], "0" * 64, t.objective_revision), t) is False
        assert (
            s.verify(
                SimpleNamespace(
                    kind="artifact", path="bad", sha256=env["content_sha256"], objective_revision=1
                ),
                t,
            )
            is False
        )
        assert (
            s.verify(
                mk_ev(env["artifact_id"], env["content_sha256"], t.objective_revision, kind="file"),
                t,
            )
            is False
        )
        other = mk_task(sid=t.session_id, tid=t.thread_id, epoch=t.epoch)
        assert s.verify(good, other) is False
        assert s.verify(mk_ev("missing", "0" * 64, 1), t) is False
        # tampered envelope hash must not verify
        tampered = mk_ev(env["artifact_id"], "f" * 64, t.objective_revision)
        assert s.verify(tampered, t) is False
    finally:
        s.close()


def test_quotas_small(tmp_path, monkeypatch):
    s = new_store(tmp_path, "q.db")
    try:
        monkeypatch.setattr(artmod, "MAX_PER_TASK", 2)
        t = mk_task()
        pub_basic(s, t, [txt("a")])
        pub_basic(s, t, [txt("b")])
        with pytest.raises((ArtifactConflict, ArtifactInvalid)):
            pub_basic(s, t, [txt("c")])
    finally:
        s.close()
    s2 = new_store(tmp_path, "q2.db")
    try:
        monkeypatch.setattr(artmod, "MAX_ACTION_KEYS", 2)
        t = mk_task()
        env = s2.publish(
            t,
            {
                "title": "T",
                "summary": "S",
                "components": [
                    {
                        "id": "se",
                        "type": "select",
                        "data": {"label": "L", "options": ["a", "b"], "value": "a"},
                    }
                ],
            },
        )
        s2.action(env["artifact_id"], _action_body(t, 1, "se", "a", "k1"))
        s2.action(env["artifact_id"], _action_body(t, 1, "se", "b", "k2"))
        with pytest.raises((ArtifactConflict, ArtifactInvalid)):
            s2.action(env["artifact_id"], _action_body(t, 1, "se", "a", "k3"))
    finally:
        s2.close()
    s3 = new_store(tmp_path, "q3.db")
    try:
        monkeypatch.setattr(artmod, "MAX_DB_ARTIFACTS", 2)
        pub_basic(s3, mk_task(task_id="t-1"), [txt("a")])
        pub_basic(s3, mk_task(task_id="t-2"), [txt("b")])
        with pytest.raises((ArtifactConflict, ArtifactInvalid)):
            pub_basic(s3, mk_task(task_id="t-3"), [txt("c")])
    finally:
        s3.close()


@pytest.mark.parametrize("bad", ["\x00", "\r", "\x7f", "\x85", "\ud800", "\udfff"])
def test_text_control_and_surrogate_policy_matches_renderer(tmp_path, bad):
    with pytest.raises(ArtifactInvalid):
        validate_components([txt(text="prefix" + bad)])
    with pytest.raises(ArtifactInvalid):
        validate_components(
            [
                {
                    "id": "table",
                    "type": "table",
                    "data": {
                        "columns": ["Synthetic"],
                        "rows": [[bad]],
                    },
                }
            ]
        )
    store = new_store(tmp_path)
    try:
        with pytest.raises(ArtifactInvalid):
            pub_basic(store, mk_task(), title="Synthetic" + bad)
    finally:
        store.close()


def test_valid_unicode_and_presentation_bounds(tmp_path):
    store = new_store(tmp_path)
    task = mk_task()
    try:
        env = pub_basic(
            store,
            task,
            [
                txt(text="Synthetic 🌱\n\tcomparison"),
                {
                    "id": "diagram",
                    "type": "diagram",
                    "data": {
                        "nodes": [{"id": "arbitrary node ID 🌱", "label": "Node"}],
                        "edges": [],
                    },
                },
                {
                    "id": "table",
                    "type": "table",
                    "data": {
                        "columns": ["Column"],
                        "rows": [[1]],
                        "selectable": False,
                    },
                },
            ],
        )
        with pytest.raises(ArtifactInvalid):
            store.action(env["artifact_id"], _action_body(task, 1, "table", 0, "disabled"))
        with pytest.raises(ArtifactInvalid):
            store.action(env["artifact_id"], _action_body(task, 1, "table", None, "disabled-null"))
        store.action(
            env["artifact_id"], _action_body(task, 1, "diagram", "arbitrary node ID 🌱", "node")
        )
        with pytest.raises(ArtifactInvalid):
            validate_components(
                [
                    {
                        "id": "source",
                        "type": "source",
                        "data": {
                            "label": "Synthetic",
                            "url": "https://example.com",
                            "freshness": "static",
                            "observed_at": 9_007_199_254_740_992,
                        },
                    }
                ]
            )
    finally:
        store.close()


def test_unexpected_sql_error_rolls_back_and_close_never_commits_partial_work(tmp_path):
    path = tmp_path / "rollback.sqlite3"
    store = ArtifactStore(path)
    task = mk_task()
    store._conn.execute(
        "CREATE TRIGGER fail_insert BEFORE INSERT ON artifacts BEGIN SELECT RAISE(FAIL, 'synthetic failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        pub_basic(store, task)
    assert not store._conn.in_transaction
    assert (
        store.page(session_id=task.session_id, thread_id=task.thread_id, epoch=task.epoch)["items"]
        == []
    )
    store._conn.execute("DROP TRIGGER fail_insert")
    envelope = pub_basic(store, task)
    store._conn.execute("BEGIN")
    store._conn.execute("UPDATE artifacts SET summary='uncommitted synthetic summary'")
    store.close()
    reopened = ArtifactStore(path)
    try:
        assert (
            reopened.get(
                envelope["artifact_id"],
                session_id=task.session_id,
                thread_id=task.thread_id,
                epoch=task.epoch,
            )["summary"]
            == "S"
        )
    finally:
        reopened.close()


def test_two_native_connections_compare_and_swap_one_content_revision(tmp_path):
    path = tmp_path / "concurrent.sqlite3"
    stores = [ArtifactStore(path), ArtifactStore(path)]
    task = mk_task()
    envelope = pub_basic(stores[0], task)

    def update(index):
        try:
            return stores[index].publish(
                task,
                {
                    "artifact_id": envelope["artifact_id"],
                    "expected_revision": 1,
                    "updates": [{"id": "t1", "data": {"text": str(index)}}],
                },
            )
        except ArtifactConflict:
            return "conflict"

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(update, [0, 1]))
        assert results.count("conflict") == 1
        assert [result for result in results if isinstance(result, dict)][0]["revision"] == 2
    finally:
        for store in stores:
            store.close()


def test_cyclic_or_deep_publication_is_rejected_before_storage(tmp_path):
    store = new_store(tmp_path)
    cyclic = {}
    cyclic["cycle"] = cyclic
    try:
        with pytest.raises(ArtifactInvalid):
            store.publish(mk_task(), cyclic)
        deep = {"nested": {}}
        cursor = deep["nested"]
        for _ in range(15):
            cursor["nested"] = {}
            cursor = cursor["nested"]
        with pytest.raises(ArtifactInvalid):
            store.publish(mk_task(), deep)
        many_values = [
            {
                "id": f"table{index}",
                "type": "table",
                "data": {
                    "columns": ["A", "B", "C"],
                    "rows": [[0, 0, 0] for _ in range(100)],
                },
            }
            for index in range(24)
        ]
        with pytest.raises(ArtifactInvalid, match="structural bounds"):
            pub_basic(store, mk_task(), many_values)
        assert not store._conn.in_transaction
    finally:
        store.close()
