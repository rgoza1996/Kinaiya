"""Optional owner-curated Converse DAG; no journal writes or inference.

The Session Store remains the sole database/lock owner. Reconciliation facts are
committed before a subsequent CAS refusal. Content pins/edges are immutable;
forgetting redacts in place and retains only bounded identifiers/digests.
"""

import hashlib
import json
import sqlite3
from functools import wraps
from uuid import uuid4

from packages.contracts.memory import (
    MemoryBinding,
    MemoryCapture,
    MemoryCaptureResult,
    MemoryConfigure,
    MemoryForget,
    MemoryForgetResult,
    MemoryInspect,
    MemoryNode,
    MemoryQuery,
    MemoryQueryResult,
    MemoryState,
)
from packages.contracts.models import MAX_PLAYBACK_GENERATION

MAX = MAX_PLAYBACK_GENERATION
BINDING = ("session_id", "thread_id", "epoch")
# Bound Python allocations before fetching journal payloads, including all
# correction rows. Oversized payloads remain transcript data, never memory.
SOURCE_COLUMNS = """sequence,event_id,session_id,thread_id,epoch,kind,writer,
    CASE WHEN length(CAST(value AS BLOB))<=262144 THEN value ELSE NULL END AS value"""
NODE_COLUMNS = """session_id,thread_id,epoch,
    CASE WHEN length(CAST(node_id AS BLOB))<=128 THEN node_id END AS node_id,
    CASE WHEN typeof(ordinal)='integer' THEN ordinal END AS ordinal,
    CASE WHEN length(CAST(kind AS BLOB))<=16 THEN kind END AS kind,
    CASE WHEN length(CAST(state AS BLOB))<=16 THEN state END AS state,
    CASE WHEN length(CAST(source_event_id AS BLOB))<=128 THEN source_event_id END AS source_event_id,
    CASE WHEN typeof(source_revision)='integer' THEN source_revision END AS source_revision,
    CASE WHEN typeof(source_sequence)='integer' THEN source_sequence END AS source_sequence,
    CASE WHEN length(CAST(body AS BLOB))<=2048 THEN body END AS body,
    CASE WHEN length(CAST(fact_key AS BLOB))<=128 THEN fact_key END AS fact_key,
    CASE WHEN length(CAST(value_json AS BLOB))<=2048 THEN value_json END AS value_json,
    CASE WHEN (body IS NOT NULL AND (typeof(body)!='text' OR length(CAST(body AS BLOB))>2048))
       OR (fact_key IS NOT NULL AND (typeof(fact_key)!='text' OR length(CAST(fact_key AS BLOB))>128))
       OR (value_json IS NOT NULL AND (typeof(value_json)!='text' OR length(CAST(value_json AS BLOB))>2048))
       OR ((fact_key IS NULL)!=(value_json IS NULL))
    THEN 1 ELSE 0 END AS invalid_content"""


class MemoryError(ValueError):
    """Fixed public error; never attach submitted or retained content."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class _Corrupt(ValueError):
    pass


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _constant(_):
    raise ValueError


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _decode(value):
    return json.loads(value, object_pairs_hook=_pairs, parse_constant=_constant)


def _integer(value, minimum=0):
    return type(value) is int and minimum <= value <= MAX


def _storage(method):
    @wraps(method)
    def call(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except sqlite3.Error:
            raise MemoryError(503, "Memory storage unavailable") from None

    return call


class MemoryStore:
    def __init__(self, store):
        self.store, self.db = store, store.db
        with store.lock:
            if self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_schema'"
            ).fetchone():
                if [r[0] for r in self.db.execute("SELECT version FROM memory_schema")] != [1]:
                    raise MemoryError(409, "Unsupported memory schema")
            try:
                self.db.executescript("""
                    CREATE TABLE IF NOT EXISTS memory_schema(version INTEGER NOT NULL);
                    INSERT INTO memory_schema SELECT 1
                        WHERE NOT EXISTS(SELECT 1 FROM memory_schema);
                    CREATE TABLE IF NOT EXISTS memory_namespaces(
                        session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
                        thread_id TEXT NOT NULL, epoch INTEGER NOT NULL,
                        enabled INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 0,
                        status TEXT NOT NULL DEFAULT 'ready', capture_floor INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY(session_id,thread_id,epoch));
                    CREATE TABLE IF NOT EXISTS memory_nodes(
                        node_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                        thread_id TEXT NOT NULL, epoch INTEGER NOT NULL, ordinal INTEGER NOT NULL,
                        kind TEXT NOT NULL, source_event_id TEXT NOT NULL,
                        source_revision INTEGER NOT NULL, source_sequence INTEGER NOT NULL,
                        body TEXT, fact_key TEXT, value_json TEXT, state TEXT NOT NULL,
                        FOREIGN KEY(session_id,thread_id,epoch) REFERENCES memory_namespaces
                            ON DELETE CASCADE,
                        UNIQUE(session_id,thread_id,epoch,ordinal));
                    CREATE INDEX IF NOT EXISTS memory_node_namespace
                        ON memory_nodes(session_id,thread_id,epoch,ordinal);
                    CREATE TABLE IF NOT EXISTS memory_edges(
                        parent_id TEXT NOT NULL REFERENCES memory_nodes(node_id) ON DELETE CASCADE,
                        child_id TEXT NOT NULL REFERENCES memory_nodes(node_id) ON DELETE CASCADE,
                        PRIMARY KEY(parent_id,child_id));
                    CREATE INDEX IF NOT EXISTS memory_edge_child ON memory_edges(child_id,parent_id);
                    CREATE TABLE IF NOT EXISTS memory_refutations(
                        node_id TEXT NOT NULL REFERENCES memory_nodes(node_id) ON DELETE CASCADE,
                        cause_id TEXT NOT NULL, reason TEXT NOT NULL,
                        PRIMARY KEY(node_id,cause_id,reason));
                    CREATE TABLE IF NOT EXISTS memory_receipts(
                        session_id TEXT NOT NULL, thread_id TEXT NOT NULL, epoch INTEGER NOT NULL,
                        idem TEXT NOT NULL, operation TEXT NOT NULL, digest TEXT NOT NULL,
                        node_id TEXT NOT NULL REFERENCES memory_nodes(node_id) ON DELETE CASCADE,
                        revision INTEGER NOT NULL, erased_ids TEXT,
                        FOREIGN KEY(session_id,thread_id,epoch) REFERENCES memory_namespaces
                            ON DELETE CASCADE,
                        PRIMARY KEY(session_id,thread_id,epoch,idem),
                        UNIQUE(node_id,operation));
                    CREATE INDEX IF NOT EXISTS entries_memory_revision_target ON entries(
                        json_extract(value,'$.payload.target_event_id'),sequence)
                        WHERE kind='conversation.revision';
                """)
                self.db.commit()
            except sqlite3.Error:
                self.db.rollback()
                raise MemoryError(503, "Memory storage unavailable") from None

    def _request(self, model, value):
        try:
            return model.model_validate(
                value.model_dump() if hasattr(value, "model_dump") else value
            )
        except (ValueError, TypeError):
            raise MemoryError(422, "Invalid memory request") from None

    def _binding(self, request):
        binding = {key: getattr(request, key) for key in BINDING}
        try:
            session = self.store.session(binding["session_id"])
        except KeyError:
            raise MemoryError(404, "Unknown memory reference") from None
        except ValueError:
            raise MemoryError(409, "Memory requires current Converse scope") from None
        if session.channel != "converse" or (session.thread_id, session.epoch) != (
            binding["thread_id"],
            binding["epoch"],
        ):
            raise MemoryError(409, "Memory requires current Converse scope")
        return binding

    def _where(self, binding):
        return tuple(binding[key] for key in BINDING)

    def _namespace(self, binding):
        row = self.db.execute(
            "SELECT * FROM memory_namespaces WHERE session_id=? AND thread_id=? AND epoch=?",
            self._where(binding),
        ).fetchone()
        return (
            dict(row)
            if row
            else {**binding, "enabled": 0, "revision": 0, "status": "ready", "capture_floor": 0}
        )

    def _ensure(self, binding):
        self.db.execute(
            "INSERT OR IGNORE INTO memory_namespaces(session_id,thread_id,epoch) VALUES(?,?,?)",
            self._where(binding),
        )

    def _state(self, binding):
        row = self._namespace(binding)
        if type(row["enabled"]) is not int or row["enabled"] not in (0, 1):
            raise MemoryError(503, "Memory state unavailable")
        count = self.db.execute(
            "SELECT count(*) FROM memory_nodes WHERE session_id=? AND thread_id=? AND epoch=?",
            self._where(binding),
        ).fetchone()[0]
        try:
            return MemoryState(
                **binding,
                enabled=bool(row["enabled"]),
                revision=row["revision"],
                status=row["status"],
                node_count=count,
            )
        except ValueError:
            raise MemoryError(503, "Memory state unavailable") from None

    def _advance(self, binding, *, disabling=False):
        current = self._namespace(binding)["revision"]
        if not _integer(current) or current >= (MAX if disabling else MAX - 1):
            raise MemoryError(409, "Memory revision exhausted")
        self._ensure(binding)
        self.db.execute(
            "UPDATE memory_namespaces SET revision=revision+1 WHERE session_id=? AND thread_id=? AND epoch=?",
            self._where(binding),
        )
        return current + 1

    def _cas(self, binding, expected):
        if self._namespace(binding)["revision"] != expected:
            raise MemoryError(409, "Memory revision conflict")

    def _node(self, binding, node, parents):
        try:
            if node["invalid_content"]:
                raise _Corrupt
            if (
                node["value_json"] is not None
                and _json(_decode(node["value_json"])) != node["value_json"]
            ):
                raise _Corrupt
            return MemoryNode(
                **binding,
                **{
                    k: node[k]
                    for k in (
                        "node_id",
                        "ordinal",
                        "kind",
                        "source_event_id",
                        "source_revision",
                        "source_sequence",
                        "body",
                        "state",
                    )
                },
                fact=None
                if node["fact_key"] is None
                else {"key": node["fact_key"], "value": _decode(node["value_json"])},
                prerequisite_ids=parents,
                content_scope="local_only",
                provenance="owner_curated",
            )
        except (ValueError, TypeError, KeyError):
            raise _Corrupt from None

    def _load(self, binding):
        ns = self._namespace(binding)
        if (
            type(ns["enabled"]) is not int
            or ns["enabled"] not in (0, 1)
            or not _integer(ns["revision"])
            or not _integer(ns["capture_floor"])
            or ns["status"] not in {"ready", "quarantined"}
        ):
            raise _Corrupt
        rows = self.db.execute(
            f"SELECT {NODE_COLUMNS} FROM memory_nodes WHERE session_id=? AND thread_id=? AND epoch=? ORDER BY ordinal LIMIT 257",
            self._where(binding),
        ).fetchall()
        if len(rows) > 256:
            raise _Corrupt
        nodes = {r["node_id"]: dict(r) for r in rows}
        parents = {key: [] for key in nodes}
        children = {key: [] for key in nodes}
        # Keep the bounded namespace outermost and limit each indexed direction
        # before deduplication. UNION DISTINCT would materialize all corrupt
        # cross-namespace fanout before its result LIMIT took effect.
        incoming = self.db.execute(
            """SELECT
                CASE WHEN typeof(e.parent_id)='text' AND length(CAST(e.parent_id AS BLOB))<=128
                    THEN e.parent_id END AS parent_id,
                CASE WHEN typeof(e.child_id)='text' AND length(CAST(e.child_id AS BLOB))<=128
                    THEN e.child_id END AS child_id
                FROM memory_nodes n INDEXED BY memory_node_namespace
                CROSS JOIN memory_edges e INDEXED BY memory_edge_child
                WHERE n.session_id=? AND n.thread_id=? AND n.epoch=?
                AND e.child_id=n.node_id LIMIT 2049""",
            self._where(binding),
        ).fetchall()
        outgoing = self.db.execute(
            """SELECT
                CASE WHEN typeof(e.parent_id)='text' AND length(CAST(e.parent_id AS BLOB))<=128
                    THEN e.parent_id END AS parent_id,
                CASE WHEN typeof(e.child_id)='text' AND length(CAST(e.child_id AS BLOB))<=128
                    THEN e.child_id END AS child_id
                FROM memory_nodes n INDEXED BY memory_node_namespace
                CROSS JOIN memory_edges e INDEXED BY sqlite_autoindex_memory_edges_1
                WHERE n.session_id=? AND n.thread_id=? AND n.epoch=?
                AND e.parent_id=n.node_id LIMIT 2049""",
            self._where(binding),
        ).fetchall()
        if len(incoming) > 2048 or len(outgoing) > 2048:
            raise _Corrupt
        edges = {tuple(row) for row in incoming} | {tuple(row) for row in outgoing}
        if len(edges) > 2048 or [n["ordinal"] for n in nodes.values()] != list(
            range(1, len(nodes) + 1)
        ):
            raise _Corrupt
        for edge in edges:
            parent, child = edge
            if (
                parent not in nodes
                or child not in nodes
                or nodes[parent]["ordinal"] >= nodes[child]["ordinal"]
            ):
                raise _Corrupt
            parents[child].append(parent)
            children[parent].append(child)
        depth = {}
        for key, node in nodes.items():
            parents[key].sort(key=lambda k: (nodes[k]["ordinal"], k))
            if len(parents[key]) > 8 or len(set(parents[key])) != len(parents[key]):
                raise _Corrupt
            depth[key] = 1 + max((depth[p] for p in parents[key]), default=0)
            if depth[key] > 16:
                raise _Corrupt
            model = self._node(binding, node, parents[key])
            if model.state == "active" and any(nodes[p]["state"] != "active" for p in parents[key]):
                raise _Corrupt
            if node["state"] == "forgotten" and any(
                node[k] is not None for k in ("body", "fact_key", "value_json")
            ):
                raise _Corrupt
        return nodes, parents, children, depth

    def _source(self, binding, source_id):
        """Validate native attribution plus every correction, never Store.resolve."""
        row = self.db.execute(
            f"SELECT {SOURCE_COLUMNS} FROM entries WHERE event_id=?", (source_id,)
        ).fetchone()
        if row is None:
            return None
        try:
            if row["value"] is None:
                return None
            original = self._entry(binding, row)
            payload = original["payload"]
            revision = payload.get("revision", 1)
            if (
                row["kind"] != "conversation.user"
                or original["origin"] != "authenticated-user"
                or payload.get("final") is not True
                or not _integer(revision, 1)
            ):
                return None
            sequence = row["sequence"]
            changes = self.db.execute(
                f"""SELECT {SOURCE_COLUMNS} FROM entries WHERE kind='conversation.revision'
                AND json_extract(value,'$.payload.target_event_id')=? ORDER BY sequence LIMIT 33""",
                (source_id,),
            ).fetchall()
            if len(changes) > 32:
                return None
            for change in changes:
                corrected = self._entry(binding, change)
                p = corrected["payload"]
                if (
                    p.get("target_event_id") != source_id
                    or not _integer(p.get("revision"), 1)
                    or p["revision"] != revision + 1
                    or change["sequence"] <= sequence
                ):
                    return None
                revision, sequence = p["revision"], change["sequence"]
            return revision, sequence
        except (ValueError, TypeError, KeyError, UnicodeError):
            return None

    def _entry(self, binding, row):
        if row["value"] is None:
            raise ValueError
        value = _decode(row["value"])
        if row["writer"] not in {"owner", "converse"} or any(row[k] != binding[k] for k in BINDING):
            raise ValueError
        for key in ("event_id", "sequence", "session_id", "thread_id", "epoch", "kind"):
            if value[key] != row[key] or type(value[key]) is not type(row[key]):
                raise ValueError
        if not _integer(row["sequence"], 1) or value.get("schema_version") != "1.0":
            raise ValueError
        expected = (
            "authenticated-user"
            if row["kind"] == "conversation.user" or row["writer"] == "owner"
            else "converse"
        )
        if (
            value.get("origin") != expected
            or not isinstance(value.get("payload"), dict)
            or not isinstance(value["payload"].get("text"), str)
        ):
            raise ValueError
        value["payload"]["text"].encode("utf-8", errors="strict")
        return value

    def _closure(self, children, roots):
        seen, pending = set(), list(roots)
        while pending:
            key = pending.pop()
            if key not in seen:
                seen.add(key)
                pending.extend(children[key])
        return seen

    def _invalidate(self, graph, roots, cause, reason):
        nodes, _, children, _ = graph
        changed = False
        for key in self._closure(children, roots):
            node = nodes[key]
            if node["state"] == "active":
                state = "tainted" if node["kind"] == "observation" else "invalidated"
                self.db.execute("UPDATE memory_nodes SET state=? WHERE node_id=?", (state, key))
                node["state"] = state
                changed = True
            if node["state"] != "forgotten":
                self.db.execute(
                    "INSERT OR IGNORE INTO memory_refutations VALUES(?,?,?)", (key, cause, reason)
                )
        return changed

    def _quarantine(self, binding):
        with self.db:
            self._ensure(binding)
            ns = self._namespace(binding)
            if ns["status"] != "quarantined":
                if _integer(ns["revision"]) and ns["revision"] < MAX - 1:
                    self._advance(binding)
                self.db.execute(
                    "UPDATE memory_namespaces SET status='quarantined' WHERE session_id=? AND thread_id=? AND epoch=?",
                    self._where(binding),
                )

    def _reconcile(self, binding):
        if self._namespace(binding)["status"] == "quarantined":
            raise MemoryError(409, "Memory namespace quarantined")
        try:
            graph = self._load(binding)
            invalid = []
            cache = {}
            for node in graph[0].values():
                key = node["source_event_id"]
                if key not in cache:
                    cache[key] = self._source(binding, key)
                if cache[key] != (node["source_revision"], node["source_sequence"]):
                    invalid.append(node["node_id"])
            with self.db:
                if any(
                    graph[0][key]["state"] == "active" for key in self._closure(graph[2], invalid)
                ):
                    self._advance(binding)
                    for key in invalid:
                        self._invalidate(
                            graph, [key], graph[0][key]["source_event_id"], "source_changed"
                        )
            return graph
        except (_Corrupt, sqlite3.Error, MemoryError):
            self._quarantine(binding)
            raise MemoryError(409, "Memory namespace quarantined") from None

    def _available(self, binding):
        if not self._namespace(binding)["enabled"]:
            raise MemoryError(409, "Converse memory is disabled")
        return self._reconcile(binding)

    def _receipt(self, binding, request, operation):
        digest = hashlib.sha256(_json(request.model_dump()).encode("utf-8")).hexdigest()
        row = self.db.execute(
            "SELECT * FROM memory_receipts WHERE session_id=? AND thread_id=? AND epoch=? AND idem=?",
            (*self._where(binding), request.idempotency_key),
        ).fetchone()
        if row:
            if row["digest"] != digest or row["operation"] != operation:
                raise MemoryError(409, "Memory idempotency conflict")
            if row["revision"] != self._namespace(binding)["revision"]:
                raise MemoryError(409, "Memory replay superseded")
        return row, digest

    def _save(self, binding, request, operation, digest, node_id, revision, erased=None):
        self.db.execute(
            "INSERT INTO memory_receipts VALUES(?,?,?,?,?,?,?,?,?)",
            (
                *self._where(binding),
                request.idempotency_key,
                operation,
                digest,
                node_id,
                revision,
                _json(erased) if erased is not None else None,
            ),
        )

    def _save_erasure(self, binding, request, digest, revision, erased):
        try:
            self._save(binding, request, "forget", digest, request.node_id, revision, erased)
        except sqlite3.IntegrityError as error:
            # A retained receipt claiming this node was erased while content
            # still exists is persisted corruption, not a reason to undo an
            # owner's erasure. Only that exact UNIQUE collision is tolerated;
            # trigger, FK and other write failures retain atomic rollback.
            if (
                error.sqlite_errorcode != sqlite3.SQLITE_CONSTRAINT_UNIQUE
                or not self.db.in_transaction
                or self.db.execute(
                    "SELECT 1 FROM memory_receipts WHERE node_id=? AND operation='forget'",
                    (request.node_id,),
                ).fetchone()
                is None
            ):
                raise
            self.db.execute(
                "UPDATE memory_namespaces SET status='quarantined' WHERE session_id=? AND thread_id=? AND epoch=?",
                self._where(binding),
            )
            return False
        return True

    @_storage
    def state(self, binding):
        request = self._request(MemoryBinding, binding)
        with self.store.lock:
            return self._state(self._binding(request))

    @_storage
    def configure(self, request):
        request = self._request(MemoryConfigure, request)
        with self.store.lock:
            binding = self._binding(request)
            if request.enabled:
                self._reconcile(binding)
            self._cas(binding, request.expected_revision)
            current = self._namespace(binding)
            if bool(current["enabled"]) != request.enabled:
                with self.db:
                    self._advance(binding, disabling=not request.enabled)
                    self.db.execute(
                        "UPDATE memory_namespaces SET enabled=? WHERE session_id=? AND thread_id=? AND epoch=?",
                        (int(request.enabled), *self._where(binding)),
                    )
            return self._state(binding)

    @_storage
    def capture(self, request):
        request = self._request(MemoryCapture, request)
        with self.store.lock:
            binding = self._binding(request)
            graph = self._available(binding)
            prior, digest = self._receipt(binding, request, "capture")
            if prior:
                return MemoryCaptureResult(
                    state=self._state(binding),
                    node=self._node(
                        binding, graph[0][prior["node_id"]], graph[1][prior["node_id"]]
                    ),
                )
            self._cas(binding, request.expected_revision)
            nodes, parents, _, depth = graph
            if (
                len(nodes) >= 256
                or sum(len(v) for v in parents.values()) + len(request.prerequisite_ids) > 2048
            ):
                raise MemoryError(409, "Memory node or edge capacity reached")
            pin = self._source(binding, request.source_event_id)
            if pin is None or pin[0] != request.source_revision:
                raise MemoryError(409, "Memory source is unavailable")
            ns = self._namespace(binding)
            if pin[1] <= ns["capture_floor"] or any(
                n["source_event_id"] == request.source_event_id and n["state"] == "forgotten"
                for n in nodes.values()
            ):
                raise MemoryError(409, "Memory source was forgotten")
            if any(
                key not in nodes or nodes[key]["state"] != "active"
                for key in request.prerequisite_ids
            ):
                raise MemoryError(409, "Memory prerequisite unavailable")
            if 1 + max((depth[p] for p in request.prerequisite_ids), default=0) > 16:
                raise MemoryError(409, "Memory depth limit reached")
            fact = request.fact
            typed = _json(fact.value) if fact is not None else None
            observations = [
                n
                for n in nodes.values()
                if n["kind"] == "observation" and fact is not None and n["fact_key"] == fact.key
            ]
            if request.kind == "observation" and any(
                n["source_sequence"] >= pin[1] for n in observations
            ):
                raise MemoryError(409, "Memory observation source is not newer")
            if request.kind == "assertion" and any(
                n["source_sequence"] >= pin[1] and n["value_json"] != typed for n in observations
            ):
                raise MemoryError(409, "Memory assertion is contradicted")
            node_id = str(uuid4())
            with self.db:
                revision = self._advance(binding)
                self.db.execute(
                    "INSERT INTO memory_nodes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        node_id,
                        *self._where(binding),
                        len(nodes) + 1,
                        request.kind,
                        request.source_event_id,
                        request.source_revision,
                        pin[1],
                        request.body,
                        fact.key if fact else None,
                        typed,
                        "active",
                    ),
                )
                self.db.executemany(
                    "INSERT INTO memory_edges VALUES(?,?)",
                    [(p, node_id) for p in request.prerequisite_ids],
                )
                if request.kind == "observation":
                    roots = [
                        n["node_id"]
                        for n in nodes.values()
                        if n["kind"] == "assertion"
                        and n["state"] == "active"
                        and n["fact_key"] == fact.key
                        and n["value_json"] != typed
                        and n["source_sequence"] <= pin[1]
                    ]
                    # The newly admitted observation may itself depend on a
                    # contradicted assertion. It belongs to that full closure.
                    self._invalidate(self._load(binding), roots, node_id, "observation")
                self._save(binding, request, "capture", digest, node_id, revision)
                current = self._load(binding)
                result = MemoryCaptureResult(
                    state=self._state(binding),
                    node=self._node(binding, current[0][node_id], current[1][node_id]),
                )
            return result

    @_storage
    def inspect(self, request):
        request = self._request(MemoryInspect, request)
        with self.store.lock:
            binding = self._binding(request)
            if self._namespace(binding)["enabled"]:
                graph = self._reconcile(binding)
                if request.node_id not in graph[0]:
                    raise MemoryError(404, "Unknown memory node")
                node = self._node(binding, graph[0][request.node_id], graph[1][request.node_id])
            else:
                row = self.db.execute(
                    f"SELECT {NODE_COLUMNS} FROM memory_nodes WHERE node_id=? AND session_id=? AND thread_id=? AND epoch=?",
                    (request.node_id, *self._where(binding)),
                ).fetchone()
                if row is None:
                    raise MemoryError(404, "Unknown memory node")
                parents = [
                    r[0]
                    for r in self.db.execute(
                        "SELECT parent_id FROM memory_edges WHERE child_id=? ORDER BY parent_id LIMIT 9",
                        (request.node_id,),
                    )
                ]
                try:
                    node = self._node(binding, dict(row), parents)
                except _Corrupt:
                    raise MemoryError(503, "Memory inspection unavailable") from None
            return MemoryCaptureResult(state=self._state(binding), node=node)

    @_storage
    def query(self, request):
        request = self._request(MemoryQuery, request)
        with self.store.lock:
            binding = self._binding(request)
            nodes, parents, _, _ = self._available(binding)
            seeds = list(request.seed_ids)
            if request.fact_keys:
                seeds = [
                    k
                    for k, n in nodes.items()
                    if n["state"] == "active" and n["fact_key"] in request.fact_keys
                ]
            if any(k not in nodes or nodes[k]["state"] != "active" for k in seeds):
                raise MemoryError(409, "Memory query seed unavailable")
            closure = set()
            pending = seeds[:]
            while pending:
                key = pending.pop()
                if key not in closure:
                    closure.add(key)
                    if len(closure) > 64:
                        raise MemoryError(409, "Complete memory query exceeds bound")
                    pending.extend(parents[key])
            result = MemoryQueryResult(
                state=self._state(binding),
                nodes=[
                    self._node(binding, nodes[k], parents[k])
                    for k in sorted(closure, key=lambda k: (nodes[k]["ordinal"], k))
                ],
            )
            if len(result.model_dump_json().encode("utf-8")) > 16 * 1024:
                raise MemoryError(409, "Complete memory query exceeds bound")
            return result

    def _erase_corrupt(self, binding, request, digest):
        """Conservative whole-scope erasure without loading corrupt bodies."""
        target = self.db.execute(
            "SELECT 1 FROM memory_nodes WHERE node_id=? AND session_id=? AND thread_id=? AND epoch=?",
            (request.node_id, *self._where(binding)),
        ).fetchone()
        if target is None:
            raise MemoryError(404, "Unknown memory node")
        pending = self.db.execute(
            """SELECT count(*) FROM memory_nodes
            WHERE session_id=? AND thread_id=? AND epoch=? AND
            (body IS NOT NULL OR fact_key IS NOT NULL OR value_json IS NOT NULL
             OR state!='forgotten')""",
            self._where(binding),
        ).fetchone()[0]
        if not pending:
            return MemoryForgetResult(state=self._state(binding), forgotten_ids=[])
        # The projection refuses oversized corrupt IDs before Python allocation.
        rows = self.db.execute(
            """SELECT CASE WHEN length(CAST(node_id AS BLOB))<=128 THEN node_id END
            FROM memory_nodes WHERE session_id=? AND thread_id=? AND epoch=?
            ORDER BY ordinal LIMIT 257""",
            self._where(binding),
        ).fetchall()
        erased = [r[0] for r in rows]
        representable = len(erased) <= 256
        if representable:
            try:
                MemoryForgetResult(state=self._state(binding), forgotten_ids=erased)
            except ValueError:
                representable = False
        with self.db:
            revision = self._advance(binding)
            floor = self.db.execute(
                """SELECT coalesce(max(CASE WHEN typeof(source_sequence)='integer'
                AND source_sequence BETWEEN 1 AND ? THEN source_sequence ELSE ? END),0)
                FROM memory_nodes WHERE session_id=? AND thread_id=? AND epoch=?""",
                (MAX, MAX, *self._where(binding)),
            ).fetchone()[0]
            self.db.execute(
                """UPDATE memory_nodes SET body=NULL,fact_key=NULL,value_json=NULL,
                state='forgotten' WHERE session_id=? AND thread_id=? AND epoch=?""",
                self._where(binding),
            )
            self.db.execute(
                """UPDATE memory_namespaces SET capture_floor=CASE
                WHEN typeof(capture_floor)='integer' AND capture_floor BETWEEN 0 AND ?
                THEN max(capture_floor,?) ELSE ? END
                WHERE session_id=? AND thread_id=? AND epoch=?""",
                (MAX, floor, MAX, *self._where(binding)),
            )
            if representable:
                representable = self._save_erasure(binding, request, digest, revision, erased)
        if not representable:
            raise MemoryError(409, "Namespace content erased; graph remains quarantined")
        return MemoryForgetResult(state=self._state(binding), forgotten_ids=erased)

    @_storage
    def forget(self, request):
        request = self._request(MemoryForget, request)
        with self.store.lock:
            binding = self._binding(request)
            if (
                self._namespace(binding)["enabled"]
                and self._namespace(binding)["status"] != "quarantined"
            ):
                self._reconcile(binding)
            prior, digest = self._receipt(binding, request, "forget")
            if prior:
                return MemoryForgetResult(
                    state=self._state(binding), forgotten_ids=_decode(prior["erased_ids"])
                )
            self._cas(binding, request.expected_revision)
            try:
                graph = self._load(binding)
                if request.node_id not in graph[0]:
                    raise MemoryError(404, "Unknown memory node")
                if graph[0][request.node_id]["state"] == "forgotten":
                    return MemoryForgetResult(state=self._state(binding), forgotten_ids=[])
                erased = sorted(
                    self._closure(graph[2], [request.node_id]),
                    key=lambda k: (graph[0][k]["ordinal"], k),
                )
                rows = [graph[0][k] for k in erased]
            except (_Corrupt, sqlite3.Error):
                self._quarantine(binding)
                self._cas(binding, request.expected_revision)
                return self._erase_corrupt(binding, request, digest)
            floor = max(
                [self._namespace(binding)["capture_floor"], *[r["source_sequence"] for r in rows]]
            )
            with self.db:
                revision = self._advance(binding)
                self.db.executemany(
                    "UPDATE memory_nodes SET body=NULL,fact_key=NULL,value_json=NULL,state='forgotten' WHERE node_id=?",
                    [(key,) for key in erased],
                )
                self.db.execute(
                    "UPDATE memory_namespaces SET capture_floor=? WHERE session_id=? AND thread_id=? AND epoch=?",
                    (floor, *self._where(binding)),
                )
                recorded = self._save_erasure(binding, request, digest, revision, erased)
            if not recorded:
                raise MemoryError(409, "Namespace content erased; graph remains quarantined")
            return MemoryForgetResult(state=self._state(binding), forgotten_ids=erased)
