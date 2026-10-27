"""Opt-in synthetic capability acquisition through the metered development proxy.

No Converse is constructed. Agent authors source; an independently frozen owner
suite evaluates it; owner controls activate it; a fresh host invokes it without
inference; then Agent uses it and writes a natively verified result.
"""

import argparse
import asyncio
import csv
import hashlib
import io
import json
from pathlib import Path

import httpx

from core.policy import ToolPolicy
from packages.contracts.models import SessionRequest, TaskRequest
from packages.providers.text import MetaResponsesProvider, ScriptedProvider
from services.agent import AgentRuntime, create_app
from services.agent.plugin_service import PluginService
from services.common.auth import Auth
from services.session.store import LocalJournal, Store

PLUGIN_ID = "public_csv_stats"
CSV = "value,label\n2,a\n8,b\n5,c\n"
MANIFEST = {
    "plugin_id": PLUGIN_ID,
    "name": "Public synthetic CSV statistics",
    "description": "Compute conventional statistics for a numeric CSV column without inference.",
    "author": "Kinaiya Agent, synthetic acquisition probe",
    "license": "Apache-2.0",
    "input_schema": {
        "type": "object",
        "properties": {
            "csv": {"type": "string", "maxLength": 4096},
            "column": {"type": "string", "minLength": 1, "maxLength": 40},
        },
        "required": ["csv", "column"],
        "additionalProperties": False,
    },
    "output_schema": {
        "type": "object",
        "properties": {
            "count": {"type": "integer", "minimum": 0},
            "sum": {"type": "number"},
            **{key: {"type": ["number", "null"]} for key in ("mean", "min", "max")},
        },
        "required": ["count", "sum", "mean", "min", "max"],
        "additionalProperties": False,
    },
}


def oracle(text, column="value"):
    values = [float(row[column]) for row in csv.DictReader(io.StringIO(text))]
    return {
        "count": len(values),
        "sum": sum(values, 0.0),
        "mean": sum(values) / len(values) if values else None,
        "min": min(values) if values else None,
        "max": max(values) if values else None,
    }


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


class CountedMeta(MetaResponsesProvider):
    def __init__(self):
        super().__init__(
            base_url="http://127.0.0.1:9790/v1",
            secret_ref="muse",
            allow_cloud=True,
            structured_actions=True,
            max_tokens=16000,
            reasoning_effort="xhigh",
        )
        self.calls = 0

    async def complete(self, messages):
        self.calls += 1
        return await super().complete(messages)


async def task(runtime, store, objective, key):
    session = store.create_session(SessionRequest(channel="agent"))
    accepted = await runtime.submit(
        TaskRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            objective=objective,
            idempotency_key=key,
        )
    )
    result = await runtime.wait(accepted.task_id, timeout=240)
    if result.state != "succeeded":
        raise RuntimeError(f"Synthetic {key} task {result.task_id}: {result.state}: {result.error}")
    return result


async def probe(output_dir):
    output_dir = output_dir.absolute()
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    workspace = output_dir / "workspace"
    workspace.mkdir(mode=0o700)
    suite = output_dir / "owner-acceptance.json"
    # Freeze criteria before the authoring provider exists. Neither this path
    # nor its concrete cases is supplied to the model or mounted in its sandbox.
    inputs = ["value\n1\n3\n5\n", "value\n-8\n2\n", "value\n", "value\n0.5\n1.5\n"]
    suite.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "plugin_id": PLUGIN_ID,
                "cases": [
                    {"input": {"csv": text, "column": "value"}, "expected": oracle(text)}
                    for text in inputs
                ],
            },
            indent=2,
        )
        + "\n"
    )
    suite_sha = hashlib.sha256(suite.read_bytes()).hexdigest()
    store = Store(output_dir / "journal.sqlite")
    author = CountedMeta()
    runtime = AgentRuntime(
        LocalJournal(store, "agent"),
        author,
        workspace,
        ToolPolicy(workspace, allow_write=True),
        max_steps=4,
    )
    report = {
        "synthetic_only": True,
        "model": author.info.model,
        "contributor_mode": True,
        "reasoning_effort": "xhigh",
        "converse_constructed": False,
        "harness_improvement_claimed": False,
        "suite_file_sha256_before_authoring": suite_sha,
    }
    try:
        generated = await task(
            runtime,
            store,
            "Author a reusable conventional CSV statistics compute plugin. Write only stats.py "
            "using write_file, then finish. Define synchronous run(arguments), where arguments "
            "contains csv (CSV text with header) and column (numeric column name). Parse all "
            "selected values as float. Return exactly count, sum, mean, min, max; count is int; "
            "sum is float (0.0 for no rows); mean/min/max are float or null for no rows. "
            "Use only csv, io and ordinary arithmetic/standard library. No files, network, "
            "processes, imports beyond csv/io/statistics/math. No print, demos or markdown. "
            "The trusted host calls run and serializes JSON. Do not build your own evaluator "
            "or lifecycle controls. This is public synthetic development code.",
            "author",
        )
        report["author_task_id"] = generated.task_id
        report["author_model_calls"] = author.calls
    finally:
        await runtime.close()

    tokens = {
        role: role + "-synthetic-control-fixture-0123456789"
        for role in (
            "owner",
            "agent",
            "converse",
            "decisions",
            "resources",
        )
    }
    host_root = output_dir / "plugins"
    service = PluginService(host_root, workspace, {PLUGIN_ID: suite})
    owner_provider = ScriptedProvider([])
    owner = AgentRuntime(
        LocalJournal(store, "agent"),
        owner_provider,
        workspace,
        ToolPolicy(workspace),
        plugin_service=service,
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(owner, Auth(tokens))),
            base_url="http://fixture",
            headers={"Authorization": "Bearer " + tokens["owner"]},
        ) as client:

            async def control(operation, key, expected=200, **fields):
                response = await client.post(
                    "/v1/plugins/control",
                    json={
                        "operation": operation,
                        "idempotency_key": key,
                        **fields,
                    },
                )
                assert response.status_code == expected, response.text
                return response.json()

            registered = await control("register", "register", path="stats.py", manifest=MANIFEST)
            version = registered["version_id"]
            evaluated = await control("evaluate", "evaluate", version_id=version)
            assert evaluated["passed"], evaluated
            assert hashlib.sha256(suite.read_bytes()).hexdigest() == suite_sha
            await control(
                "promote",
                "promote",
                version_id=version,
                report_id=evaluated["report_id"],
                reviewed=True,
            )
            await control(
                "scope",
                "scope",
                version_id=version,
                content_scope="public_synthetic",
                reviewed=True,
            )
            direct = await control(
                "run", "direct", version_id=version, arguments={"csv": CSV, "column": "value"}
            )
            assert direct["output"] == oracle(CSV)
            assert direct["receipt"]["output_sha256"] == digest(direct["output"])
            assert owner_provider.calls == []

            # An explicitly wrong update cannot replace the accepted version.
            (workspace / "bad.py").write_text(
                "def run(a):\n    return {'count': 0, 'sum': 0.0, 'mean': None, 'min': None, 'max': None}\n"
            )
            bad = await control("register", "bad-register", path="bad.py", manifest=MANIFEST)
            rejected = await control("evaluate", "bad-evaluate", version_id=bad["version_id"])
            assert rejected["passed"] is False
            await control(
                "promote",
                "bad-promote",
                expected=400,
                version_id=bad["version_id"],
                report_id=rejected["report_id"],
                reviewed=True,
            )
            assert service.registry.active(PLUGIN_ID) == version

            # A separately reviewed equivalent revision exercises pointer rollback.
            (workspace / "revision.py").write_bytes(
                (workspace / "stats.py").read_bytes() + b"\n# owner rollback probe\n"
            )
            revision = await control(
                "register", "revision-register", path="revision.py", manifest=MANIFEST
            )
            rev_report = await control(
                "evaluate", "revision-evaluate", version_id=revision["version_id"]
            )
            assert rev_report["passed"]
            await control(
                "promote",
                "revision-promote",
                version_id=revision["version_id"],
                report_id=rev_report["report_id"],
                reviewed=True,
            )
            restored = await control("rollback", "rollback", plugin_id=PLUGIN_ID, reviewed=True)
            assert restored == {"active": version}
            assert (
                await control("rollback", "rollback", plugin_id=PLUGIN_ID, reviewed=True)
                == restored
            )
            report.update(
                source_sha256=registered["source_sha256"],
                version_id=version,
                acceptance=evaluated,
                direct_without_llm=direct,
                direct_owner_model_calls=len(owner_provider.calls),
                rejected_update=rejected,
                rollback=restored,
            )
    finally:
        await owner.close()

    # Reopen the durable package under a different provider object. No rebuild.
    service = PluginService(host_root, workspace, {PLUGIN_ID: suite})
    user = CountedMeta()
    runtime = AgentRuntime(
        LocalJournal(store, "agent"),
        user,
        workspace,
        ToolPolicy(workspace, allow_write=True),
        plugin_service=service,
        max_steps=8,
    )
    try:
        persisted = await service.run(version, {"csv": CSV, "column": "value"})
        assert user.calls == 0 and persisted["output"] == oracle(CSV)
        used = await task(
            runtime,
            store,
            "Use the installed public_csv_stats compute plugin to process this public synthetic "
            "CSV; discover its selected schema with plugins_list, then invoke plugin_call. "
            "Do not calculate a replacement result yourself. Save only the actual output object "
            "as JSON to result.json with write_file and finish after native file evidence. "
            "Column: value. CSV text: " + json.dumps(CSV),
            "use-acquired-capability",
        )
        data = (workspace / "result.json").read_bytes()
        assert json.loads(data) == oracle(CSV)
        assert any(e.kind == "extension" and version in e.output for e in used.evidence)
        file_sha = hashlib.sha256(data).hexdigest()
        assert any(e.kind == "file" and e.sha256 == file_sha for e in used.evidence)
        report.update(
            reopened_without_rebuild=True,
            reopened_direct_model_calls=0,
            use_task_id=used.task_id,
            use_model_calls=user.calls,
            task_pins=runtime.plugin_pins(used.task_id),
            result_sha256=file_sha,
            task_state=used.state.value,
        )
    finally:
        await runtime.close()
        store.close()
    (output_dir / "evidence.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-contributor-synthetic", action="store_true", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(probe(args.output_dir))


if __name__ == "__main__":
    main()
