"""Source-aware regression for a real release-only fixture control shape."""

import asyncio
import json
import threading
from types import SimpleNamespace

import test_standing_physical_finalization as physical_module
from test_standing_physical_finalization import factory as factory
from test_standing_physical_finalization import physical as physical


async def test_snapshot_keeps_original_context_with_release_only_control(physical):
    snapshot = getattr(physical_module, "_physical_timeout_snapshot", None)
    assert callable(snapshot)
    value = await physical()
    await physical_module.bound(value)
    await physical_module.closure(value)
    gate = value.native.gate = physical_module.Gate(physical_module.CONTEXT, "read")
    public = physical_module.begin(value)
    await physical_module.gate_entered(gate.entered)
    originals = physical_module.context_originals(value, gate)
    sql = physical_module.SqlFault("held")
    release_only = SimpleNamespace(release=threading.Event())
    value.faults.extend((sql, release_only))

    observed = snapshot(value)
    controls = {item["id"]: item for item in observed["gates"]}
    assert id(sql) in controls and id(release_only) in controls
    assert controls[id(sql)]["kind"] == "sql"
    assert controls[id(release_only)]["kind"] == "release"
    assert not sql.release.is_set() and not release_only.release.is_set()
    assert not gate.release.is_set() and not public.done() and not originals[1].done()
    assert not originals[0].failed and not originals[0].finished
    assert len(json.dumps(observed, ensure_ascii=True, separators=(",", ":")).encode()) <= 32768

    gate.release.set()
    try:
        receipt = await asyncio.wait_for(public, 8)
    except TimeoutError as error:
        try:
            error.add_note(
                "Physical finalization timeout observation: "
                + json.dumps(snapshot(value), ensure_ascii=True, separators=(",", ":"))
            )
        except BaseException as diagnostic:
            try:
                error.add_note(
                    "Physical observation unavailable: " + type(diagnostic).__name__[:64]
                )
            except BaseException:
                pass
        raise
    assert receipt.validate_request(value.request) == receipt
    assert receipt.finalization.resource_disposition == "no-reservation"
    assert receipt.finalization.restoration == "not-required"
    await physical_module.join_context_originals(value, originals)
    assert originals[1].exception() is None
    assert value.starts == value.stops == [] and value.effect_checks == 0
    assert value.provider.calls == []
